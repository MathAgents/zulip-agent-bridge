"""`!stop NAME`, `!stop jobs` and what `!stop` says of background jobs (SPEC §4.6, §7.14; #655).

The jobs come through the bridge's `JobControl`; here a fake, which keeps each session's
running jobs and records the stops it was asked for."""

import pytest
from test_bridge import bridge as bridge
from test_bridge import user_message

from zab.core import types as t
from zab.core.commands import NO_TURN, UNKNOWN
from zab.core.jobcontrol import NO_JOBS, RunningJob, keeps_running, span, stop_target, stopped

pytestmark = pytest.mark.integration
NOW = 1_800_000_000.0


class FakeJobs:
    """Each session's running jobs; `stop` takes a job off and says how long it ran, unless
    its name is in `stubborn`."""

    def __init__(self) -> None:
        self.jobs: dict[str, list[RunningJob]] = {}
        self.stops: list[tuple[str, str]] = []
        self.stubborn: set[str] = set()

    def add(self, session: str, name: str, minutes: float) -> RunningJob:
        job = RunningJob(f"{len(self.stops):04d}{name[:8]:0<8}", name, NOW - minutes * 60)
        self.jobs.setdefault(session, []).append(job)
        return job

    def running(self, session_id: str) -> list[RunningJob]:
        return list(self.jobs.get(session_id, []))

    async def unrecorded(self, session_id: str) -> list[RunningJob]:
        return []  # every job here has its row

    async def stop(self, session_id: str, job: RunningJob) -> float | None:
        self.stops.append((session_id, job.name))
        if job.name in self.stubborn:
            return None
        self.jobs[session_id].remove(job)
        return NOW - job.started_at


def last(fake) -> str:
    return list(fake.messages.values())[-1]["content"]


async def idle_session(b, fake, backend, jobs: FakeJobs):
    await user_message(b, fake, "@**Test Bot** begin")
    actor = b.actors["native"]
    actor.jobs = jobs
    await actor.event(
        t.TurnCompleted(session_id="native", turn_id="turn1", status=t.TurnStatus.COMPLETED)
    )
    return actor


def test_the_word_after_stop():
    assert stop_target("!stop rank-search") == "rank-search"
    assert stop_target("!stop jobs") == "jobs"
    assert stop_target("!stop") is None
    assert stop_target("!stop that now") is None  # stays an unknown command, as before
    assert stop_target("!status x") is None


def test_spans_and_lines():
    assert [span(s) for s in (12, 41 * 60, 2 * 3600 + 13 * 60, 3 * 3600)] == [
        "12 s",
        "41 min",
        "2 h 13 min",
        "3 h",
    ]
    one = RunningJob("a" * 12, "rank-search", NOW)
    two = RunningJob("b" * 12, "build", NOW)
    assert keeps_running([one]) == (
        "Background job `rank-search` keeps running: `!stop rank-search` stops it, and "
        "`!stop jobs` stops all of this topic's jobs."
    )
    assert keeps_running([one, two]).startswith(
        "Background jobs `rank-search` and `build` keep running: `!stop NAME` stops one"
    )
    assert stopped([(one, 41 * 60)]) == "⏹ Stopped background job `rank-search` after 41 min."
    assert stopped([(one, 60), (two, 7200)], [RunningJob("c" * 12, "probe", NOW)]) == (
        "⏹ Stopped background jobs `rank-search` after 1 min and `build` after 2 h.\n"
        "⚠️ Couldn't stop `probe`: send the command again."
    )


async def test_a_bridge_without_jobs_answers_none_runs(bridge):
    b, fake, _ = bridge
    assert b.jobs is NO_JOBS
    await user_message(b, fake, "@**Test Bot** begin")
    assert b.actors["native"].jobs is NO_JOBS
    await user_message(b, fake, "!stop rank-search")
    assert last(fake) == "No background job `rank-search` is running here."
    await user_message(b, fake, "!stop jobs")
    assert last(fake) == "No background job is running here."


async def test_stop_name_stops_that_job_and_its_reply_is_the_end_post(bridge):
    b, fake, backend = bridge
    jobs = FakeJobs()
    actor = await idle_session(b, fake, backend, jobs)
    session = actor.row["id"]
    jobs.add(session, "rank-search", 41)
    jobs.add(session, "build", 5)
    await user_message(b, fake, "!stop rank-search")
    assert jobs.stops == [(session, "rank-search")]
    assert last(fake) == "⏹ Stopped background job `rank-search` after 41 min."
    assert "@" not in last(fake)  # no mention: the person who sent it reads the reply
    backend.interrupt.assert_not_awaited()


async def test_stop_jobs_stops_all_of_the_sessions_jobs(bridge):
    b, fake, backend = bridge
    jobs = FakeJobs()
    actor = await idle_session(b, fake, backend, jobs)
    session = actor.row["id"]
    jobs.add(session, "rank-search", 41)
    jobs.add(session, "build", 5)
    jobs.add("another-session", "elsewhere", 1)
    await user_message(b, fake, "!stop jobs")
    assert jobs.stops == [(session, "rank-search"), (session, "build")]
    assert last(fake) == (
        "⏹ Stopped background jobs `rank-search` after 41 min and `build` after 5 min."
    )
    assert jobs.running("another-session")  # never another session's


async def test_a_job_that_wont_stop_is_said(bridge):
    b, fake, backend = bridge
    jobs = FakeJobs()
    actor = await idle_session(b, fake, backend, jobs)
    jobs.add(actor.row["id"], "stuck", 3)
    jobs.stubborn.add("stuck")
    await user_message(b, fake, "!stop stuck")
    assert last(fake) == "⚠️ Couldn't stop `stuck`: send the command again."


async def test_a_name_with_no_such_job(bridge):
    b, fake, backend = bridge
    jobs = FakeJobs()
    actor = await idle_session(b, fake, backend, jobs)
    jobs.add(actor.row["id"], "build", 5)
    await user_message(b, fake, "!stop rank-search")
    assert last(fake) == "No background job `rank-search` is running here."
    await user_message(b, fake, "!stop Not`Valid")  # the person's word, as a safe code span
    assert last(fake) == "No background job `` Not`Valid `` is running here."
    assert jobs.stops == []


async def test_stop_alone_never_stops_a_job_and_names_those_that_run_on(bridge):
    b, fake, backend = bridge
    jobs = FakeJobs()
    actor = await idle_session(b, fake, backend, jobs)
    jobs.add(actor.row["id"], "rank-search", 41)
    await user_message(b, fake, "!stop")
    assert last(fake) == NO_TURN + "\n" + keeps_running(jobs.running(actor.row["id"]))
    assert jobs.stops == []


async def test_stop_during_a_turn_stops_the_turn_and_names_the_jobs(bridge):
    b, fake, backend = bridge
    jobs = FakeJobs()
    await user_message(b, fake, "@**Test Bot** begin")
    actor = b.actors["native"]
    actor.jobs = jobs
    jobs.add(actor.row["id"], "rank-search", 41)
    await user_message(b, fake, "!stop")
    backend.interrupt.assert_awaited_once()
    assert last(fake) == keeps_running(jobs.running(actor.row["id"]))
    assert jobs.stops == []
    if actor.stop_timer:
        actor.stop_timer.cancel()


async def test_stop_without_jobs_answers_as_before(bridge):
    b, fake, backend = bridge
    await idle_session(b, fake, backend, FakeJobs())
    await user_message(b, fake, "!stop")
    assert last(fake) == NO_TURN
    await user_message(b, fake, "!stop that now")
    assert last(fake) == UNKNOWN


async def test_unmentioned_each_bot_stops_its_own_and_one_answers_for_none(bridge):
    """§4.6: `!stop NAME` without a mention reaches each bot. The bot with the job stops it and
    says so; one without says nothing, except that where no bot of the topic has it, the bot
    that answers for the topic (`Delivery.first`) says that none runs."""
    from test_unmentioned import idle, replies, session_of

    b, fake, backend = bridge
    mark = await idle(b, fake, backend)  # Claude · X is another bot of the VM
    actor = b.actors["native"]
    jobs = FakeJobs()
    actor.jobs = jobs
    jobs.add(actor.row["id"], "build", 5)
    sent = [await user_message(b, fake, "!stop build")]
    assert replies(fake, mark, sent) == ["⏹ Stopped background job `build` after 5 min."]
    sent.append(await user_message(b, fake, "!stop nothing"))  # no other session here
    assert replies(fake, mark, sent)[-1] == "No background job `nothing` is running here."
    session_of(b, "claude", card=51)  # Claude has a session too, and comes first by name
    jobs.add("claude-topic", "search", 1)
    for text in ("!stop search", "!stop nothing", "!stop jobs"):  # Claude's, or its answer
        sent.append(await user_message(b, fake, text))
    assert len(replies(fake, mark, sent)) == 2
    assert jobs.stops == [(actor.row["id"], "build")]
    sent.append(await user_message(b, fake, "@**Test Bot** !stop nothing"))  # named: it answers
    assert replies(fake, mark, sent)[-1] == "No background job `nothing` is running here."


@pytest.mark.parametrize("order", ["stopped first", "still running"])
async def test_the_answer_for_the_topic_waits_for_another_bots_stop(bridge, order):
    """Codex round 1 on #934: an unmentioned `!stop build` reaches each bot, and each handles it
    in its own actor, in either order. Here this bot answers for the topic and has no job; the
    other bot's session has `build`. Whether the other bot stopped it already (its record of
    the message written first, then the job gone) or hasn't yet (the job still running), this
    bot says nothing: never "none runs" after "Stopped"."""
    from test_unmentioned import idle, replies, session_of

    from zab.core.session import JOB_STOP, Delivery

    b, fake, backend = bridge
    mark = await idle(b, fake, backend)
    actor = b.actors["native"]
    jobs = FakeJobs()
    actor.jobs = jobs
    session_of(b, "claude", card=51)
    job = jobs.add("claude-topic", "build", 5)
    mid = 9001
    if order == "stopped first":
        b.store.set(f"{JOB_STOP}{mid}", "claude")  # as the other bot's stop_jobs does, first
        jobs.jobs["claude-topic"].remove(job)
    await actor.command(
        "!stop build", delivery=Delivery(mid, 20, "Owner", "!stop build", quiet=True, first=True)
    )
    assert replies(fake, mark, []) == []
    # A message no bot acted on still gets its one answer.
    jobs.jobs["claude-topic"] = []
    await actor.command(
        "!stop build", delivery=Delivery(9002, 20, "Owner", "!stop build", quiet=True, first=True)
    )
    assert replies(fake, mark, []) == ["No background job `build` is running here."]


async def test_a_bot_that_stops_a_job_records_the_message_first(bridge):
    from zab.core.session import JOB_STOP

    b, fake, backend = bridge
    jobs = FakeJobs()
    actor = await idle_session(b, fake, backend, jobs)
    jobs.add(actor.row["id"], "build", 5)
    seen = []
    stop = jobs.stop

    async def stop_and_look(session_id, job):
        command = max(fake.messages)  # the `!stop build` being handled: no reply yet
        seen.append(b.store.get(f"{JOB_STOP}{command}"))
        return await stop(session_id, job)

    jobs.stop = stop_and_look
    await user_message(b, fake, "!stop build")
    assert seen == ["bot"]  # written before the stop was asked for
