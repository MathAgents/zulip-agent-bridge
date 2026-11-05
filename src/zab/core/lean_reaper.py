"""Stop the idle Lean language servers that Codex threads hold (SPEC §7.12, issue #249).

A Lean tool call through `lean-lsp-mcp` starts a server: one `lake serve`, its `lean --server`,
and one `lean --worker` per open file, about 1.3 GB of anonymous memory with Mathlib (VERIFY,
DECISIONS 2026-10-25). Under Claude the server exits with the turn. Under the Codex app-server,
each loaded thread keeps its own `lean-lsp-mcp`, which keeps its servers until the daemon
restarts. The reaper stops a server that has been idle for `[lean] reap_idle_minutes`, in any
working directory: the same processes throughout, gaining less than a small allowance of CPU
time at every pass (`IDLE_TICKS_PER_PROCESS`). That thread's next Lean call starts a fresh one
(the same VERIFY), so `lean-lsp-mcp` itself is never touched.

Linux only, standard library only (SPEC §3.3): `/proc/<pid>/{stat,cmdline,status,cwd}`, and a
pidfd for each signal, so that a signal reaches the process that was checked and never the next
owner of its PID. The bridge runs it every `PASS_S` seconds, as its own user (`Runtime`).
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import platform
import signal
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from zab.config import Config

#: Seconds between two passes: the idle time is measured to within one pass.
PASS_S = 60
#: The CPU time an idle tree may gain, in clock ticks (`/proc`'s USER_HZ, 1/100 s) a minute:
#: `IDLE_TICKS_PER_PROCESS` for each of its processes, and at least `IDLE_TICKS_MIN`. A pass
#: whose tree gained more than nothing and at least that much, pro rata, is a busy one, and
#: starts the idle window again. Idle trees are not still: on the test VM (live checks of #635,
#: Oct 25) idle trees gained about 1 tick a minute per process, a 3-process tree about 1 and a
#: 5-process tree about 3.5, and so were never reaped while the rule was "no tick at all". A
#: first `lean_diagnostic_messages` on a file cost about 2,000 ticks; one more `example` in an
#: open file, re-checked, about 2. So 4 a process (12 a minute for one open file, 48 for ten)
#: is about 4 times the idle rate and far below an elaboration. The lightest calls look idle;
#: stopping their server makes the next call start a fresh one, which re-elaborates the open
#: file (10 to 20 s for a small file).
IDLE_TICKS_PER_PROCESS = 4
IDLE_TICKS_MIN = 10
#: Seconds between a tree's SIGTERM and the SIGKILL of what is left of it.
GRACE_S = 5

LOG = logging.getLogger("zab.lean")


@dataclass(frozen=True)
class Proc:
    """One process, as `/proc/<pid>/stat` and `cmdline` show it."""

    pid: int
    ppid: int
    start: int  # clock ticks after boot: with the PID, the process's identity
    state: str  # "S", "R", "Z", ...
    comm: str
    argv: tuple[str, ...]
    cpu: int  # utime + stime + cutime + cstime, in clock ticks

    @property
    def name(self) -> str:
        """The basename of argv[0]: the toolchain's `lake` and `lean` run by absolute path."""
        return os.path.basename(self.argv[0]) if self.argv else ""


@dataclass(frozen=True)
class Tree:
    """A `lake serve` under a Codex thread's `lean-lsp-mcp`, and every process under it."""

    members: tuple[Proc, ...]  # the `lake serve` first, then breadth first
    cwd: str  # the `lake serve`'s working directory
    anon: dict[int, int]  # per PID: anonymous memory (RssAnon), in bytes

    @property
    def root(self) -> Proc:
        return self.members[0]

    def freed(self, members: Iterable[Proc]) -> int:
        """The anonymous memory of these members: what stopping them frees. Their RSS is mostly
        the mapped `.olean` files, shared between processes and reclaimable, so a sum of RSS
        would count Mathlib once per process (22.6 GB on a 32 GB VM)."""
        return sum(self.anon.get(p.pid, 0) for p in members)

    @property
    def key(self) -> tuple[int, int]:
        return self.root.pid, self.root.start

    @property
    def processes(self) -> frozenset[tuple[int, int]]:
        """Its processes as (PID, start time): a process joining or leaving is activity."""
        return frozenset((p.pid, p.start) for p in self.members)

    @property
    def cpu(self) -> int:
        """The CPU time of its processes and of their children that exited, in clock ticks."""
        return sum(p.cpu for p in self.members)


def allowance(seconds: float, processes: int) -> float:
    """The clock ticks an idle tree of `processes` processes may gain in `seconds`."""
    return max(IDLE_TICKS_MIN, IDLE_TICKS_PER_PROCESS * processes) * seconds / 60


def read_proc(proc: str, pid: int) -> Proc | None:
    """The process `pid` from `stat` and `cmdline`; None when it is gone or unreadable."""
    root = Path(proc, str(pid))
    try:
        stat = root.joinpath("stat").read_bytes()
        cmdline = root.joinpath("cmdline").read_bytes()
    except OSError:
        return None
    # the command between the parentheses may hold spaces, parentheses and any byte at all
    head, _, rest = stat.rpartition(b")")
    fields = rest.split()
    try:
        state = fields[0].decode()
        ppid = int(fields[1])
        cpu = sum(int(x) for x in fields[11:15])
        start = int(fields[19])
    except (IndexError, ValueError, UnicodeDecodeError):
        return None
    comm = head.partition(b"(")[2].decode(errors="replace")
    args = cmdline.split(b"\0")
    if args and args[-1] == b"":
        args.pop()
    argv = tuple(a.decode(errors="replace") for a in args)
    return Proc(pid, ppid, start, state, comm, argv, cpu)


def read_status(proc: str, pid: int) -> tuple[tuple[int, ...], int] | None:
    """(real, effective, saved and file-system uids; RssAnon in bytes), from
    `/proc/<pid>/status`; None when it is gone or unreadable."""
    uids: tuple[int, ...] = ()
    anon = 0
    try:
        for line in Path(proc, str(pid), "status").read_bytes().splitlines():
            if line.startswith(b"Uid:"):
                uids = tuple(int(x) for x in line.split()[1:])
            elif line.startswith(b"RssAnon:"):
                anon = int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return (uids, anon) if uids else None


def is_lake_serve(p: Proc) -> bool:
    return p.name == "lake" and p.argv[1:2] == ("serve",)


def is_lean_server(p: Proc) -> bool:
    """`lean --server` or `lean --worker`: a build's `lean` (`lake build`, `lake env lean`) is
    neither."""
    return p.name == "lean" and bool({"--server", "--worker"} & set(p.argv[1:]))


def is_lean_lsp_mcp(p: Proc) -> bool:
    """The uv tool runs as `…/lean-lsp-mcp/bin/python /home/agent/.local/bin/lean-lsp-mcp`."""
    return (
        p.comm == "lean-lsp-mcp"
        or any(os.path.basename(a) == "lean-lsp-mcp" for a in p.argv[:2])
        or p.argv[1:3] == ("-m", "lean_lsp_mcp")
    )


def is_codex_app_server(p: Proc) -> bool:
    return p.name == "codex" and "app-server" in p.argv[1:]


def is_claude(p: Proc) -> bool:
    """Claude Code, native (`claude`) or under node: its servers exit with its turn."""
    if p.name == "claude" or p.comm == "claude":
        return True
    return p.name in ("node", "nodejs") and len(p.argv) > 1 and "claude" in p.argv[1]


def size(n: int) -> str:
    """Binary GB from 1 GB, else MB (the repository's GB: 1024³ bytes)."""
    return f"{n / 1024**3:.1f} GB" if n >= 1024**3 else f"{round(n / 1024**2)} MB"


def printable(text: str) -> str:
    return "".join(c if c.isprintable() else "?" for c in text)


def describe(members: Iterable[Proc]) -> str:
    """`lake serve 4142, lean --server 4191, lean --worker 4199 4200`."""
    groups: dict[str, list[str]] = {}
    for p in members:
        if is_lake_serve(p):
            label = "lake serve"
        elif is_lean_server(p):
            label = "lean --server" if "--server" in p.argv[1:] else "lean --worker"
        else:
            label = printable(p.name or p.comm)
        groups.setdefault(label, []).append(str(p.pid))
    return ", ".join(f"{label} {' '.join(pids)}" for label, pids in groups.items())


#: The pidfd system calls, the same on x86_64 and aarch64 (the unified table, Linux 5.1 and 5.3).
SYS_PIDFD_SEND_SIGNAL, SYS_PIDFD_OPEN = 424, 434


def _syscall(number: int, *args: int | None) -> int:
    """A system call through libc's `syscall`, for the Pythons built without the pidfd
    functions: uv's standalone builds of 3.12 have neither `os.pidfd_open` nor
    `signal.pidfd_send_signal`, Ubuntu's both. An error raises its `OSError` subclass."""
    if platform.machine() not in ("x86_64", "aarch64"):
        raise OSError(38, "pidfd system calls not known on this machine")  # ENOSYS
    libc = ctypes.CDLL(None, use_errno=True)
    values = [ctypes.c_void_p(None) if a is None else ctypes.c_long(a) for a in args]
    result = libc.syscall(ctypes.c_long(number), *values)
    if result == -1:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return result


def pidfd_open(pid: int) -> int:
    if hasattr(os, "pidfd_open"):
        return os.pidfd_open(pid)
    return _syscall(SYS_PIDFD_OPEN, pid, 0)


def pidfd_send_signal(fd: int, sig: int) -> None:
    if hasattr(signal, "pidfd_send_signal"):
        signal.pidfd_send_signal(fd, sig)
    else:
        _syscall(SYS_PIDFD_SEND_SIGNAL, fd, sig, None, 0)


def signal_if_same(
    proc: str, member: Proc, sig: int, kill: Callable[[int, int], None] | None
) -> str:
    """Send `sig` to `member` only while its PID still names it: the same start time and argv,
    read after the PID is pinned by a pidfd, which the signal then goes through. Without a pidfd
    (an old kernel, no file descriptor left) nothing is sent: a plain `kill` after the check
    could reach the PID's next owner (Codex round 1 on #635). `kill` replaces the pidfd (tests
    on a fake `/proc`). Returns "sent", "gone", "changed", or "unpinned: <error>"."""
    fd = None
    if kill is None:
        try:
            fd = pidfd_open(member.pid)
        except ProcessLookupError:
            return "gone"
        except OSError as exc:
            return f"unpinned: {type(exc).__name__}"
    try:
        now = read_proc(proc, member.pid)
        if now is None or now.state in ("Z", "X"):
            return "gone"
        if (now.start, now.argv) != (member.start, member.argv):
            return "changed"
        if kill is not None:
            kill(member.pid, sig)
        else:
            pidfd_send_signal(fd, sig)
        return "sent"
    except ProcessLookupError:
        return "gone"
    finally:
        if fd is not None:
            os.close(fd)


@dataclass
class Track:
    processes: frozenset[tuple[int, int]]
    cpu: int  # the tree's CPU time at the last pass
    seen: float  # `clock()` at the last pass
    since: float  # `clock()` at the start of the idle window: the last busy pass
    base: int  # the tree's CPU time then


class LeanReaper:
    """Every `PASS_S`: find the Codex threads' Lean servers, and stop each one whose processes
    have not changed, and which has had no busy pass (`IDLE_TICKS_PER_PROCESS`), for
    `idle_minutes`."""

    def __init__(
        self,
        idle_minutes: int,
        *,
        proc: str = "/proc",
        uid: int | None = None,
        kill: Callable[[int, int], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.limit = idle_minutes * 60
        self.proc = proc
        self.uid = os.getuid() if uid is None else uid
        self.kill = kill
        self.clock = clock
        self.sleep = sleep
        self.tracks: dict[tuple[int, int], Track] = {}
        self.refused: set[tuple[int, int]] = set()  # trees left alone, logged once each
        self.failure: str | None = None
        self.unpinned: str | None = None  # the last pidfd failure logged

    # ------------------------------------------------------------------------------ the scan

    def scan(self) -> list[Tree]:
        """Every Lean server tree a Codex thread holds, owned by this user, and nothing else."""
        procs = {}
        # in PID order, so that trees and their processes come in an order a test can name
        for pid in sorted(int(e.name) for e in os.scandir(self.proc) if e.name.isdigit()):
            if p := read_proc(self.proc, pid):
                procs[p.pid] = p
        children: dict[int, list[Proc]] = {}
        for p in procs.values():
            children.setdefault(p.ppid, []).append(p)
        trees, seen = [], set()
        for p in procs.values():
            if not is_lake_serve(p) or not self.held_by_codex(p, procs):
                continue
            seen.add((p.pid, p.start))
            tree = self.tree(p, children)
            if tree is not None:
                trees.append(tree)
        self.refused &= seen
        return trees

    def held_by_codex(self, p: Proc, procs: dict[int, Proc]) -> bool:
        """Whether `p`'s parents reach a `codex app-server` through a `lean-lsp-mcp`, without
        another `lake serve` (`p` is then inside that one's tree) or Claude on the way."""
        chain, visited, pid = [], {p.pid}, p.ppid
        while pid in procs and pid not in visited:
            visited.add(pid)
            chain.append(procs[pid])
            pid = procs[pid].ppid
        mcp = next((i for i, a in enumerate(chain) if is_lean_lsp_mcp(a)), None)
        return (
            mcp is not None
            and not any(is_lake_serve(a) for a in chain[:mcp])
            and any(is_codex_app_server(a) for a in chain[mcp + 1 :])
            and not any(is_claude(a) for a in chain)
        )

    def tree(self, root: Proc, children: dict[int, list[Proc]]) -> Tree | None:
        """The `lake serve` and the processes under it, breadth first, with their memory; None
        (logged once) when one of them is anything but `lean --server` or `lean --worker`, or
        another user's. A zombie, which holds no memory, is left out."""
        members, queue, visited = [], [root], {root.pid}
        while queue:
            p = queue.pop(0)
            members.append(p)
            for child in children.get(p.pid, ()):
                if child.pid not in visited and child.state not in ("Z", "X"):
                    visited.add(child.pid)
                    queue.append(child)
        anon = {}
        why = None
        for p in members:
            if not p.argv:
                return None  # being started: argv reads empty until its exec completes
            if p is not root and not is_lean_server(p):
                why = f"`{printable(p.name or p.comm)}` ({p.pid}) runs in it"
                break
            status = read_status(self.proc, p.pid)
            if status is None:
                return None  # gone meanwhile: the next pass sees what is left
            uids, p_anon = status
            if any(u != self.uid for u in uids[:2]):
                why = f"process {p.pid} is another user's"
                break
            anon[p.pid] = p_anon
        try:
            cwd = os.readlink(Path(self.proc, str(root.pid), "cwd"))
        except OSError:
            cwd = "?"
        if why is not None:
            if (root.pid, root.start) not in self.refused:
                self.refused.add((root.pid, root.start))
                LOG.info(
                    "Left a Lean server alone (lake serve %d in %s): %s",
                    root.pid,
                    printable(cwd),
                    why,
                    extra={"event": "lean_reap_skipped"},
                )
            return None
        return Tree(tuple(members), cwd, anon)

    # ------------------------------------------------------------------------------ the pass

    async def run(self) -> None:
        LOG.info(
            "Lean reaper on: Codex's Lean servers idle for %d minutes are stopped",
            self.limit // 60,
            extra={"event": "lean_reaper"},
        )
        while True:
            await self.sleep(PASS_S)
            try:
                await self.sweep()
                self.failure = None
            except Exception as exc:  # a pass that fails is retried at the next one
                if type(exc).__name__ != self.failure:
                    self.failure = type(exc).__name__
                    LOG.warning("Lean reaper pass failed (%s)", self.failure)

    async def sweep(self) -> list[Tree]:
        """One pass: update each tree's idle time, then stop the trees idle for the limit.
        Returns the trees stopped."""
        trees = await asyncio.to_thread(self.scan)
        now = self.clock()
        due = []
        for tree in trees:
            track = self.tracks.get(tree.key)
            if track is None or track.processes != tree.processes:
                self.tracks[tree.key] = Track(tree.processes, tree.cpu, now, now, tree.cpu)
                continue
            gained = tree.cpu - track.cpu
            if gained > 0 and gained >= allowance(now - track.seen, len(tree.members)):
                track.since, track.base = now, tree.cpu  # a busy pass: the window starts again
            track.cpu, track.seen = tree.cpu, now
            if now - track.since >= self.limit:
                due.append((tree, track))
        current = {tree.key for tree in trees}
        self.tracks = {k: v for k, v in self.tracks.items() if k in current}
        if not due:
            return []
        termed = await asyncio.to_thread(self.terminate, [tree for tree, _ in due])
        if any(termed):
            await self.sleep(GRACE_S)
        stopped = []
        for (tree, track), sent in zip(due, termed, strict=True):
            self.tracks.pop(tree.key, None)
            if not sent:
                continue  # changed since the scan: busy again, or gone
            killed = await asyncio.to_thread(self.signal_all, sent, signal.SIGKILL)
            stopped.append(tree)
            LOG.info(
                "Stopped an idle Lean server (idle %d min, %.2f s of CPU) in %s: %s; freed %s "
                "of anonymous memory%s",
                (track.seen - track.since) // 60,
                (track.cpu - track.base) / 100,
                printable(tree.cwd),
                describe(sent),
                size(tree.freed(sent)),
                f"; {len(killed)} needed SIGKILL" if killed else "",
                extra={"event": "lean_reaped"},
            )
        return stopped

    def terminate(self, trees: list[Tree]) -> list[list[Proc]]:
        """SIGTERM every process of each tree that is still as the scan found it: the same
        processes, and no CPU time gained since (tens of milliseconds: an idle tick lands in
        them about once in a thousand stops, which then wait a window). Returns, per tree, the
        processes signalled ([] for none)."""
        fresh = {tree.key: tree for tree in self.scan()}
        sent = []
        for tree in trees:
            now = fresh.get(tree.key)
            if now is None or now.processes != tree.processes or now.cpu > tree.cpu:
                sent.append([])
                continue
            sent.append(self.signal_all(tree.members, signal.SIGTERM))
        return sent

    def signal_all(self, members: Iterable[Proc], sig: int) -> list[Proc]:
        """`sig` to each member that is still itself (PID and start time); returns those."""
        done = []
        for p in members:
            try:
                result = signal_if_same(self.proc, p, sig, self.kill)
            except PermissionError:
                LOG.warning("Cannot signal Lean process %d", p.pid)
                continue
            if result == "sent":
                done.append(p)
            elif result.startswith("unpinned") and result != self.unpinned:
                self.unpinned = result  # once per kind of failure, not once per process
                LOG.warning("Lean process %d not signalled: no pidfd (%s)", p.pid, result[10:])
        return done


def for_config(config: Config) -> LeanReaper | None:
    """The reaper the bridge runs: none with `reap_idle_minutes = 0`, without a Codex backend,
    or without `/proc`."""
    minutes = config.lean.reap_idle_minutes
    if minutes <= 0 or not any(b.kind == "codex" for b in config.backends.values()):
        return None
    if not os.path.isdir("/proc"):
        return None
    return LeanReaper(minutes)
