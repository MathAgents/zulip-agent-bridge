"""Background jobs in the topic and DM commands, with the watcher as their `JobControl` (SPEC
§4.6, §7.14, §13.5; #655 PR 6b): topic `!status`'s job lines, DM `!status`'s count, `!stop
jobs` stopping concurrently, and #934's commands against the real `JobWatcher`."""

from __future__ import annotations

import asyncio
import time

import pytest
from test_bridge import bridge as bridge
from test_bridge import user_message
from test_jobwatch import NOW, Systemd, ended, job

from zab import jobs as zjobs
from zab.core import jobwatch as jw
from zab.core import types as t
from zab.core.jobcontrol import NO_JOBS, RunningJob, count_text, status_lines

pytestmark = pytest.mark.integration
A, B, C, D = "aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc", "dddddddddddd"


def last(fake) -> str:
    return list(fake.messages.values())[-1]["content"]


async def idle(b, fake):
    await user_message(b, fake, "@**Test Bot** begin")
    actor = b.actors["native"]
    await actor.event(
        t.TurnCompleted(session_id="native", turn_id="turn1", status=t.TurnStatus.COMPLETED)
    )
    return actor


def row(job_id, name, state, started, ended_at=None, result=None):
    return dict(
        id=job_id, name=name, state=state, started_at=started, ended_at=ended_at, result=result
    )


# ------------------------------------------------------------------------------ the words


def test_status_lines_in_the_end_posts_words(tmp_path):
    directory = zjobs.jobs_dir(tmp_path / "state")
    finished = job(tmp_path, A, "build", started=NOW - 2460)
    ended(finished, NOW)
    failed = job(tmp_path, B, "probe", started=NOW - 60)
    ended(failed, NOW, result="exit-code", status="1")
    stopped = job(tmp_path, C, "search", started=NOW - 600)
    ended(stopped, NOW, result="success", code="killed", status="TERM")
    zjobs.write_json(stopped.directory / "stopped.json", {"by": "!stop", "at": NOW})
    rows = [
        row(D, "rank-search", "running", NOW - 7980),
        row(A, "build", "ended", NOW - 2460, NOW, "finished"),
        row(B, "probe", "ended", NOW - 60, NOW, "failed"),
        row(C, "search", "ended", NOW - 600, NOW, "stopped"),
        row("eeeeeeeeeeee", "gone", "lost", NOW - 100, NOW, "lost"),  # its directory removed
    ]
    lines = status_lines(rows, directory, boot="boot-1")
    at = "<time:2027-01-15T08:00:00+00:00>"
    assert lines == [
        f"⚙️ Job `rank-search` · running since <time:2027-01-15T05:47:00+00:00> · log "
        f"`{directory / D / 'output.log'}`",
        f"⚙️ Job `build` · finished: exit 0 after 41 min · ended {at}",
        f"⚙️ Job `probe` · failed: exit 1 after 1 min · ended {at}",
        f"⚙️ Job `search` · was stopped after 10 min · ended {at}",
        f"⚙️ Job `gone` · was cut off when the VM stopped · ended {at}",
    ]
    assert status_lines(rows[:1], None, boot="boot-1") == [
        "⚙️ Job `rank-search` · running since <time:2027-01-15T05:47:00+00:00>"
    ]


def test_counts():
    assert [count_text(n) for n in (0, 1, 2)] == ["", "1 job running", "2 jobs running"]


# ------------------------------------------------------------------- the commands, wired


class Jobs:
    """A `JobControl` whose stops wait until released, to see them run together."""

    def __init__(self, directory=None) -> None:
        self.directory = directory
        self.jobs: dict[str, list[RunningJob]] = {}
        self.rows: dict[str, list[dict]] = {}
        self.active = self.most = 0
        self.release = asyncio.Event()

    def running(self, session_id):
        return list(self.jobs.get(session_id, []))

    async def unrecorded(self, session_id):
        return []  # every job here has its row

    def jobs_of(self, session_id, since):
        return [r for r in self.rows.get(session_id, []) if (r["ended_at"] or NOW) >= since]

    async def stop(self, session_id, job):
        self.active += 1
        self.most = max(self.most, self.active)
        await self.release.wait()
        self.active -= 1
        self.jobs[session_id].remove(job)
        return 60.0


async def test_stop_jobs_stops_them_together(bridge):
    """#934's N5: a stop waits up to 15 s for a job that ignores SIGTERM, inside the session's
    actor; `!stop jobs` stops a session's jobs at once, a few at a time, not one after another."""
    b, fake, _ = bridge
    actor = await idle(b, fake)
    jobs = Jobs()
    actor.jobs = jobs
    names = ["a", "b", "c", "d", "e"]
    jobs.jobs[actor.row["id"]] = [RunningJob(f"{i:012x}", n, NOW) for i, n in enumerate(names)]
    task = asyncio.create_task(user_message(b, fake, "!stop jobs"))
    for _ in range(50):
        await asyncio.sleep(0)
    assert jobs.most == 4  # STOPS_AT_ONCE: the first four together, the fifth after
    jobs.release.set()
    await task
    assert last(fake).startswith("⏹ Stopped background jobs `a` after 1 min, `b` after 1 min")
    assert last(fake).endswith("and `e` after 1 min.")  # in the session's order


async def test_topic_status_lists_the_sessions_jobs(bridge, tmp_path):
    b, fake, _ = bridge
    actor = await idle(b, fake)
    directory = tmp_path / "jobs"
    jobs = Jobs(directory)
    actor.jobs = jobs
    await user_message(b, fake, "!status")
    assert "⚙️ Job" not in last(fake)  # no jobs: the line as before
    jobs.rows[actor.row["id"]] = [
        row(A, "rank-search", "running", NOW - 600),
        row(B, "old", "ended", time.time() - 4 * 86400, time.time() - 3 * 86400, "finished"),
    ]
    await user_message(b, fake, "!status")
    first, *lines = last(fake).splitlines()
    assert first.startswith("💤 Idle")
    assert lines == [
        "⚙️ Job `rank-search` · running since <time:2027-01-15T07:50:00+00:00> · log "
        f"`{directory / A / 'output.log'}`"
    ]


async def test_dm_status_gives_the_bots_running_jobs(bridge):
    b, fake, _ = bridge
    actor = await idle(b, fake)

    async def dm(text):
        mid = await b.client.send(1, "ignored", text)
        await b.handle(
            {
                "type": "message",
                "message": fake.messages[mid] | {"sender_id": 20, "type": "private"},
            }
        )
        return last(fake)

    assert b.jobs is NO_JOBS and "background" not in await dm("!status")
    # the watcher's rows, by bot (#941's review, N4): a job of this bot's session, an
    # unattached one of this bot, another bot's, and an ended one
    for job_id, bot, session, state in (
        (A, "bot", actor.row["id"], "running"),
        (B, "bot", None, "running"),
        (C, "other-bot", None, "running"),
        (D, "bot", actor.row["id"], "ended"),
    ):
        b.store.insert(
            "jobs", id=job_id, bot=bot, session_id=session, name="x", state=state,
            started_at=NOW,
        )  # fmt: skip
    assert (await dm("!status")).endswith(" · background 2 jobs running")


async def test_the_commands_drive_the_real_watcher(bridge, tmp_path):
    """#934's commands with #940's watcher as their control: `!stop NAME` writes the job's
    `stopped.json` first, stops its unit, and the watcher records the end, with no post of its
    own; `!stop` alone names the job that runs on."""
    b, fake, _ = bridge
    actor = await idle(b, fake)
    session = actor.row["id"]
    systemd = Systemd({A, B})
    watcher = jw.JobWatcher(
        tmp_path / "state", b.store, [b], runner=systemd, clock=lambda: NOW, boot=lambda: "boot-1"
    )
    actor.jobs = b.jobs = watcher
    for job_id, name in ((A, "rank-search"), (B, "build")):
        job(tmp_path, job_id, name, attached=session, bot="bot")
        b.store.insert(
            "jobs", id=job_id, bot="bot", session_id=session, name=name, state="running",
            started_at=NOW - 3550,
        )  # fmt: skip
    await user_message(b, fake, "!stop")
    assert "Background jobs `rank-search` and `build` keep running" in last(fake)
    await user_message(b, fake, "!stop build")
    assert last(fake) == "⏹ Stopped background job `build` after 59 min."
    assert ["systemctl", "--user", "stop", zjobs.unit(B)] in systemd.calls
    assert zjobs.read_json(zjobs.jobs_dir(tmp_path / "state") / B / "stopped.json")["by"] == "!stop"
    (stopped,) = b.store.rows("jobs", id=B)
    assert (stopped["state"], stopped["result"]) == ("ended", "stopped")
    assert stopped["handled_at"] is not None  # its end handled: the reply was its end post
    assert [j.name for j in watcher.running(session)] == ["rank-search"]
    await user_message(b, fake, "!status")
    assert "⚙️ Job `rank-search` · running since" in last(fake)
    assert "⚙️ Job `build` · was stopped after 59 min" in last(fake)


def test_a_busy_days_status_lists_the_newest_ended_jobs_and_counts_the_rest(tmp_path):
    """#941's review, B1: every running job, then the newest ten ended ones, then one line for
    the rest, so that a day of short jobs still fits one message."""
    rows = [row(A, "rank-search", "running", NOW - 600)]
    rows += [
        row(f"{i:012x}", f"step-{i}", "ended", NOW - 7200 + i, NOW - 7000 + i, "finished")
        for i in range(120)
    ]
    lines = status_lines(rows, None, boot="boot-1")
    assert len(lines) == 1 + 10 + 1
    assert lines[0].startswith("⚙️ Job `rank-search` · running since")
    assert [line.split("`")[1] for line in lines[1:11]] == [f"step-{i}" for i in range(110, 120)]
    assert lines[-1] == "… and 110 more ended in the last 24 hours"


async def test_a_busy_days_topic_status_still_posts(bridge):
    """The same at the actor: 120 ended jobs no longer make `!status` too long for Zulip, which
    put the session in `error` (#941's review, B1, reproduced)."""
    b, fake, _ = bridge
    actor = await idle(b, fake)
    jobs = Jobs()
    actor.jobs = jobs
    long = "n" + "-x" * 15  # a name of the pattern's 32 characters
    jobs.rows[actor.row["id"]] = [
        row(f"{i:012x}", long, "ended", time.time() - 7200 + i, time.time() - 7000 + i, "finished")
        for i in range(120)
    ]
    await user_message(b, fake, "!status")
    assert actor.row["state"] == "idle"
    text = last(fake)
    assert text.endswith("… and 110 more ended in the last 24 hours")
    assert len(text) <= b.client.limits["max_message_length"]


def test_names_and_times_from_a_hand_written_job_are_safe(tmp_path):
    """#941's review, B2 and N2: a name the pattern doesn't allow is a safe code span, and a time
    that doesn't convert is "an unknown time", in each of #941's outputs."""
    from zab.core.jobcontrol import job_entry, keeps_running, name_code, stopped, when

    bad = "x` @**all** [click](https://example.com) `y"
    safe = "`` x` @**all** [click](https://example.com) `y ``"
    assert name_code("rank-search") == "`rank-search`" and name_code(bad) == safe
    (line,) = status_lines([row(A, bad, "running", NOW)], None, boot="boot-1")
    assert line.startswith(f"⚙️ Job {safe} · running since")
    assert job_entry(bad, NOW, "memory unavailable") == (
        f"{safe} since <time:2027-01-15T08:00:00+00:00>, memory unavailable"
    )
    job = RunningJob(A, bad, NOW)
    assert keeps_running([job]).startswith(f"Background job {safe} keeps running: `!stop NAME`")
    assert stopped([(job, 60.0)]) == f"⏹ Stopped background job {safe} after 1 min."
    for nonsense in (1e300, float("nan"), float("inf")):
        assert when(nonsense) == "an unknown time"
    assert (
        job_entry("x", 1e300, "memory unavailable")
        == "`x` since an unknown time, memory unavailable"
    )


async def test_a_stop_that_raises_is_reported_with_the_others(bridge):
    """#941's review, N3: one stop raising no longer loses the others' reply, nor puts the
    session in `error`."""
    b, fake, _ = bridge
    actor = await idle(b, fake)

    class Raising(Jobs):
        async def stop(self, session_id, job):
            if job.name == "gone":
                raise OSError("its directory is gone")
            self.jobs[session_id].remove(job)
            return 60.0

    jobs = Raising()
    actor.jobs = jobs
    jobs.jobs[actor.row["id"]] = [RunningJob(A, "build", NOW), RunningJob(B, "gone", NOW)]
    await user_message(b, fake, "!stop jobs")
    assert last(fake) == (
        "⏹ Stopped background job `build` after 1 min.\n"
        "⚠️ Couldn't stop `gone`: send the command again."
    )
    assert actor.row["state"] == "idle"


def test_running_lines_are_capped_too():
    """#941's round 2, N2: the running lines have the same bound, for a very large
    `[jobs] max_running`."""
    rows = [row(f"{i:012x}", f"run-{i}", "running", NOW - 3600 + i) for i in range(15)]
    lines = status_lines(rows, None, boot="boot-1")
    assert [line.split("`")[1] for line in lines[:10]] == [f"run-{i}" for i in range(5, 15)]
    assert lines[10:] == ["… and 5 more running"]


async def test_a_job_started_a_second_ago_is_listed_and_stopped(bridge, tmp_path):
    """#935's review: in a job's first seconds, before the watcher's next pass makes its row,
    topic `!status` lists it and `!stop NAME` stops it, from `job.json`'s attachment and its
    running unit; once the pass has made the row, it is counted once; the pass then reads the
    stop as a stop, with no post of its own."""
    b, fake, _ = bridge
    actor = await idle(b, fake)
    session = actor.row["id"]
    systemd = Systemd({A})
    watcher = jw.JobWatcher(
        tmp_path / "state", b.store, [b], runner=systemd, clock=lambda: NOW,
        boot=lambda: "boot-1",
    )  # fmt: skip
    actor.jobs = b.jobs = watcher
    job(tmp_path, A, "fresh", started=NOW - 1, attached=session, bot="bot")
    job(tmp_path, B, "elsewhere", started=NOW - 1, attached="another-session", bot="bot")
    assert not b.store.rows("jobs")  # no pass yet
    assert [j.name for j in await watcher.unrecorded(session)] == ["fresh"]
    await user_message(b, fake, "!status")
    assert "⚙️ Job `fresh` · running since" in last(fake)
    assert "elsewhere" not in last(fake)
    await user_message(b, fake, "!stop")
    assert "Background job `fresh` keeps running" in last(fake)
    await user_message(b, fake, "!stop elsewhere")  # another session's: not this topic's
    assert last(fake) == "No background job `elsewhere` is running here."
    await user_message(b, fake, "!stop fresh")
    assert last(fake) == "⏹ Stopped background job `fresh` after 1 s."
    assert ["systemctl", "--user", "stop", zjobs.unit(A)] in systemd.calls
    directory = zjobs.jobs_dir(tmp_path / "state") / A
    assert zjobs.read_json(directory / "stopped.json")["by"] == "!stop"
    assert not b.store.rows("jobs")  # read-only but for the stop: the pass makes the row
    stop = zjobs.read_json(directory / "stopped.json")  # as `_ended` copies it
    ended(
        zjobs.Job.load(directory), NOW, result="success", code="killed", status="TERM", stopped=stop
    )
    posts = len(fake.messages)
    assert await watcher.pass_once() == []  # its end read as a stop: no post of its own
    (stopped,) = b.store.rows("jobs", id=A)
    assert (stopped["result"], len(fake.messages)) == ("stopped", posts)


async def test_a_fresh_job_is_counted_once_when_its_row_exists(bridge, tmp_path):
    b, fake, _ = bridge
    actor = await idle(b, fake)
    session = actor.row["id"]
    watcher = jw.JobWatcher(
        tmp_path / "state", b.store, [b], runner=Systemd({A}), clock=lambda: NOW,
        boot=lambda: "boot-1",
    )  # fmt: skip
    actor.jobs = b.jobs = watcher
    job(tmp_path, A, "fresh", started=NOW - 1, attached=session, bot="bot")
    b.store.insert(
        "jobs", id=A, bot="bot", session_id=session, name="fresh", state="running",
        started_at=NOW - 1,
    )  # fmt: skip
    assert await watcher.unrecorded(session) == []
    assert [j.name for j in await actor.session_jobs()] == ["fresh"]
    await user_message(b, fake, "!status")
    assert last(fake).count("⚙️ Job `fresh`") == 1
