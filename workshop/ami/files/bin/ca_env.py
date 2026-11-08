#!/usr/bin/env python3
"""ca_env.py: no variable that brings in a trust store besides the system's is set for the
agent (#882).

    sudo ca_env.py [USER]

`SSL_CERT_FILE` or `CODEX_CA_CERTIFICATE` in an agent's environment adds roots to the system
store's: Codex 0.157.1 then switches its HTTPS client to rustls with reqwest's built-in roots,
emSign ECC Root CA - G3 among them, besides the system's and the file's; Claude Code adds the
file to its own CA list (DECISIONS, the 2026-11-06 row; #859). This reads, as root, where USER's
environment (`agent` by default) comes from:

- **the login shell:** `bash -lic`, as an SSH session's shell starts (`/etc/environment` through
  sudo's PAM as through sshd's, `/etc/profile`, `~/.profile` and `~/.bashrc`);
- **the user manager:** `systemctl --user show-environment`, which every user unit inherits,
  zab.service's included;
- **zab.service's own `Environment=`,** which `zab service install` writes: none before.
  `codex-app-server.service`, which runs Codex for the bridge, isn't read: `zab service install`
  gives it the same lines, from `[bridge] service_env`, besides its `CODEX_HOME`, and it inherits
  the same user manager's environment.

Not read either: `~/.pam_environment`, which sshd's `pam_env` reads (`user_readenv=1`) and
sudo's doesn't, and the `env` of Claude Code's settings, managed or the user's.

Names only: no value is printed. One line per place, `ok` or the variables set there. Exit 0 when
none is set in the places read; 1 when one is, or when a place can't be read (a check that read
nothing proves nothing). `build.sh`'s smoke test runs it, and `build_ami.py --smoke`'s.
Standard library only: the VM's system Python runs it.
"""

from __future__ import annotations

import pwd
import re
import subprocess
import sys

NAMES = ("SSL_CERT_FILE", "CODEX_CA_CERTIFICATE")
#: What the login shell prints: a line per variable set, then DONE, so that a shell that ended
#: early (an `exit` in a profile) reads as not read rather than clean.
SET, DONE = "ca-env set:", "ca-env done"
SHELL_PROBE = (
    'for v in SSL_CERT_FILE CODEX_CA_CERTIFICATE; do [ -z "${!v+x}" ] || echo "ca-env set: $v";'
    ' done; echo "ca-env done"'
)
TIMEOUT_S = 60
#: A name assigned in `systemctl show -p Environment --value`'s line: space-separated, quoted
#: where a value holds a space.
ASSIGNED = re.compile(r'(?:^|\s)"?([A-Za-z_][A-Za-z0-9_]*)=')


def run(argv: list[str]) -> subprocess.CompletedProcess[str] | None:
    """ARGV from /, never through a shell, stdin empty; None when it can't start or ends late."""
    try:
        return subprocess.run(  # noqa: S603 - an argument list
            argv, cwd="/", stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=TIMEOUT_S, check=False,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return None


def as_user(user: str, uid: int, argv: list[str]) -> list[str]:
    """ARGV as USER with its user manager's bus, as push_config.py reaches it."""
    run_dir = f"/run/user/{uid}"
    bus = f"DBUS_SESSION_BUS_ADDRESS=unix:path={run_dir}/bus"
    return ["sudo", "-u", user, "env", f"XDG_RUNTIME_DIR={run_dir}", bus, *argv]


def places(user: str) -> list[tuple[str, set[str] | None]]:
    """Each place and the names of NAMES set there; None for a place that couldn't be read."""
    uid = pwd.getpwnam(user).pw_uid
    out: list[tuple[str, set[str] | None]] = []
    shell = run(["sudo", "-u", user, "-H", "bash", "-lic", SHELL_PROBE])
    lines = shell.stdout.splitlines() if shell else []
    names = {x.removeprefix(SET).strip() for x in lines if x.startswith(SET)}
    out.append(("login shell", names if DONE in lines else None))
    manager = run(as_user(user, uid, ["systemctl", "--user", "show-environment"]))
    ok = manager is not None and manager.returncode == 0
    names = {x.split("=", 1)[0] for x in manager.stdout.splitlines()} if ok else set()
    out.append(("user manager", names if ok else None))
    unit = ["systemctl", "--user", "show", "--property=Environment", "--value", "zab.service"]
    service = run(as_user(user, uid, unit))
    ok = service is not None and service.returncode == 0
    names = set(ASSIGNED.findall(service.stdout)) if ok else set()
    out.append(("zab.service", names if ok else None))
    return [(where, None if found is None else found & set(NAMES)) for where, found in out]


def main(argv: list[str]) -> int:
    if len(argv) > 1 or (argv and argv[0].startswith("-")):
        print(__doc__.strip().split("\n\n")[1], file=sys.stderr)
        return 2
    user = argv[0] if argv else "agent"
    try:
        found = places(user)
    except KeyError:
        print(f"ca_env.py: no user {user}", file=sys.stderr)
        return 2
    for where, names in found:
        if names is None:
            print(f"{where}: FAILED, not read")
        elif names:
            print(f"{where}: SET {', '.join(sorted(names))}")
        else:
            print(f"{where}: ok")
    ok = all(names == set() for _, names in found)
    print(
        f"ok: neither {' nor '.join(NAMES)} is set for {user} in the places read"
        if ok
        else "FAILED: the SET or FAILED lines above (either variable adds roots to the system's)"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
