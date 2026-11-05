"""`python -m zab.admin`: the admin server's command line (docs/ADMIN.md).

- `audit verify [PATH] [--head SEQ:HEX ...]` checks the audit log's hash chain (§2.8).
- `starter` is zab-admin-starter.service, the waker's start path (§1.2, PR 21).
- `bot --config PATH` runs every admin bot of admin.toml (`zab-admin-bot.service`, §1.2, PR 9).
- `collector --config PATH [--once]` is zab-admin-collector.service: the console's snapshot
  (§4.1, `zab.admin.collector`).
- `console --config PATH` is zab-console.service: the read-only console on 127.0.0.1:8090
  (§4.3, `zab.admin.console`).
- `selfcheck` is what `deploy.sh` runs in a new release before it switches to it (§1.6).

The root helper of `!new-workshop` (§2.9) is not a command here: `zab-admin-config.service` runs
`zab.admin.confighelper` itself, with the system interpreter under `-I -B -S`, so that root runs
none of this module's imports and nothing of the venv (§1.6, #437).

**The self-check's contract.** It runs as `zab-build`, which can't read the secrets; at a
bootstrap, before the secrets and the fleet key exist; under `env -i`, without IMDS, a terminal
or new privileges. So it checks what that context can, and nothing that needs more:
- admin.toml loads with the strict loader (#343);
- every subcommand's modules import, from this release's tree and venv: the starter's SDK
  (`starter.sdk()`, boto3), the bot's (every other `zab.admin` module, the Zulip client and
  httpx), the collector's and the console's, whose two static files are read;
- the config helper's own command, as its unit gives it, answers `--help` from this release
  (`check_helper`): the system interpreter under `-I -B -S`, from the standard library and
  `src/` alone;
- every script the actions run answers `--help` (`checks.check_scripts`), without writing
  into the tree.
Exit 0 and `selfcheck: OK` when all pass; 2 for an admin.toml the loader refuses; 1 otherwise.
The owners and modes of admin.toml, the keys, the break-glass files and the starter's start
log (#359), and each bot's `GET /users/me`, are the bot's to check at its start, which refuses
to start without them; `bot --check` runs those checks alone. (On an upgrade the self-check
runs before `tmpfiles.conf` makes a new directory, so it can't check them.)
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from pathlib import Path

from zab.admin import audit, starter
from zab.admin.config import DEFAULT_PATH, AdminConfigError, load_admin_config

_USAGE = (
    "usage: python -m zab.admin audit verify [PATH] [--head SEQ:HEX ...]\n"
    "       python -m zab.admin starter [--config PATH] [--state-dir DIR] [--socket PATH]\n"
    "       python -m zab.admin bot [--config PATH] [--check]\n"
    "       python -m zab.admin collector [--config PATH] [--once]\n"
    "       python -m zab.admin console [--config PATH]\n"
    "       python -m zab.admin selfcheck [--config PATH]"
)
#: The config helper's unit (§1.6, #437), the release its command names (`current`), and its
#: interpreter: the system's, which deploy.sh builds the venv from, never the venv's.
HELPER_UNIT = "workshop/adminserver/zab-admin-config.service"
CURRENT = "/opt/zab-admin/current"
HELPER_PYTHON = "/usr/bin/python3"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["audit"]:
        return audit.main(args[1:])
    if args[:1] == ["bot"]:
        from zab.admin import bot  # the starter and `audit` never need the bot's imports

        return bot.main(args[1:])
    if args[:1] == ["collector"]:
        from zab.admin import collector

        return collector.main(args[1:])
    if args[:1] == ["console"]:
        from zab.admin import console

        return console.main(args[1:])
    if args[:1] not in (["starter"], ["selfcheck"]):
        print(_USAGE, file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(prog=f"python -m zab.admin {args[0]}")
    parser.add_argument("--config", type=Path, default=DEFAULT_PATH)
    if args[0] == "starter":
        parser.add_argument("--state-dir", type=Path, help="default: $STATE_DIRECTORY")
        parser.add_argument("--socket", type=Path, help="listen here without systemd's socket")
    options = parser.parse_args(args[1:])
    try:
        if args[0] == "starter":
            return starter.main(options.config, options.state_dir, options.socket)
        load_admin_config(options.config)
    except AdminConfigError as exc:
        print(f"admin.toml refused: {exc}")
        return 2
    starter.sdk()  # the starter's SDK is in the release's venv
    # The bot is imported for what it imports: every other zab.admin module, the Zulip client
    # and httpx; the collector and the console for theirs, and the console's two static files
    # are read. A release without one of them fails here, before the switch.
    from zab.admin import bot, checks, collector, console

    del bot, collector
    for name, _ in console.STATIC.values():
        (Path(console.__file__).parent / "static" / name).read_bytes()
    problems = checks.check_scripts(checks.release_root())
    problems += check_helper(checks.release_root())
    for problem in problems:
        print(f"selfcheck: {problem}")
    if problems:
        return 1
    print("selfcheck: OK")
    return 0


def check_helper(root: Path) -> list[str]:
    """The config helper's command, as `HELPER_UNIT` gives it, run with `--help` from the release
    at `root` (for `current`): the interpreter root runs, the system's, under `-I -B -S`. So this
    fails when the helper imports anything from the venv (#437)."""
    try:
        lines = (root / HELPER_UNIT).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return [f"{HELPER_UNIT}: {exc.strerror}"]
    starts = [line.partition("=")[2] for line in lines if line.startswith("ExecStart=")]
    argv = shlex.split(starts[0].replace(CURRENT, str(root))) if len(starts) == 1 else []
    if argv[:1] != [HELPER_PYTHON]:
        return [f"{HELPER_UNIT}: not one ExecStart of {HELPER_PYTHON}"]
    try:  # the system's interpreter, and the release's own unit: no shell, no message text
        done = subprocess.run(  # noqa: S603
            [*argv, "--help"], capture_output=True, timeout=60, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [f"the config helper's command: {type(exc).__name__}"]
    if done.returncode != 0 or not done.stdout.startswith(b"usage: zab-admin-config"):
        last = done.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
        return [f"the config helper's command exited {done.returncode}: {last[0][:200]}"]
    return []


if __name__ == "__main__":
    sys.exit(main())
