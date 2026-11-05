"""The admin server's audit log: append-only JSON Lines with a hash chain (docs/ADMIN.md §2.8).

Each line is one JSON object, in ASCII: `seq` (1, 2, 3, …), `ts`, `event`, the event's fields,
and `prev`, the SHA-256 (hex) of the previous line's bytes without its newline, or 64 zeros on
the first line. Editing, deleting or reordering any line but the last breaks the next line's
`prev` or the run of `seq`. The last line, and a rewrite of the whole chain, are caught by the
chain heads the bot posts in `#organizers` (`audit #seq · chain <12 hex>`), which `verify`
checks with `--head`. Twelve hex digits make a forged tail cost 2^48 hashes per published
head, not 2^24.

Records are scrubbed with `Scrubber.record` before they are written. Each append takes an
exclusive `flock`, reads the head from the file itself (so two writers can't fork the chain),
writes the line with one `O_APPEND` write, and `fsync`s. Root sets `chattr +a` at bootstrap.
A log that ends in a partial line or an unreadable record is not extended: `AuditError`.

    python -m zab.admin audit verify [PATH] [--head SEQ:HEX ...]
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from zab.admin.scrub import Scrubber

DEFAULT_PATH = Path("/var/lib/zab-admin/audit.jsonl")
GENESIS = "0" * 64
EVENTS = frozenset(
    "request plan refused unchanged foreign approval reject expired stale apply_start apply_end"
    " interrupted upgrade backup read console_login wake_check config waker".split()
)  # §2.8
_RESERVED = frozenset({"seq", "ts", "event", "prev"})
# `SEQ:HEX`, or a head pasted as posted: `audit #SEQ · chain HEX`.
_HEAD_ARG = re.compile(
    r"(?:audit\s+)?#?(?P<seq>[1-9][0-9]*)(?::|\s*·\s*chain\s+)(?P<hex>[0-9a-fA-F]{6,64})"
)


class AuditError(Exception):
    """The log can't be extended: it ends in a partial line, or its last line is no record."""


@dataclass(frozen=True, slots=True)
class Head:
    """A record's place in the chain: its `seq` and the SHA-256 of its line."""

    seq: int
    sha256: str

    @property
    def chain(self) -> str:
        """The 12 hex digits each result message in `#organizers` shows."""
        return self.sha256[:12]

    def __str__(self) -> str:
        return f"audit #{self.seq} · chain {self.chain}"


def line_hash(line: bytes) -> str:
    """The SHA-256 (hex) of a line's bytes, without its newline."""
    return hashlib.sha256(line).hexdigest()


class AuditLog:
    """The append side. `scrub.record` scrubs every record, keys and values in context."""

    def __init__(
        self,
        path: Path = DEFAULT_PATH,
        *,
        scrub: Scrubber,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.path = path
        self._scrub = scrub
        self._now = now

    def append(self, event: str, **fields: Any) -> Head:
        """Append one record and return its head. `fields` must be JSON values."""
        if event not in EVENTS:
            raise ValueError(f"unknown audit event {event!r}")
        body = self._scrub.record(fields)
        if clash := _RESERVED & body.keys():  # after scrubbing: `se\x00q` becomes `seq`
            raise ValueError(f"reserved audit fields: {sorted(clash)}")
        flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC
        fd = os.open(self.path, flags, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            head = _tail_head(fd)
            ts = self._now().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            record = {"seq": head.seq + 1, "ts": ts, "event": event, **body, "prev": head.sha256}
            line = json.dumps(record, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
            data = line.encode("ascii")
            view = memoryview(data + b"\n")
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        finally:
            os.close(fd)  # and with it the lock
        return Head(head.seq + 1, line_hash(data))

    def head(self) -> Head:
        """The last record's head; seq 0 and the genesis hash for an empty or missing log."""
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_CLOEXEC)
        except FileNotFoundError:
            return Head(0, GENESIS)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH)
            return _tail_head(fd)
        finally:
            os.close(fd)


def _tail_head(fd: int) -> Head:
    """The head of the file's last line, read backwards from the end."""
    size = os.fstat(fd).st_size
    if size == 0:
        return Head(0, GENESIS)
    if os.pread(fd, 1, size - 1) != b"\n":
        raise AuditError("the audit log ends in a partial line: run `audit verify`")
    end = start = size - 1
    step = 4096
    while start > 0:
        lo = max(0, start - step)
        nl = os.pread(fd, start - lo, lo).rfind(b"\n")
        if nl >= 0:
            start = lo + nl + 1
            break
        start, step = lo, step * 2
    line = os.pread(fd, end - start, start)
    try:
        seq = json.loads(line)["seq"]
    except (ValueError, KeyError, TypeError) as exc:
        raise AuditError("the audit log's last line is not a record: run `audit verify`") from exc
    if type(seq) is not int or seq < 1:
        raise AuditError("the audit log's last line has no valid seq: run `audit verify`")
    return Head(seq, line_hash(line))


@dataclass(frozen=True, slots=True)
class Verification:
    """The result of `verify`: `problem` is None when the chain holds."""

    records: int
    head: Head
    problem: str | None = None
    line: int | None = None

    @property
    def ok(self) -> bool:
        return self.problem is None


def verify(path: Path, heads: Iterable[tuple[int, str]] = ()) -> Verification:
    """Check the chain from the first line, and every published head `(seq, hex prefix)`.

    Each head is checked, a repeated seq too: a wrong one is never hidden by a right one.
    """
    wanted: dict[int, list[str]] = {}
    for seq, prefix in heads:
        wanted.setdefault(seq, []).append(prefix.lower())
    head = Head(0, GENESIS)
    n = 0

    def fail(problem: str) -> Verification:
        return Verification(records=head.seq, head=head, problem=problem, line=n or None)

    try:
        f = path.open("rb")
    except OSError as exc:
        return fail(f"cannot read {path}: {exc.strerror}")
    with f:
        fcntl.flock(f.fileno(), fcntl.LOCK_SH)  # no half-written line from a running append
        for n, raw in enumerate(f, 1):
            if not raw.endswith(b"\n"):
                return fail("the last line is incomplete (no newline)")
            line = raw[:-1]
            try:
                record = json.loads(line)
            except ValueError:
                return fail("not JSON")
            if not isinstance(record, dict):
                return fail("not a JSON object")
            seq = record.get("seq")
            if type(seq) is not int or seq != head.seq + 1:
                return fail(
                    f"seq {seq!r} where {head.seq + 1} was expected:"
                    " a line was deleted, inserted or moved"
                )
            if record.get("prev") != head.sha256:
                return fail(
                    f"prev does not match line {n - 1}: that line was edited,"
                    " or a line was deleted or moved"
                    if n > 1
                    else "prev is not the genesis hash: the log does not start here"
                )
            head = Head(seq, line_hash(line))
            for prefix in wanted.get(seq, ()):
                if not head.sha256.startswith(prefix):
                    return fail(
                        f"#{seq} hashes to {head.sha256[: len(prefix)]}, but chain {prefix} was"
                        " published: the log was rewritten at this line or before it"
                    )
    missing = sorted(seq for seq in wanted if seq > head.seq)
    if missing:
        return fail(f"#{missing[0]} was published as a chain head but is missing: truncated")
    return Verification(records=head.seq, head=head)


def _head_arg(text: str) -> tuple[int, str]:
    m = _HEAD_ARG.fullmatch(text.strip())
    if m is None:
        raise argparse.ArgumentTypeError(
            f"expected SEQ:HEX or 'audit #SEQ · chain HEX' (6 to 64 hex digits), not {text!r}"
        )
    return int(m.group("seq")), m.group("hex")


def main(argv: list[str] | None = None) -> int:
    """`audit verify [PATH] [--head SEQ:HEX ...]`: exit 0 when the chain holds, 1 if not."""
    parser = argparse.ArgumentParser(prog="python -m zab.admin audit")
    sub = parser.add_subparsers(dest="command", required=True)
    cmd = sub.add_parser("verify", help="check the audit log's hash chain")
    cmd.add_argument("path", nargs="?", type=Path, default=DEFAULT_PATH)
    cmd.add_argument(
        "--head",
        action="append",
        default=[],
        type=_head_arg,
        metavar="SEQ:HEX",
        help="a chain head posted in #organizers, as SEQ:HEX or pasted as posted; repeatable",
    )
    args = parser.parse_args(argv)
    result = verify(args.path, args.head)
    if result.ok:
        print(f"ok: {result.records} records; {result.head}; sha256 {result.head.sha256}")
        return 0
    where = f" at line {result.line}" if result.line else ""
    print(f"FAILED{where}: {result.problem}", file=sys.stderr)
    print(f"the chain holds through {result.head}", file=sys.stderr)
    return 1
