#!/usr/bin/env python3
"""The agents' CLIs at the release's pins (SPEC §8.3), on a VM whose image has others.

    sudo -u agent -H /usr/bin/python3 agent_clis.py install --codex VERSION SHA256 \\
        --claude VERSION SHA256 [--home HOME] [--deadline-s SECONDS]

An image installs Codex and Claude Code at its build only (`workshop/ami/build.sh` §5, §6), so
every release rollout of `push_config.py` runs this, as agent, with the pins of the release it
installs (its `zab/pins.py`, `read_pins`). For each CLI whose `--version` isn't its pin, it
downloads the pinned file to a staging directory, checks its SHA-256 before anything reads it,
and runs the staged CLI's `--version`. Only once every CLI that needs it has passed does it
change anything, so a failed download or a mismatch changes nothing:

- Codex: the package's members that build.sh extracts (`codex_members`), and every `bin/`
  entry its `codex-package.json` names checked to be an executable file, go to
  `~/.local/lib/codex-<version>`, and `~/.local/bin/codex` links to its `bin/codex`. The
  daemon's unit names that package by its resolved path, so the rollout's `zab service install`
  after this step rewrites the unit and restarts a running daemon onto the new one. The
  package gets `codex-package.sha256`, the hash it was checked against, as build.sh records it;
- Claude Code: the binary becomes `~/.local/share/claude/versions/<version>`, where the native
  installer keeps each version, and `~/.local/bin/claude` links to it, by its absolute path as
  the installer writes it. The bridge resolves that link when it starts (`zab.config`), so turns
  run the new version from its restart on, which the rollout's service install does.

Each link is replaced in one rename (`ln -sfn` on a temporary name, then `mv -T`). The version
it pointed to stays, for a rollback and for what still runs it: a Codex daemon started before
finds its package's helpers there. Only older versions are removed. Nothing else is touched:
the sign-ins, `~/.codex/config.toml` and Claude's settings stay as the image and the trust fix
left them.

Every download and unpacking ends within `--deadline-s` of the start, and past it nothing is
switched: push_config waits longer than that, so a run it stopped waiting for can never switch
the links later.

One line on stdout, `agent CLIs: codex 0.157.1 → 0.169.1, claude 2.1.283 → 2.1.311`, with
`codex current` for one at its pin, or `agent CLIs: current`. Then, when this run replaced
Codex, for each version that zab's Codex daemon (`codex app-server --listen unix://…`, its unit's
command) still runs, `---ZAB-CODEX-DAEMON=<version>`. A failure is one line on stderr, `agent
CLIs: failed: …`, and exit 1. Standard library only: the VM's system Python runs it.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass

HOME = "/home/agent"
#: build.sh's download (§5), and the native installer's (https://claude.ai/install.sh, read on
#: 2026-11-17: `$DOWNLOAD_BASE_URL/$version/$platform/claude`, checked against its manifest)
CODEX_URL = (
    "https://github.com/openai/codex/releases/download/rust-v{version}/"
    "codex-package-x86_64-unknown-linux-musl.tar.gz"
)
CLAUDE_URL = "https://downloads.claude.ai/claude-code-releases/{version}/linux-x64/claude"
#: build.sh's `codex_members`: the core members, which tar then requires, the first two the
#: programs Codex needs; then the package's other `bin/` members where the archive has them,
#: those its `codex-package.json` names (0.169.1's `execServer`) and `bin/codex-exec-server`
CORE_PROGRAMS = ("bin/codex", "bin/codex-code-mode-host")
CODEX_MEMBERS = (*CORE_PROGRAMS, "codex-package.json", "codex-path", "codex-resources")
EXEC_SERVER = "bin/codex-exec-server"
BIN_ENTRY = re.compile(r"bin/[A-Za-z0-9._-]{1,100}")
CURRENT = "agent CLIs: current"
DAEMON_MARK = "---ZAB-CODEX-DAEMON="
PIN = re.compile(r"[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}")
SHA256 = re.compile(r"[0-9a-f]{64}")
#: What `--version` may print as a version: a pin, or one with a pre-release or build suffix
REPORTED = re.compile(r"[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}(?:[-+][0-9A-Za-z.+-]{1,40})?")
#: The staging directory, under ~/.local/lib so that each rename stays on one file system; one
#: left by a run that was killed is removed by a later run after an hour
STAGING = ".zab-agent-clis-"
STALE_S = 3600
#: The step's bound, `--deadline-s`: under push_config's wait for it (`AGENT_CLIS_TIMEOUT_S`)
DEADLINE_S = 720
_deadline = math.inf  # `install` sets it
_now = time.monotonic


class Failed(Exception):
    """A step that can't go on, said in one line."""


@dataclass(frozen=True, slots=True)
class Pins:
    codex: str
    codex_sha256: str
    claude: str
    claude_sha256: str

    def __post_init__(self) -> None:
        for version in (self.codex, self.claude):
            if not PIN.fullmatch(version):
                raise ValueError(f"not a pinned version: {version!r}")
        for digest in (self.codex_sha256, self.claude_sha256):
            if not SHA256.fullmatch(digest):
                raise ValueError(f"not a SHA-256: {digest!r}")


_NAMES = {
    "CODEX_VERSION": "codex",
    "CODEX_PACKAGE_SHA256": "codex_sha256",
    "CLAUDE_VERSION": "claude",
    "CLAUDE_SHA256": "claude_sha256",
}


def read_pins(source: str) -> Pins:
    """The four strings of a `zab/pins.py`, by `ast`: the release's file is never imported."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ValueError(f"doesn't parse: {exc}") from exc
    found = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in _NAMES
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            found[_NAMES[node.targets[0].id]] = node.value.value
    missing = [name for name, field in _NAMES.items() if field not in found]
    if missing:
        raise ValueError(f"has no string {', '.join(missing)}")
    return Pins(**found)


@dataclass(frozen=True, slots=True)
class Layout:
    """Where build.sh and the native installer put the CLIs, under agent's home."""

    home: str = HOME

    @property
    def bin(self) -> str:
        return os.path.join(self.home, ".local", "bin")

    @property
    def lib(self) -> str:
        return os.path.join(self.home, ".local", "lib")

    @property
    def versions(self) -> str:
        return os.path.join(self.home, ".local", "share", "claude", "versions")


def version_of(program: str) -> str | None:
    """What `program --version` says (`codex-cli 0.157.1`, `2.1.283 (Claude Code)`); None when
    it doesn't run or names no version."""
    try:
        done = subprocess.run(  # noqa: S603 - argument list
            [program, "--version"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=60,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    words = done.stdout.split() if done.returncode == 0 else []
    return next((word for word in words if REPORTED.fullmatch(word)), None)


def _left(what: str) -> float:
    """The seconds left of the step's bound; past it, a failure."""
    left = _deadline - _now()
    if left <= 0:
        raise Failed(f"{what}: past the step's {DEADLINE_S} s")
    return left


def _run(argv: list[str], what: str, timeout: float) -> bytes:
    timeout = min(timeout, _left(what))
    done = subprocess.run(argv, capture_output=True, timeout=timeout, check=False)  # noqa: S603
    if done.returncode:
        detail = done.stderr.decode("utf-8", "replace").strip().splitlines()
        raise Failed(f"{what}: {detail[0] if detail else f'exit {done.returncode}'}")
    return done.stdout


def fetch(url: str, dest: str) -> None:
    """`url` to `dest` over https, retried as build.sh's `fetch_checked` does, within the step's
    bound; a transfer slower than 100 kB/s for a minute is cut off and retried."""
    left = str(max(1, int(_left(f"download {url}"))))
    argv = ["curl", "-fsSL", "--proto", "=https", "--connect-timeout", "30"]
    argv += ["--speed-limit", "102400", "--speed-time", "60", "--max-time", left]
    argv += ["--retry", "3", "--retry-all-errors", "--retry-delay", "5", "--retry-max-time", left]
    _run([*argv, "-o", dest, url], f"download {url}", float(left))


def verify(path: str, want: str, what: str) -> None:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    if digest.hexdigest() != want:
        raise Failed(f"{what}: SHA-256 {digest.hexdigest()}, not the pin's {want}")


def _strings(value: object) -> list[str]:
    """Every string in a JSON value, at any depth (jq's `.. | strings`)."""
    if isinstance(value, str):
        return [value]
    items = value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
    return [s for item in items for s in _strings(item)]


def codex_members(archive: str) -> tuple[list[str], list[str]]:
    """What build.sh's `codex_members` extracts from the package, and the programs that must
    then be executable files, as its check has them: the core ones and every `bin/` entry that
    `codex-package.json` names, and an extra member taken."""
    listing = set(_run(["tar", "-tzf", archive], "tar", 600).decode("utf-8", "replace").split("\n"))
    meta = json.loads(_run(["tar", "-xzOf", archive, "codex-package.json"], "tar", 600))
    named = [s for s in _strings(meta) if s.startswith("bin/")]
    extra = [
        m
        for m in sorted({EXEC_SERVER, *named})
        if m not in CORE_PROGRAMS and BIN_ENTRY.fullmatch(m) and m in listing
    ]
    return [*CODEX_MEMBERS, *extra], list(dict.fromkeys([*CORE_PROGRAMS, *named, *extra]))


def stage_codex(pins: Pins, staging: str) -> str:
    """The pinned package's members, checked, in `staging`; their directory."""
    archive = os.path.join(staging, "codex-package.tar.gz")
    fetch(CODEX_URL.format(version=pins.codex), archive)
    verify(archive, pins.codex_sha256, f"codex {pins.codex}")
    package = os.path.join(staging, f"codex-{pins.codex}")
    os.mkdir(package, 0o755)
    members, programs = codex_members(archive)
    _run(["tar", "-xzf", archive, "-C", package, *members], "tar", 600)
    os.unlink(archive)
    with open(os.path.join(package, "codex-package.sha256"), "w") as f:  # as build.sh's
        f.write(pins.codex_sha256 + "\n")
    for program in programs:  # each one there, and executable, before any change
        path = os.path.join(package, program)
        if not (os.path.isfile(path) and os.access(path, os.X_OK)):
            raise Failed(f"codex {pins.codex}: the package's {program} isn't an executable file")
    said = version_of(os.path.join(package, "bin", "codex"))
    if said != pins.codex:
        raise Failed(f"codex {pins.codex}: its bin/codex says {said or 'no version'}")
    return package


def stage_claude(pins: Pins, staging: str) -> str:
    """The pinned binary, checked, in `staging`; its path."""
    binary = os.path.join(staging, pins.claude)
    fetch(CLAUDE_URL.format(version=pins.claude), binary)
    verify(binary, pins.claude_sha256, f"claude {pins.claude}")
    os.chmod(binary, 0o755)  # noqa: S103 - a program, as the installer leaves it
    said = version_of(binary)
    if said != pins.claude:
        raise Failed(f"claude {pins.claude}: the binary says {said or 'no version'}")
    return binary


def versioned(name: str, prefix: str, suffix: str = "") -> str | None:
    """The version in `<prefix><version><suffix>`; None for any other name."""
    if not (name.startswith(prefix) and name.endswith(suffix)):
        return None
    version = name[len(prefix) : len(name) - len(suffix)]
    return version if PIN.fullmatch(version) else None


def linked_version(link: str, directory: str, prefix: str, suffix: str = "") -> str | None:
    """The version in the path `link` points to, `<directory>/<prefix><version><suffix>`; None
    for any other target, or none."""
    try:
        target = os.path.normpath(os.path.join(os.path.dirname(link), os.readlink(link)))
    except OSError:
        return None
    inside = os.path.join(directory, "")
    return versioned(target[len(inside) :], prefix, suffix) if target.startswith(inside) else None


def relink(link: str, target: str) -> None:
    """`link` to `target` in one rename: the new link under a temporary name, then renamed over
    the old one, which is replaced and never followed (`ln -sfn`, then `mv -T`)."""
    os.makedirs(os.path.dirname(link), exist_ok=True)
    tmp = os.path.join(os.path.dirname(link), f".{os.path.basename(link)}.zab-{os.getpid()}")
    if os.path.lexists(tmp):
        os.unlink(tmp)
    os.symlink(target, tmp)
    try:
        os.replace(tmp, link)
    except BaseException:
        os.unlink(tmp)
        raise


def _key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def prune(directory: str, prefix: str, previous: str | None, keep: str, *, tree: bool) -> None:
    """Versions older than `previous`, the one the link pointed to, except `keep`: directories
    `<prefix><version>` (`tree`), or files `<prefix><version>`. Nothing when no version was
    linked, and nothing that is a link or of another kind."""
    if previous is None:
        return
    for name in os.listdir(directory):
        version = versioned(name, prefix)
        if version is None or version == keep or _key(version) >= _key(previous):
            continue
        path = os.path.join(directory, name)
        mode = os.lstat(path).st_mode
        if tree and stat.S_ISDIR(mode):
            shutil.rmtree(path)
        elif not tree and stat.S_ISREG(mode):
            os.unlink(path)


def place_codex(pins: Pins, layout: Layout, package: str, staging: str) -> None:
    link = os.path.join(layout.bin, "codex")
    previous = linked_version(link, layout.lib, "codex-", "/bin/codex")
    final = os.path.join(layout.lib, f"codex-{pins.codex}")
    if os.path.lexists(final):  # from a run that didn't link it, or a broken one: replaced
        os.rename(final, os.path.join(staging, "replaced"))
    os.rename(package, final)
    relink(link, os.path.join(final, "bin", "codex"))
    prune(layout.lib, "codex-", previous, pins.codex, tree=True)


def place_claude(pins: Pins, layout: Layout, binary: str) -> None:
    link = os.path.join(layout.bin, "claude")
    previous = linked_version(link, layout.versions, "")
    final = os.path.join(layout.versions, pins.claude)
    os.makedirs(layout.versions, exist_ok=True)
    os.replace(binary, final)
    relink(link, final)
    prune(layout.versions, "", previous, pins.claude, tree=False)


def daemon_versions(link: str, proc: str = "/proc") -> list[str]:
    """The versions that this user's Codex daemons, as zab's unit runs them (`codex app-server
    --listen unix://…`), run other than `link`'s: from their executable's path,
    `…/codex-<version>/bin/codex`, or `another binary`. Another client's app-server, such as an
    editor's, isn't one."""
    want, uid, found = os.path.realpath(link), os.getuid(), []
    for pid in sorted(os.listdir(proc)):
        base = os.path.join(proc, pid)
        try:
            if not pid.isdigit() or os.stat(base).st_uid != uid:
                continue
            with open(os.path.join(base, "cmdline"), "rb") as f:
                argv = f.read().split(b"\0")
            exe = os.readlink(os.path.join(base, "exe")).removesuffix(" (deleted)")
        except OSError:  # gone meanwhile
            continue
        if len(argv) < 2 or os.path.basename(argv[0]) != b"codex" or argv[1] != b"app-server":
            continue
        at = argv.index(b"--listen") if b"--listen" in argv else len(argv)
        if at + 1 >= len(argv) or not argv[at + 1].startswith(b"unix://"):
            continue
        if exe != want:  # `…/codex-<version>/bin/codex`, as build.sh and this script install
            package, _, program = exe.rpartition("/bin/")
            version = versioned(os.path.basename(package), "codex-") if program == "codex" else None
            found.append(version or "another binary")
    return list(dict.fromkeys(found))


def _clean_stale(lib: str) -> None:
    for name in os.listdir(lib):
        path = os.path.join(lib, name)
        try:
            st = os.lstat(path)
            if name.startswith(STAGING) and stat.S_ISDIR(st.st_mode):
                if time.time() - st.st_mtime > STALE_S:
                    shutil.rmtree(path)
        except OSError:
            continue


def install(
    pins: Pins, layout: Layout, proc: str = "/proc", deadline_s: float = DEADLINE_S
) -> list[str]:
    """Both CLIs at `pins` (the module docstring), within `deadline_s`; the output lines."""
    global _deadline
    _deadline = _now() + deadline_s
    try:
        return _install(pins, layout, proc)
    finally:
        _deadline = math.inf


def _install(pins: Pins, layout: Layout, proc: str) -> list[str]:
    links = {name: os.path.join(layout.bin, name) for name in ("codex", "claude")}
    before = {name: version_of(link) for name, link in links.items()}
    behind = [name for name in links if before[name] != getattr(pins, name)]
    if behind:
        os.makedirs(layout.lib, exist_ok=True)
        _clean_stale(layout.lib)
        staging = tempfile.mkdtemp(prefix=STAGING, dir=layout.lib)
        try:
            try:  # all the network and the checks first: a failure here changes nothing
                package = stage_codex(pins, staging) if "codex" in behind else None
                binary = stage_claude(pins, staging) if "claude" in behind else None
                _left("the switch")  # never after push_config stopped waiting
            except (Failed, OSError, ValueError, subprocess.SubprocessError) as exc:
                raise Failed(f"{exc}; nothing changed") from exc
            if package:
                place_codex(pins, layout, package, staging)
            try:
                if binary:
                    place_claude(pins, layout, binary)
            except OSError as exc:
                done = f"; codex {pins.codex} installed" if package else ""
                raise Failed(f"claude: {exc}{done}") from exc
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    parts = []
    for name in links:
        was = before[name] or ("?" if os.path.lexists(links[name]) else "none")
        parts.append(
            f"{name} {was} → {getattr(pins, name)}" if name in behind else f"{name} current"
        )
    lines = ["agent CLIs: " + ", ".join(parts) if behind else CURRENT]
    if "codex" not in behind:  # nothing changed for the daemon
        return lines
    return lines + [DAEMON_MARK + v for v in daemon_versions(links["codex"], proc)]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="agent_clis.py", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="command", required=True)
    inst = sub.add_parser("install", help="bring Codex and Claude Code to the pins")
    inst.add_argument("--codex", nargs=2, required=True, metavar=("VERSION", "SHA256"))
    inst.add_argument("--claude", nargs=2, required=True, metavar=("VERSION", "SHA256"))
    inst.add_argument("--home", default=HOME)
    inst.add_argument("--deadline-s", type=int, default=DEADLINE_S, metavar="SECONDS")
    args = p.parse_args(argv)
    try:
        pins = Pins(*args.codex, *args.claude)
        print("\n".join(install(pins, Layout(args.home), deadline_s=args.deadline_s)))
    except (Failed, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"agent CLIs: failed: {str(exc)[:300]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
