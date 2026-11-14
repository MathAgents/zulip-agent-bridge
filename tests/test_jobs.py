"""`zab.jobs`: a background job's record, how it ended, and its unit (SPEC §7.14, #655)."""

from __future__ import annotations

import json
import stat

import pytest

from zab import jobs
from zab.tools.common import RunResult


def make(tmp_path, **changes) -> jobs.Job:
    fields = {"id": "0123456789ab", "name": "search", "argv": ["python3", "s.py"], "cwd": "/w"}
    job = jobs.Job(
        **{**fields, **changes}, directory=tmp_path / (changes.get("id") or fields["id"])
    )
    job.directory.mkdir()
    job.save()
    return job


def test_a_jobs_environment_has_no_credentials():
    env = {
        "PATH": "/usr/bin", "HOME": "/home/a", "ANTHROPIC_API_KEY": "x", "CLAUDECODE": "1",
        "GH_TOKEN": "x", "MY_SECRET_FILE": "x", "db_password": "x", "SSH_AUTH_SOCK": "/s",
        "AWS_SECRET_ACCESS_KEY": "x", "ZAB_SESSION": "7",
    }  # fmt: skip
    assert jobs.scrubbed(env) == {
        "PATH": "/usr/bin", "HOME": "/home/a", "SSH_AUTH_SOCK": "/s", "ZAB_SESSION": "7",
    }  # fmt: skip


SESSION = "1b4e28ba-2fa1-41d2-883f-0016d3cca427"
THREAD = "019a5e2c-7d41-7a33-9c1e-5f2b8e4d1a60"


def test_a_record_is_saved_private_and_read_back(tmp_path):
    job = make(tmp_path, env={"A": "1"}, session=SESSION, bot="claude-bot", attached=SESSION)
    path = job.directory / "job.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["schema"] == 1
    assert jobs.Job.load(job.directory) == job
    data = json.loads(path.read_text())
    jobs.write_json(path, {**data, "attached": {"not": "an id"}})
    assert jobs.Job.load(job.directory) is None


@pytest.mark.parametrize(
    ("value", "session"),
    [(SESSION, SESSION), ("7", ""), (SESSION.upper(), ""), (SESSION + "\n", ""),
     (SESSION.replace("-41d2-", "-11d2-"), ""), ("../" + SESSION, "")],
)  # fmt: skip
def test_only_a_session_id_is_a_session(value, session):
    """Any agent can set `ZAB_SESSION`: only a uuid4 as the core writes it counts."""
    assert jobs.session_of({"ZAB_SESSION": value}) == session


def test_only_a_thread_id_is_a_thread():
    assert jobs.thread_of({"CODEX_THREAD_ID": THREAD}) == THREAD
    assert jobs.thread_of({"CODEX_THREAD_ID": "x; rm -rf /"}) == jobs.thread_of({}) == ""


@pytest.mark.parametrize(
    "change",
    [{"started_at": "x"}, {"argv": []}, {"argv": ["a", 5]}, {"cwd": 5}, {"env": "no"},
     {"env": {"A": 1}}, {"max_hours": "12"}, {"report": "yes"}, {"attached": 7},
     {"name": None},
     # review of #940, B1: numbers are finite (NaN and Infinity load from JSON), the cap positive
     {"started_at": float("nan")}, {"started_at": float("inf")}, {"max_hours": float("nan")},
     {"started_at": 10**400}, {"max_hours": 10**400},
     {"max_hours": float("-inf")}, {"max_hours": 0}, {"started_at": True},
     # B2: identifiers have their forms, so that no markup reaches a post
     {"name": "x` @**all** `y"}, {"name": "Rank"}, {"name": ""}, {"session": "7"},
     {"attached": "x` @**all**"}, {"attached": ""}, {"codex_thread": "@**all**"},
     {"bot": "b` @**all**"}, {"boot_id": "x\n@**all**"}],
)  # fmt: skip
def test_a_record_with_a_wrong_type_is_no_job(tmp_path, change, caplog, monkeypatch):
    """Review round 1 on #928, N6: and `all_jobs` leaves it out, logged once (review round 2:
    not at every pass of the watcher), beside a good job, never raising in its sort."""
    monkeypatch.setattr(jobs, "_WARNED", set())
    bad = make(tmp_path, id="0123456789ab")
    data = json.loads((bad.directory / "job.json").read_text())
    jobs.write_json(bad.directory / "job.json", {**data, **change})
    good = make(tmp_path, id="fedcba987654", started_at=5.0)
    assert jobs.Job.load(bad.directory) is None
    assert [j.id for j in jobs.all_jobs(tmp_path)] == [good.id]
    assert "0123456789ab" in caplog.text
    caplog.clear()
    assert [j.id for j in jobs.all_jobs(tmp_path)] == [good.id]
    assert "0123456789ab" not in caplog.text


def test_concurrent_writers_never_leave_half_a_file(tmp_path):
    """Review round 1 on #928, N1: `!stop` and `zab job stop` may write `stopped.json` at
    once; each write has a temporary file of its own."""
    import threading

    path = tmp_path / "stopped.json"
    errors = []

    def writer(by: str) -> None:
        try:
            for i in range(200):
                jobs.write_json(path, {"by": by, "at": float(i) + 0.123456})
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(by,)) for by in ("agent", "person")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors and jobs.read_json(path)["by"] in ("agent", "person")
    assert [p.name for p in tmp_path.iterdir()] == ["stopped.json"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_later_schemas_keys_are_left_out_and_strangers_ignored(tmp_path):
    job = make(tmp_path)
    data = json.loads((job.directory / "job.json").read_text())
    jobs.write_json(job.directory / "job.json", {**data, "schema": 2, "new_key": [1]})
    assert jobs.Job.load(job.directory).name == "search"
    other = tmp_path / "fedcba987654"
    other.mkdir()
    jobs.write_json(other / "job.json", {**data})  # its id is another directory's
    (tmp_path / "notes").mkdir()
    assert jobs.Job.load(other) is None
    assert [j.id for j in jobs.all_jobs(tmp_path)] == [job.id]


def ended(job, result, code, status, **more):
    jobs.write_json(
        job.directory / "ended.json",
        {"SERVICE_RESULT": result, "EXIT_CODE": code, "EXIT_STATUS": status, "at": 1.0, **more},
    )


@pytest.mark.parametrize(
    ("result", "code", "status", "kind", "words"),
    [  # what #655's probe saw systemd 255 report for each ending
        ("success", "exited", "0", "finished", "finished: exit 0"),
        ("exit-code", "exited", "1", "failed", "failed: exit 1"),
        ("signal", "killed", "KILL", "signal", "was killed by signal SIGKILL"),
        ("timeout", "killed", "TERM", "limit", "reached its 12-hour limit and was stopped"),
        ("oom-kill", "killed", "KILL", "oom", "ran out of memory and was stopped"),
        ("success", "killed", "TERM", "signal", "was killed by signal SIGTERM"),
        ("core-dump", "dumped", "SEGV", "signal", "was killed by signal SIGSEGV"),
        ("resources", "", "", "unknown", "ended, but zab couldn't tell how"),
    ],
)
def test_each_ending_in_zabs_words(tmp_path, result, code, status, kind, words):
    job = make(tmp_path)
    ended(job, result, code, status)
    end = jobs.ending(job, running=False, boot="b")
    assert (end.kind, end.words, end.ok) == (kind, words, kind == "finished")


def test_a_stop_is_known_by_its_record_not_by_the_result(tmp_path):
    """A stop that needed SIGKILL reads `timeout`, as the cap does (#655 V3): `_ended`'s copy
    of `stopped.json` tells them apart, and is enough on its own."""
    job = make(tmp_path)
    ended(job, "timeout", "killed", "KILL", stopped={"by": "person", "at": 1.0})
    assert not (job.directory / "stopped.json").exists()
    assert jobs.ending(job, running=False, boot="b").kind == "stopped"


def test_a_stop_asked_while_ended_runs_doesnt_take_the_jobs_ending(tmp_path):
    """Review round 1 on #928, N3: once `ended.json` exists, only its own copy counts; a
    `stopped.json` written later found the job already ending by itself."""
    job = make(tmp_path)
    ended(job, "exit-code", "exited", "3", stopped=None)
    jobs.write_json(job.directory / "stopped.json", {"by": "agent", "at": 2.0})
    assert jobs.ending(job, running=False, boot="b").words == "failed: exit 3"


def test_a_job_cut_off_by_the_vm(tmp_path):
    job = make(tmp_path, boot_id="before")
    assert jobs.ending(job, running=True, boot="before") is None
    assert jobs.ending(job, running=False, boot="after").kind == "lost"
    assert jobs.ending(job, running=False, boot="before").kind == "unknown"
    ended(job, "success", "killed", "TERM", manager_state="stopping")  # a clean shutdown
    assert jobs.ending(job, running=False, boot="after").words == "was cut off when the VM stopped"


@pytest.mark.parametrize(
    ("result", "code", "status"),
    [("timeout", "killed", "KILL"), ("oom-kill", "killed", "KILL"), ("signal", "killed", "TERM")],
)
def test_an_end_while_the_manager_stops_is_a_cut_off_whatever_systemd_says(
    tmp_path, result, code, status
):
    """Review round 1 on #928, B1: a shutdown stops a job that ignores SIGTERM past its
    `TimeoutStopSec` as `killed/KILL/timeout`, like the cap; the manager's `stopping` comes
    first, so that no report turn starts for it at the next boot."""
    job = make(tmp_path, boot_id="before")
    ended(job, result, code, status, manager_state="stopping")
    assert jobs.ending(job, running=False, boot="after").kind == "lost"


@pytest.mark.parametrize(
    ("code", "status"),
    [("killed", "@**all** see https://x"), ("killed", "ÉVERYONE"), ("killed", "a" * 40),
     ("exited", "\u0663"), ("killed", "KILL\n@**all**")],
)  # fmt: skip
def test_only_zabs_own_words_from_exit_status(tmp_path, code, status):
    """Review round 1 on #928, N2: `ended.json` is in a directory the agent can write, and the
    words go to Zulip; only ASCII `[A-Z0-9]` is repeated, anything else reads unknown."""
    job = make(tmp_path)
    ended(job, "signal", code, status)
    end = jobs.ending(job, running=False, boot="b")
    assert end.kind == "unknown" and "@" not in end.words


def test_a_job_just_written_isnt_ended_before_its_unit_starts(tmp_path):
    """`zab job start` writes `job.json` just before `systemd-run`: no unit and no `ended.json`
    means ended only once `UNSEEN_GRACE_S` have passed."""
    job = make(tmp_path, boot_id="b", started_at=1000.0)
    assert jobs.ending(job, running=False, boot="b", now=1010.0) is None
    assert jobs.ending(job, running=False, boot="b", now=1061.0).kind == "unknown"


def test_the_cap_and_a_jobs_length_in_words(tmp_path):
    assert jobs.limit_words(12) == "12-hour" and jobs.limit_words(0.05) == "3-minute"
    assert [jobs.span(s) for s in (5, 61, 3600, 8000)] == ["5 s", "1 min", "1 h", "2 h 13 min"]


def test_the_unit_is_built_from_zabs_own_values():
    argv = jobs.unit_argv("/home/a/.local/bin/zab", "0123456789ab", 12, jobs.Path("/s/jobs"))
    assert argv[:6] == [
        "systemd-run", "--user", "--unit=zab-job-0123456789ab", "--slice=zab-jobs.slice",
        "--collect", "--quiet",
    ]  # fmt: skip
    assert "--property=RuntimeMaxSec=43200" in argv
    assert not any(a.startswith("--setenv") for a in argv)  # the directory is an argument
    zab, job = "/home/a/.local/bin/zab", "/s/jobs/0123456789ab"
    assert f"--property=ExecStopPost={zab} job _ended {job}" in argv
    assert argv[-5:] == ["--", zab, "job", "_exec", job]
    for hours, seconds in ((0.05, "180"), (1e-9, "1"), (0.0001, "1"), (2.5, "9000")):
        # a whole number of seconds, at least 1: `[jobs] max_hours` is a float (#930's review)
        argv = jobs.unit_argv("/z", "0123456789ab", hours, jobs.Path("/j"))
        assert f"--property=RuntimeMaxSec={seconds}" in argv
    for zab, job_id, where in [
        ("/a b/zab", "0123456789ab", "/j"), ("/z$HOME", "0123456789ab", "/j"),
        ("/z", "0123456789ab", "/j/%h"), ("/z", "../../etc", "/j"), ("zab", "0123456789ab", "/j"),
        ("/z", "0123456789ab", "/s/x/../jobs"), ("/a/../zab", "0123456789ab", "/j"),
    ]:  # fmt: skip
        with pytest.raises(ValueError):
            jobs.unit_argv(zab, job_id, 12, jobs.Path(where))


class Recorder:
    def __init__(self, *results: RunResult) -> None:
        self.calls, self.results = [], list(results)

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        return self.results.pop(0) if self.results else RunResult(0, "", "")


def test_the_units_present_and_a_manager_that_doesnt_answer():
    listing = (
        "zab-job-0123456789ab.service loaded active running /home/a/.local/bin/zab job _exec\n"
        "zab-job-ba9876543210.service loaded deactivating stop-post zab\n"
        "zab-job-aaaaaaaaaaaa.service loaded failed failed zab\n"
    )
    runner = Recorder(RunResult(0, listing, ""), RunResult(1, "", "Failed to connect to bus"))
    assert jobs.present_units(runner) == {
        "zab-job-0123456789ab.service", "zab-job-ba9876543210.service",
    }  # fmt: skip
    assert runner.calls[0][1]["env"]["XDG_RUNTIME_DIR"]
    assert jobs.present_units(runner) is None


def test_a_stop_records_who_first(tmp_path):
    job = make(tmp_path)
    seen = []
    runner = Recorder()

    def recording(argv, **kwargs):
        seen.append((job.directory / "stopped.json").exists())
        return runner(argv, **kwargs)

    jobs.stop(job, "agent", recording)
    jobs.stop(job, "person", recording, block=False)
    assert seen == [True, True] and job.stopped()["by"] == "person"
    assert [argv for argv, _ in runner.calls] == [
        ["systemctl", "--user", "stop", "zab-job-0123456789ab.service"],
        ["systemctl", "--user", "stop", "--no-block", "zab-job-0123456789ab.service"],
    ]


def test_a_failed_stop_takes_its_record_back(tmp_path):
    """Codex round 1 on #928: a unit no longer loaded can't be stopped; the job ended by
    itself, and `ended.json` says how, not `stopped.json`."""
    job = make(tmp_path)
    ended(job, "exit-code", "exited", "3")
    refused = Recorder(RunResult(5, "", "Unit zab-job-0123456789ab.service not loaded."))
    assert not jobs.stop(job, "agent", refused).ok
    assert job.stopped() is None
    assert jobs.ending(job, running=False, boot="b").words == "failed: exit 3"


def test_an_end_whose_time_isnt_a_number_still_counts_without_one(tmp_path):
    """Review of #940, B1: `ended.json`'s `at` must be a finite number; otherwise the ending
    stands, with no time, and nothing computes with it."""
    job = make(tmp_path)
    for at in (float("nan"), float("inf"), "soon", True, 10**400):
        ended(job, "success", "exited", "0", at=at)
        assert "at" not in job.ended()
        assert jobs.ending(job, running=False, boot="b").words == "finished: exit 0"
    ended(job, "success", "exited", "0", at=12.5)
    assert job.ended()["at"] == 12.5


@pytest.mark.parametrize("bot", ["_claude", "-x", "c" * 100, "claude-alice", "B_2"])
def test_every_bot_name_the_config_accepts_is_a_jobs(tmp_path, bot):
    """Codex round 6 on #928: the record takes the config's own pattern for a bot's name
    (`zab.config.BOT_NAME_RE`), a leading underscore or dash and any length included."""
    from zab.config import BOT_NAME_RE

    assert BOT_NAME_RE.fullmatch(bot)
    job = make(tmp_path, bot=bot)
    assert jobs.Job.load(job.directory) == job
