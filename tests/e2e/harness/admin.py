"""The admin scripts as the scenarios need them (issue #263): `push_config.py` for the test VM.

The tested build's `workshop/admin/push_config.py` (`ZAB_E2E_ADMIN_DIR`, set by `run.py` to its
worktree of that build, so idle stop installs that build's `idle_stop.py`: codex on #285), else
this checkout's; with the rig's roster, out dir, known_hosts and key (`ZAB_SSH_IDENTITY`), for
the rig's participant only. Argument lists only.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

from e2e.harness.config import CHECKOUT, RigConfig
from e2e.harness.vm import Result

Local = Callable[[list[str], dict[str, str], float], Result]


def admin_dir(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    given = env.get("ZAB_E2E_ADMIN_DIR")
    return Path(given) if given else CHECKOUT / "workshop" / "admin"


def local(argv: list[str], env: dict[str, str], timeout: float) -> Result:
    try:
        done = subprocess.run(  # noqa: S603 - an argument list
            argv,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return Result(124, "", f"timed out after {timeout:.0f} s")
    return Result(done.returncode, done.stdout, done.stderr)


def push_config(rig: RigConfig, *extra: str, run: Local = local, timeout: float = 900) -> Result:
    """`push_config.py … --only <slug> <extra>`; RuntimeError unless it succeeds."""
    argv = [
        sys.executable,
        str(admin_dir() / "push_config.py"),
        *("--roster", str(rig.roster), "--only", rig.slug),
        *("--out-dir", str(rig.out_dir), "--known-hosts", str(rig.known_hosts)),
        *extra,
    ]
    env = {**os.environ, "ZAB_SSH_IDENTITY": str(rig.ssh_key)}
    result = run(argv, env, timeout)
    if not result.ok:
        raise RuntimeError(f"push_config {' '.join(extra)}: {failure(result, rig.slug)}")
    return result


def failures(output: str, slug: str) -> str:
    """The `failures` cell of `slug`'s row in the table push_config prints (`render`: one row
    per VM, the last column `failures`, every column padded to its width), or ""."""
    lines = output.splitlines()
    for i, head in enumerate(lines):
        if head.startswith("participant ") and head.rstrip().endswith(" failures"):
            at = head.rstrip().rindex(" failures") + 1
            for row in lines[i + 2 :]:  # past the header's dashes
                if row.split(maxsplit=1)[:1] == [slug]:
                    return row[at:].strip()
    return ""


def failure(result: Result, slug: str) -> str:
    """Why a push_config run failed: its exit status, and its row's failures (#517), else the
    last line of its output (a crash, a timeout): its stderr ends `1 VM(s) failed`."""
    said = failures(result.stdout, slug)
    if not said:
        tail = (result.stderr or result.stdout).strip().splitlines()[-1:] or [""]
        said = tail[0]
    return f"exit {result.returncode}: {said[:600]}"
