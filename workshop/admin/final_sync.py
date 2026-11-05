#!/usr/bin/env python3
"""`final_sync.py --roster roster.csv [--only SLUG] [--dry-run]` (README.md §7.1).

Before the VMs are stopped or deleted (Drew, Sat Oct 10): make sure each participant's work is
on GitHub. For every provisioned row (`repo_status` = `ok`), over SSH as `ubuntu` with the
host key pinned (`scan_hosts.py`), it runs `final_sync_remote.py` as `agent`, read from stdin.
On the VM, that script, for the checkout and each topic worktree:

1. commits work not yet committed, unless a turn is running there. It skips `.lake`, files over
   50 MB and anything that looks like a credential, and lists what it left out;
2. pushes the branch, never forced, and then every other local branch the same way;
3. writes `TOPICS.md` on main: one row per Zulip topic and branch, with the agents' last
   commit and `HANDOVER.md` link.

**A repository that's gone** (#714): with a GitHub token (`GH_TOKEN`, which the admin bot
gives this step), each row's repository (the roster's `repo`) is looked up first, with the file
server's rule (`provision_repos.repo_gone`). One deleted or transferred by its participant is
not synced, and its row says `<repo> no longer exists here: the participant deleted or
transferred it`. When every row that failed failed so, the exit is 4 (`GONE`), which the admin
bot names in a `!retire` row's cell. Without a token, or on any GitHub error, the rows are synced
as before.

It prints one table, and writes the full JSON report to `workshop/admin/out/final_sync-<time>.json`
(git-ignored). `--strict`, which the admin bot always passes (docs/ADMIN.md §3, action 7): a
row with a VM (an `ip`) that is skipped, as not provisioned, is a failure too, so that exit 0
means every row with a VM was synced. `--dry-run` commits, writes and pushes nothing
(`git push --dry-run`). A rerun is harmless: clean worktrees and pushed branches report `clean`
and `up to date`.

Idle stop is on during every workshop (#440), so each VM's sync runs under the idle-stop hold
that rollouts take (`admin_common.IDLE_HOLD_ON_CMD`, #370): written before the sync, with an
expiry an hour ahead, and removed once the sync has ended on the VM, whatever its outcome. When
the sync's connection is cut instead (a timeout, ssh's own failure, a killed client, an
interrupt), the script may still be running there, so the hold is left to lapse at its expiry,
unless a later run on the VM (a rerun, a rollout) removes it first. A VM whose hold connection
fails or hangs isn't synced: a stopped VM is started first (`!start`). The JSON report says
what became of each row's hold (`idle_hold`). `--dry-run` takes no hold.

The remote commands are fixed literals; nothing from the roster reaches a shell.
Standalone: `uv run python workshop/admin/final_sync.py …`.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from admin_common import (
    DEFAULT_KNOWN_HOSTS,
    IDLE_HOLD_OFF_CMD,
    IDLE_HOLD_ON_CMD,
    Runner,
    RunResult,
    first_line,
    format_table,
    host_of,
    load_roster,
    run_parallel,
    select_rows,
    slug_of,
    ssh_argv,
    subprocess_runner,
)
from provision_repos import GitHub, repo_gone

REMOTE_SCRIPT = pathlib.Path(__file__).resolve().parent / "final_sync_remote.py"
OUT_DIR = pathlib.Path(__file__).resolve().parent / "out"  # git-ignored
#: Fixed literals: the script arrives on stdin; `cd /` because ubuntu's cwd is not agent's.
REMOTE_CMD = "cd / && sudo -u agent -H /usr/bin/python3 -"
REMOTE_CMD_DRY = REMOTE_CMD + " --dry-run"
#: One VM's sync at most: a sync that times out still has half the hold's hour to finish.
SYNC_TIMEOUT_S = 1800
#: The exit when every row that failed failed because its repository is gone (#714).
GONE = 4
#: A roster's `repo`, `<owner>/<name>`, as provision_repos.py writes it: checked before it is
#: put in an API path.
_REPO_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}")
#: ssh's own exit status: no connection, or one cut. A negative one is the runner's: -1 for a
#: timeout, -N for a client killed by signal N.
SSH_FAILED = 255
#: `report["index"]` values that are not a problem: done, or nothing to index, or a running
#: turn in the main checkout (already counted as one); anything else means no TOPICS.md on
#: GitHub (an error, a failed push, the checkout off main, a diverged main)
INDEX_FINE = ("written", "unchanged", "would write")
INDEX_SKIPS_FINE = ("skipped: no sessions", "skipped: a turn is running")
COLUMNS = [
    "participant",
    "host",
    "topics",
    "committed",
    "pushed",
    "left out",
    "TOPICS.md",
    "problems",
]


@dataclass(slots=True)
class SyncResult:
    slug: str
    host: str = "-"
    report: dict[str, Any] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    skipped: bool = False  # not provisioned: nothing to sync, and not a failure
    gone: bool = False  # its repository was deleted or transferred (#714): not synced
    hold: str = ""  # the idle-stop hold (#370): `released`, or why not; "" when none was tried

    def row(self) -> list[str]:
        trees = self.report.get("worktrees") or []
        states = [str(t.get("state", "")) for t in trees]
        # the branches no worktree has checked out are pushed too (review on #206)
        refs = trees + list(self.report.get("branches") or [])
        pushes = [str(t.get("push", "")) for t in refs]
        committed = sum(s in ("committed", "would commit") for s in states)
        pushed = sum(p in ("ok", "would push") for p in pushes)
        left = sum(len(t.get("big") or []) + len(t.get("secret") or []) for t in trees)
        running = sum(s.startswith("turn running") for s in states)

        def name(t: Mapping[str, Any]) -> str:  # the worktree's directory when it has no branch
            return str(t.get("branch") or pathlib.PurePath(str(t.get("path", ""))).name)

        problems = list(self.problems)
        problems += [
            f"{name(t)}: {s}" for t, s in zip(trees, states, strict=True) if s.startswith("error")
        ]
        # a detached worktree's commits are on no branch, so on the VM only (codex round 2)
        problems += [
            f"{name(t)}: push {p}"
            for t, p in zip(refs, pushes, strict=True)
            if p.startswith(("rejected", "error", "detached"))
        ]
        problems += [str(e) for e in self.report.get("errors") or []]
        index = str(self.report.get("index") or "")
        if index and index not in INDEX_FINE and not index.startswith(INDEX_SKIPS_FINE):
            problems.append(f"TOPICS.md: {index}")  # e.g. `written, push error: …` (codex round 2)
        if running:
            problems.append(f"{running} with a turn running (not committed)")
        return [
            self.slug,
            self.host,
            str(len(trees)) if trees else ("-" if not self.report else "0"),
            str(committed),
            str(pushed),
            str(left),
            str(self.report.get("index", "-")),
            first_line("; ".join(problems), 160),
        ]


def parse_report(stdout: str) -> dict[str, Any] | None:
    """The JSON object on the last non-empty line of the remote script's output."""
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                return None
            return data if isinstance(data, dict) else None
    return None


def sync_vm(
    row: Mapping[str, str],
    runner: Runner,
    *,
    dry_run: bool,
    script: str,
    known_hosts: str = str(DEFAULT_KNOWN_HOSTS),
    strict: bool = False,
    gh: GitHub | None = None,
) -> SyncResult:
    slug = slug_of(row)
    status = (row.get("repo_status") or "").strip()
    if status != "ok":
        if strict and (row.get("ip") or "").strip():  # a `repo_status` isn't proof of no work
            why = (
                f"repo_status is {first_line(status, 40)!r}, not ok" if status else "no repo_status"
            )
            return SyncResult(slug=slug, problems=[f"has a VM but {why}: not synced"])
        return SyncResult(slug=slug, problems=["not provisioned: nothing to sync"], skipped=True)
    repo = (row.get("repo") or "").strip()
    if gh is not None and _REPO_RE.fullmatch(repo) and repo_gone(gh, *repo.split("/")):
        return SyncResult(slug=slug, problems=[gone_line(repo)], gone=True)
    try:
        host = host_of(row)
    except ValueError as exc:
        return SyncResult(slug=slug, problems=[str(exc)])

    def ssh(remote: str) -> list[str]:
        return ssh_argv(host, remote, known_hosts=known_hosts)

    if dry_run:  # writes nothing on the VM, the hold included
        return synced(
            slug, host, runner(ssh(REMOTE_CMD_DRY), timeout_s=SYNC_TIMEOUT_S, stdin=script)
        )
    held = runner(ssh(IDLE_HOLD_ON_CMD), timeout_s=60)
    if cut(held):  # unreachable (a stopped VM, most often) or hung: no sync either
        return synced(slug, host, held, hold=f"not confirmed: {cause(held)}")
    res = runner(ssh(REMOTE_CMD), timeout_s=SYNC_TIMEOUT_S, stdin=script)
    if not held.ok:  # refused on the VM; the sync went ahead: idle stop counts it as a login
        hold = f"not taken: {cause(held)}"
    elif parse_report(res.stdout) is None and cut(res):
        # cut, not ended: the script may still run on the VM (an exception, such as an
        # interrupt, propagates with the hold left on too); it lapses within the hour
        hold = "left to lapse: the sync's connection was cut"
    else:  # the sync ended on the VM, whatever its outcome
        off = runner(ssh(IDLE_HOLD_OFF_CMD), timeout_s=60)
        hold = "released" if off.ok else f"not released (it lapses within the hour): {cause(off)}"
    return synced(slug, host, res, hold=hold)


def gone_line(repo: str) -> str:
    return f"{repo} no longer exists here: the participant deleted or transferred it"


def github(env: Mapping[str, str] | None = None) -> GitHub | None:
    """A client with `GH_TOKEN` (or `GITHUB_TOKEN`), or None without one: no check (#714)."""
    env = os.environ if env is None else env
    token = (env.get("GH_TOKEN") or env.get("GITHUB_TOKEN") or "").strip()
    return GitHub(token) if token else None


def cut(res: RunResult) -> bool:
    """Whether the connection ended without the remote command's own exit status."""
    return res.exit_code < 0 or res.exit_code == SSH_FAILED


def cause(res: RunResult) -> str:
    return first_line(res.stderr, 100) or f"ssh exit {res.exit_code}"


def synced(slug: str, host: str, res: RunResult, *, hold: str = "") -> SyncResult:
    """The row's result, from the report on the last line of the connection's output."""
    report = parse_report(res.stdout)
    if report is None:
        return SyncResult(slug=slug, host=host, problems=[f"no report: {cause(res)}"], hold=hold)
    return SyncResult(slug=slug, host=host, report=report, hold=hold)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="final_sync.py", description=__doc__.split("\n\n")[1])
    parser.add_argument("--roster", required=True)
    parser.add_argument("--only", action="append", metavar="SLUG", help="repeatable")
    parser.add_argument("--dry-run", action="store_true", help="commit, write and push nothing")
    parser.add_argument("--parallel", type=int, default=6)
    parser.add_argument("--known-hosts", default=str(DEFAULT_KNOWN_HOSTS))
    parser.add_argument("--out-dir", default=str(OUT_DIR), help="where the JSON report goes")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="a row with a VM skipped as not provisioned is a failure (the admin bot's mode)",
    )
    args = parser.parse_args(argv)
    try:
        rows = select_rows(load_roster(args.roster), args.only)
    except ValueError as exc:
        print(f"roster: {exc}", file=sys.stderr)
        return 1
    script = REMOTE_SCRIPT.read_text(encoding="utf-8")
    gh = github()
    results = run_parallel(
        rows,
        lambda row: sync_vm(
            row,
            subprocess_runner,
            dry_run=args.dry_run,
            script=script,
            known_hosts=args.known_hosts,
            strict=args.strict,
            gh=gh,
        ),
        parallel=args.parallel,
        on_error=lambda row, exc: SyncResult(
            slug=slug_of(row), problems=[f"{type(exc).__name__}: {exc}"]
        ),
    )
    print(format_table(COLUMNS, [r.row() for r in results]))
    out = pathlib.Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = out / f"final_sync-{stamp}{'-dry' if args.dry_run else ''}.json"
    path.write_text(
        json.dumps(
            {
                r.slug: {
                    "host": r.host,
                    **r.report,
                    "problems": r.problems,
                    **({"idle_hold": r.hold} if r.hold else {}),
                }
                for r in results
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    # an unreachable VM or a missing report is a failure too: its work may not be on GitHub
    # (codex review on #206); only unprovisioned rows are skipped
    failed = [r for r in results if r.row()[-1] and not r.skipped]
    skipped = sum(r.skipped for r in results)
    print(
        f"\nreport: {path}"
        + (f"; {len(failed)} VM(s) with problems" if failed else "; no problems")
        + (f"; {skipped} row(s) not provisioned (skipped)" if skipped else "")
    )
    if args.dry_run:
        print("(dry run: nothing committed, written or pushed)")
    if gh is None:  # by hand, without a token: the admin bot always gives one (ADMIN §2.3)
        print("(no GitHub token: whether each repository still exists was not checked)")
    if failed and all(r.gone for r in failed):
        return GONE
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
