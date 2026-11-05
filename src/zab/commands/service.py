"""Linux user services and their explicit environment (SPEC §13.1, §13.3)."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import pwd
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Iterator, Sequence
from dataclasses import asdict
from pathlib import Path

from zab.config import Config, load_config

MARKER = "# Managed by zab service install\n"
#: `zab sentinel`'s unit (#204; SPEC §13.3): `!vm` and `!restart` in a DM, also while the bridge
#: is down. No tie to `zab.service`, so that it keeps running while the bridge doesn't.
SENTINEL = "zab-sentinel.service"
#: The Codex app-server's open files (#664). Each Codex thread it has loaded keeps a
#: `lean-lsp-mcp` child (#632) and its pipes: on the test VM, 109 of them held 967 of the default
#: soft limit, 1024, a minute after boot, and a shell there failed with "No file descriptors
#: available". `nofile_limit` keeps it within the user manager's hard limit.
CODEX_NOFILE = 65536
_NOFILE_LINE = re.compile(r"^LimitNOFILE=.*\n", re.MULTILINE)
SCRUB = (
    "OPENAI_API_KEY",
    "CODEX_ACCESS_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
)


def unit_directory() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "systemd/user"


def quote(value: str, *, command: bool = False) -> str:
    if any(ord(char) < 32 for char in value):
        raise ValueError("control characters are not allowed in service settings")
    value = value.replace("%", "%%")
    return json.dumps(value.replace("$", "$$") if command else value, ensure_ascii=False)


def service_path(prepend: Sequence[str] = ()) -> str:
    standard = [str(Path.home() / ".local/bin"), "/usr/local/bin", "/usr/bin", "/bin"]
    directories = [
        str(Path(found).parent)
        for name in ("codex", "claude", "uv", "git", "gp")
        if (found := shutil.which(name)) and str(Path(found).parent) not in standard
    ]
    return ":".join(
        dict.fromkeys(
            [
                *[str(Path(p).expanduser()) for p in prepend],
                *directories,
                *standard,
            ]
        )
    )


def binary(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise ValueError(f"required executable not found: {name}")
    return str(Path(found).absolute())


def nofile_limit() -> int:
    """`CODEX_NOFILE`, or the user manager's `DefaultLimitNOFILE` where that is less (#664;
    1048576 on the AMI). systemd would clamp a unit's value to the manager's hard limit itself;
    the cap is for `raise_limit`: a daemon started from a unit without the line has that hard
    limit, and an unprivileged `prlimit` can't raise a hard limit. The installing process's own
    limit binds neither, and is not read."""
    caps = [CODEX_NOFILE]
    with contextlib.suppress(OSError, ValueError, subprocess.SubprocessError):
        shown = run(
            [binary("systemctl"), "--user", "show", "-p", "DefaultLimitNOFILE", "--value"],
            check=False,
        )
        if shown.returncode == 0 and shown.stdout.strip().isdigit():
            caps.append(int(shown.stdout.strip()))
    return min(caps)


def limit_only(old: str, new: str) -> bool:
    """Whether a unit's text changed in its `LimitNOFILE=` line alone (#664)."""
    return old != new and _NOFILE_LINE.sub("", old) == _NOFILE_LINE.sub("", new)


def limit_note(name: str, nofile: int) -> str:
    """The line install prints when `raise_limit` couldn't: `push_config.py` puts it in the
    row's notes (`LIMIT_NOTE_RE`)."""
    return f"{name}: its open-file limit ({nofile}) applies at its next start"


def raise_limit(name: str, nofile: int) -> bool:
    """Give the running daemon of `name` its unit's new open-file limit now, with `prlimit`
    (#664), instead of a restart that would end its running Codex turns. Its next start reads
    it from the unit. False when it couldn't: the limit then applies at that start."""
    with contextlib.suppress(OSError, ValueError, subprocess.SubprocessError):
        shown = run(
            [binary("systemctl"), "--user", "show", "-p", "MainPID", "--value", name], check=False
        )
        pid = shown.stdout.strip()
        if shown.returncode == 0 and pid.isdigit() and pid != "0":
            done = run(
                [binary("prlimit"), f"--pid={pid}", f"--nofile={nofile}:{nofile}"], check=False
            )
            return done.returncode == 0
    return False


def units(config: Config, *, nofile: int = CODEX_NOFILE) -> dict[str, str]:
    path = service_path(config.bridge.service_path_prepend)
    environment = f"Environment={quote('PATH=' + path)}\nUnsetEnvironment={' '.join(SCRUB)}\n"
    for key, value in config.bridge.service_env.items():
        if key not in SCRUB:
            if key == "VIRTUAL_ENV":
                value = str(Path(value).expanduser())
            environment += f"Environment={quote(key + '=' + value)}\n"
    daemons = dict.fromkeys(
        (
            binary(c.binary or "codex"),
            str(Path(c.codex_home or os.environ.get("CODEX_HOME", "~/.codex")).expanduser()),
            c.socket,
        )
        for c in config.backends.values()
        if c.kind == "codex" and c.connect == "daemon"
    )
    result = {}
    for index, (executable, home, socket) in enumerate(daemons):
        name = (
            "codex-app-server.service" if len(daemons) == 1 else f"codex-app-server-{index}.service"
        )
        argv = [executable, "app-server", "--listen", "unix://" + socket]
        result[name] = (
            MARKER
            + "[Unit]\nDescription=Codex app-server daemon for zab\n[Service]\n"
            + "ExecStart="
            + " ".join(quote(arg, command=True) for arg in argv)
            + "\n"
            + environment
            + f"Environment={quote('CODEX_HOME=' + home)}\n"
            + f"LimitNOFILE={nofile}\n"
            + "Restart=on-failure\nRestartSec=5\n[Install]\nWantedBy=default.target\n"
        )
    dependencies = " ".join(result)
    fingerprint = hashlib.sha256(
        json.dumps(asdict(config), sort_keys=True, default=str).encode()
    ).hexdigest()
    bridge_environment = environment
    if any(c.kind == "codex" for c in config.backends.values()):
        fallback_home = str(Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser())
        bridge_environment += f"Environment={quote('CODEX_HOME=' + fallback_home)}\n"
    result["zab.service"] = (
        MARKER
        + f"# Configuration: {fingerprint}\n"
        + "[Unit]\nDescription=zab Zulip agent bridge\n"
        + (f"After={dependencies}\nWants={dependencies}\n" if dependencies else "")
        + "[Service]\nType=notify\nNotifyAccess=main\nTimeoutStartSec=1800\n"
        + "WatchdogSec=300\nExecStart="
        + " ".join(
            quote(arg, command=True) for arg in [binary("zab"), "run", "--config", str(config.path)]
        )
        + "\n"
        + bridge_environment
        + "Restart=on-failure\nRestartSec=5\nKillMode=mixed\nTimeoutStopSec=30\n"
        + "[Install]\nWantedBy=default.target\n"
    )
    result[SENTINEL] = (
        MARKER
        + "[Unit]\nDescription=zab sentinel: !vm and !restart in direct messages\n[Service]\n"
        + "ExecStart="
        + " ".join(
            quote(arg, command=True)
            for arg in [binary("zab"), "sentinel", "--config", str(config.path)]
        )
        + "\n"
        + environment
        + "Restart=always\nRestartSec=10\n[Install]\nWantedBy=default.target\n"
    )
    return result


def managed_units(directory: Path) -> list[Path]:
    return sorted(
        p
        for p in directory.glob("*.service")
        if (p.name in ("zab.service", SENTINEL) or p.name.startswith("codex-app-server"))
        and p.read_text().startswith(MARKER)
    )


def run(argv: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(argv, check=check, capture_output=True, text=True, timeout=45)  # noqa: S603 - fixed commands with argv, no shell


def control(*args: str) -> subprocess.CompletedProcess:
    return run([binary("systemctl"), "--user", *args])


@contextlib.contextmanager
def service_environment() -> Iterator[None]:
    text = (unit_directory() / "zab.service").read_text()
    assignments = [
        part
        for line in text.splitlines()
        if line.startswith("Environment=")
        for part in shlex.split(line.removeprefix("Environment="))
    ]
    values = dict(part.replace("%%", "%").split("=", 1) for part in assignments)
    if "PATH" not in values:
        raise ValueError("zab.service has no explicit PATH; run zab service install")
    old = {key: os.environ.get(key) for key in (*values, *SCRUB)}
    try:
        os.environ.update(values)
        for key in SCRUB:
            os.environ.pop(key, None)
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="zab service")
    parser.add_argument("action", choices=("install", "uninstall", "status"))
    parser.add_argument("--config")
    args = parser.parse_args(argv)
    try:
        if sys.platform != "linux":
            raise ValueError("user services require Linux; run zab run in a terminal on macOS")
        directory = unit_directory()
        existing = managed_units(directory)
        if args.action == "install":
            nofile = nofile_limit()
            rendered = units(load_config(args.config), nofile=nofile)
            changed_active, limited = [], []
            for name in rendered:
                path = directory / name
                if path.exists() and not path.read_text().startswith(MARKER):
                    raise ValueError(f"refusing to overwrite unmanaged unit: {path}")
                if path.exists() and path.read_text() != rendered[name]:
                    state = run(
                        [binary("systemctl"), "--user", "is-active", "--quiet", name], check=False
                    )
                    if state.returncode != 0:
                        continue
                    # a Codex daemon whose unit gained only its open-file limit is not restarted,
                    # which would end its running turns: `prlimit` gives it the limit (#664)
                    if name.startswith("codex-app-server") and limit_only(
                        path.read_text(), rendered[name]
                    ):
                        limited.append(name)
                    else:
                        changed_active.append(name)
            directory.mkdir(parents=True, exist_ok=True)
            for path in existing:
                if path.name not in rendered:
                    control("disable", "--now", path.name)
                    path.unlink()
            for name, body in rendered.items():
                target = directory / name
                temporary = target.with_suffix(".tmp")
                temporary.write_text(body)
                temporary.replace(target)
            control("daemon-reload")
            # The sentinel reads the configuration and its code only when it starts, and keeps
            # no state: restarted on every install, so an upgrade or a new owner takes effect.
            control("try-restart", "--no-block", *dict.fromkeys([*changed_active, SENTINEL]))
            for name in limited:
                if not raise_limit(name, nofile):
                    print(limit_note(name, nofile))
            control("enable", "--now", "--no-block", *rendered)
            user = pwd.getpwuid(os.getuid()).pw_name
            linger = run([binary("loginctl"), "enable-linger", user], check=False)
            if linger.returncode:
                print(
                    "To start services before login, run: "
                    + shlex.join(["sudo", "loginctl", "enable-linger", user])
                )
            print(
                "Installed user services; startup/restart queued and may still be pending. "
                "Run zab service status, then zab doctor --service-env once active."
            )
        elif args.action == "uninstall":
            for path in existing:
                control("disable", "--now", path.name)
                path.unlink()
            control("daemon-reload")
        else:
            result = run(
                [binary("systemctl"), "--user", "status", "--no-pager", *(p.name for p in existing)]
                if existing
                else [binary("systemctl"), "--user", "status", "--no-pager", "zab.service"],
                check=False,
            )
            print(result.stdout, end="")
            return result.returncode
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"zab service failed: {exc}", file=sys.stderr)
        return 1
    return 0
