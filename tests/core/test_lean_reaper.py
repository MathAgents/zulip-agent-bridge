"""The Lean reaper (SPEC §7.12, issue #249) against a fake `/proc`: a temporary directory per test.

The trees are the test VM's (VERIFY, DECISIONS 2026-10-25): `codex app-server` →
`…/lean-lsp-mcp/bin/python ~/.local/bin/lean-lsp-mcp` → `…/bin/lake serve -- …` →
`…/bin/lean --server …` → `…/bin/lean --worker … file:///…`.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from zab.config import BackendConfig, BridgeConfig, Config, LeanConfig, RenderConfig, load_config
from zab.core import lean_reaper as lr

pytestmark = pytest.mark.contract

UID = os.getuid()
TOOLCHAIN = "/home/agent/.elan/toolchains/leanprover--lean4---v4.34.1/bin"
MCP = (
    "/home/agent/.local/share/uv/tools/lean-lsp-mcp/bin/python",
    "/home/agent/.local/bin/lean-lsp-mcp",
)
LAKE_SERVE = (f"{TOOLCHAIN}/lake", "serve", "--", "-Dserver.reportDelayMs=0")
LEAN_SERVER = (f"{TOOLCHAIN}/lean", "--server", "-Dserver.reportDelayMs=0")
MIB = 1024**2


def worker(path: str) -> tuple[str, ...]:
    return (f"{TOOLCHAIN}/lean", "--worker", "-Dserver.reportDelayMs=0", f"file://{path}")


class FakeProc:
    """`/proc/<pid>/{stat,cmdline,status,cwd}` for the processes a test declares."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir()

    def add(
        self,
        pid: int,
        ppid: int,
        argv: tuple[str, ...],
        *,
        cpu: int = 0,
        start: int | None = None,
        uid: int = UID,
        euid: int | None = None,
        cwd: str | None = None,
        anon_mb: int = 0,
        state: str = "S",
        comm: str | None = None,
    ) -> None:
        d = self.root / str(pid)
        d.mkdir(exist_ok=True)
        comm = comm or os.path.basename(argv[0])[:15]
        start = pid * 100 if start is None else start
        # pid (comm) state ppid pgrp session tty tpgid flags minflt cminflt majflt cmajflt
        # utime stime cutime cstime priority nice threads itrealvalue starttime vsize rss
        fields = [
            state,
            ppid,
            pid,
            pid,
            0,
            -1,
            0,
            0,
            0,
            0,
            0,
            cpu,
            0,
            0,
            0,
            20,
            0,
            1,
            0,
            start,
            0,
            0,
        ]
        (d / "stat").write_text(f"{pid} ({comm}) " + " ".join(map(str, fields)) + "\n")
        (d / "cmdline").write_bytes(b"".join(a.encode() + b"\0" for a in argv))
        e = uid if euid is None else euid  # the effective, saved and file-system uids
        (d / "status").write_text(
            f"Name:\t{comm}\nUid:\t{uid}\t{e}\t{e}\t{e}\nRssAnon:\t{anon_mb * 1024} kB\n"
        )
        if cwd is not None:
            link = d / "cwd"
            if link.is_symlink():
                link.unlink()
            link.symlink_to(cwd)

    def cpu(self, pid: int, ticks: int) -> None:
        """Give `pid` this much CPU time, keeping the rest of its stat."""
        path = self.root / str(pid) / "stat"
        head, _, rest = path.read_text().rpartition(")")
        fields = rest.split()
        fields[11] = str(ticks)
        path.write_text(head + ") " + " ".join(fields) + "\n")

    def remove(self, pid: int) -> None:
        for f in (self.root / str(pid)).iterdir():
            f.unlink()
        (self.root / str(pid)).rmdir()


def lean_tree(fake: FakeProc, base: int, mcp_pid: int, cwd: str, *, workers: int = 1) -> list[int]:
    """`lake serve` (base) → `lean --server` (base+1) → workers (base+2…); the VERIFY's memory."""
    fake.add(base, mcp_pid, LAKE_SERVE, cwd=cwd, anon_mb=75)
    fake.add(base + 1, base, LEAN_SERVER, anon_mb=1160)
    for i in range(workers):
        fake.add(base + 2 + i, base + 1, worker(f"{cwd}/F{i}.lean"), anon_mb=46)
    return list(range(base, base + 2 + workers))


@pytest.fixture
def fake(tmp_path):
    fake = FakeProc(tmp_path / "proc")
    fake.add(1, 0, ("/sbin/init",))
    fake.add(900, 1, ("/usr/lib/systemd/systemd", "--user"))
    fake.add(941, 900, ("/home/agent/.local/bin/codex", "app-server", "--listen", "unix://"))
    fake.add(945, 941, ("/home/agent/.local/lib/codex-0.157.1/bin/codex-code-mode-host",))
    return fake


@pytest.fixture
def logged():
    """The reaper's log records, on its own logger (whatever propagation another test left)."""
    records: list[logging.LogRecord] = []

    class Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger, handler = logging.getLogger("zab.lean"), Handler()
    level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    yield records
    logger.removeHandler(handler)
    logger.setLevel(level)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def reaper(fake):
    sent: list[tuple[int, int]] = []
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    r = lr.LeanReaper(
        30,
        proc=str(fake.root),
        uid=UID,
        kill=lambda pid, sig: sent.append((pid, sig)),
        clock=Clock(),
        sleep=sleep,
    )
    r.sent, r.slept = sent, slept  # type: ignore[attr-defined]
    return r


async def passes(reaper: lr.LeanReaper, minutes: float, *, step: float = 1) -> list[lr.Tree]:
    """Passes `step` minutes apart over `minutes`: the first at once on a fresh reaper, else a
    step after the last one (never two passes at the same instant)."""
    stopped = []
    elapsed = 0.0
    if reaper.tracks:
        reaper.clock.now += step * 60  # type: ignore[attr-defined]
    while True:
        stopped += await reaper.sweep()
        if elapsed + step > minutes + 1e-9:
            return stopped
        reaper.clock.now += step * 60  # type: ignore[attr-defined]
        elapsed += step


def signalled(reaper: lr.LeanReaper, sig: int = signal.SIGTERM) -> set[int]:
    return {pid for pid, s in reaper.sent if s == sig}  # type: ignore[attr-defined]


async def test_only_the_idle_tree_is_signalled_and_the_log_names_it(fake, reaper, logged):
    """Two worktrees and `~/lean/workspace`: the busy tree keeps running, the idle ones are
    stopped whole (`lake serve`, `lean --server`, every worker), with one line each."""
    fake.add(1001, 941, MCP)
    fake.add(1002, 941, MCP)
    fake.add(1003, 941, MCP)
    idle = lean_tree(fake, 2000, 1001, "/home/agent/work/repo/.zab/worktrees/a", workers=2)
    busy = lean_tree(fake, 3000, 1002, "/home/agent/work/repo/.zab/worktrees/b")
    scratch = lean_tree(fake, 4000, 1003, "/home/agent/lean/workspace")
    await reaper.sweep()
    for minute in range(1, 30):
        reaper.clock.now += 60
        fake.cpu(3002, minute * 300)  # the busy tree's worker keeps elaborating
        for pid in (2001, 4001):
            fake.cpu(pid, 2 * minute)  # idle trees tick, 1 to 2 a minute (live check of #635)
        assert await reaper.sweep() == []
    assert reaper.sent == []
    reaper.clock.now += 60
    fake.cpu(3002, 99_999)
    for pid in (2001, 4001):
        fake.cpu(pid, 60)
    stopped = await reaper.sweep()
    assert {t.root.pid for t in stopped} == {2000, 4000}
    assert signalled(reaper) == set(idle) | set(scratch)
    assert not signalled(reaper) & {1001, 1002, 1003, 941, *busy}
    # SIGTERM in tree order (the root first), then the grace, then SIGKILL for what is left:
    # the fake processes never exit, so every one of them
    terms = [pid for pid, s in reaper.sent if s == signal.SIGTERM and pid in idle]
    assert terms == [2000, 2001, 2002, 2003]
    assert reaper.slept == [lr.GRACE_S]
    assert signalled(reaper, signal.SIGKILL) == set(idle) | set(scratch)
    lines = [r.getMessage() for r in logged if getattr(r, "event", "") == "lean_reaped"]
    assert len(lines) == 2
    line = next(x for x in lines if "worktrees/a" in x)
    assert "idle 30 min, 0.60 s of CPU" in line
    assert "lake serve 2000, lean --server 2001, lean --worker 2002 2003" in line
    assert line.endswith("freed 1.3 GB of anonymous memory; 4 needed SIGKILL")
    assert "RSS" not in line  # summed RSS counts the shared .olean files once per process
    assert "/home/agent/lean/workspace" in next(x for x in lines if x is not line)


async def test_a_tree_used_again_inside_the_limit_keeps_its_server(fake, reaper):
    fake.add(1001, 941, MCP)
    lean_tree(fake, 2000, 1001, "/home/agent/lean/workspace")
    assert await passes(reaper, 20) == []
    fake.cpu(2002, 2000)  # a Lean call before minute 21: the window starts again
    assert await passes(reaper, 29) == []  # minutes 21 to 50
    assert reaper.sent == []
    reaper.clock.now += 60
    assert [t.root.pid for t in await reaper.sweep()] == [2000]


@pytest.mark.parametrize(
    "workers,ticks,reaped",
    [(1, 2, True), (1, 11, True), (1, 12, False), (1, 200, False), (10, 12, True)],
)
async def test_an_idle_tree_may_tick_below_the_allowance(fake, reaper, workers, ticks, reaped):
    """Idle trees gain about 1 tick a minute per process (live checks of #635): below 4 a
    process (at least 10) at every pass a tree is idle, at it or above it is busy. A 3-process
    tree may gain 11 a minute, not 12; a 12-process one (ten open files) ticking 1 a process
    is reaped, where a flat 10 a minute would have kept it for ever."""
    assert (lr.IDLE_TICKS_PER_PROCESS, lr.IDLE_TICKS_MIN) == (4, 10)
    fake.add(1001, 941, MCP)
    lean_tree(fake, 2000, 1001, "/w", workers=workers)
    await reaper.sweep()
    stopped = []
    for minute in range(1, 61):
        reaper.clock.now += 60
        fake.cpu(2001, ticks * minute)
        stopped += await reaper.sweep()
        if stopped:
            break
    assert (minute if stopped else None) == (30 if reaped else None)


async def test_a_busy_pass_starts_the_window_again(fake, reaper):
    """200 ticks at one pass, inside the window: the tree is kept, and reaped 30 minutes after
    that pass, not before; the allowance is pro rata when a pass comes late."""
    fake.add(1001, 941, MCP)
    lean_tree(fake, 2000, 1001, "/w")
    assert await passes(reaper, 19) == []  # minutes 0 to 19
    fake.cpu(2002, 200)
    assert await passes(reaper, 29) == []  # minute 20 is busy; idle to minute 49
    reaper.clock.now += 120  # a late pass, at minute 51: 23 ticks in 2 minutes is still idle
    fake.cpu(2002, 223)
    assert [t.root.pid for t in await reaper.sweep()] == [2000]


async def test_a_pass_that_gained_nothing_is_never_busy(fake, reaper):
    """Two passes at the same instant (an allowance of 0) don't start the window again."""
    fake.add(1001, 941, MCP)
    lean_tree(fake, 2000, 1001, "/w")
    assert await passes(reaper, 29) == []
    assert await reaper.sweep() == []  # minute 29 again
    reaper.clock.now += 60
    assert [t.root.pid for t in await reaper.sweep()] == [2000]


async def test_cpu_gained_between_the_scan_and_the_stop_keeps_the_tree(fake, reaper):
    """The recheck before SIGTERM: one tick gained in the tens of milliseconds since the pass's
    scan means a Lean call may have started, and nothing is signalled."""
    fake.add(1001, 941, MCP)
    lean_tree(fake, 2000, 1001, "/w")
    await passes(reaper, 29)
    original = reaper.scan

    def scan_then_work():
        trees = original()
        reaper.scan = rescan
        return trees

    def rescan():
        fake.cpu(2002, 1)
        return original()

    reaper.scan = scan_then_work
    reaper.clock.now += 60
    assert await reaper.sweep() == []
    assert reaper.sent == []


async def test_a_new_worker_or_an_exited_one_is_activity(fake, reaper):
    fake.add(1001, 941, MCP)
    lean_tree(fake, 2000, 1001, "/w")
    await passes(reaper, 20)
    fake.add(2003, 2001, worker("/w/G.lean"))  # a file opened: no CPU change in the fake
    await passes(reaper, 20)
    fake.remove(2003)  # and closed again
    assert await passes(reaper, 29) == []
    assert reaper.sent == []


async def test_lean_lsp_mcp_builds_and_claude_are_never_signalled(fake, reaper):
    """`lean-lsp-mcp` itself, a `lake build` and a `lake env lean` under it, a server under
    Claude, one not under the Codex app-server, and another user's: none is touched, however
    long they stay idle. Each guard has a fixture of its own, which only it stops."""
    fake.add(1001, 941, MCP)
    build = (f"{TOOLCHAIN}/lake", "build")
    fake.add(1100, 1001, build, cwd="/home/agent/work/repo")
    fake.add(1101, 1100, (f"{TOOLCHAIN}/lean", "Repo/Basic.lean", "-o", "x.olean"))
    fake.add(1200, 1001, (f"{TOOLCHAIN}/lake", "env", "lean", "Scratch.lean"), cwd="/w")
    fake.add(1201, 1200, (f"{TOOLCHAIN}/lean", "Scratch.lean"))
    # Claude's own lean-lsp-mcp and server
    fake.add(5000, 900, ("/home/agent/.local/bin/claude", "-p", "--resume", "x"))
    fake.add(5001, 5000, MCP)
    lean_tree(fake, 5100, 5001, "/home/agent/work/repo/.zab/worktrees/a")
    # a Codex app-server running under Claude (an agent's own): still Claude's
    fake.add(5200, 5000, ("codex", "app-server"))
    fake.add(5201, 5200, MCP)
    lean_tree(fake, 5300, 5201, "/w")
    # a `codex exec` (not the app-server), and an orphan reparented to systemd
    fake.add(6000, 900, ("codex", "exec", "prove it"))
    fake.add(6001, 6000, MCP)
    lean_tree(fake, 6100, 6001, "/w")
    lean_tree(fake, 6200, 900, "/w")
    # another user's tree under this app-server
    fake.add(7001, 941, MCP, uid=UID + 1)
    fake.add(7100, 7001, LAKE_SERVE, uid=UID + 1, cwd="/w")
    fake.add(7101, 7100, LEAN_SERVER, uid=UID + 1)
    # a member whose real uid is ours and effective uid another's
    fake.add(7201, 941, MCP)
    lean_tree(fake, 7300, 7201, "/w")
    fake.add(7301, 7300, LEAN_SERVER, euid=UID + 1)
    # a childless `lake build` (lean_build's, waiting on a lock): not a `lake serve`
    fake.add(7401, 941, MCP)
    fake.add(7500, 7401, build, cwd="/w")
    # a tree whose worker runs something else: left alone whole
    fake.add(8001, 941, MCP)
    lean_tree(fake, 8100, 8001, "/w")
    fake.add(8200, 8102, ("/usr/bin/sage", "-c", "1"))
    # a plain `lean` (neither --server nor --worker) under a `lake serve`
    fake.add(8301, 941, MCP)
    fake.add(8400, 8301, LAKE_SERVE, cwd="/w")
    fake.add(8401, 8400, (f"{TOOLCHAIN}/lean", "Scratch.lean"))
    assert await passes(reaper, 120) == []
    assert reaper.sent == []


async def test_a_reused_pid_is_never_signalled(fake, reaper):
    """A member whose start time changed after the scan is another process: no signal to it,
    nor to the rest of its tree, which changed too."""
    fake.add(1001, 941, MCP)
    lean_tree(fake, 2000, 1001, "/w")
    await passes(reaper, 29)
    original = reaper.scan

    def scan_then_reuse():
        trees = original()
        reaper.scan = rescan  # the recheck before SIGTERM sees the new process
        return trees

    def rescan():
        fake.add(2001, 2000, LEAN_SERVER, start=999_999)
        return original()

    reaper.scan = scan_then_reuse
    reaper.clock.now += 60
    assert await reaper.sweep() == []
    assert reaper.sent == []


async def test_the_recheck_before_sigkill_spares_a_reused_pid(fake, reaper):
    fake.add(1001, 941, MCP)
    lean_tree(fake, 2000, 1001, "/w")
    await passes(reaper, 29)

    async def sleep(seconds):
        # during the grace, the worker exits and its PID goes to an unrelated process
        fake.add(2002, 900, ("/usr/bin/python3", "job.py"), start=888_888)

    reaper.sleep = sleep
    reaper.clock.now += 60
    assert [t.root.pid for t in await reaper.sweep()] == [2000]
    assert signalled(reaper) == {2000, 2001, 2002}
    assert signalled(reaper, signal.SIGKILL) == {2000, 2001}


async def test_a_pid_reused_between_the_recheck_and_its_turn_is_skipped(fake, reaper):
    """Each process is rechecked just before its own signal: the worker, replaced while its
    `lake serve` was being signalled, gets none."""
    fake.add(1001, 941, MCP)
    lean_tree(fake, 2000, 1001, "/w")
    await passes(reaper, 29)
    sent = reaper.sent

    def kill(pid, sig):
        sent.append((pid, sig))
        if pid == 2000:
            fake.add(2002, 900, ("/usr/bin/python3", "job.py"), start=777_777)

    reaper.kill = kill
    reaper.clock.now += 60
    assert [t.root.pid for t in await reaper.sweep()] == [2000]
    assert signalled(reaper) == {2000, 2001}
    assert 2002 not in signalled(reaper, signal.SIGKILL)


@pytest.mark.parametrize("path", ["python", "syscall"])
def test_signal_if_same_goes_through_a_pidfd_to_the_checked_process(path, monkeypatch):
    """The production path on the real `/proc`, through Python's pidfd functions and through
    the system calls (uv's standalone Pythons lack the functions): a live child is signalled;
    with another start time it is not, nor without a pidfd; and a reaped one is gone."""
    if not Path("/proc/self/stat").exists():
        pytest.skip("Linux only")
    if path == "python":
        if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
            pytest.skip("this Python was built without the pidfd functions")
    else:
        monkeypatch.delattr(os, "pidfd_open", raising=False)
        monkeypatch.delattr(signal, "pidfd_send_signal", raising=False)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        for _ in range(500):  # argv reads empty for a moment while exec completes
            me = lr.read_proc("/proc", child.pid)
            if me is not None and me.argv[-1:] == ("import time; time.sleep(60)",):
                break
            time.sleep(0.01)
        # (until exec, a forked child shows its parent's argv, an xdist worker's say)
        assert me is not None and me.argv[1:2] == ("-c",)
        other = lr.Proc(me.pid, me.ppid, me.start + 1, me.state, me.comm, me.argv, me.cpu)
        assert lr.signal_if_same("/proc", other, signal.SIGTERM, None) == "changed"
        assert child.poll() is None
        # without a pidfd nothing is sent: a plain kill could reach the PID's next owner
        with pytest.MonkeyPatch.context() as mp:

            def no_pidfd(pid):
                raise OSError(errno.EMFILE, "Too many open files")

            mp.setattr(lr, "pidfd_open", no_pidfd)
            assert lr.signal_if_same("/proc", me, signal.SIGTERM, None) == "unpinned: OSError"
            reaper = lr.LeanReaper(1, uid=UID)
            assert reaper.signal_all([me, me], signal.SIGTERM) == []
            assert reaper.unpinned == "unpinned: OSError"
        assert child.poll() is None
        assert lr.signal_if_same("/proc", me, signal.SIGTERM, None) == "sent"
        assert child.wait(10) == -signal.SIGTERM
        assert lr.signal_if_same("/proc", me, signal.SIGKILL, None) == "gone"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def test_reap_idle_minutes_zero_runs_no_reaper(tmp_path):
    def config(minutes: int, kind: str = "codex") -> Config:
        return Config(
            tmp_path / "config.toml",
            BridgeConfig(state_dir=str(tmp_path)),
            [],
            {"codex": BackendConfig(kind=kind)},
            [],
            RenderConfig(),
            LeanConfig(reap_idle_minutes=minutes),
        )

    assert lr.for_config(config(0)) is None
    assert lr.for_config(config(30, "claude")) is None  # no Codex, nothing to reap
    if os.path.isdir("/proc"):
        assert lr.for_config(config(30)).limit == 30 * 60


async def test_the_run_loop_survives_a_failing_pass(fake, reaper, logged):
    calls = []

    async def sweep():
        calls.append(1)
        if len(calls) < 3:
            raise OSError("boom")
        raise asyncio.CancelledError

    reaper.sweep = sweep
    with pytest.raises(asyncio.CancelledError):
        await reaper.run()
    assert len(calls) == 3
    assert reaper.slept == [lr.PASS_S] * 3
    warnings = [r.getMessage() for r in logged if r.levelno == logging.WARNING]
    assert warnings == ["Lean reaper pass failed (OSError)"]  # once, not once per pass


def test_the_setting_loads_validates_and_defaults_to_30(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text("")
    assert load_config(path).lean.reap_idle_minutes == 30
    path.write_text("[lean]\nreap_idle_minutes = 0\n")
    assert load_config(path).lean.reap_idle_minutes == 0
    from zab.config import ConfigError

    for bad in ("-1", "true", '"30"'):
        path.write_text(f"[lean]\nreap_idle_minutes = {bad}\n")
        with pytest.raises(ConfigError, match=r"lean\.reap_idle_minutes"):
            load_config(path)
    path.write_text("[lean]\nreap_idle = 3\n")
    with pytest.raises(ConfigError, match=r"lean\.reap_idle"):
        load_config(path)
    example = load_config(Path(__file__).parents[2] / "config/zab.example.toml")
    assert example.lean.reap_idle_minutes == 30
