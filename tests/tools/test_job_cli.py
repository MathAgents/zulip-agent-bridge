"""`zab job start/list/status/log/stop` (SPEC §7.14, #655), against a fake systemd and a real
bridge store: the record, the attachment `start` decides once, the refusals, and `seen`."""

from __future__ import annotations

import io
import itertools
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from zab import jobs
from zab.core.store import Store
from zab.tools import job as cli
from zab.tools.common import RunResult

ZAB = "/home/agent/.local/bin/zab"
S1, S2 = "1b4e28ba-2fa1-41d2-883f-0016d3cca427", "6fa459ea-ee8a-4ca4-894e-db77e160355e"
THREAD = "019a5e2c-7d41-7a33-9c1e-5f2b8e4d1a60"
TURN_CGROUP = "/user.slice/user-1001.slice/user@1001.service/app.slice/zab.service"
JOB_CGROUP = "/user.slice/user-1001.slice/user@1001.service/zab.slice/zab-jobs.slice/zab-job-0123456789ab.service"  # noqa: E501
CARDS = itertools.count(1)


class FakeSystemd:
    """`systemctl --user` and `systemd-run --user` for job units: a unit starts present, and
    `end` (a job's `ended.json`) can be written as it starts, as for a command that fails at
    once."""

    def __init__(self) -> None:
        self.present: set[str] = set()
        self.calls: list[list[str]] = []
        self.envs: list[dict | None] = []
        self.gate: threading.Event | None = None  # holds `systemd-run` until set
        self.show_fails = False
        self.answer = True
        self.run_fails = ""
        self.end: dict | None = None
        # `zab-jobs.slice` as `zab service install` writes it (#930)
        self.slice = {
            "LoadState": "loaded",
            "FragmentPath": "/home/agent/.config/systemd/user/zab-jobs.slice",
            "MemoryMax": "24679948288",
            "MemorySwapMax": "0",
        }

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        self.envs.append(kwargs.get("env"))
        if not self.answer:
            return RunResult(1, "", "Failed to connect to bus: No medium found")
        if argv[:4] == ["systemctl", "--user", "show", jobs.SLICE]:
            if self.show_fails:
                return RunResult(1, "", "Failed to get properties")
            return RunResult(0, "".join(f"{k}={v}\n" for k, v in self.slice.items()), "")
        if argv[:3] == ["systemctl", "--user", "list-units"]:
            lines = [f"{u} loaded active running zab" for u in sorted(self.present)]
            return RunResult(0, "\n".join(lines), "")
        if argv[0] == "systemd-run":
            if self.gate is not None:
                self.gate.wait(10)
            if self.run_fails:
                return RunResult(1, "", self.run_fails)
            directory = Path(argv[-1])  # `_exec`'s argument, the job's directory
            self.present.add(jobs.unit(directory.name))
            if self.end is not None:
                self.present.discard(jobs.unit(directory.name))
                jobs.write_json(directory / "ended.json", self.end)
            return RunResult(0, "", "")
        if argv[:3] == ["systemctl", "--user", "stop"]:
            self.present.discard(argv[-1])
            return RunResult(0, "", "")
        return RunResult(0, "", "")


def session(state: Path, session_id: str = S1, *, bot: str = "claude-bot",
            row_state: str = "idle", turn: str | None = "running") -> None:  # fmt: skip
    """A session of the bridge's store, and its turn."""
    store = Store(state)
    store.insert(
        "sessions", id=session_id, bot=bot, backend="claude", native_id=f"n-{session_id}",
        card_message_id=next(CARDS), stream_id=5, topic=f"t-{session_id}", workspace="/w",
        workspace_mode="shared", repo_root="/w", settings_json="{}", state=row_state,
        created_at=0.0, updated_at=0.0,
    )  # fmt: skip
    if turn:
        store.insert("turns", session_id=session_id, native_turn_id="t1", trigger_msg_ids="[]",
                     state=turn, sender_id=9, started_at=900.0)  # fmt: skip
    store.close()


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "fullness", lambda path: 0.5)
    env = {"PATH": "/usr/bin", "ZAB_BOT": "claude-bot", "ZAB_SESSION": S1, "GH_TOKEN": "x"}
    work = tmp_path / "work"
    work.mkdir()
    session(tmp_path / "state")
    return cli.Context(cli.Settings(tmp_path / "state" / "jobs"), ZAB, env, str(work),
                       runner=FakeSystemd(), clock=lambda: 1000.0, sleep=lambda s: None,
                       boot="boot-1", cgroup=TURN_CGROUP)  # fmt: skip


def started(ctx, capsys, name="search", *command, report=True) -> tuple[int, str, jobs.Job | None]:
    code = cli.start(ctx, name, list(command or ["python3", "-u", "s.py"]), report=report)
    found = [j for j in jobs.all_jobs(ctx.settings.jobs) if j.name == name]
    return code, capsys.readouterr().out, found[-1] if found else None


def test_start_writes_the_record_attached_and_starts_the_unit(ctx, capsys):
    slept = []
    ctx.sleep = slept.append
    code, out, job = started(ctx, capsys)
    assert code == 0 and slept == [cli.START_WAIT_S]
    assert job.argv == ["python3", "-u", "s.py"] and job.cwd == ctx.cwd
    assert (job.bot, job.session, job.attached) == ("claude-bot", S1, S1)
    assert (job.boot_id, job.started_at) == ("boot-1", 1000.0)
    assert job.env == {"PATH": "/usr/bin", "ZAB_BOT": "claude-bot", "ZAB_SESSION": S1}
    assert job.report and job.max_hours == 12
    assert ctx.runner.calls[-1] == jobs.unit_argv(ZAB, job.id, 12, ctx.settings.jobs)
    assert f"Started background job `search` (id {job.id})." in out and str(job.log) in out
    assert "start a turn for you to report on it. You can end your turn now." in out
    assert oct(job.directory.stat().st_mode & 0o777) == "0o700"


def test_start_without_a_report(ctx, capsys):
    _, out, job = started(ctx, capsys, "quiet", report=False)
    assert not job.report and out.rstrip().endswith("zab will post in this topic when it ends.")
    ctx.settings.report = False
    _, _, job = started(ctx, capsys, "quieter")
    assert not job.report


@pytest.mark.parametrize(
    ("change", "why"),
    [
        ({"ZAB_SESSION": None}, "no zab session started it"),
        ({"ZAB_SESSION": "7"}, "no zab session started it"),
        ({"ZAB_SESSION": S1.upper()}, "no zab session started it"),
        ({"ZAB_BOT": None}, "no bot of zab's started it"),
        ({"ZAB_BOT": "codex-bot"}, "its session isn't one of this bot's"),
        ({"ZAB_SESSION": S2}, "its session isn't one of this bot's"),
    ],
)
def test_a_job_is_unattached_unless_its_session_is_the_bots_with_a_turn_running(
    ctx, capsys, change, why
):
    """`start` decides the attachment once (§7.14): it promises nothing for a job that isn't,
    and starts it all the same."""
    ctx.env = {k: v for k, v in {**ctx.env, **change}.items() if v is not None}
    code, out, job = started(ctx, capsys)
    assert code == 0 and job.attached is None
    assert f"This job is unattached ({why}): nothing will be posted about it" in out
    assert "will post in this topic" not in out


def test_a_detached_session_or_one_with_no_turn_running_attaches_nothing(ctx, capsys, tmp_path):
    session(tmp_path / "state", S2, row_state="detached")
    ctx.env = {**ctx.env, "ZAB_SESSION": S2}
    _, out, job = started(ctx, capsys, "a")
    assert job.attached is None and "(its session is detached)" in out
    third = "9b2d1c3e-5f6a-4b7c-8d9e-0f1a2b3c4d5e"
    session(tmp_path / "state", third, turn="completed")
    ctx.env = {**ctx.env, "ZAB_SESSION": third}
    _, out, job = started(ctx, capsys, "b")
    assert job.attached is None and "(no turn of its session is running)" in out


def test_a_codex_jobs_thread_is_recorded_when_it_is_a_thread_id(ctx, capsys):
    ctx.env = {**ctx.env, "CODEX_THREAD_ID": THREAD}
    assert started(ctx, capsys, "a")[2].codex_thread == THREAD
    ctx.env = {**ctx.env, "CODEX_THREAD_ID": "x; rm -rf /"}
    assert started(ctx, capsys, "b")[2].codex_thread == ""


def test_start_refuses_inside_a_job(ctx):
    """A job must not start its successor and exit, which would keep the VM up past its bound:
    told by the cgroup, which a job can't leave by unsetting a variable (§7.14)."""
    ctx.cgroup = JOB_CGROUP
    with pytest.raises(cli.Refused) as refused:
        cli.start(ctx, "next", ["true"], report=True)
    assert str(refused.value) == cli.INSIDE_A_JOB
    assert ctx.runner.calls == [] and not ctx.settings.jobs.exists()
    ctx.cgroup = TURN_CGROUP.replace("zab.service", "zab-jobs-like.service")
    assert cli.start(ctx, "next", ["true"], report=True) == 0  # a name, not the slice


def test_start_inside_a_job_exits_2_from_the_command_line(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "own_cgroup", lambda: JOB_CGROUP)

    def never(runner):
        raise AssertionError("reached the user manager")

    monkeypatch.setattr(cli, "present_units", never)
    config = tmp_path / "config.toml"
    config.write_text(f'[bridge]\nstate_dir = "{tmp_path / "state"}"\n')
    assert cli.main(["--config", str(config), "start", "--name", "x", "--", "true"]) == 2
    assert capsys.readouterr().err.strip() == f"zab job: {cli.INSIDE_A_JOB}"


def test_a_command_that_fails_at_once_is_reported_there(ctx, capsys):
    ctx.runner.end = {"SERVICE_RESULT": "exit-code", "EXIT_CODE": "exited", "EXIT_STATUS": "2"}
    code, out, job = started(ctx, capsys)
    assert code == 1 and "`search` failed: exit 2 within 3 s" in out and job.seen
    ctx.runner.end = {"SERVICE_RESULT": "success", "EXIT_CODE": "exited", "EXIT_STATUS": "0"}
    code, out, job = started(ctx, capsys, "quick")
    assert code == 0 and "finished: exit 0" in out


@pytest.mark.parametrize("name", ["", "Search", "-x", "a b", "jobs", "x" * 33, "a/b"])
def test_start_refuses_a_bad_name(ctx, name):
    with pytest.raises(cli.Refused, match="invalid name"):
        cli.start(ctx, name, ["true"], report=True)
    assert ctx.runner.calls == []


def test_start_refuses_past_the_machines_limit_and_a_name_running_here(ctx, capsys):
    ctx.settings.max_running = 2
    started(ctx, capsys, "one")
    with pytest.raises(cli.Refused, match="already runs in this session"):
        cli.start(ctx, "one", ["true"], report=True)
    ctx.env = {**ctx.env, "ZAB_SESSION": S2}
    assert started(ctx, capsys, "one")[0] == 0  # another session's name
    with pytest.raises(cli.Refused, match="2 background jobs already run on this machine"):
        cli.start(ctx, "two", ["true"], report=True)
    ctx.runner.present.clear()  # both ended: the name is free again
    assert started(ctx, capsys, "one")[0] == 0


def test_start_refuses_without_a_manager_jobs_off_a_full_disk_or_a_failed_start(ctx, monkeypatch):
    ctx.runner.answer = False
    with pytest.raises(cli.Refused, match="no systemd user manager"):
        cli.start(ctx, "a", ["true"], report=True)
    ctx.runner.answer, ctx.runner.run_fails = True, "Unit zab-job-x.service already exists."
    with pytest.raises(cli.Refused, match="systemd-run --user failed: Unit"):
        cli.start(ctx, "a", ["true"], report=True)
    assert jobs.all_jobs(ctx.settings.jobs) == []  # its directory is gone again
    with pytest.raises(cli.Refused, match="nothing to run"):
        cli.start(ctx, "a", [], report=True)
    monkeypatch.setattr(cli, "fullness", lambda path: 0.93)
    with pytest.raises(cli.Refused, match="93% full"):
        cli.start(ctx, "a", ["true"], report=True)
    ctx.settings.max_running = 0
    with pytest.raises(cli.Refused, match="off on this machine"):
        cli.start(ctx, "a", ["true"], report=True)


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
def test_start_refuses_where_it_cant_write_the_jobs(ctx, tmp_path):
    locked_out = tmp_path / "ro"
    locked_out.mkdir(mode=0o500)
    ctx.settings.jobs = locked_out / "jobs"
    with pytest.raises(cli.Refused, match="can't write"):
        cli.start(ctx, "a", ["true"], report=True)


def test_a_zab_path_that_cant_go_into_a_unit_leaves_nothing(ctx):
    ctx.zab = "/home/a b/zab"
    with pytest.raises(ValueError, match="can't go into a systemd unit"):
        cli.start(ctx, "a", ["true"], report=True)
    assert jobs.all_jobs(ctx.settings.jobs) == [] and not any(
        c[0] == "systemd-run" for c in ctx.runner.calls
    )


def test_list_status_and_log_show_this_sessions_jobs_and_mark_an_end_seen(ctx, capsys):
    _, _, job = started(ctx, capsys)
    job.log.write_text("".join(f"line {i}\n" for i in range(30)))
    assert cli.list_jobs(ctx, everywhere=False) == 0
    assert "`search`" in capsys.readouterr().out and not job.seen
    jobs.write_json(job.directory / "ended.json", {"SERVICE_RESULT": "success",
                    "EXIT_CODE": "exited", "EXIT_STATUS": "0", "at": 1000.0 + 125})  # fmt: skip
    ctx.runner.present.clear()
    assert cli.status(ctx, "search") == 0
    out = capsys.readouterr().out
    assert "finished: exit 0 after 2 min" in out and "Command: python3 -u s.py" in out
    assert "line 9\n" not in out and "line 10\nline 11" in out and job.seen
    assert cli.log(ctx, "search", 3) == 0
    assert capsys.readouterr().out == "line 27\nline 28\nline 29\n"
    ctx.env = {**ctx.env, "ZAB_SESSION": S2}
    assert cli.status(ctx, "search") == 1
    assert cli.list_jobs(ctx, everywhere=False) == 0
    assert "No background jobs in this session." in capsys.readouterr().out
    assert cli.list_jobs(ctx, everywhere=True) == 0
    assert "`search`" in capsys.readouterr().out


def test_stop_records_the_agent_and_stops_the_unit(ctx, capsys):
    _, _, job = started(ctx, capsys)
    assert cli.stop_job(ctx, "search") == 0
    assert job.stopped()["by"] == "agent"
    assert ctx.runner.calls[-1] == ["systemctl", "--user", "stop", jobs.unit(job.id)]
    assert "Stopped background job `search`" in capsys.readouterr().out
    assert cli.stop_job(ctx, "search") == 1  # no longer running
    assert "No running background job `search`" in capsys.readouterr().out


def test_the_command_line(tmp_path, capsys):
    config = tmp_path / "config.toml"
    config.write_text(f'[bridge]\nstate_dir = "{tmp_path / "state"}"\n')
    with pytest.raises(SystemExit):
        cli.main(["--config", str(config), "status", "x", "--", "ls"])
    assert cli.main(["--config", str(tmp_path / "missing.toml"), "list"]) == 2


@pytest.mark.parametrize(
    "shown",
    [
        # no file: systemd makes the slice on the fly, without limits (#930's review)
        {"LoadState": "loaded", "FragmentPath": "", "MemoryMax": "infinity",
         "MemorySwapMax": "infinity"},
        {"LoadState": "not-found", "FragmentPath": "", "MemoryMax": "infinity",
         "MemorySwapMax": "infinity"},
        # a file without the limits
        {"MemoryMax": "infinity"},
        {"MemorySwapMax": "infinity"},
        {"MemorySwapMax": "1073741824"},
        # the limits right, the file or the load wrong (review round 1 on #932)
        {"FragmentPath": ""},
        {"LoadState": "not-found"},
    ],
)  # fmt: skip
def test_start_refuses_without_the_slices_limits(ctx, shown):
    """Codex round 1 on #932: fail closed. Without `zab-jobs.slice`'s file, or with a file
    whose memory isn't bounded, a job would run unlimited."""
    ctx.runner.slice = {**ctx.runner.slice, **shown}
    with pytest.raises(cli.Refused) as refused:
        cli.start(ctx, "a", ["true"], report=True)
    assert str(refused.value) == cli.SLICE_MISSING
    assert not any(c[0] == "systemd-run" for c in ctx.runner.calls)
    assert jobs.all_jobs(ctx.settings.jobs) == []
    props = ("LoadState", "FragmentPath", "MemoryMax", "MemorySwapMax")
    show = ["systemctl", "--user", "show", jobs.SLICE, *(f"--property={p}" for p in props)]
    assert show in ctx.runner.calls


def test_start_runs_with_the_installed_slice(ctx, capsys):
    """The good case, which every other start here takes: loaded from its file, a finite
    `MemoryMax`, no swap."""
    assert started(ctx, capsys)[0] == 0


def test_start_refuses_when_the_slice_cant_be_read(ctx):
    """Review round 1 on #932, N4: a failed `systemctl show` is no slice."""
    ctx.runner.show_fails = True
    with pytest.raises(cli.Refused, match="isn't installed with its memory limits"):
        cli.start(ctx, "a", ["true"], report=True)
    assert "zab service install" in cli.SLICE_MISSING
    assert "ask whoever runs this machine" in cli.SLICE_MISSING  # for a person, not the agent


def test_another_sessions_listing_leaves_a_job_unseen(ctx, capsys):
    """Review round 1 on #932, B1: `seen` is what the job's own agent saw; another session's
    `list --all`, or a terminal's, must not cancel its end post and report turn."""
    _, _, job = started(ctx, capsys)
    jobs.write_json(job.directory / "ended.json", {"SERVICE_RESULT": "success",
                    "EXIT_CODE": "exited", "EXIT_STATUS": "0", "at": 1010.0})  # fmt: skip
    ctx.runner.present.clear()
    for env in ({**ctx.env, "ZAB_SESSION": S2}, {"PATH": "/usr/bin"}):
        ctx.env = env
        assert cli.list_jobs(ctx, everywhere=True) == 0
        assert "finished: exit 0" in capsys.readouterr().out and not job.seen
    ctx.env = {**ctx.env, "ZAB_SESSION": S1}
    assert cli.list_jobs(ctx, everywhere=True) == 0 and job.seen


def test_systemd_run_reaches_the_manager_without_the_callers_runtime_dir(ctx, capsys):
    """Review round 1 on #932, N4: as from a Codex exec session (#655 V1)."""
    assert "XDG_RUNTIME_DIR" not in ctx.env
    started(ctx, capsys)
    (env,) = [e for c, e in zip(ctx.runner.calls, ctx.runner.envs, strict=True)
              if c[0] == "systemd-run"]  # fmt: skip
    assert env["XDG_RUNTIME_DIR"] == f"/run/user/{os.getuid()}"


def test_two_starts_at_once_count_each_other(ctx, capsys):
    """Review round 1 on #932, N4: the count and the start run under one lock, so that two
    starts at once can't both take the machine's last place."""
    ctx.settings.max_running = 1
    ctx.runner.gate = threading.Event()
    results: dict[str, object] = {}

    def start(name: str) -> None:
        try:
            results[name] = cli.start(ctx, name, ["true"], report=True)
        except cli.Refused as exc:
            results[name] = str(exc)

    first = threading.Thread(target=start, args=("first",))
    first.start()
    deadline = time.monotonic() + 5
    while not any(c[0] == "systemd-run" for c in ctx.runner.calls):  # first holds the lock
        assert time.monotonic() < deadline
        time.sleep(0.01)
    second = threading.Thread(target=start, args=("second",))
    second.start()
    second.join(0.3)
    assert second.is_alive()  # waiting for the lock
    ctx.runner.gate.set()
    first.join(5)
    second.join(5)
    capsys.readouterr()
    assert results["first"] == 0
    assert "1 background jobs already run on this machine" in str(results["second"])


def test_the_inside_a_job_check_reads_whole_components(ctx):
    """Review round 1 on #932, N4: a cgroup that holds the slice's name inside a component,
    not as one, isn't a job's."""
    ctx.cgroup = "/user.slice/user-1001.slice/user@1001.service/app.slice/x-zab-jobs.slice.d"
    assert cli.start(ctx, "a", ["true"], report=True) == 0


def test_log_and_status_print_a_bounded_end_without_terminal_escapes(ctx, capsys):
    """Review round 1 on #932, N1, N2, N4: no control character but newlines and tabs; `log`
    at most 256 KiB, `status` 16 KiB, saying how to see more."""
    _, _, job = started(ctx, capsys)
    job.log.write_bytes(b"x" * (600 * 1024) + b"\n\x1b]0;pwned\x07\x1b[2J\tred\x9bok\n")
    assert cli.log(ctx, "search", 5) == 0
    out = capsys.readouterr().out
    assert len(out) <= cli.LOG_BYTES + 2 and "\x1b" not in out and "\x07" not in out
    assert "\ufffd]0;pwned\ufffd\ufffd[2J\tred\ufffdok" in out
    assert cli.status(ctx, "search") == 0
    out = capsys.readouterr().out
    assert len(out) < cli.STATUS_BYTES + 2048 and "`zab job log search -n N` for more" in out


def test_an_id_already_taken_is_refused_and_the_other_job_kept(ctx, capsys, monkeypatch):
    """Review round 1 on #932, N5: never `rmtree` another job's directory."""
    _, _, other = started(ctx, capsys, "first")
    monkeypatch.setattr(cli.secrets, "token_hex", lambda n: other.id)
    with pytest.raises(cli.Refused, match="exists already"):
        cli.start(ctx, "second", ["true"], report=True)
    assert jobs.Job.load(other.directory) == other


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
def test_a_jobs_directory_that_exists_but_cant_be_written_is_refused(ctx):
    """Review round 1 on #932, N6: the "can't write" refusal, not a traceback at the lock."""
    ctx.settings.jobs.mkdir(parents=True)
    ctx.settings.jobs.chmod(0o500)
    try:
        with pytest.raises(cli.Refused, match="can't write"):
            cli.start(ctx, "a", ["true"], report=True)
    finally:
        ctx.settings.jobs.chmod(0o700)


class Growing(io.BytesIO):
    """A log the job still appends to: its end, when sought, is short of what a read finds."""

    def seek(self, offset: int, whence: int = 0) -> int:
        if whence == io.SEEK_END:
            return super().seek(100)  # the size when sought
        return super().seek(offset, whence)


def test_tail_reads_at_most_its_bound_from_a_growing_log():
    """Codex round 2 on #932: the bound holds for a log that grows between the seek and the
    read; before, an unbounded read took all that came since."""
    log = SimpleNamespace(open=lambda mode: Growing(b"y" * (1 << 20)))
    out = cli.tail(log, 10_000, limit=16 * 1024)
    assert len(out) == 16 * 1024


def test_tail_reads_carriage_returns_as_line_ends():
    """#932's review, N1: CRLF lines don't end in U+FFFD, and a progress bar's CR splits it
    into lines that `-n` counts."""
    log = SimpleNamespace(open=lambda mode: io.BytesIO(b"a\r\nb\r\n10%\r50%\r100%\n"))
    assert cli.tail(log, 10) == "a\nb\n10%\n50%\n100%"
    assert cli.tail(log, 2) == "50%\n100%"


@pytest.mark.parametrize("bot", ["b` @**all**", "a b", "x/y", "é"])
def test_a_zab_bot_that_isnt_a_bots_name_is_no_bot(ctx, capsys, bot):
    """#932's re-check, N2: recorded as it came, `ZAB_BOT` would make the record one that
    `Job.typed()` refuses, and the job would never run; it is no bot's instead, unattached."""
    ctx.env = {**ctx.env, "ZAB_BOT": bot}
    code, out, job = started(ctx, capsys)
    assert code == 0 and job is not None and job.bot == "" and job.attached is None
    assert "This job is unattached (no bot of zab's started it)" in out
    assert jobs.Job.load(job.directory) == job
