"""The background jobs' watcher (SPEC §7.14, §6.2; #655 PR 4), against job directories as `zab
job` writes them, a real store, a fake systemd and a fake poster."""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from zab import jobs as zjobs
from zab.core import jobwatch as jw
from zab.core import unmentioned
from zab.core.store import Store
from zab.tools.common import RunResult
from zab.zulip.client import ZulipError
from zab.zulip.poster import ACK

NOW = 1_800_000_000.0
BOT, OTHER_BOT = "claude-alice", "codex-alice"
SESSION, OTHER = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
ALICE, BOB = 7, 8
USERS = {ALICE: {"full_name": "Alice"}, BOB: {"full_name": "Bob"}}


class Poster:
    def __init__(self, server: list[dict] | None = None) -> None:
        self.sent: list[tuple[int, str, str, int]] = []
        self.server = server if server is not None else []  # what the topic holds
        self.now = NOW  # Zulip's clock, for the messages' `timestamp`

    async def send(self, stream_id, topic, content, priority):
        self.sent.append((stream_id, topic, content, priority))
        mid = 900 + len(self.sent)
        self.server.append(
            {"id": mid, "content": content, "timestamp": int(self.now), "stream_id": stream_id,
             "topic": topic}
        )  # fmt: skip
        return mid

    async def call(self, fn):
        return await fn()


class Bridge:
    def __init__(self, bot: str = BOT, kind: str = "claude") -> None:
        self.bot = SimpleNamespace(name=bot)
        self.backend = SimpleNamespace(kind=kind)
        self.server: list[dict] = []  # the bot's messages in the topic, as Zulip has them
        self.poster = Poster(self.server)
        self.router = SimpleNamespace(user_id=42)
        self.reads: list[dict] = []
        self.users = dict(USERS)
        self.actors: dict[str, object] = {}
        self.verified: list[str] = []
        self.card_fails = self.card_gone = False
        self.unverified: set[str] = set()  # `BotBridge.unverified`: cards not yet confirmed
        self.store = None
        self.client = SimpleNamespace(request=self.request)

    async def request(self, method, path, **kw):
        if path == "messages":
            self.reads.append(kw)
            where = {term["operator"]: term["operand"] for term in kw["narrow"]}
            return {
                "messages": [
                    m for m in self.server
                    if m.get("stream_id", where["channel"]) == where["channel"]
                    and m.get("topic", where["topic"]) == where["topic"]
                ]
            }  # fmt: skip
        return {"user": {"full_name": "Carol"}}

    async def verify_card(self, session_id):
        self.verified.append(session_id)
        if self.card_fails:
            raise ConnectionError("Zulip answered 502")
        if self.card_gone:  # as `BotBridge.verify_card` does for a card that is gone
            self.store.update("sessions", {"id": session_id}, state="detached")
            self.store.set(unmentioned.card_gone(session_id), True)


class Systemd:
    """`systemctl --user list-units` and `stop` for the job units it is told run."""

    def __init__(self, running: set[str] | None = None, answers: bool = True) -> None:
        self.running, self.answers = running or set(), answers
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        if not self.answers:
            return RunResult(1, "", "Failed to connect to bus")
        if argv[:3] == ["systemctl", "--user", "list-units"]:
            lines = [f"{zjobs.unit(i)} loaded active running x" for i in sorted(self.running)]
            return RunResult(0, "\n".join(lines) + "\n", "")
        if argv[:3] == ["systemctl", "--user", "stop"]:
            job_id = argv[-1].removeprefix(zjobs.UNIT_PREFIX).removesuffix(".service")
            if job_id not in self.running:
                return RunResult(5, "", "Unit not loaded.")
            self.running.discard(job_id)
            return RunResult(0, "", "")
        raise AssertionError(argv)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "state")
    yield s
    s.close()


def session(store, sid=SESSION, bot=BOT, state="idle", topic="t", native="n1"):
    store.insert(
        "sessions", id=sid, bot=bot, backend="claude", native_id=native,
        card_message_id=abs(hash(sid)) % 10**6, stream_id=5, topic=topic, workspace="/w",
        workspace_mode="worktree", repo_root="/r", settings_json="{}", state=state,
        created_at=0, updated_at=0,
    )  # fmt: skip


def turn(store, sid=SESSION, started=NOW - 3600, ended=NOW - 3500, sender=ALICE) -> int:
    return store.insert(
        "turns", session_id=sid, trigger_msg_ids="[]", state="completed" if ended else "running",
        started_at=started, ended_at=ended, sender_id=sender,
    )  # fmt: skip


def job(
    tmp_path: Path,
    job_id: str,
    name: str = "rank",
    *,
    started=NOW - 3550,
    attached=SESSION,
    bot=BOT,
    boot="boot-1",
) -> zjobs.Job:
    directory = zjobs.jobs_dir(tmp_path / "state") / job_id
    directory.mkdir(parents=True, exist_ok=True)
    made = zjobs.Job(
        id=job_id, name=name, argv=["python3", "x.py"], cwd="/w", bot=bot, session=SESSION,
        attached=attached, started_at=started, boot_id=boot, directory=directory,
    )  # fmt: skip
    made.save()
    return made


def ended(made: zjobs.Job, at: float, result="success", code="exited", status="0", **extra):
    data = {"SERVICE_RESULT": result, "EXIT_CODE": code, "EXIT_STATUS": status, "at": at,
            "boot_id": "boot-1", "manager_state": "running", **extra}  # fmt: skip
    zjobs.write_json(made.directory / "ended.json", data)


def watcher(tmp_path, store, bridges, systemd=None, now=NOW, hook=None):
    return jw.JobWatcher(
        tmp_path / "state", store, bridges, runner=systemd or Systemd(), clock=lambda: now,
        boot=lambda: "boot-1", hook=hook,
    )  # fmt: skip


A, B, C = "aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc"


async def test_a_finished_job_is_posted_once_mentioning_its_owner(tmp_path, store):
    session(store)
    turn(store)
    made = job(tmp_path, A)
    ended(made, NOW - 100)
    bridge = Bridge()
    w = watcher(tmp_path, store, [bridge])
    batches = await w.pass_once()
    assert len(batches) == 1
    ((stream, topic, text, priority),) = bridge.poster.sent
    assert (stream, topic, priority) == (5, "t", ACK)
    assert text == (
        "🔧 **Bridge** (not Claude): @**Alice|7** background job `rank` finished: exit 0 after "
        "57 min. (job aaaaaaaaaaaa)"
    )
    assert bridge.verified == [SESSION]  # at the card's confirmed location (§4.4)
    row = store.rows("jobs", id=A)[0]
    assert (row["state"], row["result"], row["session_id"]) == ("ended", "finished", SESSION)
    assert (row["ended_at"], row["handled_at"], row["end_message_id"]) == (NOW - 100, NOW, 901)
    assert await w.pass_once() == [] and len(bridge.poster.sent) == 1  # once


@pytest.mark.parametrize(
    ("result", "code", "status", "words"),
    [
        ("exit-code", "exited", "1", "failed: exit 1 after 57 min"),
        ("signal", "killed", "KILL", "was killed by signal SIGKILL after 57 min"),
        ("oom-kill", "killed", "KILL", "ran out of memory and was stopped"),
        ("timeout", "killed", "TERM", "reached its 12-hour limit and was stopped"),
        # never free text from `ended.json` in a post (#928)
        ("exit-code", "exited", "1; see /etc", "ended, but zab couldn't tell how"),
    ],
)
async def test_other_endings_are_warnings_in_zabs_own_words(
    tmp_path, store, result, code, status, words
):
    session(store)
    turn(store)
    ended(job(tmp_path, A), NOW - 100, result, code, status)
    bridge = Bridge()
    await watcher(tmp_path, store, [bridge]).pass_once()
    text = bridge.poster.sent[0][2]
    assert text == (
        f"⚠️ **Bridge** (not Claude): @**Alice|7** background job `rank` {words}. (job {A})"
    )


async def test_a_job_of_an_earlier_boot_without_its_end_was_cut_off(tmp_path, store):
    session(store)
    turn(store)
    job(tmp_path, A, boot="boot-0")  # no ended.json, another boot: the VM stopped under it
    clean = job(tmp_path, B, name="build", boot="boot-0")
    ended(clean, NOW - 200, "success", "killed", "TERM", manager_state="stopping")
    bridge = Bridge()
    await watcher(tmp_path, store, [bridge]).pass_once()
    ((_, _, text, _),) = bridge.poster.sent  # one post for the session's two ends
    assert text == (
        "⚠️ **Bridge** (not Claude): @**Alice|7** 2 background jobs ended:\n"
        "- `rank` was cut off when the VM stopped\n- `build` was cut off when the VM stopped\n"
        f"(jobs {A}, {B})"
    )
    assert {r["state"] for r in store.rows("jobs")} == {"lost"}


async def test_no_post_for_an_unattached_or_stopped_job(tmp_path, store):
    session(store)
    session(store, OTHER, bot=OTHER_BOT, topic="u", native="n2")
    turn(store)
    turn(store, OTHER)
    ended(job(tmp_path, A, attached=None), NOW)  # unattached at its start
    ended(job(tmp_path, B, attached=OTHER), NOW)  # another bot's session: not this job's
    late = job(tmp_path, C, started=NOW - 10)  # no turn of the session ran at its start
    ended(late, NOW)
    stopped = job(tmp_path, "dddddddddddd")
    record = {"by": "agent", "at": NOW}
    zjobs.write_json(stopped.directory / "stopped.json", record)
    ended(stopped, NOW, "success", "killed", "TERM", stopped=record)  # `_ended`'s own copy
    bridge = Bridge()
    await watcher(tmp_path, store, [bridge, Bridge(OTHER_BOT, "codex")]).pass_once()
    assert bridge.poster.sent == []
    rows = {r["id"]: r for r in store.rows("jobs")}
    assert [rows[i]["session_id"] for i in (A, B, C)] == [None, None, None]
    assert all(r["handled_at"] == NOW and r["end_message_id"] is None for r in rows.values())
    assert rows["dddddddddddd"]["result"] == "stopped"


async def test_an_end_during_its_turn_waits_for_it_and_a_seen_one_isnt_posted(tmp_path, store):
    session(store)
    running = turn(store, started=NOW - 600, ended=None)
    made = job(tmp_path, A, started=NOW - 500)
    ended(made, NOW - 60)
    bridge = Bridge()
    bridge.actors["n1"] = SimpleNamespace(turn={"id": running})
    w = watcher(tmp_path, store, [bridge])
    await w.pass_once()
    assert bridge.poster.sent == [] and store.rows("jobs", id=A)[0]["handled_at"] is None
    # the turn ends: posted at the next pass
    store.update("turns", {"id": running}, state="completed", ended_at=NOW - 30)
    bridge.actors["n1"] = SimpleNamespace(turn=None)
    await w.pass_once()
    assert "background job `rank` finished" in bridge.poster.sent[0][2]
    # a job whose end the agent saw (`zab job status`) during that turn: no post
    other = job(tmp_path, B, name="quick", started=NOW - 400)
    ended(other, NOW - 300)
    other.mark_seen()
    await w.pass_once()
    assert len(bridge.poster.sent) == 1 and store.rows("jobs", id=B)[0]["handled_at"] == NOW


async def test_a_row_a_crash_left_running_holds_nothing_back(tmp_path, store):
    """The turn row says running, but the session's actor has no turn: posted."""
    session(store)
    turn(store, started=NOW - 600, ended=None)
    ended(job(tmp_path, A, started=NOW - 500), NOW - 60)
    bridge = Bridge()
    await watcher(tmp_path, store, [bridge]).pass_once()
    assert len(bridge.poster.sent) == 1


async def test_several_owners_are_each_mentioned_once(tmp_path, store):
    session(store)
    turn(store, started=NOW - 3600, ended=NOW - 3500, sender=ALICE)
    turn(store, started=NOW - 3000, ended=NOW - 2900, sender=BOB)
    ended(job(tmp_path, A, started=NOW - 3550), NOW - 100)
    ended(job(tmp_path, B, name="b", started=NOW - 2950), NOW - 100)
    ended(job(tmp_path, C, name="c", started=NOW - 3540), NOW - 100, "exit-code", "exited", "2")
    bridge = Bridge()
    await watcher(tmp_path, store, [bridge]).pass_once()
    ((_, _, text, _),) = bridge.poster.sent
    assert text.startswith("⚠️ **Bridge** (not Claude): @**Alice|7** @**Bob|8** 3 background jobs")
    assert "\n- `c` failed: exit 2 after 57 min" in text


async def test_a_detached_sessions_job_is_still_posted_in_its_topic(tmp_path, store):
    session(store, state="detached", topic="old place")
    turn(store)
    ended(job(tmp_path, A), NOW - 100)
    bridge = Bridge()
    await watcher(tmp_path, store, [bridge]).pass_once()
    assert bridge.poster.sent[0][:2] == (5, "old place")


async def test_an_owner_unknown_to_the_bridge_is_looked_up(tmp_path, store):
    session(store)
    turn(store, sender=99)
    ended(job(tmp_path, A), NOW - 100)
    bridge = Bridge()
    await watcher(tmp_path, store, [bridge]).pass_once()
    assert "@**Carol|99** background job" in bridge.poster.sent[0][2]
    assert bridge.users[99] == {"full_name": "Carol"}


async def test_a_running_job_has_a_row_and_no_end(tmp_path, store):
    session(store)
    turn(store)
    job(tmp_path, A)
    bridge = Bridge()
    w = watcher(tmp_path, store, [bridge], Systemd({A}))
    await w.pass_once()
    assert store.rows("jobs", id=A)[0]["state"] == "running" and bridge.poster.sent == []
    # no answer from the user manager: a job without its end counts as running
    w.runner = Systemd(answers=False)
    await w.pass_once()
    assert store.rows("jobs", id=A)[0]["state"] == "running"
    # its unit gone with no ended.json, in this boot, a minute after its start: unknown
    w.runner = Systemd()
    await w.pass_once()
    assert "ended, but zab couldn't tell how" in bridge.poster.sent[0][2]


async def test_systemd_is_asked_only_when_a_job_lacks_its_end(tmp_path, store):
    session(store)
    turn(store)
    ended(job(tmp_path, A), NOW - 100)
    systemd = Systemd()
    await watcher(tmp_path, store, [Bridge()], systemd).pass_once()
    assert systemd.calls == []


class Hook:
    def __init__(self) -> None:
        self.heard: list[tuple[list[str], int | None]] = []

    async def sentence(self, batch):
        return "Claude will report on it." if batch.ok else ""

    def record(self, batch, message_id):
        self.heard.append(([end.job.id for end in batch.ends], message_id))
        return lambda: None


async def test_the_hook_ends_the_post_and_hears_of_it(tmp_path, store):
    session(store)
    turn(store)
    ended(job(tmp_path, A), NOW - 100)
    bridge, hook = Bridge(), Hook()
    await watcher(tmp_path, store, [bridge], hook=hook).pass_once()
    assert bridge.poster.sent[0][2].endswith(f"after 57 min. Claude will report on it. (job {A})")
    assert hook.heard == [([A], 901)]


async def test_job_control_lists_and_stops_any_sessions_jobs(tmp_path, store):
    """#934's `JobControl`, machine-wide (its N3): one instance answers for every session."""
    session(store)
    session(store, OTHER, bot=OTHER_BOT, topic="u", native="n2")
    turn(store)
    turn(store, OTHER)
    job(tmp_path, A, started=NOW - 3550)
    job(tmp_path, B, name="early", started=NOW - 3560)
    job(tmp_path, C, name="theirs", attached=OTHER, bot=OTHER_BOT)
    systemd = Systemd({A, B, C})
    w = watcher(tmp_path, store, [Bridge(), Bridge(OTHER_BOT, "codex")], systemd)
    await w.pass_once()
    assert [j.name for j in w.running(SESSION)] == ["early", "rank"]
    assert [j.name for j in w.running(OTHER)] == ["theirs"]
    rank = w.running(SESSION)[1]
    assert await w.stop(SESSION, rank) == 3550
    assert zjobs.read_json(zjobs.jobs_dir(tmp_path / "state") / A / "stopped.json")["by"] == "!stop"
    row = store.rows("jobs", id=A)[0]
    assert (row["state"], row["result"], row["handled_at"]) == ("ended", "stopped", NOW)
    assert [j.name for j in w.running(SESSION)] == ["early"]
    # not this session's, or no longer running: nothing stopped
    assert await w.stop(SESSION, w.running(OTHER)[0]) is None
    assert await w.stop(SESSION, rank) is None
    gone = w.running(SESSION)[0]
    systemd.running.discard(B)  # ended meanwhile: the stop fails, its record goes
    assert await w.stop(SESSION, gone) is None
    assert not (zjobs.jobs_dir(tmp_path / "state") / B / "stopped.json").exists()


async def test_jobs_of_a_session_are_the_running_and_the_recently_ended(tmp_path, store):
    session(store)
    turn(store)
    job(tmp_path, A)
    ended(job(tmp_path, B, name="done"), NOW - 100)
    w = watcher(tmp_path, store, [Bridge()], Systemd({A}))
    await w.pass_once()
    assert [r["name"] for r in w.jobs_of(SESSION, NOW - 3600)] == ["rank", "done"]
    assert [r["name"] for r in w.jobs_of(SESSION, NOW)] == ["rank"]


async def test_a_handled_jobs_directory_goes_after_14_days(tmp_path, store):
    session(store)
    turn(store)
    made = job(tmp_path, A)
    ended(made, NOW - 100)
    bridge = Bridge()
    await watcher(tmp_path, store, [bridge]).pass_once()
    later = watcher(tmp_path, store, [bridge], now=NOW + jw.KEEP_S - 1)
    await later.pass_once()
    assert made.directory.exists() and store.rows("jobs", id=A)
    later = watcher(tmp_path, store, [bridge], now=NOW + jw.KEEP_S)
    await later.pass_once()
    assert not made.directory.exists() and not store.rows("jobs", id=A)
    assert len(bridge.poster.sent) == 1  # gone, never posted again


async def test_a_symlink_in_place_of_a_jobs_directory_is_never_followed(tmp_path, store):
    target = tmp_path / "precious"
    target.mkdir()
    (target / "keep").write_text("x")
    jobs = zjobs.jobs_dir(tmp_path / "state")
    jobs.mkdir(parents=True)
    (jobs / A).symlink_to(target)
    store.insert("jobs", id=A, bot=BOT, name="x", state="ended", started_at=0, handled_at=0)
    await watcher(tmp_path, store, [Bridge()]).pass_once()
    assert (target / "keep").exists() and store.rows("jobs", id=A)


async def refuse(*args):
    raise ZulipError("BAD_REQUEST", 400)


async def unknown(*args):
    raise ConnectionError("the connection dropped")


async def passes(w, until: float, every: float = 10.0) -> None:
    """The watcher's passes every `every` seconds from NOW until `until`."""
    at = NOW
    while at <= until:
        w.clock = lambda at=at: at
        await w.pass_once()
        at += every


async def test_a_refused_post_is_tried_again_backed_off_then_dropped(tmp_path, store):
    """Review of #940, N5 and its follow-up: a refused post is tried again at about 1, 3, 7, 15
    and 31 minutes, 6 tries in all, never once a pass (Zulip Cloud's limits, #754), then
    dropped within the hour, the hook told; a refused post needs no look at the topic."""
    session(store)
    turn(store)
    ended(job(tmp_path, A), NOW - 100)
    bridge, hook = Bridge(), Hook()
    tries: list[float] = []
    w = watcher(tmp_path, store, [bridge], hook=hook)

    async def counted(*args):
        tries.append(w.clock())
        await refuse()

    bridge.poster.send = counted
    await passes(w, NOW + jw.CONFIRM_S + 60)
    assert [t - NOW for t in tries] == [0, 60, 180, 420, 900, 1860]
    assert hook.heard == [([A], None)] and store.rows("jobs", id=A)[0]["handled_at"] == NOW + 1860
    assert bridge.reads == [] and store.rows("job_posts") == []


async def test_a_post_that_goes_through_on_a_later_try_is_posted_once(tmp_path, store):
    session(store)
    turn(store)
    ended(job(tmp_path, A), NOW - 100)
    bridge, hook = Bridge(), Hook()
    send = bridge.poster.send
    bridge.poster.send = refuse
    w = watcher(tmp_path, store, [bridge], hook=hook)
    await w.pass_once()
    assert hook.heard == [] and store.rows("jobs", id=A)[0]["handled_at"] is None
    # an end of the same session meanwhile waits for the post being tried
    ended(job(tmp_path, B, name="later"), NOW - 50)
    bridge.poster.send = send
    w.clock = lambda: NOW + 30
    await w.pass_once()
    assert bridge.poster.sent == []  # not due before 60 s
    w.clock = lambda: NOW + 60
    await w.pass_once()
    assert [m[2].endswith(f"(job {A})") for m in bridge.poster.sent] == [True]
    w.clock = lambda: NOW + 70
    await w.pass_once()
    assert bridge.poster.sent[1][2].endswith(f"(job {B})") and store.rows("job_posts") == []
    assert hook.heard == [([A], 901), ([B], 902)]


async def test_a_post_of_unknown_outcome_that_landed_isnt_sent_again(tmp_path, store):
    """The follow-up to N5: a send whose outcome is unknown (the connection dropped after it
    went out) is looked for in the topic, by the job ids the post carries, before the next try;
    found, it is that post, never sent twice."""
    session(store)
    turn(store)
    ended(job(tmp_path, A), NOW - 100)
    bridge, hook = Bridge(), Hook()
    send = bridge.poster.send

    async def landed_but_lost(*args):
        await send(*args)  # Zulip took it
        await unknown()

    bridge.poster.send = landed_but_lost
    w = watcher(tmp_path, store, [bridge], hook=hook)
    await w.pass_once()
    assert store.rows("jobs", id=A)[0]["handled_at"] is None
    w.clock = lambda: NOW + 60
    await w.pass_once()
    assert len(bridge.poster.sent) == 1 and hook.heard == [([A], 901)]
    assert store.rows("jobs", id=A)[0]["end_message_id"] == 901
    (read,) = bridge.reads
    assert read["narrow"][2] == {"operator": "sender", "operand": 42}
    # one that didn't land is sent again
    ended(job(tmp_path, B, name="b"), NOW - 50)
    bridge.poster.send = unknown
    w.clock = lambda: NOW + 100
    await w.pass_once()
    bridge.poster.send = send
    w.clock = lambda: NOW + 160
    await w.pass_once()
    assert bridge.poster.sent[-1][2].endswith(f"(job {B})") and len(bridge.poster.sent) == 2


async def test_the_table_is_added_to_an_existing_database(tmp_path):
    first = Store(tmp_path / "s")
    with first.transaction():
        first.db.execute("DROP TABLE jobs")
    first.close()
    again = Store(tmp_path / "s")
    try:
        again.insert("jobs", id=A, bot=BOT, name="x", state="running", started_at=time.time())
        assert again.rows("jobs", id=A)
        again.forget_job(A)
        assert not again.rows("jobs", id=A)
    finally:
        again.close()


async def test_an_end_waits_while_its_card_cant_be_confirmed(tmp_path, store):
    """Codex round 1 on #940: SPEC §4.4, never at a location this process hasn't confirmed. A
    card that can't be read leaves the end unhandled; a later try that confirms it posts."""
    session(store)
    turn(store)
    ended(job(tmp_path, A), NOW - 100)
    bridge, hook = Bridge(), Hook()
    bridge.card_fails = True
    w = watcher(tmp_path, store, [bridge], hook=hook)
    await w.pass_once()
    assert bridge.poster.sent == [] and hook.heard == []
    assert store.rows("jobs", id=A)[0]["handled_at"] is None
    bridge.card_fails = False
    w.clock = lambda: NOW + 60
    await w.pass_once()
    assert len(bridge.poster.sent) == 1 and hook.heard == [([A], 901)]
    assert store.rows("job_posts") == []


async def test_an_end_whose_card_stays_unreadable_is_dropped_within_the_hour(tmp_path, store):
    """Its tries backed off as a refused post's are: the card is read at 0, 1, 3, 7, 15 and
    31 minutes, then the end is dropped, unposted, the hook told."""
    session(store)
    turn(store)
    ended(job(tmp_path, A), NOW - 100)
    bridge, hook = Bridge(), Hook()
    bridge.card_fails = True
    w = watcher(tmp_path, store, [bridge], hook=hook)
    await passes(w, NOW + jw.CONFIRM_S + 60)
    assert len(bridge.verified) == 6
    row = store.rows("jobs", id=A)[0]
    assert (row["handled_at"], row["end_message_id"]) == (NOW + 1860, None)
    assert (
        bridge.poster.sent == [] and hook.heard == [([A], None)] and store.rows("job_posts") == []
    )


async def test_no_end_post_where_the_card_is_found_gone(tmp_path, store):
    session(store)
    turn(store)
    ended(job(tmp_path, A), NOW - 100)
    bridge = Bridge()
    bridge.store, bridge.card_gone = store, True
    await watcher(tmp_path, store, [bridge]).pass_once()
    assert bridge.poster.sent == [] and store.rows("jobs", id=A)[0]["handled_at"] == NOW


async def test_a_job_whose_card_moved_to_a_public_channel_isnt_posted(tmp_path, store):
    """Codex round 2 on #940: `verify_card` makes such a session read-only, and nothing about
    its jobs (name, end, owner) goes to that public channel: handled, no post, no report."""
    session(store)
    turn(store)
    ended(job(tmp_path, A), NOW - 100)
    bridge, hook = Bridge(), Hook()

    async def moved(session_id):
        store.update("sessions", {"id": session_id}, state="readonly")

    bridge.verify_card = moved
    await watcher(tmp_path, store, [bridge], hook=hook).pass_once()
    assert bridge.poster.sent == [] and hook.heard == [([A], None)]
    assert store.rows("jobs", id=A)[0]["handled_at"] == NOW
    # one read-only before the pass: the same
    session(store, OTHER, state="readonly", topic="public", native="n2")
    turn(store, OTHER)
    ended(job(tmp_path, B, attached=OTHER), NOW - 100)
    plain = Bridge()
    await watcher(tmp_path, store, [plain]).pass_once()
    assert plain.poster.sent == []


async def test_a_stop_asked_after_the_job_ended_leaves_its_own_end(tmp_path, store):
    """#928: only `ended.json`'s copy of `stopped.json` says the job was stopped; a record
    written after `_ended` ran stopped nothing."""
    session(store)
    turn(store)
    made = job(tmp_path, A)
    ended(made, NOW - 100)
    zjobs.write_json(made.directory / "stopped.json", {"by": "agent", "at": NOW - 50})
    bridge = Bridge()
    await watcher(tmp_path, store, [bridge]).pass_once()
    assert "background job `rank` finished: exit 0" in bridge.poster.sent[0][2]


async def test_a_bad_record_is_skipped(tmp_path, store):
    jobs = zjobs.jobs_dir(tmp_path / "state")
    (jobs / A).mkdir(parents=True)
    (jobs / A / "job.json").write_text('{"id": "aaaaaaaaaaaa", "name": 3}')
    (jobs / B).mkdir()
    (jobs / B / "job.json").write_text("not json")
    await watcher(tmp_path, store, [Bridge()]).pass_once()
    assert store.rows("jobs") == []


async def test_the_hook_sees_one_batch_object_and_lost_jobs_too(tmp_path, store):
    """For #655's report turns: the cap's decision kept on the batch between `sentence` and
    `record`; a lost job's end comes through, for its note to wait."""
    session(store)
    turn(store)
    job(tmp_path, A, boot="boot-0")  # cut off by a VM stop
    seen: list[tuple[str, int]] = []

    class Same:
        async def sentence(self, batch):
            batch.decided = True  # what a hook may keep between the two calls
            seen.append(("sentence", id(batch)))
            return ""

        def record(self, batch, message_id):
            assert batch.decided
            seen.append(("record", id(batch)))
            assert [end.ending.kind for end in batch.ends] == ["lost"]
            return lambda: None

    await watcher(tmp_path, store, [Bridge()], hook=Same()).pass_once()
    assert [kind for kind, _ in seen] == ["sentence", "record"] and seen[0][1] == seen[1][1]


async def test_two_processes_on_one_state_directory_keep_to_their_own_bots(tmp_path, store):
    """Codex round 3 on #940: the locks are per bot, so two bridge processes with different
    bots can share a state directory; neither handles, nor marks, the other's jobs."""
    session(store)
    session(store, OTHER, bot=OTHER_BOT, topic="u", native="n2")
    turn(store)
    turn(store, OTHER)
    ended(job(tmp_path, A), NOW - 100)
    ended(job(tmp_path, B, name="theirs", attached=OTHER, bot=OTHER_BOT), NOW - 100)
    mine, theirs = Bridge(), Bridge(OTHER_BOT, "codex")
    first = watcher(tmp_path, store, [mine])
    await first.pass_once()
    assert [r["id"] for r in store.rows("jobs")] == [A]  # no row, no mark for B
    assert len(mine.poster.sent) == 1
    second = watcher(tmp_path, store, [theirs])
    await second.pass_once()
    assert "background job `theirs` finished" in theirs.poster.sent[0][2]
    assert store.rows("jobs", id=B)[0]["handled_at"] == NOW
    await first.pass_once()
    await second.pass_once()
    assert len(mine.poster.sent) == len(theirs.poster.sent) == 1


async def test_a_card_left_unconfirmed_after_verify_is_retried(tmp_path, store):
    """Codex round 3 on #940: `verify_card` may return with the card still unconfirmed, its
    three tries undone by moves or a new epoch: the same retry, and the same hour."""
    session(store)
    turn(store)
    ended(job(tmp_path, A), NOW - 100)
    bridge, hook = Bridge(), Hook()
    bridge.unverified = {SESSION}
    w = watcher(tmp_path, store, [bridge], hook=hook)
    await passes(w, NOW + jw.CONFIRM_S + 60)
    assert bridge.poster.sent == [] and hook.heard == [([A], None)]
    # one confirmed on a later try is posted
    ended(job(tmp_path, B, name="next"), NOW - 50)
    w.clock = lambda: NOW + 4000
    await w.pass_once()
    bridge.unverified = set()
    w.clock = lambda: NOW + 4060
    await w.pass_once()
    assert "background job `next` finished" in bridge.poster.sent[0][2]


async def test_any_watcher_removes_a_botless_jobs_directory_14_days_after_its_end(tmp_path, store):
    """Codex round 4 on #940: a job from an ordinary shell (no `ZAB_BOT`) is no process's, so no
    watcher keeps a row or posts for it; but its directory, a log of up to 1 GiB, goes 14 days
    after its end, by whichever watcher passes first, the other finding it gone."""
    shell = job(tmp_path, A, bot="", attached=None, started=NOW - jw.KEEP_S - 3600)
    ended(shell, NOW - jw.KEEP_S + 1)  # not quite 14 days ago
    # no ended.json: ended by its cap at the latest, 12 hours after its start (N4)
    quiet = job(tmp_path, B, name="q", bot="", attached=None, started=NOW - jw.KEEP_S - 43260)
    first = watcher(tmp_path, store, [Bridge()])
    second = watcher(tmp_path, store, [Bridge(OTHER_BOT, "codex")])
    await first.pass_once()
    assert shell.directory.exists() and not quiet.directory.exists()
    assert store.rows("jobs") == []
    second.clock = first.clock = lambda: NOW + 1
    await second.pass_once()
    await first.pass_once()  # gone already: no failure
    assert not shell.directory.exists() and store.rows("jobs") == []
    young = job(tmp_path, C, name="y", bot="", attached=None, started=NOW - 600)
    ended(young, NOW - 100)
    await first.pass_once()
    assert young.directory.exists()


async def test_a_botless_jobs_removal_never_follows_a_symlink(tmp_path, store):
    target = tmp_path / "elsewhere" / A
    target.mkdir(parents=True)
    old = zjobs.Job(
        id=A, name="x", argv=["true"], cwd="/w", bot="", started_at=NOW - jw.KEEP_S - 7200,
        boot_id="boot-1", directory=target,
    )  # fmt: skip
    old.save()
    ended(old, NOW - jw.KEEP_S - 3600)
    jobs = zjobs.jobs_dir(tmp_path / "state")
    jobs.mkdir(parents=True)
    (jobs / A).symlink_to(target)
    w = watcher(tmp_path, store, [Bridge()])
    await w.pass_once()
    assert (target / "job.json").exists() and (jobs / A).is_symlink()
    assert w.remove(["../x", A]) == []  # neither a stray name nor a symlink


async def test_no_end_post_for_a_card_found_gone_before_but_one_after_a_detach(tmp_path, store):
    """Codex round 5 on #940: a card a start's check (or an earlier pass) found gone left the
    session detached, which `verify_card` no longer checks; posting at its last location could
    recreate a deleted topic. The flag that check keeps (`unmentioned.card_gone`) says so: no
    post, the hook told of none. A `!detach` keeps no flag, and its jobs' ends are posted."""
    session(store, state="detached", topic="deleted")
    store.set(unmentioned.card_gone(SESSION), True)
    turn(store)
    ended(job(tmp_path, A), NOW - 100)
    session(store, OTHER, state="detached", topic="detached by hand", native="n2")
    turn(store, OTHER)
    ended(job(tmp_path, B, name="kept", attached=OTHER), NOW - 100)
    bridge, hook = Bridge(), Hook()
    await watcher(tmp_path, store, [bridge], hook=hook).pass_once()
    ((_, topic, text, _),) = bridge.poster.sent
    assert topic == "detached by hand" and "background job `kept` finished" in text
    assert ([A], None) in hook.heard and store.rows("jobs", id=A)[0]["handled_at"] == NOW


async def test_one_jobs_trouble_skips_that_job_only(tmp_path, store, monkeypatch, caplog):
    """Review of #940, B1: a job whose end can't be read, whose row can't be made, or whose
    batch can't be posted is logged once and skipped; every other job is still handled."""
    session(store)
    session(store, OTHER, topic="u", native="n2")
    turn(store)
    turn(store, OTHER)
    ended(job(tmp_path, A, name="good"), NOW - 100)
    ended(job(tmp_path, B, name="unreadable"), NOW - 100)
    ended(job(tmp_path, C, name="rowless"), NOW - 100)
    ended(job(tmp_path, "dddddddddddd", name="unposted", attached=OTHER), NOW - 100)
    real_ending, real_row = jw.zjobs.ending, jw.JobWatcher.row

    def ending(job, **kw):
        if job.id == B:
            raise OverflowError("cannot convert float infinity to integer")
        return real_ending(job, **kw)

    def row(self, job):
        if job.id == C:
            raise ValueError("NOT NULL constraint failed: jobs.started_at")
        return real_row(self, job)

    monkeypatch.setattr(jw.zjobs, "ending", ending)
    monkeypatch.setattr(jw.JobWatcher, "row", row)

    class Picky(Hook):
        async def sentence(self, batch):
            if batch.session["id"] == OTHER:
                raise RuntimeError("the hook failed")
            return ""

    bridge = Bridge()
    w = watcher(tmp_path, store, [bridge], hook=Picky())
    await w.pass_once()
    await w.pass_once()
    sent = [m[2] for m in bridge.poster.sent]
    assert len(sent) == 1 and "background job `good` finished" in sent[0]
    skipped = [r.getMessage() for r in caplog.records if "skipped" in r.getMessage()]
    assert len(skipped) == 3  # once each, over two passes
    assert {m.split()[2] for m in skipped} == {B, C, "dddddddddddd"}


async def test_an_infinite_end_time_is_no_time(tmp_path, store):
    """B1: `ended.json`'s `at` of Infinity or NaN reads as no time, never a failure."""
    session(store)
    turn(store)
    made = job(tmp_path, A)
    ended(made, NOW - 100)
    data = zjobs.read_json(made.directory / "ended.json")
    (made.directory / "ended.json").write_text(
        __import__("json").dumps(data).replace(str(NOW - 100), "Infinity")
    )
    bridge = Bridge()
    await watcher(tmp_path, store, [bridge]).pass_once()
    assert bridge.poster.sent[0][2].endswith(f"background job `rank` finished: exit 0. (job {A})")


async def test_a_handled_lost_job_asks_systemd_no_more(tmp_path, store):
    """Review of #940, N3: a job cut off by a VM stop keeps no `ended.json` for 14 days; once
    its end is handled, no pass asks systemd about it."""
    session(store)
    turn(store)
    job(tmp_path, A, boot="boot-0")
    systemd = Systemd()
    w = watcher(tmp_path, store, [Bridge()], systemd)
    await w.pass_once()
    assert len(systemd.calls) == 1 and store.rows("jobs", id=A)[0]["handled_at"] == NOW
    await w.pass_once()
    assert len(systemd.calls) == 1


async def test_a_job_of_a_bot_no_process_runs_goes_after_14_days(tmp_path, store):
    """Review of #940, N4: retention goes by "no row of anyone's", so a job of a bot removed
    from the config is removed too; one another process keeps a row for is left to it."""
    gone = job(tmp_path, A, bot="retired-bot", attached=None, started=NOW - jw.KEEP_S - 7200)
    ended(gone, NOW - jw.KEEP_S - 3600)
    kept = job(tmp_path, B, bot=OTHER_BOT, attached=None, started=NOW - jw.KEEP_S - 7200)
    ended(kept, NOW - jw.KEEP_S - 3600)
    store.insert("jobs", id=B, bot=OTHER_BOT, name="x", state="ended", started_at=0)
    await watcher(tmp_path, store, [Bridge()]).pass_once()
    assert not gone.directory.exists() and kept.directory.exists()


async def test_a_stop_whose_directory_went_meanwhile_is_a_clean_failure(
    tmp_path, store, monkeypatch
):
    """Review of #940, N6: an `OSError` from the stop (its directory gone) is None, never an
    exception for `!stop NAME`."""
    session(store)
    turn(store)
    job(tmp_path, A)
    w = watcher(tmp_path, store, [Bridge()], Systemd({A}))
    await w.pass_once()

    def gone(*args, **kw):
        raise FileNotFoundError("stopped.json")

    monkeypatch.setattr(jw.zjobs, "stop", gone)
    assert await w.stop(SESSION, w.running(SESSION)[0]) is None
    assert store.rows("jobs", id=A)[0]["state"] == "running"


async def test_a_pending_post_whose_job_was_seen_meanwhile_holds_no_later_end(tmp_path, store):
    """Codex round 6 on #940: a job ends during its turn, its post fails after the turn, and
    `zab job` then shows the agent its end: handled with no post. Its pending post must end
    with it, or it would hold every later end of the session back for good."""
    session(store)
    running = turn(store, started=NOW - 600, ended=None)
    first = job(tmp_path, A, started=NOW - 500)
    ended(first, NOW - 400)
    bridge = Bridge()
    bridge.actors["n1"] = SimpleNamespace(turn={"id": running})
    send = bridge.poster.send
    bridge.poster.send = refuse
    w = watcher(tmp_path, store, [bridge])
    await w.pass_once()  # its turn runs: it waits
    store.update("turns", {"id": running}, state="completed", ended_at=NOW - 300)
    bridge.actors["n1"] = SimpleNamespace(turn=None)
    await w.pass_once()  # the turn ended: posted, refused
    assert bridge.poster.sent == []
    assert [(r["bot"], r["session_id"]) for r in store.rows("job_posts")] == [(BOT, SESSION)]
    first.mark_seen()  # `zab job status` showed it (an end during its turn)
    turn(store, started=NOW - 200, ended=NOW - 100)
    ended(job(tmp_path, B, name="later", started=NOW - 150), NOW - 120)
    bridge.poster.send = send  # Zulip is back
    w.clock = lambda: NOW + 60
    await w.pass_once()
    w.clock = lambda: NOW + 70
    await w.pass_once()
    assert [m[2] for m in bridge.poster.sent] == [
        f"🔧 **Bridge** (not Claude): @**Alice|7** background job `later` finished: exit 0 "
        f"after 30 s. (job {B})"
    ]
    assert store.rows("job_posts") == [] and store.rows("jobs", id=A)[0]["end_message_id"] is None


async def test_only_an_end_post_sent_since_the_first_try_counts_as_landed(tmp_path, store):
    """Codex round 7 on #940: a bot message that merely names a job's id (an answer saying it
    started the job, a log's path, a `!status` line) is no end post, nor is one ending with the
    exact tag that isn't a bridge note, nor an end-post-shaped message from before the row's
    first try and its margin; only a bridge note ending with the batch's exact `(job <id>)`,
    sent since, counts."""
    session(store)
    turn(store)
    ended(job(tmp_path, A), NOW - 100)
    bridge, hook = Bridge(), Hook()
    bridge.server += [
        {"id": 10, "content": f"Started background job {A} with `zab job start`.",
         "timestamp": int(NOW + 5)},
        {"id": 13, "content": f"Started `rank`; its log is `/home/agent/.local/state/zab/jobs/"
         f"{A}/output.log`.", "timestamp": int(NOW + 5)},
        {"id": 14, "content": f"⚙️ Job `rank` · running since <time:2026-11-21T14:02:00Z> · log "
         f"`/home/agent/.local/state/zab/jobs/{A}/output.log`", "timestamp": int(NOW + 5)},
        {"id": 11, "content": f"⚠️ **Bridge** (not Claude): an old note (job {A})",
         "timestamp": int(NOW - jw.LANDED_MARGIN_S - 60)},
        {"id": 15, "content": f"Its end post will say (job {A})", "timestamp": int(NOW + 5)},
        {"id": 12, "content": f"🔧 **Bridge** (not Claude): about (job {A}) and more",
         "timestamp": int(NOW + 5)},
    ]  # fmt: skip
    send = bridge.poster.send
    bridge.poster.send = unknown  # it didn't land
    w = watcher(tmp_path, store, [bridge], hook=hook)
    await w.pass_once()
    bridge.poster.send = send
    w.clock = lambda: NOW + 60
    await w.pass_once()
    assert len(bridge.poster.sent) == 1 and hook.heard == [([A], 901)]  # sent again, once
    assert store.rows("jobs", id=A)[0]["end_message_id"] == 901


async def test_a_landed_multi_job_post_is_found_by_its_exact_tag(tmp_path, store):
    session(store)
    turn(store)
    ended(job(tmp_path, A), NOW - 100)
    ended(job(tmp_path, B, name="b"), NOW - 100)
    bridge = Bridge()
    send = bridge.poster.send

    async def landed_but_lost(*args):
        await send(*args)
        await unknown()

    bridge.poster.send = landed_but_lost
    w = watcher(tmp_path, store, [bridge])
    await w.pass_once()
    assert bridge.poster.sent[0][2].endswith(f"(jobs {A}, {B})")
    w.clock = lambda: NOW + 60
    await w.pass_once()
    assert len(bridge.poster.sent) == 1
    assert {r["end_message_id"] for r in store.rows("jobs")} == {901}


async def test_a_failed_write_after_a_send_never_sends_it_again(tmp_path, store, monkeypatch):
    """Review round 2 of #940, B1: the send went through, then the store refused the write
    ("database or disk is full"). Each try is counted before it goes out, its outcome unknown,
    so the next try looks for the post and finds it: one post in an hour of passes, and only a
    few looks, backed off."""
    session(store)
    turn(store)
    ended(job(tmp_path, A), NOW - 100)
    bridge = Bridge()
    real = store.update

    def full(table, where, **values):
        if table == "jobs" and "handled_at" in values:
            raise sqlite3.OperationalError("database or disk is full")
        return real(table, where, **values)

    monkeypatch.setattr(store, "update", full)
    w = watcher(tmp_path, store, [bridge])
    await passes(w, NOW + jw.CONFIRM_S + 600)
    assert len(bridge.poster.sent) == 1
    assert len(bridge.reads) == 5  # a look at each later try: 1, 3, 7, 15 and 31 min
    # the disk freed: the next look records the post, never sends it again
    monkeypatch.setattr(store, "update", real)
    w.clock = lambda: NOW + jw.CONFIRM_S + 610
    await w.pass_once()
    assert len(bridge.poster.sent) == 1


async def test_a_waiting_post_whose_job_went_holds_no_later_end(tmp_path, store):
    """Review round 2 of #940, B2: a refused post, then its job's directory removed (the agent
    cleaned up): the post being tried ends whether or not a batch forms, and a later end of the
    session is posted once Zulip takes posts again."""
    session(store)
    turn(store)
    first = job(tmp_path, A)
    ended(first, NOW - 100)
    bridge = Bridge()
    send = bridge.poster.send
    bridge.poster.send = refuse
    w = watcher(tmp_path, store, [bridge])
    await w.pass_once()
    assert len(store.rows("job_posts")) == 1
    shutil.rmtree(first.directory)
    turn(store, started=NOW - 90, ended=NOW - 10)
    ended(job(tmp_path, B, name="later", started=NOW - 80), NOW - 20)
    bridge.poster.send = send
    w.clock = lambda: NOW + 10
    await w.pass_once()
    assert [m[2].endswith(f"(job {B})") for m in bridge.poster.sent] == [True]
    assert store.rows("job_posts") == []


async def test_a_restart_looks_before_sending_again(tmp_path, store):
    """Review round 2 of #940, N2: the outcome unknown is kept in the store, so a watcher
    started afresh looks for the post before any send, and doesn't send it twice."""
    session(store)
    turn(store)
    ended(job(tmp_path, A), NOW - 100)
    bridge = Bridge()
    send = bridge.poster.send

    async def landed_but_lost(*args):
        await send(*args)
        await unknown()

    bridge.poster.send = landed_but_lost
    await watcher(tmp_path, store, [bridge]).pass_once()
    bridge.poster.send = send
    again = watcher(tmp_path, store, [bridge], now=NOW + 60)  # a new process
    await again.pass_once()
    assert len(bridge.poster.sent) == 1 and store.rows("jobs", id=A)[0]["end_message_id"] == 901


async def test_a_landed_two_job_post_is_found_after_one_job_was_seen(tmp_path, store):
    """Codex 6710713831: a post for A and B landed, its answer lost; then `zab job` showed the
    agent A's end. The post is looked for by the tag it was sent with, `(jobs A, B)`, not by
    the jobs still pending, and B is handled with it: never a second post for B."""
    session(store)
    turn(store, started=NOW - 3600, ended=NOW - 3000)
    first = job(tmp_path, A, started=NOW - 3550)
    ended(first, NOW - 3100)
    ended(job(tmp_path, B, name="b", started=NOW - 3540), NOW - 3100)
    bridge, hook = Bridge(), Hook()
    send = bridge.poster.send

    async def landed_but_lost(*args):
        await send(*args)
        await unknown()

    bridge.poster.send = landed_but_lost
    w = watcher(tmp_path, store, [bridge], hook=hook)
    await w.pass_once()
    assert bridge.poster.sent[0][2].endswith(f"(jobs {A}, {B})")
    first.mark_seen()
    bridge.poster.send = send
    w.clock = lambda: NOW + 60
    await w.pass_once()
    assert len(bridge.poster.sent) == 1
    rows = {r["id"]: r for r in store.rows("jobs")}
    assert rows[A]["end_message_id"] is None and rows[A]["handled_at"] == NOW + 60  # seen
    assert rows[B]["end_message_id"] == 901 and store.rows("job_posts") == []
    assert hook.heard == [([B], 901)]


async def test_a_job_without_its_end_is_kept_until_its_cap_and_14_days(tmp_path, store):
    """Review of #940, N1: a row-less job that left no `ended.json` counts as ended at its
    start plus its cap, not at its start: one started 14 days and an hour ago is kept."""
    kept = job(tmp_path, A, bot="", attached=None, started=NOW - jw.KEEP_S - 3600)
    gone = job(tmp_path, B, name="q", bot="", attached=None, started=NOW - jw.KEEP_S - 43200)
    await watcher(tmp_path, store, [Bridge()]).pass_once()
    assert kept.directory.exists() and not gone.directory.exists()


async def test_a_try_whose_count_cant_be_written_does_nothing(tmp_path, store, monkeypatch):
    """Review round 2 of #940: each try is counted in the store before any call to Zulip.
    While that write fails, no card is read and nothing is sent; once it works, one post."""
    session(store)
    turn(store)
    ended(job(tmp_path, A), NOW - 100)
    bridge = Bridge()
    real = store.update

    def full(table, where, **values):
        if table == "job_posts":
            raise sqlite3.OperationalError("database or disk is full")
        return real(table, where, **values)

    monkeypatch.setattr(store, "update", full)
    w = watcher(tmp_path, store, [bridge])
    await passes(w, NOW + 600)
    assert (bridge.verified, bridge.poster.sent, store.rows("job_posts")) == ([], [], [])
    monkeypatch.setattr(store, "update", real)
    w.clock = lambda: NOW + 610
    await w.pass_once()
    assert len(bridge.poster.sent) == 1 and store.rows("job_posts") == []


async def test_another_process_s_pending_post_is_left_alone(tmp_path, store):
    """Codex round 3 on #940, kept: a pending post of a bot this process doesn't run is its
    own process's to try and to end, even past the hour."""
    store.insert(
        "job_posts", bot="other", session_id="s9", job_ids=json.dumps([B]), sends="[]",
        first_at=NOW - 2 * jw.CONFIRM_S, attempts=1, next_at=NOW - 60, unknown=1,
    )  # fmt: skip
    await watcher(tmp_path, store, [Bridge()]).pass_once()
    assert [r["bot"] for r in store.rows("job_posts")] == ["other"]


async def test_a_pending_post_past_the_hour_ends_at_the_next_pass(tmp_path, store):
    """Review round 2 of #940: a post first tried an hour ago or more ends at the start of
    the next pass, a bridge down meanwhile included: its job handled with no post, the hook
    told, nothing sent."""
    session(store)
    turn(store)
    ended(job(tmp_path, A), NOW - 100)
    bridge, hook = Bridge(), Hook()
    send, bridge.poster.send = bridge.poster.send, unknown
    w = watcher(tmp_path, store, [bridge], hook=hook, now=NOW - jw.CONFIRM_S - 60)
    await w.pass_once()  # tried, of unknown outcome; then the bridge is down for an hour
    bridge.poster.send = send
    again = watcher(tmp_path, store, [bridge], hook=hook)
    await again.pass_once()
    assert (bridge.poster.sent, bridge.reads, store.rows("job_posts")) == ([], [], [])
    assert store.rows("jobs", id=A)[0]["handled_at"] == NOW and hook.heard == [([A], None)]


async def test_a_job_unreadable_for_a_pass_keeps_its_pending_post(tmp_path, store):
    """Codex 6711044076 on #940: a send of unknown outcome landed, then the job's record didn't
    read for a pass. Its pending post stays, with its stored send and backoff, so that once
    the record reads again the post is looked for and found: never a second post."""
    session(store)
    turn(store)
    first = job(tmp_path, A)
    ended(first, NOW - 100)
    bridge = Bridge()
    send = bridge.poster.send

    async def landed_but_lost(*args):
        await send(*args)
        await unknown()

    bridge.poster.send = landed_but_lost
    w = watcher(tmp_path, store, [bridge])
    await w.pass_once()
    bridge.poster.send = send
    record = first.directory / "job.json"
    kept = record.read_bytes()
    record.write_text("{")  # doesn't read, for a pass, at its next try
    w.clock = lambda: NOW + 60
    await w.pass_once()
    assert [r["unknown"] for r in store.rows("job_posts")] == [1]
    record.write_bytes(kept)
    w.clock = lambda: NOW + 70
    await w.pass_once()
    assert len(bridge.poster.sent) == 1 and store.rows("job_posts") == []
    assert store.rows("jobs", id=A)[0]["end_message_id"] == 901


async def test_a_found_post_handles_only_the_jobs_it_carries_that_read(tmp_path, store):
    """Codex 6711044076 on #940: a `(jobs A, B)` post landed unseen; at its next try A doesn't
    read. The look finds it for B, which alone is handled; the row stays for A, which the
    next due try finds in the same post. One post, and the hook hears of each job once."""
    session(store)
    turn(store)
    first = job(tmp_path, A)
    ended(first, NOW - 100)
    ended(job(tmp_path, B, name="b"), NOW - 100)
    bridge, hook = Bridge(), Hook()
    send = bridge.poster.send

    async def landed_but_lost(*args):
        await send(*args)
        await unknown()

    bridge.poster.send = landed_but_lost
    w = watcher(tmp_path, store, [bridge], hook=hook)
    await w.pass_once()
    bridge.poster.send = send
    record = first.directory / "job.json"
    kept = record.read_bytes()
    record.write_text("{")
    w.clock = lambda: NOW + 60
    await w.pass_once()
    assert hook.heard == [([B], 901)] and len(store.rows("job_posts")) == 1
    record.write_bytes(kept)
    await passes(w, NOW + 300)
    assert len(bridge.poster.sent) == 1 and store.rows("job_posts") == []
    assert hook.heard == [([B], 901), ([A], 901)]
    assert {r["end_message_id"] for r in store.rows("jobs")} == {901}


async def test_a_found_send_that_carried_part_of_the_batch_handles_that_part(tmp_path, store):
    """Codex 6711044076 on #940: the `(jobs A, B)` send didn't land; at the next try A didn't
    read, and the `(job B)` send landed unseen. Once A reads again, the look finds B's post,
    which handles B alone; A, which no landed post carried, is sent at its next try."""
    session(store)
    turn(store)
    first = job(tmp_path, A)
    ended(first, NOW - 100)
    ended(job(tmp_path, B, name="b"), NOW - 100)
    bridge, hook = Bridge(), Hook()
    send = bridge.poster.send

    async def landed_but_lost(*args):
        await send(*args)
        await unknown()

    bridge.poster.send = unknown  # (jobs A, B): it didn't land
    w = watcher(tmp_path, store, [bridge], hook=hook)
    await w.pass_once()
    record = first.directory / "job.json"
    kept = record.read_bytes()
    record.write_text("{")
    bridge.poster.send = landed_but_lost  # (job B): it landed unseen
    w.clock = lambda: NOW + 60
    await w.pass_once()
    record.write_bytes(kept)
    bridge.poster.send = send
    await passes(w, NOW + 430)
    assert [m[2][-18:] for m in bridge.poster.sent] == [f"(job {B})", f"(job {A})"]
    assert hook.heard == [([B], 901), ([A], 902)] and store.rows("job_posts") == []


async def test_a_landed_send_is_found_where_it_went_after_the_card_moved(tmp_path, store):
    """Codex 6711200018 on #940: a send of unknown outcome landed in topic `t`, then the card
    alone moved to `t2` (a partial move, SPEC §4.4). The look reads where the send went as
    well as the card's location now, finds it in `t`, and sends nothing again."""
    session(store)
    turn(store)
    ended(job(tmp_path, A), NOW - 100)
    bridge = Bridge()
    send = bridge.poster.send

    async def landed_but_lost(*args):
        await send(*args)
        await unknown()

    bridge.poster.send = landed_but_lost
    w = watcher(tmp_path, store, [bridge])
    await w.pass_once()
    bridge.poster.send = send
    store.update("sessions", {"id": SESSION}, topic="t2")  # the card moved, the post didn't
    w.clock = lambda: NOW + 60
    await w.pass_once()
    assert len(bridge.poster.sent) == 1 and store.rows("jobs", id=A)[0]["end_message_id"] == 901
    places = [{t["operator"]: t["operand"] for t in r["narrow"]}["topic"] for r in bridge.reads]
    assert places == ["t"]  # where the send went, read first


async def test_a_landed_send_is_found_with_the_vm_clock_ahead_of_zulip(tmp_path, store):
    """Review of #940: the VM's clock 5 minutes ahead of Zulip's. The look counts posts from
    `LANDED_MARGIN_S` before the row's first try, so the landed send, stamped by Zulip's clock,
    is found: never sent again."""
    session(store)
    turn(store)
    ended(job(tmp_path, A), NOW - 100)
    bridge = Bridge()
    bridge.poster.now = NOW - 300  # Zulip's clock
    send = bridge.poster.send

    async def landed_but_lost(*args):
        await send(*args)
        await unknown()

    bridge.poster.send = landed_but_lost
    w = watcher(tmp_path, store, [bridge])
    await w.pass_once()
    bridge.poster.send = send
    w.clock = lambda: NOW + 60
    await w.pass_once()
    assert len(bridge.poster.sent) == 1 and store.rows("jobs", id=A)[0]["end_message_id"] == 901


async def test_a_malformed_pending_post_is_ended_and_the_pass_goes_on(tmp_path, store, caplog):
    """Review of #940, N3: a `job_posts` row whose sends don't decode is logged once and ended,
    its job handled with no post and the hook told; another end of the session is posted in
    the same pass, and the next pass is quiet."""
    session(store)
    turn(store)
    ended(job(tmp_path, A), NOW - 100)
    ended(job(tmp_path, B, name="b"), NOW - 100)
    bridge, hook = Bridge(), Hook()
    w = watcher(tmp_path, store, [bridge], hook=hook)
    await w.pass_once()  # A and B posted: their rows made
    store.update("jobs", {"id": A}, handled_at=None, end_message_id=None)
    store.update("jobs", {"id": B}, handled_at=None, end_message_id=None)
    store.insert(
        "job_posts", bot=BOT, session_id=SESSION, job_ids=json.dumps([A]), sends="{not json",
        first_at=NOW, attempts=1, next_at=NOW, unknown=1,
    )  # fmt: skip
    bridge.poster.sent.clear()
    hook.heard.clear()
    w.clock = lambda: NOW + 60
    await w.pass_once()
    assert [m[2][-18:] for m in bridge.poster.sent] == [f"(job {B})"]
    assert store.rows("jobs", id=A)[0]["handled_at"] == NOW + 60 and store.rows("job_posts") == []
    assert ([A], None) in hook.heard
    warned = [r for r in caplog.records if "doesn't read" in r.getMessage()]
    w.clock = lambda: NOW + 70
    await w.pass_once()
    assert len(warned) == 1 and len(bridge.poster.sent) == 1


async def test_a_pending_post_whose_ids_dont_read_is_dropped(tmp_path, store):
    """Review of #940, N3: a row whose `job_ids` don't decode either is removed, logged once,
    and the pass goes on."""
    store.insert(
        "job_posts", bot=BOT, session_id=SESSION, job_ids="[", sends="[]", first_at=NOW,
        attempts=1, next_at=NOW, unknown=0,
    )  # fmt: skip
    await watcher(tmp_path, store, [Bridge()]).pass_once()
    assert store.rows("job_posts") == []


async def test_a_pass_whose_settling_fails_still_posts(tmp_path, store):
    """Review of #940, N3: the caller of `close_posts` is guarded: should settling fail as a
    whole, the batches are still tried."""
    session(store)
    turn(store)
    ended(job(tmp_path, A), NOW - 100)
    bridge = Bridge()
    w = watcher(tmp_path, store, [bridge])

    async def broken(now, pending):
        raise OSError("disk I/O error")

    w.close_posts = broken
    await w.pass_once()
    assert len(bridge.poster.sent) == 1


async def test_a_landed_send_is_found_at_the_card_s_location_now(tmp_path, store):
    """Review of #940, N2: a send of unknown outcome landed, then the whole topic was renamed,
    the post with it. The look reads where the send went (`t`, empty now), then the card's
    location now (`t2`), finds it there, and sends nothing again."""
    session(store)
    turn(store)
    ended(job(tmp_path, A), NOW - 100)
    bridge = Bridge()
    send = bridge.poster.send

    async def landed_but_lost(*args):
        await send(*args)
        await unknown()

    bridge.poster.send = landed_but_lost
    w = watcher(tmp_path, store, [bridge])
    await w.pass_once()
    bridge.poster.send = send
    for message in bridge.server:  # the whole topic renamed
        message["topic"] = "t2"
    store.update("sessions", {"id": SESSION}, topic="t2")
    w.clock = lambda: NOW + 60
    await w.pass_once()
    assert len(bridge.poster.sent) == 1 and store.rows("jobs", id=A)[0]["end_message_id"] == 901
    places = [{t["operator"]: t["operand"] for t in r["narrow"]}["topic"] for r in bridge.reads]
    assert places == ["t", "t2"]


class Flaky(Hook):
    """A hook whose store write fails the first `fails` times (Codex round 2 on #970), after
    writing a key of its own, which the failure must take back with the jobs' completion."""

    def __init__(self, store_, fails: int = 1) -> None:
        super().__init__()
        self.store, self.fails, self.after = store_, fails, 0

    def record(self, batch, message_id):
        self.store.set("hook:wrote", message_id)
        if self.fails:
            self.fails -= 1
            raise OSError("disk full")
        super().record(batch, message_id)

        def after():
            self.after += 1

        return after


async def test_a_hook_that_fails_leaves_the_end_post_for_its_next_try(tmp_path, store):
    """Codex round 2 on #970: the post said "Claude will report on it.", so the report's
    pending delivery commits with the jobs' completion or not at all. The hook's write fails:
    nothing commits, its own key included, and the post's row stays; the next try finds the
    send that landed (no second send), and the hook records it then, once, and what follows
    the commit runs once."""
    session(store)
    turn(store)
    ended(job(tmp_path, A), NOW - 100)
    bridge, hook = Bridge(), Flaky(store)
    w = watcher(tmp_path, store, [bridge], hook=hook)
    await w.pass_once()
    assert len(bridge.poster.sent) == 1
    assert bridge.poster.sent[0][2].endswith(f"Claude will report on it. (job {A})")
    assert store.rows("jobs", id=A)[0]["handled_at"] is None and store.get("hook:wrote") is None
    assert len(store.rows("job_posts")) == 1 and hook.heard == [] and hook.after == 0
    w.clock = lambda: NOW + 60
    await w.pass_once()
    assert len(bridge.poster.sent) == 1  # found landed, never sent again
    assert hook.heard == [([A], 901)] and hook.after == 1 and store.get("hook:wrote") == 901
    assert store.rows("jobs", id=A)[0]["handled_at"] == NOW + 60 and store.rows("job_posts") == []
    await w.pass_once()
    assert hook.heard == [([A], 901)] and hook.after == 1  # handled: never again


async def test_a_hook_that_fails_with_no_post_leaves_the_end_for_the_next_pass(tmp_path, store):
    """Codex round 2 on #970, the unposted paths: a card moved to a public channel, no post;
    the hook's write fails, so the job stays unhandled, its row's next try kept, and that try
    hands it to the hook again."""
    session(store)
    turn(store)
    ended(job(tmp_path, A), NOW - 100)
    bridge, hook = Bridge(), Flaky(store)

    async def moved(session_id):
        store.update("sessions", {"id": session_id}, state="readonly")

    bridge.verify_card = moved
    w = watcher(tmp_path, store, [bridge], hook=hook)
    await w.pass_once()
    assert store.rows("jobs", id=A)[0]["handled_at"] is None and store.get("hook:wrote") is None
    w.clock = lambda: NOW + 60  # the row's next try
    await w.pass_once()
    assert bridge.poster.sent == [] and hook.heard == [([A], None)] and hook.after == 1
    assert store.rows("jobs", id=A)[0]["handled_at"] == NOW + 60 and store.rows("job_posts") == []
