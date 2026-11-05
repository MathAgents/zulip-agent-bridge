#!/usr/bin/env python3
"""`scan_hosts.py --roster roster.csv [--known-hosts PATH] [--replace]` (README.md §0).

Records every VM's SSH host key, once, right after `launch.py` filled in the roster's `ip`
column, so that `push_config.py` and `status.py` can pin them (`StrictHostKeyChecking=yes` with
only this file trusted). For each row it runs `ssh-keyscan` against the IP and checks the
ed25519 key's `SHA256:` fingerprint against the row's `host_keys`: the fingerprints cloud-init
printed to the instance's console at first boot, which `launch.py` recorded (PM on issue #36,
Mon 07:10: Drew compares nothing by hand). A key that is not among them is refused, whatever the
options: whatever answered on that address is not the VM. A row without `host_keys` (a VM
launched before the column, or stopped before its console was read) falls back to recording the
key with a warning, for a comparison by hand with `aws ec2 get-console-output --latest
--instance-id …`. A key that differs from the one already in `known_hosts` is refused unless
`--replace` is given (a reinstalled VM; anything else is a reason to stop), or unless the row's
`host_keys` has it (#604): the console vouches for it as this VM's, as for a key recorded the
first time, and the recorded one was a previous VM's at that address. EC2 may give an Elastic IP
released with one VM to the next one it allocates, in this workshop too.

Standalone: `uv run python workshop/admin/scan_hosts.py …`. Nothing here is secret: host keys
are public, and the file lives in the git-ignored `out/`.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from dataclasses import dataclass

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from admin_common import (
    DEFAULT_KNOWN_HOSTS,
    Runner,
    first_line,
    format_table,
    host_key_fingerprint,
    host_of,
    load_roster,
    run_parallel,
    select_rows,
    slug_of,
    subprocess_runner,
)

KEY_TYPE = "ssh-ed25519"


@dataclass(slots=True)
class Scan:
    slug: str
    ip: str = ""
    status: str = ""  # added | unchanged | CHANGED | replaced | MISMATCH | unreachable | error
    fingerprint: str = ""
    console: str = ""  # verified | unverified (no host_keys in the roster)
    detail: str = ""
    line: str = ""  # the known_hosts line to record (added and replaced only)


def keyscan_argv(ip: str) -> list[str]:
    return ["ssh-keyscan", "-T", "10", "-t", "ed25519", "--", ip]


def key_line(stdout: str, ip: str) -> str | None:
    """The `<ip> ssh-ed25519 <key>` line of `ssh-keyscan`'s output (comments go to stderr)."""
    for line in stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0] == ip and parts[1] == KEY_TYPE:
            return " ".join(parts[:3])
    return None


def read_known_hosts(path: pathlib.Path) -> dict[str, str]:
    """`{ip: line}` for the ed25519 entries of the file (other lines are kept verbatim on write)."""
    entries: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[1] == KEY_TYPE and not parts[0].startswith(("#", "@")):
                entries[parts[0]] = " ".join(parts[:3])
    return entries


def write_known_hosts(path: pathlib.Path, entries: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    others: list[str] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if not (len(parts) >= 3 and parts[1] == KEY_TYPE and parts[0] in entries):
                others.append(line)
    body = "\n".join([*others, *(entries[ip] for ip in sorted(entries))]).strip("\n")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(body + "\n", encoding="utf-8")
    tmp.replace(path)


def scan_row(row: dict[str, str], runner: Runner, known: dict[str, str], *, replace: bool) -> Scan:
    """One `ssh-keyscan`; compares with the recorded key and says what to write (`line`)."""
    scan = Scan(slug=slug_of(row))
    try:
        scan.ip = host_of(row)
    except ValueError as exc:
        scan.status, scan.detail = "error", str(exc)
        return scan
    res = runner(keyscan_argv(scan.ip), timeout_s=30)
    line = key_line(res.stdout, scan.ip)
    if line is None:
        scan.status = "unreachable"
        scan.detail = first_line(res.stderr, 80) or f"ssh-keyscan exit {res.exit_code}"
        return scan
    try:
        scan.fingerprint = host_key_fingerprint(line)
    except ValueError as exc:
        scan.status, scan.detail = "error", str(exc)
        return scan
    printed = (row.get("host_keys") or "").split()
    if printed and scan.fingerprint not in printed:
        scan.status = "MISMATCH"
        scan.detail = (
            f"not the key the VM's console printed ({', '.join(printed)}); nothing recorded"
        )
        return scan
    scan.console = "verified" if printed else "unverified"
    recorded = known.get(scan.ip)
    if recorded is None:
        scan.status, scan.line = "added", line
    elif recorded == line:
        scan.status = "unchanged"
    elif replace or printed:  # the console vouches for the new key: the VM is this row's (#604)
        scan.status, scan.line = "replaced", line
        vouched = "; the console printed this one: the recorded key was a previous VM's here"
        scan.detail = f"was {host_key_fingerprint(recorded)}" + ("" if replace else vouched)
    else:
        scan.status = "CHANGED"
        scan.detail = f"recorded {host_key_fingerprint(recorded)}; not replaced (use --replace)"
    return scan


def render(scans: list[Scan]) -> str:
    return format_table(
        ["participant", "ip", "key", "console", "fingerprint", "detail"],
        [
            [s.slug, s.ip or "-", s.status, s.console or "-", s.fingerprint or "-", s.detail]
            for s in scans
        ],
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scan_hosts.py",
        description="Record the VMs' SSH host keys for pinning (README.md §0).",
    )
    parser.add_argument("--roster", required=True)
    parser.add_argument("--known-hosts", default=str(DEFAULT_KNOWN_HOSTS))
    parser.add_argument("--only", action="append", metavar="SLUG")
    parser.add_argument("--parallel", type=int, default=8)
    parser.add_argument(
        "--replace", action="store_true", help="accept a key that differs from the recorded one"
    )
    args = parser.parse_args(argv)
    try:
        rows = select_rows(load_roster(args.roster), args.only)
    except ValueError as exc:
        print(f"roster: {exc}", file=sys.stderr)
        return 1
    path = pathlib.Path(args.known_hosts).expanduser()
    known = read_known_hosts(path)
    scans = run_parallel(
        rows,
        lambda row: scan_row(row, subprocess_runner, known, replace=args.replace),
        parallel=args.parallel,
        on_error=lambda row, exc: Scan(
            slug=slug_of(row), status="error", detail=f"{type(exc).__name__}: {exc}"
        ),
    )
    written = {scan.ip: scan.line for scan in scans if scan.line}
    if written:
        write_known_hosts(path, {**known, **written})
    print(render(scans))
    verified = sum(s.console == "verified" for s in scans)
    print(f"\n{len(written)} key(s) written to {path}; {verified} matched the console.")
    unverified = [s.slug for s in scans if s.console == "unverified"]
    if unverified:
        print(
            f"WARNING not verified (no host_keys in the roster): {', '.join(unverified)}. "
            "Compare each fingerprint with `aws ec2 get-console-output --latest --instance-id … "
            "--output text`, between the BEGIN/END SSH HOST KEY FINGERPRINTS lines, or rerun "
            "launch.py, which records them while the console still has them.",
            file=sys.stderr,
        )
    bad = [s for s in scans if s.status in ("CHANGED", "MISMATCH", "unreachable", "error")]
    if bad:
        print(f"\n{len(bad)} host(s) need attention", file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
