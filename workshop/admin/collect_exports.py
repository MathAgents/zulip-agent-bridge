#!/usr/bin/env python3
"""`collect_exports.py --roster R --known-hosts KH --out-dir DIR [--only SLUG …] [--dry-run]`
(README.md §7.2, steps 1 to 4; docs/ADMIN.md §3, action 7, `!export`).

For each selected row with a VM, over SSH as `ubuntu` with the host key pinned (`scan_hosts.py`):

1. runs `export.sh` as `agent`, read from stdin (a fixed command; nothing from the roster reaches
   the VM's shell unquoted). It writes `/home/agent/export-<slug>.tar.gz`, without build trees
   and without any credential, and refuses to leave an archive that names one;
2. fetches the archive as `agent` (`sudo -u agent cat`, never root) into a temporary file, new,
   0600, in a 0700 directory: at most `--max-mb`, and never past `--reserve-mb` of free space,
   since the server's disk also holds its audit log. One run at a time per `DIR` (a lock),
   which first deletes what a killed run left;
3. has it re-packed (#374) by the sandboxed checker, `zab-archive-check@.service`, on its socket
   (`--checker-socket`, `CHECKER_SOCKET`): no network, no key, a user of its own. This script
   parses none of the archive, which is the VM's: untrusted input to `gzip` and `tarfile`. It
   hands the checker the fetched file, read-only, through the descriptor it was written with,
   and writes the new archive the checker sends back to a second temporary file, renamed to
   `DIR/export-<slug>.tar.gz` only once the answer ends whole (`Deframer`). The participant's
   own archive is always deleted; a failed rerun keeps the archive collected before. What the
   stored archive is, is stated once, in `archive_check.py`'s docstring: only regular files and
   directories that its own writer produced from the members the check approved, with
   symlinks listed in `SYMLINKS.txt`; extracted into an empty directory, it puts no file at a
   credential's path (`auth.json`, `.credentials.json`, `*.zuliprc`, `.config/zab/`). Not
   claimed: a credential under another name, or inside a nested archive.

Every process it runs, SSH, goes through `Bounded`: its output is read as it comes, into a
file up to a cap while the disk has room or only its first few KB kept, and the process is
stopped past a cap or a timeout. The checker's answer is read the same way, within a timeout.
So nothing a VM sends can fill the server's memory or disk.

Rows not provisioned (`repo_status` other than `ok`) have nothing to export and are skipped, as
in `final_sync.py`. It prints one table, and exits 1 if any other row failed. The archives are
the participants' private work: keep `DIR` out of any synced folder, give each archive to its
participant only, and delete it once delivered (README.md §7.2, by hand).

`--dry-run` lists what it would collect and connects to nothing.
Standalone: `uv run python workshop/admin/collect_exports.py …`.
"""

from __future__ import annotations

import argparse
import array
import contextlib
import fcntl
import os
import pathlib
import re
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import BinaryIO, Protocol

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from admin_common import (
    DEFAULT_KNOWN_HOSTS,
    RunResult,
    first_line,
    format_table,
    host_of,
    load_roster,
    run_parallel,
    select_rows,
    slug_of,
    ssh_argv,
)

HERE = pathlib.Path(__file__).resolve().parent
EXPORT_SCRIPT = HERE / "export.sh"
OUT_DIR = HERE / "out" / "exports"  # git-ignored
#: A fixed literal: the script arrives on stdin; `cd /` because ubuntu's cwd is not agent's.
EXPORT_CMD = "cd / && sudo -u agent -H bash -s"
_SLUG = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?", re.ASCII)
EXPORT_TIMEOUT = 1200.0  # seconds for export.sh on one VM
FETCH_TIMEOUT = 1200.0  # and for its fetch
CHECK_TIMEOUT = 900.0  # and for the re-pack, whose CPU is limited too (archive_check.py)
CHUNK = 1 << 20
KEPT = 4096  # bytes of a process's stdout (when not a file) and stderr kept
OUTPUT_CAP = 1 << 20  # a process printing more, on either stream, is stopped
MAX_EXPANDED = 65536 << 20  # bytes the check decompresses at most, by default
COLUMNS = ["participant", "host", "archive", "size", "result"]


def remote_archive(slug: str) -> str:
    """Where `export.sh` leaves the archive on the VM (`$HOME` is agent's)."""
    return f"/home/agent/export-{slug}.tar.gz"


def archive_name(slug: str) -> str:
    return f"export-{slug}.tar.gz"


class Run(Protocol):
    def __call__(
        self, argv: list[str], *, timeout_s: float, stdin: bytes | BinaryIO | None = None,
        dest: BinaryIO | None = None, limit: int = OUTPUT_CAP,
        room: Callable[[], bool] = lambda: True, env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> RunResult: ...  # fmt: skip


class Bounded:
    """Runs a process with its output bounded (Codex rounds 1 and 3): `stdin` is bytes, fed on
    a thread, or a file; stdout goes to `dest` up to `limit` bytes while `room()` holds, or,
    without one, only its first `KEPT` bytes are kept and the process is stopped past `limit`;
    stderr is read on a thread, its first `KEPT` bytes kept, and the process stopped past
    `OUTPUT_CAP`; all within `timeout_s`. A process stopped for any of these gives exit -1 and
    the reason as its stderr. A seam: tests pass their own for the SSH calls."""

    def __call__(
        self, argv: list[str], *, timeout_s: float, stdin: bytes | BinaryIO | None = None,
        dest: BinaryIO | None = None, limit: int = OUTPUT_CAP,
        room: Callable[[], bool] = lambda: True, env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> RunResult:  # fmt: skip
        feed = subprocess.PIPE if isinstance(stdin, bytes) else stdin or subprocess.DEVNULL
        try:  # an argument list from this script, no shell (CONTRIBUTING.md, rule 3)
            proc = subprocess.Popen(  # noqa: S603
                argv, stdin=feed, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=cwd
            )
        except OSError as exc:
            return RunResult(-1, "", f"cannot run {argv[0]}: {exc}")
        why: list[str] = []  # the first reason it was stopped, from any thread
        out, err = bytearray(), bytearray()

        def stop(reason: str) -> None:
            why.append(reason)
            proc.kill()

        def write() -> None:
            assert proc.stdin is not None and isinstance(stdin, bytes)
            try:
                proc.stdin.write(stdin)
            except (BrokenPipeError, OSError):
                pass  # it stopped reading: its exit says why
            finally:
                proc.stdin.close()

        def drain() -> None:
            assert proc.stderr is not None
            total = 0
            while chunk := proc.stderr.read(65536):
                total += len(chunk)
                err.extend(chunk[: max(0, KEPT - len(err))])
                if total > OUTPUT_CAP:
                    stop(f"its stderr passed {OUTPUT_CAP >> 10} KB")
                    break

        threads = [threading.Thread(target=drain, daemon=True)]
        threads += [threading.Thread(target=write, daemon=True)] if isinstance(stdin, bytes) else []
        for thread in threads:
            thread.start()
        timer = threading.Timer(timeout_s, stop, (f"timed out after {timeout_s:g} s",))
        timer.start()
        size = 0
        try:
            assert proc.stdout is not None
            while chunk := proc.stdout.read(CHUNK):
                size += len(chunk)
                if size > limit:
                    unit = f"{limit >> 20} MB" if limit >= 1 << 20 else f"{limit} bytes"
                    stop(f"larger than {unit}: not kept" if dest else f"printed over {unit}")
                    break
                if dest is None:
                    out.extend(chunk[: max(0, KEPT - len(out))])
                elif not room():
                    stop("the disk is nearly full: not kept")
                    break
                else:
                    dest.write(chunk)
            code = proc.wait()
        finally:
            timer.cancel()
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            for thread in threads:
                thread.join(timeout=5)
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                if pipe is not None:
                    pipe.close()
        if why:
            return RunResult(-1, "", why[0])
        text = bytes(out).decode("utf-8", "replace")
        return RunResult(code, text, bytes(err).decode("utf-8", "replace"))


#: `zab-archive-check.socket`: the sandboxed re-pack (archive_check.py, docs/ADMIN.md §1.2).
CHECKER_SOCKET = "/run/zab-archive-check/check.sock"
MAX_CHECKS = 4  # its MaxConnections: one more at once is closed unanswered
FRAME = 1 << 20  # the largest data frame (archive_check.CHUNK)
NOTE = 300  # the longest end or refusal frame
_TEXT = re.compile(r"[ -~]*")


class Unanswered(Exception):
    """The checker's answer is not one `archive_check.Frames` writes, or can't be kept."""


class Deframer:
    """The checker's answer (`archive_check.Frames`), taken as it comes: the new archive's
    bytes to `dest`, at most `limit` of them and only while `room()` holds; then its end, which
    must count them, or a refusal; then nothing. Anything else raises `Unanswered`."""

    def __init__(self, dest: BinaryIO, limit: int, room: Callable[[], bool]) -> None:
        self.dest, self.limit, self.room = dest, limit, room
        self.pending, self.got = bytearray(), 0
        self.end: tuple[bytes, str] | None = None  # (b"E", note) or (b"R", refusal)

    def write(self, data: bytes) -> int:
        self.pending += data
        while len(self.pending) >= 5:
            kind, size = bytes(self.pending[:1]), int.from_bytes(self.pending[1:5], "big")
            if self.end is not None:
                raise Unanswered("more after its end")
            framed = kind == b"D" and 0 < size <= FRAME
            if not (framed or (kind in (b"E", b"R") and size <= NOTE)):
                raise Unanswered("a malformed answer")
            if len(self.pending) < 5 + size:
                break
            payload = bytes(self.pending[5 : 5 + size])
            del self.pending[: 5 + size]
            if kind == b"D":
                self.got += size
                if self.got > self.limit:
                    raise Unanswered(f"larger than {self.limit >> 20} MB: not kept")
                if not self.room():
                    raise Unanswered("the disk is nearly full: not kept")
                self.dest.write(payload)
                continue
            text = payload.decode("ascii", "replace")
            if not _TEXT.fullmatch(text) or (kind == b"R" and not text):
                raise Unanswered("a malformed answer")
            if kind == b"E":
                count, _, text = text.partition(" ")
                if count != str(self.got):
                    raise Unanswered("its end doesn't count what came")
            self.end = (kind, text)
        return len(data)

    def verdict(self) -> tuple[str, str]:
        """(refusal, "") or ("", note), once the checker has closed the connection."""
        if self.pending:
            raise Unanswered("a malformed answer")
        if self.end is None:
            raise Unanswered("no end to its answer")
        kind, text = self.end
        return (f"{text}: deleted", "") if kind == b"R" else ("", text)


def peer_uid(sock: socket.socket) -> int:
    """The uid of the process at the other end of `sock` (`SO_PEERCRED`): for a socket systemd
    listens on, systemd's."""
    creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", creds)[1]


def repack_file(
    archive: BinaryIO, dest: BinaryIO, *, budget: int = MAX_EXPANDED, limit: int = 4096 << 20,
    room: Callable[[], bool] = lambda: True, checker: str | None = None,
) -> tuple[str, str]:  # fmt: skip
    """The archive in the open file `archive`, checked and re-packed into `dest` by the
    sandboxed checker on `checker` (`CHECKER_SOCKET`; archive_check.py): ("", note) once the
    new archive is whole in `dest`, else (why it can't be handed over, ""). Nothing here parses
    the archive. The checker must be root's (or this process's own user's, as in the tests),
    and gets the same file read-only, reopened through its descriptor's /proc link (so the
    very inode, with no race), so it can't change the bytes it reads twice. Only an answer that
    ends as `Frames` ends one counts: a crash, a limit, the timeout, any other output refuses
    the archive, and what `dest` holds then is not kept."""
    answer = Deframer(dest, limit, room)
    path = checker or CHECKER_SOCKET
    deadline = time.monotonic() + CHECK_TIMEOUT
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(CHECK_TIMEOUT)
            try:
                sock.connect(path)
            except OSError as exc:
                raise Unanswered(f"no checker on {path}: {type(exc).__name__}") from None
            if peer_uid(sock) not in (0, os.getuid()):
                raise Unanswered(f"{path} is not root's")
            flags = os.O_RDONLY | os.O_CLOEXEC
            readonly = os.open(f"/proc/self/fd/{archive.fileno()}", flags)
            try:
                fds = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [readonly]))]
                sock.sendmsg([b"repack %d\n" % budget], fds)
            except OSError:
                pass  # refused before it read the request: its answer says why
            finally:
                os.close(readonly)
            answered = False
            while True:
                left = deadline - time.monotonic()
                try:
                    if left <= 0:
                        raise TimeoutError
                    sock.settimeout(left)
                    chunk = sock.recv(FRAME + 5)
                except TimeoutError:
                    raise Unanswered(f"timed out after {CHECK_TIMEOUT:g} s") from None
                except ConnectionResetError:
                    chunk = b""  # it closed with the request unread: what came is its answer
                except OSError as exc:
                    raise Unanswered(f"its answer broke off: {type(exc).__name__}") from None
                if not chunk:
                    if not answered:
                        raise Unanswered(
                            f"no answer at all: busy, at most {MAX_CHECKS} archives at once"
                            ", or it failed; export again"
                        )
                    return answer.verdict()
                answered = True
                answer.write(chunk)
    except Unanswered as exc:
        return f"the archive check failed ({exc}): deleted", ""


@dataclass(slots=True)
class ExportResult:
    slug: str
    host: str = "-"
    archive: str = "-"
    size: str = "-"
    problems: list[str] = field(default_factory=list)  # any makes the row a failure
    note: str = "ok"  # otherwise: ok, skipped (not provisioned), or what a dry run would do

    def row(self) -> list[str]:
        result = first_line("; ".join(self.problems), 100) if self.problems else self.note
        return [self.slug, self.host, self.archive, self.size, result]


def _size(n: int) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n} {unit}"
        n //= 1024
    return f"{n} GB"


def export_dir(path: pathlib.Path) -> pathlib.Path:
    """`DIR`, made 0700 (the workshop's `out/` is already): the archives are private work."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"{path} is not a directory")
    os.chmod(path, 0o700)
    return path


def _failed(what: str, ran: RunResult) -> str:
    return f"{what} failed: {first_line(ran.stderr, 100) or f'exit {ran.exit_code}'}"


def collect(
    row: Mapping[str, str],
    ssh: Run,
    *,
    out_dir: pathlib.Path,
    script: str,
    known_hosts: str,
    limit: int,
    reserve: int,
    dry_run: bool,
    expanded: int = MAX_EXPANDED,
    checker: str | None = None,
) -> ExportResult:
    """One row: export on the VM, fetch, check, keep (module doc)."""
    slug = slug_of(dict(row))
    if not _SLUG.fullmatch(slug):
        return ExportResult(slug=slug or "?", problems=["not a valid slug"])
    if (row.get("repo_status") or "").strip() != "ok":
        return ExportResult(slug=slug, note="skipped: not provisioned, nothing to export")
    try:
        host = host_of(row)
    except ValueError as exc:
        return ExportResult(slug=slug, problems=[str(exc)])
    final = out_dir / archive_name(slug)
    result = ExportResult(slug=slug, host=host, archive=final.name)
    if dry_run:
        result.note = "would export and fetch" + (", replacing the one here" * final.exists())
        return result
    made = ssh(
        ssh_argv(host, EXPORT_CMD, known_hosts=known_hosts),
        stdin=script.encode("utf-8"),
        timeout_s=EXPORT_TIMEOUT,
    )
    if not made.ok:
        result.problems.append(_failed("export.sh", made))
        return result
    part, new = (out_dir / f".{final.name}.{end}" for end in ("part", "new"))
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    # as agent, who owns the file: never root, which would follow a link agent planted
    fetch_cmd = shlex.join(["sudo", "-u", "agent", "cat", "--", remote_archive(slug)])
    room = lambda: shutil.disk_usage(out_dir).free >= reserve  # noqa: E731
    try:
        part.unlink(missing_ok=True)
        new.unlink(missing_ok=True)
        with os.fdopen(os.open(part, flags, 0o600), "w+b") as fetched:
            got = ssh(
                ssh_argv(host, fetch_cmd, known_hosts=known_hosts),
                dest=fetched,
                limit=limit,
                timeout_s=FETCH_TIMEOUT,
                room=room,
            )
            if not got.ok:
                result.problems.append(_failed("fetch", got))
                return result
            fetched.flush()
            with os.fdopen(os.open(new, flags, 0o600), "w+b") as dest:
                # the bytes fetched, never reopened by name; only the new archive is kept
                problem, note = repack_file(
                    fetched, dest, budget=expanded, limit=limit, room=room, checker=checker
                )
                if problem:
                    result.problems.append(problem)
                    return result
                dest.flush()
                written, there = os.fstat(dest.fileno()), os.lstat(new)
                if (written.st_dev, written.st_ino) != (there.st_dev, there.st_ino):
                    result.problems.append("its temporary file was replaced: not kept")
                    return result
                result.size = _size(written.st_size)
                result.note = f"ok: {note}" if note else "ok"
                os.rename(new, final)
    finally:
        part.unlink(missing_ok=True)  # the participant's own archive is never kept
        new.unlink(missing_ok=True)
    return result


@contextlib.contextmanager
def exports_lock(out_dir: pathlib.Path) -> Iterator[None]:
    """One run at a time on `out_dir`, the bot's or one by hand (the review of #360): an
    exclusive `flock` on `out_dir/.lock` for the whole run, refused if another run holds it.
    Under it, the temporary files a killed run left behind are deleted first."""
    fd = os.open(out_dir / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(f"another collect_exports.py run is using {out_dir}") from None
        for end in ("part", "new"):
            for stale in out_dir.glob(f".export-*.tar.gz.{end}"):
                stale.unlink(missing_ok=True)
        yield
    finally:
        os.close(fd)


def main(argv: list[str] | None = None, *, ssh: Run | None = None) -> int:
    """`ssh`: what runs the SSH calls (`Bounded`); tests pass a fake VM."""
    parser = argparse.ArgumentParser(
        prog="collect_exports.py", description=__doc__.split("\n\n")[1]
    )
    parser.add_argument("--roster", required=True)
    parser.add_argument("--only", action="append", metavar="SLUG", help="repeatable")
    parser.add_argument("--dry-run", action="store_true", help="list; connect to nothing")
    parser.add_argument("--parallel", type=int, default=2)
    parser.add_argument("--known-hosts", default=str(DEFAULT_KNOWN_HOSTS))
    parser.add_argument("--out-dir", default=str(OUT_DIR), help="where the archives go (0700)")
    parser.add_argument("--max-mb", type=int, default=4096, help="the largest archive kept")
    parser.add_argument(
        "--reserve-mb", type=int, default=2048, help="the free space a fetch never goes below"
    )
    parser.add_argument(
        "--max-expanded-mb",
        type=int,
        default=MAX_EXPANDED >> 20,
        help="the most the re-pack of one archive decompresses, and writes",
    )
    parser.add_argument(
        "--checker-socket",
        default=CHECKER_SOCKET,
        help="the re-pack's socket (zab-archive-check.socket on the admin server)",
    )
    args = parser.parse_args(argv)
    if min(args.max_mb, args.max_expanded_mb, args.parallel) < 1 or args.reserve_mb < 0:
        parser.error("--max-mb, --max-expanded-mb and --parallel take at least 1")
    lock = contextlib.ExitStack()
    try:
        rows = select_rows(load_roster(args.roster), args.only)
        out_dir = pathlib.Path(args.out_dir)
        if not args.dry_run:
            out_dir = export_dir(out_dir)
            lock.enter_context(exports_lock(out_dir))
    except (ValueError, OSError) as exc:
        print(f"roster or --out-dir: {exc}", file=sys.stderr)
        return 1
    with lock:
        return _run(args, rows, out_dir, ssh or Bounded())


def _run(
    args: argparse.Namespace, rows: list[dict[str, str]], out_dir: pathlib.Path, ssh: Run
) -> int:
    script = EXPORT_SCRIPT.read_text(encoding="utf-8")
    results = run_parallel(
        rows,
        lambda row: collect(
            row,
            ssh,
            out_dir=out_dir,
            script=script,
            known_hosts=args.known_hosts,
            limit=args.max_mb << 20,
            reserve=args.reserve_mb << 20,
            dry_run=args.dry_run,
            expanded=args.max_expanded_mb << 20,
            checker=args.checker_socket,
        ),
        parallel=1 if args.dry_run else args.parallel,
        on_error=lambda row, exc: ExportResult(
            slug=slug_of(row), problems=[f"{type(exc).__name__}: {exc}"]
        ),
    )
    print(format_table(COLUMNS, [r.row() for r in results]))
    failed = [r for r in results if r.problems]
    skipped = sum(r.note.startswith("skipped") for r in results)
    print(
        f"\narchives: {out_dir}"
        + (f"; {len(failed)} VM(s) with problems" if failed else "; no problems")
        + (f"; {skipped} row(s) not provisioned (skipped)" if skipped else "")
    )
    if args.dry_run:
        print("(dry run: nothing exported or fetched)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
