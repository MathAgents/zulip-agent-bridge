"""`zab job`: a background job, which outlives the agent's turn (SPEC §7.14, #655).

    zab job start [--no-report] --name NAME -- CMD ARG…
    zab job list [--all]
    zab job status NAME
    zab job log NAME [-n N]
    zab job stop NAME

For agents: a computation that takes longer than a few minutes, or one your user needn't wait
for, runs as a job; then end your turn. zab posts in the topic when it ends and starts a turn
for you to report on it. The command after `--` is an argument list, run without a shell, with
stdin `/dev/null`, in the current directory, with your environment less the variables named
like credentials.

`status`, `log` and `stop` take this session's newest job of that name (`ZAB_SESSION`; the
jobs started without it share one). Exit codes: 0 done; 1 the job ended at once with an error,
or no such job; 2 refused, in one line: inside a job, a limit, a full disk, a name, no systemd
user manager.

`zab job _exec DIR` and `zab job _ended DIR` are the job unit's own: its main process and its
`ExecStopPost` (`zab.jobs`). `zab job start` writes the job's directory and starts the unit,
whose command lines carry that directory, absolute: a service of the user manager has neither
the caller's environment nor its `ZAB_CONFIG`, so neither looks for it.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import resource
import secrets
import select
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

from zab.config import BOT_NAME_RE, ConfigError, load_config
from zab.jobs import (
    ID_RE,
    LOG_CAP,
    NAME_RE,
    RESERVED_NAMES,
    SAFE_PATH_RE,
    SLICE,
    Job,
    all_jobs,
    boot_id,
    ending,
    jobs_dir,
    locked,
    present_units,
    read_json,
    scrubbed,
    session_of,
    span,
    stop,
    thread_of,
    unit,
    unit_argv,
    user_env,
    write_json,
)
from zab.tools.common import Runner, first_line, subprocess_runner

#: How long `start` waits, so that a command that fails at once is reported there (§7.14)
START_WAIT_S = 3.0
#: `start` refuses when the disk of the jobs or of the working directory is this full
DISK_FULL = 0.90
TAIL_LINES, MAX_LINES = 20, 10_000
#: How long `stop` waits for `_ended` after the unit stopped
STOP_WAIT_S = 5.0
LOG_FULL = b"\n[zab: the log reached 1 GiB; the rest of the job's output was dropped]\n"
#: The signals `_exec` itself takes no action on: a stop's or the cap's TERM, sent to every
#: process of the unit, reaches the job directly, exactly once (review round 2 on #928)
IGNORED = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
#: How often `_exec` looks whether the job exited, while its output is quiet
POLL_S = 0.2
#: The most `_exec` reads at a time before it looks at the job again; once the job exited, the
#: most it still reads, for at most `AFTER_EXIT_S`: what the job left in the pipe (64 KiB to
#: 1 MiB), not a descendant's endless stream
DRAIN_BYTES, AFTER_EXIT_S = 1 << 20, 0.5
#: What `log` prints at most, and `status`, whose output an agent's tool cuts at about 30,000
#: characters, so that its end stays readable (review round 1 on #932)
LOG_BYTES, STATUS_BYTES = 256 * 1024, 16 * 1024
#: C0 controls but newline and tab, DEL and C1 controls
CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
INSIDE_A_JOB = "zab job start can't run inside a background job: start the next one from a turn."


class Refused(Exception):
    """`zab job` won't: one line for the agent, exit 2."""


@dataclass
class Settings:
    """`state_dir` and `[jobs]` (SPEC §5.2): the defaults stand until the config has the table."""

    jobs: Path  # `<state_dir>/jobs`
    max_running: int = 4
    max_hours: float = 12.0
    max_memory_percent: int = 75
    report: bool = True


def settings(config_path: str | None = None) -> Settings:
    config = load_config(config_path)
    found = Settings(jobs_dir(config.bridge.state_dir))
    table = getattr(config, "jobs", None)
    for key in ("max_running", "max_hours", "max_memory_percent", "report"):
        if table is not None and hasattr(table, key):
            setattr(found, key, getattr(table, key))
    return found


def own_cgroup() -> str:
    """This process's cgroup (v2), from `/proc/self/cgroup`."""
    try:
        lines = Path("/proc/self/cgroup").read_text().splitlines()
    except OSError:
        return ""
    return next((line[3:] for line in lines if line.startswith("0::")), "")


@dataclass
class Context:
    settings: Settings
    zab: str
    env: Mapping[str, str]
    cwd: str
    runner: Runner = subprocess_runner
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    boot: str = field(default_factory=boot_id)
    cgroup: str = field(default_factory=lambda: own_cgroup())

    @property
    def session(self) -> str:
        return session_of(self.env)

    @property
    def bot(self) -> str:
        """`ZAB_BOT` when it is a bot's name as the config takes it, else "": a job of no bot,
        unattached. Recorded as it came, a name `Job.typed()` refuses would make the record
        bad, and the job would never run (#932's re-check, N2)."""
        value = self.env.get("ZAB_BOT", "")
        return value if BOT_NAME_RE.fullmatch(value) else ""


def zab_path() -> str:
    """zab's own executable, by absolute path, unresolved: `~/.local/bin/zab` stays, so that a
    job's `_ended` comes from the install of its end (§7.14)."""
    here = Path(sys.argv[0])
    if here.name == "zab" and here.is_absolute():
        return str(here)
    if found := shutil.which("zab"):
        return str(Path(found).absolute())
    raise Refused("can't find zab's own executable on PATH")


def fullness(path: Path) -> float:
    usage = shutil.disk_usage(path)
    return usage.used / max(1, usage.used + usage.free)  # as `df` counts it


def hours_words(hours: float) -> str:
    if float(hours).is_integer():
        return f"{int(hours)} hour{'' if hours == 1 else 's'}"
    return f"{max(1, round(hours * 60))} minutes"


def when(t: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(t))


def tail(path: Path, lines: int, *, limit: int = LOG_BYTES) -> str:
    """The last `lines` lines of `path`, at most `limit` bytes of them, read from its end, with
    no control character but newlines and tabs: a job's output can hold terminal escapes, which
    a person's terminal would run (review round 1 on #932). A carriage return ends a line, as a
    progress bar's does: CRLF and CR are newlines first, so that `-n` counts them (review of
    #932). At most `limit` bytes are read, also from a log the job still appends to (Codex
    round 2 on #932)."""
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - limit))
            data = f.read(limit)
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(CONTROLS.sub("\ufffd", text).splitlines()[-lines:])


def attachment(state_dir: Path, bot: str, session: str) -> tuple[str | None, str]:
    """The session a job started now belongs to (§7.14, "Which topic it belongs to"), decided
    once: `ZAB_SESSION` names a session of `ZAB_BOT`'s bot, not detached, with a turn running.
    Read from the bridge's store read-only, as `zab status` reads it. Returns the session's id,
    or None and why not."""
    if not session:
        return None, "no zab session started it"
    path = state_dir / "zab.db"
    if not bot or not path.exists():
        return None, "no bot of zab's started it"
    try:
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            row = db.execute(
                "SELECT state FROM sessions WHERE id=? AND bot=?", (session, bot)
            ).fetchone()
            running = db.execute(
                "SELECT 1 FROM turns WHERE session_id=? AND state='running'", (session,)
            ).fetchone()
        finally:
            db.close()
    except sqlite3.Error as exc:
        return None, f"zab's store can't be read: {type(exc).__name__}"
    if row is None:
        return None, "its session isn't one of this bot's"
    if row[0] == "detached":
        return None, "its session is detached"
    if running is None:
        return None, "no turn of its session is running"
    return session, ""


def refuse_without_manager(ctx: Context) -> set[str]:
    present = present_units(ctx.runner)
    if present is None:
        raise Refused(
            "no systemd user manager answers `systemctl --user` here: background jobs need one "
            "(Linux, with lingering on)"
        )
    return present


#: For a person, workshop or not: `zab service install` can restart `zab.service`, which ends
#: every running turn, so it isn't the agent's to run (review round 1 on #932, and its review)
SLICE_MISSING = (
    f"background jobs are off here: {SLICE} isn't installed with its memory limits "
    "(ask whoever runs this machine to run `zab service install`)"
)


def refuse_without_slice(ctx: Context) -> None:
    """Fail closed without the slice's limits (§7.14, §13.3; Codex round 1 on #932): without its
    file, systemd makes `zab-jobs.slice` on the fly, with no memory limit, and every job would
    run unbounded. So the slice must be loaded from a file, with a finite `MemoryMax` and
    `MemorySwapMax=0` (#655 V2: with swap, `MemoryMax` alone kills nothing)."""
    props = ("LoadState", "FragmentPath", "MemoryMax", "MemorySwapMax")
    argv = ["systemctl", "--user", "show", SLICE, *(f"--property={p}" for p in props)]
    done = ctx.runner(argv, env=user_env(ctx.env), timeout_s=30)
    shown = dict(line.split("=", 1) for line in done.stdout.splitlines() if "=" in line)
    if not (
        done.ok
        and shown.get("LoadState") == "loaded"
        and shown.get("FragmentPath")
        and shown.get("MemoryMax", "infinity").isdigit()
        and shown.get("MemorySwapMax") == "0"
    ):
        raise Refused(SLICE_MISSING)


def start(ctx: Context, name: str, command: list[str], *, report: bool) -> int:
    s = ctx.settings
    if SLICE in ctx.cgroup.split("/"):
        raise Refused(INSIDE_A_JOB)  # a job must not keep the VM up by starting its successor
    if not NAME_RE.fullmatch(name) or name in RESERVED_NAMES:
        raise Refused(
            f"invalid name {name!r}: up to 32 lower-case letters, digits and dashes, starting "
            "with a letter or digit, and not `jobs`"
        )
    if not command:
        raise Refused("nothing to run: `zab job start --name NAME -- CMD ARG…`")
    if s.max_running <= 0:
        raise Refused("background jobs are off on this machine (`[jobs] max_running = 0`)")
    try:
        s.jobs.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise Refused(f"can't write {s.jobs}: {exc.strerror}") from None
    if not os.access(s.jobs, os.W_OK | os.X_OK):  # it exists, and isn't ours to write
        raise Refused(f"can't write {s.jobs}: permission denied")
    for path in (s.jobs, Path(ctx.cwd)):
        if (full := fullness(path)) >= DISK_FULL:
            raise Refused(f"the disk of {path} is {full:.0%} full: free some space first")
    attached, why = attachment(s.jobs.parent, ctx.bot, ctx.session)
    with locked(s.jobs):
        present = refuse_without_manager(ctx)
        refuse_without_slice(ctx)
        if len(present) >= s.max_running:
            raise Refused(
                f"{len(present)} background jobs already run on this machine, its limit: wait "
                "for one to end, or stop one (`zab job list --all`)"
            )
        if any(
            j.name == name and j.session == ctx.session and unit(j.id) in present
            for j in all_jobs(s.jobs)
        ):
            raise Refused(f"a job named `{name}` already runs in this session: pick another name")
        job_id = secrets.token_hex(6)
        # refuses an unsafe path before anything is written
        argv = unit_argv(ctx.zab or zab_path(), job_id, s.max_hours, s.jobs)
        job = Job(
            id=job_id,
            name=name,
            argv=list(command),
            cwd=ctx.cwd,
            env=scrubbed(ctx.env),
            bot=ctx.bot,
            session=ctx.session,
            codex_thread=thread_of(ctx.env),
            attached=attached,
            started_at=ctx.clock(),
            boot_id=ctx.boot,
            report=report and s.report,
            max_hours=s.max_hours,
            directory=s.jobs / job_id,
        )
        try:
            job.directory.mkdir(mode=0o700)
        except FileExistsError:  # another job's: never removed (review round 1 on #932)
            raise Refused("a job with this id exists already: try again") from None
        except OSError as exc:
            raise Refused(f"can't write {s.jobs}: {exc.strerror}") from None
        try:
            job.save()
        except OSError as exc:
            shutil.rmtree(job.directory, ignore_errors=True)
            raise Refused(f"can't write {s.jobs}: {exc.strerror}") from None
        done = ctx.runner(argv, env=user_env(ctx.env), timeout_s=60)
        if not done.ok:
            shutil.rmtree(job.directory, ignore_errors=True)
            raise Refused(f"systemd-run --user failed: {first_line(done.stderr) or done.exit_code}")
    ctx.sleep(START_WAIT_S)
    if job.ended() is not None:  # it ended at once: say how, here
        end = ending(job, running=False, boot=ctx.boot)
        assert end is not None
        job.mark_seen()
        print(f"Background job `{name}` {end.words} within {START_WAIT_S:.0f} s.")
        print(f"The end of its log, {job.log}:\n{tail(job.log, TAIL_LINES)}")
        return 0 if end.ok else 1
    print(f"Started background job `{name}` (id {job_id}).")
    print(f"Its log: {job.log}")
    print(
        f"Limits: {hours_words(job.max_hours)} for this job; {s.max_running} jobs at a time on "
        f"this machine, all of them together at most {s.max_memory_percent}% of its memory."
    )
    if attached is None:
        print(
            f"This job is unattached ({why}): nothing will be posted about it, and no turn will "
            f"start. Check it with `zab job status {name}`."
        )
    elif job.report:
        print(
            "zab will post in this topic when it ends and start a turn for you to report on it. "
            "You can end your turn now."
        )
    else:
        print("zab will post in this topic when it ends.")
    return 0


def state(ctx: Context, job: Job, present: set[str]) -> str:
    """A job's state in one line. An ended job of this session is marked as seen by its agent,
    and only then: another session's agent, or a terminal, listing the machine's jobs saw
    nothing for it (review round 1 on #932)."""
    end = ending(job, running=unit(job.id) in present, boot=ctx.boot, now=ctx.clock())
    if end is None:
        return f"running for {span(ctx.clock() - job.started_at)} since {when(job.started_at)}"
    if job.session == ctx.session:
        job.mark_seen()
    at = (job.ended() or {}).get("at")
    if isinstance(at, int | float):
        return f"{end.words} after {span(at - job.started_at)}, at {when(at)}"
    return f"{end.words} (started {when(job.started_at)})"


def newest(ctx: Context, name: str, present: set[str], *, running: bool = False) -> Job | None:
    for job in reversed(all_jobs(ctx.settings.jobs)):
        if job.name == name and job.session == ctx.session:
            if not running or unit(job.id) in present:
                return job
    return None


def missing(name: str, what: str = "") -> int:
    print(f"No {what}background job `{name}` in this session (`zab job list --all` lists all).")
    return 1


def list_jobs(ctx: Context, *, everywhere: bool) -> int:
    present = refuse_without_manager(ctx)
    jobs = [j for j in all_jobs(ctx.settings.jobs) if everywhere or j.session == ctx.session]
    if not jobs:
        print("No background jobs " + ("on this machine." if everywhere else "in this session."))
    for job in reversed(jobs):
        where = ""
        if everywhere:
            where = " · this session" if job.session == ctx.session else f" · bot {job.bot or '-'}"
        print(f"`{job.name}` (id {job.id}) · {state(ctx, job, present)}{where} · log {job.log}")
    return 0


def status(ctx: Context, name: str) -> int:
    present = refuse_without_manager(ctx)
    if (job := newest(ctx, name, present)) is None:
        return missing(name)
    print(f"Background job `{name}` (id {job.id}): {state(ctx, job, present)}.")
    print(f"Command: {shlex.join(job.argv)}\nDirectory: {job.cwd}")
    size = job.log.stat().st_size if job.log.exists() else 0
    print(f"Log: {job.log} ({size} bytes); its last {TAIL_LINES} lines:")
    print(tail(job.log, TAIL_LINES, limit=STATUS_BYTES))
    if size > STATUS_BYTES:
        print(f"[at most the last {STATUS_BYTES // 1024} KiB: `zab job log {name} -n N` for more]")
    return 0


def log(ctx: Context, name: str, lines: int) -> int:
    present = refuse_without_manager(ctx)
    if (job := newest(ctx, name, present)) is None:
        return missing(name)
    if ending(job, running=unit(job.id) in present, boot=ctx.boot, now=ctx.clock()):
        job.mark_seen()
    print(tail(job.log, max(1, min(lines, MAX_LINES))))
    return 0


def stop_job(ctx: Context, name: str) -> int:
    present = refuse_without_manager(ctx)
    if (job := newest(ctx, name, present, running=True)) is None:
        return missing(name, "running ")
    done = stop(job, "agent", ctx.runner)
    if not done.ok:
        print(f"Couldn't stop `{name}`: {first_line(done.stderr) or done.exit_code}")
        return 1
    for _ in range(int(STOP_WAIT_S / 0.2)):
        if job.ended() is not None:
            break
        ctx.sleep(0.2)
    job.mark_seen()
    print(f"Stopped background job `{name}` after {span(ctx.clock() - job.started_at)}.")
    return 0


def die_by(sig: int) -> int:
    """End as the job did, by its signal, so that systemd sees it; no core file of the wrapper."""
    with contextlib.suppress(OSError, ValueError):
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        signal.signal(sig, signal.SIG_DFL)
    os.kill(os.getpid(), sig)
    return 128 + sig


def pending(pid: int) -> list[int]:
    """The signals of `IGNORED` waiting, blocked, in process `pid`: its `SigPnd` and `ShdPnd`
    in `/proc/PID/status`, hexadecimal masks whose bit n-1 is signal n; none if unreadable."""
    mask = 0
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            key, _, value = line.partition(":")
            if key in ("SigPnd", "ShdPnd"):
                mask |= int(value, 16)
    except (OSError, ValueError):
        return []
    return [sig for sig in IGNORED if mask >> (sig - 1) & 1]


def gate() -> None:
    """The job's process between its fork and its exec (`preexec_fn`), while the wrapper waits
    in `Popen` with the signals of `IGNORED` blocked (#1020). They go back to their defaults
    and are unblocked here, so that one sent to this process, at the fork or since, ends it now,
    before the job runs, and one sent later reaches the job. One sent before the fork reached
    the wrapper alone and waits there: this process takes it on, and the job never runs. So a
    stop reaches the job exactly once, or ends it before it runs, however soon it comes."""
    with contextlib.suppress(Exception):  # an exception here would fail the job's start
        for sig in IGNORED:
            signal.signal(sig, signal.SIG_DFL)
        for sig in pending(os.getppid()):
            os.kill(os.getpid(), sig)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, IGNORED)


def run_job(directory: Path, *, cap: int = LOG_CAP) -> int:
    """`zab job _exec`, the unit's main process: the job's argument list run with no shell, with
    stdin `/dev/null`, in its directory, with its own environment; its stdout and stderr copied
    to the log up to `cap` bytes, the rest read and dropped. It ends as the job did: its exit
    status, or its signal (#655 V3).

    Signals: a stop (`zab job stop`, `!stop`) and the cap (`RuntimeMaxSec`) send TERM to every
    process of the unit (`KillMode=control-group`), so the job gets it directly, exactly once,
    and a TERM handler that saves its work runs once. The wrapper takes no action on TERM, INT
    or HUP (a handler of its own that does nothing, which the job's process resets to the
    default) and waits for the job; after `TimeoutStopSec` systemd kills what is left. A
    signal sent to the wrapper alone (`kill <MainPID>`, `systemctl --user kill
    --kill-whom=main`) therefore does nothing: stop a job with `zab job stop`. (Passing
    signals on, as Codex rounds 3 and 4 on #928 had it, delivered a second TERM into every stop
    and cap: a bash trap ran twice, and a Python save handler re-entered; review round 2.)

    Until the job's process is forked, the wrapper keeps them blocked, and that process, before
    its exec, takes on any that waits in the wrapper (`gate`): a stop that comes as the job
    starts, before the job's process exists, ends it before it runs. Taken by the handler, it
    would have stopped nothing, and the job would have run on until systemd's SIGKILL (#1020).
    There, a signal sent to the wrapper alone can't be told from a stop, and ends the job too."""
    job = Job.load(directory)
    if job is None:
        print(f"zab job _exec: no job in {directory}", file=sys.stderr)
        return 2
    signal.pthread_sigmask(signal.SIG_BLOCK, IGNORED)  # until the fork: `gate`
    for sig in IGNORED:
        signal.signal(sig, lambda *_: None)  # a handler, reset to the default in `gate`
    fd = os.open(job.log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    # unbuffered: a failed write fails at once, in `copy`, and nothing is left for the close to
    # flush and fail on, which would replace the job's status (Codex round 5 on #928)
    out = os.fdopen(fd, "ab", buffering=0)
    try:
        try:
            child = subprocess.Popen(  # noqa: S603 - the job's own argument list, no shell
                job.argv, cwd=job.cwd, env=job.env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, preexec_fn=gate,
            )  # fmt: skip
        except OSError as exc:
            with contextlib.suppress(OSError):
                out.write(f"zab: can't run {job.argv[0]!r} in {job.cwd}: {exc.strerror}\n".encode())
            return 127
        finally:
            signal.pthread_sigmask(signal.SIG_UNBLOCK, IGNORED)  # the job's process was forked
        assert child.stdout is not None
        copy(child, child.stdout.fileno(), out, cap)
        code = child.wait()
    finally:
        with contextlib.suppress(OSError):
            out.close()
    return die_by(-code) if code < 0 else code


def copy(
    child: Any,
    fd: int,
    out: BinaryIO,
    cap: int,
    read: Callable[[int, int], bytes] = os.read,
) -> None:
    """The job's output into the log until the job itself exits, then what the pipe already
    holds: never until the pipe's end, which a descendant that kept it open (a `setsid` child)
    would hold back (Codex round 1 on #928). Each read of what is there is bounded in bytes and
    time, and the job is looked at between them, so that a descendant writing without a pause
    can't keep the wrapper reading either (round 2). The wrapper then exits, and the unit's stop
    ends that descendant. A log that can't be written (a full disk) drops the rest, as at the
    cap, and the job runs on (review round 2)."""
    written = out.tell()

    def drain(limit: int, seconds: float) -> bool:
        """What the pipe holds now, at most `limit` bytes or `seconds`; True at its end."""
        nonlocal written
        got, until = 0, time.monotonic() + seconds
        while got < limit and time.monotonic() < until:
            try:
                chunk = read(fd, 1 << 16)
            except BlockingIOError:
                return False
            if not chunk:
                return True  # every writer has closed it
            got += len(chunk)
            if written < cap:
                keep = chunk[: cap - written]
                written += len(keep)
                try:
                    out.write(keep)
                    if written >= cap:
                        out.write(LOG_FULL)
                    out.flush()
                except OSError:
                    written = cap  # the rest is read and dropped; the job isn't stopped
        return False

    os.set_blocking(fd, False)
    while child.poll() is None:
        select.select([fd], [], [], POLL_S)
        if drain(DRAIN_BYTES, POLL_S):
            return  # the pipe has ended; the wrapper then waits for the job
    drain(DRAIN_BYTES, AFTER_EXIT_S)  # what the job left there, not a descendant's stream


def record_end(directory: Path, runner: Runner = subprocess_runner) -> int:
    """`zab job _ended`, the unit's `ExecStopPost` (§7.14): what systemd says of the ending, the
    time and the boot, the user manager's state (`stopping` when a clean shutdown stops it, which
    cut the job off), and a copy of `stopped.json`, so that the watcher reads one file."""
    if not directory.is_dir():
        return 0
    data: dict[str, object] = {
        k: os.environ.get(k) for k in ("SERVICE_RESULT", "EXIT_CODE", "EXIT_STATUS")
    }
    data["at"] = time.time()
    data["boot_id"] = boot_id()
    state = runner(["systemctl", "--user", "is-system-running"], env=user_env(), timeout_s=5)
    data["manager_state"] = state.stdout.strip()
    data["stopped"] = read_json(directory / "stopped.json")
    write_json(directory / "ended.json", data)
    return 0


def internal(argv: list[str]) -> int:
    """`_exec DIR` or `_ended DIR`: a job's absolute directory, named by its id."""
    directory = Path(argv[1]) if len(argv) == 2 else None
    if (
        directory is None
        or not SAFE_PATH_RE.fullmatch(argv[1])
        or not ID_RE.fullmatch(directory.name)
        or ".." in directory.parts
    ):
        print(f"usage: zab job {argv[0]} DIR (a job's absolute directory)", file=sys.stderr)
        return 2
    return run_job(directory) if argv[0] == "_exec" else record_end(directory)


def main(argv: list[str]) -> int:
    if argv[:1] in (["_exec"], ["_ended"]):
        return internal(argv)
    head, command = (
        (argv[: argv.index("--")], argv[argv.index("--") + 1 :]) if "--" in argv else (argv, [])
    )
    parser = argparse.ArgumentParser(
        prog="zab job",
        description="An agent's background job, which outlives its turn (SPEC §7.14).",
    )
    parser.add_argument(
        "--config", help="zab's config (default: $ZAB_CONFIG, or ~/.config/zab/config.toml)"
    )
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("start", help="start a job: zab job start --name NAME -- CMD ARG…")
    p.add_argument("--name", required=True)
    p.add_argument("--no-report", action="store_true", help="no report turn when it ends")
    p = sub.add_parser("list", help="this session's jobs")
    p.add_argument("--all", action="store_true", dest="everywhere", help="the machine's jobs")
    for action, text in (
        ("status", "a job's state and its log's end"),
        ("log", "a job's log"),
        ("stop", "stop a job"),
    ):
        p = sub.add_parser(action, help=text)
        p.add_argument("name")
        if action == "log":
            p.add_argument("-n", type=int, default=100, dest="lines", help="lines (default 100)")
    args = parser.parse_args(head)
    if command and args.action != "start":
        parser.error("only `start` takes a command after `--`")
    try:
        ctx = Context(settings(args.config), "", os.environ, os.getcwd())
        if args.action == "start":
            return start(ctx, args.name, command, report=not args.no_report)
        if args.action == "list":
            return list_jobs(ctx, everywhere=args.everywhere)
        if args.action == "status":
            return status(ctx, args.name)
        if args.action == "log":
            return log(ctx, args.name, args.lines)
        return stop_job(ctx, args.name)
    except (Refused, ConfigError, ValueError, OSError) as exc:
        print(f"zab job: {exc}", file=sys.stderr)
        return 2
