"""A background job's unit side (SPEC §7.14, #655): `zab job _exec`, its main process, and
`zab job _ended`, its `ExecStopPost`, run for real, each in a process of its own (`_exec` sets
signal handlers)."""

from __future__ import annotations

import contextlib
import itertools
import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from zab import jobs
from zab.cli import COMMANDS
from zab.tools import job as cli
from zab.tools.common import RunResult

RUNS = itertools.count(1)


def made(tmp_path, argv: list[str]) -> jobs.Job:
    job_id = f"{next(RUNS):012x}"
    job = jobs.Job(id=job_id, name="t", argv=argv, cwd=str(tmp_path),
                   env={"PATH": os.environ["PATH"], "ONLY": "mine"},
                   directory=tmp_path / "jobs" / job_id)  # fmt: skip
    job.directory.mkdir(parents=True)
    job.save()
    return job


def run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    env = {**os.environ, "CALLERS": "theirs", **kwargs.pop("env", {})}
    return subprocess.run(argv, env=env, check=False, timeout=30, **kwargs)  # noqa: S603


def exec_job(tmp_path, argv: list[str], *, cap: int | None = None) -> tuple[int, bytes]:
    """`run_job` on a new job, as the unit runs it."""
    job = made(tmp_path, argv)
    code = "import sys; from pathlib import Path; from zab.tools.job import run_job; "
    code += f"sys.exit(run_job(Path({str(job.directory)!r}){f', cap={cap}' if cap else ''}))"
    return run([sys.executable, "-c", code]).returncode, job.log.read_bytes()


def test_exec_runs_the_argument_list_with_its_own_environment_and_no_stdin(tmp_path):
    script = (
        "import os, sys; print(os.getcwd(), os.environ.get('ONLY'), os.environ.get('CALLERS'));"
        "print(repr(sys.stdin.read())); print('to stderr', file=sys.stderr); sys.exit(3)"
    )
    code, log = exec_job(tmp_path, [sys.executable, "-u", "-c", script, "$HOME"])
    assert code == 3
    assert log.decode().splitlines() == [f"{tmp_path} mine None", "''", "to stderr"]


def test_exec_ends_by_the_jobs_signal_and_reports_a_missing_program(tmp_path):
    kill = "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"
    assert exec_job(tmp_path, [sys.executable, "-c", kill])[0] == -signal.SIGTERM
    code, log = exec_job(tmp_path, ["no-such-program-zab"])
    assert code == 127 and b"can't run 'no-such-program-zab'" in log


def running(marker: str) -> bool:
    """Whether a process has `marker` among its arguments."""
    for entry in Path("/proc").iterdir():
        try:
            if entry.name.isdigit() and marker.encode() in (entry / "cmdline").read_bytes():
                return True
        except OSError:
            continue
    return False


def test_exec_ends_with_the_job_not_with_a_descendant_holding_its_output(tmp_path):
    """Codex round 1 on #928: a `setsid` descendant that keeps stdout open must not keep the
    wrapper, and so the unit, waiting after the job itself exited. The wrapper ends with the
    job's status at once; the unit's stop then ends the descendant (`KillMode=control-group`,
    the probe's `setsid-exit` case)."""
    marker = f"zab-test-{os.getpid()}-holder"
    holder = f"import time; print('holder', flush=True); time.sleep(30)  # {marker}"
    script = (
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {holder!r}, {marker!r}],"
        " start_new_session=True)\n"
        "time.sleep(0.5); print('job done', flush=True); sys.exit(7)\n"
    )
    began = time.monotonic()
    try:
        code, log = exec_job(tmp_path, [sys.executable, "-c", script])
        assert code == 7 and time.monotonic() - began < 10
        assert b"holder\n" in log and b"job done\n" in log
    finally:
        for entry in Path("/proc").iterdir():
            with contextlib.suppress(OSError, ValueError):
                argv = (entry / "cmdline").read_bytes().split(b"\0")
                if marker.encode() in argv[1:]:
                    os.kill(int(entry.name), signal.SIGKILL)


class Exits:
    """A job that has exited at the `at`-th look."""

    def __init__(self, at: int) -> None:
        self.at, self.looks = at, 0

    def poll(self) -> int | None:
        self.looks += 1
        return 0 if self.looks >= self.at else None


def test_exec_ends_with_the_job_while_a_descendant_keeps_writing(tmp_path):
    """Codex round 2 on #928: a descendant that writes without a pause after the job exited
    must not keep the wrapper reading it. Here the pipe is never empty (each read returns a full
    chunk): the wrapper still looks at the job between bounded reads, and once it exited reads
    only a bounded amount more."""
    reads: list[int] = []

    def endless(fd: int, n: int) -> bytes:
        reads.append(n)
        return b"w" * n

    rfd, wfd = os.pipe()
    os.write(wfd, b"x")  # `select` finds it readable at once
    try:
        with (tmp_path / "log").open("wb") as out:
            cli.copy(Exits(3), rfd, out, 10 * cli.DRAIN_BYTES, endless)
    finally:
        os.close(rfd)
        os.close(wfd)
    total = sum(reads)
    assert total <= 3 * cli.DRAIN_BYTES + 3 * (1 << 16)  # two reads while it ran, one after
    assert (tmp_path / "log").stat().st_size == total


def test_a_stop_reaches_the_child_and_the_wrapper_waits_for_it(tmp_path):
    """systemd's stop sends SIGTERM to every process of the unit (#655 V3): the job dies of it,
    and the wrapper, which ignored it, then dies of the same signal."""
    marker = f"zab-test-{os.getpid()}-stop"
    job = made(tmp_path, [sys.executable, "-c", "import time; time.sleep(60)", marker])
    code = "import sys; from pathlib import Path; from zab.tools.job import run_job; "
    code += f"sys.exit(run_job(Path({str(job.directory)!r})))"
    wrapper = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)  # noqa: S603
    try:
        deadline = time.monotonic() + 10
        while not running(marker):
            assert time.monotonic() < deadline, "the job never ran"
            time.sleep(0.05)
        os.killpg(wrapper.pid, signal.SIGTERM)
        assert wrapper.wait(10) == -signal.SIGTERM
        assert not running(marker)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(wrapper.pid, signal.SIGKILL)


def test_exec_stops_the_log_at_its_cap(tmp_path):
    many = "import sys; sys.stdout.write('x' * 5000); sys.stdout.flush(); print('done')"
    code, log = exec_job(tmp_path, [sys.executable, "-c", many], cap=1000)
    assert code == 0 and log == b"x" * 1000 + cli.LOG_FULL


def test_ended_records_what_systemd_says(tmp_path, monkeypatch):
    directory = tmp_path / "0123456789ab"
    directory.mkdir()
    for key, value in (
        ("SERVICE_RESULT", "timeout"),
        ("EXIT_CODE", "killed"),
        ("EXIT_STATUS", "TERM"),
    ):
        monkeypatch.setenv(key, value)
    calls = []

    def runner(argv, **kw):
        calls.append(argv)
        return RunResult(0, "stopping\n", "")

    jobs.write_json(directory / "stopped.json", {"by": "person", "at": 5.0})
    assert cli.record_end(directory, runner) == 0
    data = json.loads((directory / "ended.json").read_text())
    assert data["SERVICE_RESULT"] == "timeout" and data["manager_state"] == "stopping"
    assert data["stopped"] == {"by": "person", "at": 5.0} and data["boot_id"] == jobs.boot_id()
    assert calls == [["systemctl", "--user", "is-system-running"]]
    assert cli.record_end(tmp_path / "gone", runner) == 0  # a deleted job: nothing to do


def test_exec_and_ended_take_the_jobs_directory_and_no_config(tmp_path):
    """The unit's command lines carry the job's directory: a service of the user manager has
    neither the caller's `ZAB_CONFIG` nor any config here, and a custom `state_dir` works."""
    job = made(tmp_path / "custom-state", [sys.executable, "-c", "print('ran')"])
    home = tmp_path / "empty-home"  # no ~/.config/zab/config.toml
    home.mkdir()
    env = {"HOME": str(home), "ZAB_CONFIG": str(tmp_path / "none.toml"),
           "SERVICE_RESULT": "success", "EXIT_CODE": "exited", "EXIT_STATUS": "0"}  # fmt: skip
    argv = jobs.unit_argv("/z", job.id, 12, job.directory.parent)
    assert argv[-1] == str(job.directory) and f"_ended {job.directory}" in " ".join(argv)
    for helper in ("_exec", "_ended"):
        done = run([sys.executable, "-m", "zab", "job", helper, str(job.directory)], env=env)
        assert done.returncode == 0
    assert job.log.read_text() == "ran\n"
    assert jobs.ending(job, running=False, boot="").words == "finished: exit 0"


def test_the_command_line(tmp_path):
    assert COMMANDS["job"] == "zab.tools.job"
    for wrong in (
        ["_exec", "0123456789ab"],
        ["_exec", "/j/../etc"],
        ["_ended"],
        ["_ended", str(tmp_path / "not-an-id")],
        ["_exec", "/a b/0123456789ab"],
        ["_exec", "/j/../0123456789ab"],
    ):
        assert cli.main(wrong) == 2  # fmt: skip
    with pytest.raises(SystemExit):  # the commands' own usage (#655 PR 2b)
        cli.main(["start"])


@pytest.mark.parametrize("cap", [1, 65536, 65537])
def test_the_cap_holds_across_reads(tmp_path, cap):
    many = "import sys; sys.stdout.write('y' * 200000)"
    code, log = exec_job(tmp_path, [sys.executable, "-c", many], cap=cap)
    assert code == 0 and log == b"y" * cap + cli.LOG_FULL


def spawn(job: jobs.Job, **env: str) -> subprocess.Popen:
    """`_exec` on `job` in a session of its own, its stdin a pipe held open."""
    code = "import sys; from pathlib import Path; from zab.tools.job import run_job; "
    code += f"sys.exit(run_job(Path({str(job.directory)!r})))"
    return subprocess.Popen(  # noqa: S603 - this interpreter, a fixed snippet
        [sys.executable, "-c", code], env={**os.environ, **env}, stdin=subprocess.PIPE,
        start_new_session=True,
    )  # fmt: skip


def wait_for_line(log: Path, line: bytes) -> None:
    deadline = time.monotonic() + 10
    while line not in (log.read_bytes() if log.exists() else b""):
        assert time.monotonic() < deadline, f"no {line!r} in the log"
        time.sleep(0.05)


#: A job that saves its work on SIGTERM for a second, then exits 3; each TERM it gets is a line
TRAPS_TERM = (
    "import signal, sys, time\n"
    "def save(*_):\n"
    "    print('term', flush=True); time.sleep(1); print('saved', flush=True); sys.exit(3)\n"
    "signal.signal(signal.SIGTERM, save)\nprint('ready', flush=True)\ntime.sleep(60)\n"
)


def test_a_stop_gives_the_job_one_term_and_its_save_completes(tmp_path):
    """Review round 2 on #928: a stop's TERM, sent to every process of the unit as systemd
    sends it, reaches the job exactly once, and its save runs to the end; the wrapper waits
    for it and exits 3. (Passed on as well, a second TERM cut the save short.)"""
    job = made(tmp_path, [sys.executable, "-c", TRAPS_TERM])
    wrapper = spawn(job)
    try:
        wait_for_line(job.log, b"ready\n")
        os.killpg(wrapper.pid, signal.SIGTERM)
        assert wrapper.wait(10) == 3
        assert job.log.read_bytes() == b"ready\nterm\nsaved\n"  # one TERM, one whole save
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(wrapper.pid, signal.SIGKILL)


def test_a_term_to_the_wrapper_alone_leaves_the_job_running(tmp_path):
    """Review round 2 on #928: `kill <MainPID>` does nothing; `zab job stop` stops a job."""
    job = made(tmp_path, [sys.executable, "-c", TRAPS_TERM])
    wrapper = spawn(job)
    try:
        wait_for_line(job.log, b"ready\n")
        os.kill(wrapper.pid, signal.SIGTERM)
        time.sleep(1.0)
        assert wrapper.poll() is None and job.log.read_bytes() == b"ready\n"
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(wrapper.pid, signal.SIGKILL)


class Full:
    """A log on a full disk: every write fails."""

    def __init__(self) -> None:
        self.writes = 0

    def tell(self) -> int:
        return 0

    def write(self, data: bytes) -> int:
        self.writes += 1
        raise OSError(28, "No space left on device")

    def flush(self) -> None:
        raise OSError(28, "No space left on device")


def test_a_log_that_cant_be_written_doesnt_stop_the_job(tmp_path):
    """Review round 2 on #928, N3: a full disk drops the rest of the output, as the cap does,
    and the wrapper keeps reading and waiting for the job."""
    rfd, wfd = os.pipe()
    os.write(wfd, b"x" * 1000)
    out = Full()
    try:
        cli.copy(Exits(3), rfd, out, 1 << 20)  # returns once the job exits, raising nothing
    finally:
        os.close(rfd)
        os.close(wfd)
    assert out.writes == 1  # tried once, then dropped


def test_the_job_gets_no_stdin_even_with_the_wrappers_open(tmp_path):
    """Review round 1 on #928, N4: `stdin=DEVNULL`, whatever stdin the wrapper has."""
    job = made(tmp_path, [sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"])
    wrapper = spawn(job)
    try:
        assert wrapper.wait(10) == 0  # the wrapper's own stdin, a pipe, stays open
        assert job.log.read_text() == "''\n"
    finally:
        wrapper.stdin.close()


def test_the_log_is_private(tmp_path):
    """Review round 1 on #928, N7."""
    code, _ = exec_job(tmp_path, [sys.executable, "-c", "print('x')"])
    (log,) = (tmp_path / "jobs").glob("*/output.log")
    assert code == 0 and stat.S_IMODE(log.stat().st_mode) == 0o600


def test_what_the_job_left_in_the_pipe_is_kept_once_it_exited(tmp_path):
    """Review round 1 on #928, N4: a job already exited at the first look, its last bytes
    still in the pipe: the drain after the exit keeps them."""
    rfd, wfd = os.pipe()
    os.write(wfd, b"the last line\n")
    try:
        with (tmp_path / "log").open("wb") as out:
            cli.copy(Exits(1), rfd, out, 1 << 20)
    finally:
        os.close(rfd)
        os.close(wfd)
    assert (tmp_path / "log").read_bytes() == b"the last line\n"


@pytest.mark.parametrize("how", ["dotdot", "space"])
def test_exec_and_ended_refuse_a_directory_not_given_plainly(tmp_path, how):
    """Review round 1 on #928, N4: a real job's directory, reached through `..` or with a space
    in its path, is refused before anything is read or written."""
    base = tmp_path / ("with space" if how == "space" else "plain")
    job = made(base, [sys.executable, "-c", "print('x')"])
    given = str(job.directory) if how == "space" else f"{base}/jobs/x/../{job.id}"
    for helper in ("_ended", "_exec"):
        assert cli.main([helper, given]) == 2
    assert not (job.directory / "ended.json").exists() and not job.log.exists()


@pytest.mark.skipif(not Path("/dev/full").exists(), reason="no /dev/full")
@pytest.mark.parametrize(
    ("argv", "expected"),
    [([sys.executable, "-c", "print('x' * 100); import sys; sys.exit(4)"], 4),
     ([sys.executable, "-c", "print('x' * 100, flush=True); import os, signal; "
       "os.kill(os.getpid(), signal.SIGTERM)"], -signal.SIGTERM),
     (["no-such-program-zab"], 127)],
)  # fmt: skip
def test_a_full_disk_keeps_the_jobs_status(tmp_path, argv, expected):
    """Codex round 5 on #928: every write to the log fails with ENOSPC (`/dev/full`), through
    the log's close too; the wrapper still ends as the job did, and a missing program still
    ends 127 though its message can't be written (review of #928, N2)."""
    job = made(tmp_path, argv)
    job.log.symlink_to("/dev/full")
    code = run([sys.executable, "-c", "import sys; from pathlib import Path; "
                "from zab.tools.job import run_job; "
                f"sys.exit(run_job(Path({str(job.directory)!r})))"]).returncode  # fmt: skip
    assert code == expected
