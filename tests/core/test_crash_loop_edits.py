"""A bridge that restarts in a loop edits a surviving turn's status message within its bounds
(#757; SPEC §4.9, D41): its recovery refreshes the message at each start, and the store keeps
each message's edit count and last edit across the restarts."""

import asyncio
import time
from dataclasses import replace
from itertools import pairwise
from unittest.mock import AsyncMock

import pytest
from test_bridge import user_message

from zab.config import BackendConfig, BotConfig, BridgeConfig, Config, RenderConfig
from zab.core import types as t
from zab.core.bridge import BotBridge
from zab.core.session import PAUSED, Reopen, StopTimeout
from zab.core.store import Store
from zab.render.markdown import MarkdownRenderer
from zab.testing.fake_clock import FrozenClock
from zab.testing.fake_zulip import FakeZulip
from zab.workspace.local import LocalWorkspace
from zab.zulip.client import GAP, MAX_EDITS, ZulipClient

pytestmark = pytest.mark.integration

RESTART_S = 5  # systemd restarts the bridge 5 s after a failure (`Restart=on-failure`)
# How long each of the 30 restarted bridges runs before it fails, in seconds: the short runs
# meet the gap, the long ones would pass the cap
LIFETIMES = [65, 65, 5] * 10


class Loop:
    """One bridge process after another, on one Zulip, store and backend (a Codex turn
    survives the bridge, DECISIONS D4), each with its own client, poster and clock."""

    def __init__(self, tmp_path):
        self.tmp_path, self.fake, self.now = tmp_path, FakeZulip(), time.time()
        self.config = Config(
            tmp_path / "config.toml",
            BridgeConfig(default_project=str(tmp_path)),
            [BotConfig("bot", "unused", "codex", [20])],
            {"codex": BackendConfig(kind="codex")},
            [],
            RenderConfig(),
        )
        backend = self.backend = AsyncMock()
        backend.update_settings.side_effect = lambda sid, settings: settings
        backend.workspace = lambda root, cwd: LocalWorkspace(root, cwd)
        backend.capabilities = t.Capabilities(
            steer=True, approvals=True, live_attach=True, rename=True
        )
        backend.create_session.return_value = info = t.SessionInfo(
            native_id="native",
            cwd=str(tmp_path),
            settings=t.SessionSettings(),
            resume_hint="codex resume native",
        )
        backend.open_session.return_value = replace(info, active_turn_id="turn1")  # running
        backend.start_turn.side_effect = ["turn1"]
        self.manager = AsyncMock()
        self.manager.prepare.return_value = t.PreparedWorkspace(
            repo_root=str(tmp_path), cwd=str(tmp_path), branch="main", base=None
        )
        self.manager.snapshot.return_value = None
        self.renderer = MarkdownRenderer()
        self.hung = False  # the actors may hang: settle without waiting for their inboxes
        self.sent: list[tuple[float, int, str]] = []  # every PATCH: when, which message, what
        self.posted: list[tuple[float, int, str]] = []  # every message posted, the same

    async def start(self, *, reopen=True):
        """A bridge process: its clock begins again (a reboot's monotonic clock, the worst
        case), its store connection and client are its own, and the backend's first
        `BackendStatus(up)` reopens its sessions (SPEC §7.8.1)."""
        self.clock = clock = FrozenClock(self.now, boot=self.now - 1)
        self.store = Store(self.tmp_path / "state")
        client = ZulipClient("https://zulip.example", "bot", "decoy", transport=self.fake.transport)
        client.budget.clock = clock
        request = client.request

        async def timed(method, endpoint, **params):
            if method == "PATCH":
                self.sent.append((clock.now, int(endpoint.split("/")[1]), params["content"]))
            result = await request(method, endpoint, **params)
            if (method, endpoint) == ("POST", "messages"):
                self.posted.append((clock.now, result["id"], params["content"]))
            return result

        client.request = timed
        self.bridge = b = BotBridge(
            self.config,
            self.config.bots[0],
            self.store,
            self.backend,
            self.manager,
            self.renderer,
            asyncio.Semaphore(2),
            client,
        )
        await b.start()
        b.listener.cancel()  # this test delivers the one message itself
        await b.housekeeping
        if reopen:
            for actor in b.actors.values():
                actor.put(Reopen())
        await self.settle()
        return b

    async def settle(self):
        for _ in range(10):
            await asyncio.sleep(0)
        for actor in [] if self.hung else self.bridge.actors.values():
            await actor.inbox.join()

    async def run(self, seconds):
        """The process runs `seconds`: its held edits go out when their gap has passed."""
        for _ in range(seconds):
            self.clock.advance(1)
            await self.settle()

    async def crash(self):
        """The process fails, its held edits lost; systemd starts the next `RESTART_S` later."""
        await self.bridge.close()
        self.store.close()
        self.now = self.clock.now + RESTART_S


async def begun(tmp_path):
    """The loop, after the first process started a turn, edited its status message once (the
    model's edit) and failed 5 s later: and that message's id."""
    await LocalWorkspace(str(tmp_path)).run(["git", "init", "-q"])
    loop = Loop(tmp_path)
    b = await loop.start(reopen=False)
    await user_message(b, loop.fake, "@**Test Bot** work through a crash loop")
    actor = b.actors["native"]
    mid = actor.turn["status_message_id"]
    actor.put(t.TurnStarted(session_id="native", turn_id="turn1", model="m", effort="high"))
    await loop.settle()
    await loop.run(5)
    await loop.crash()
    return loop, mid


async def test_a_crash_loop_edits_a_surviving_turns_status_at_most_20_times(tmp_path):
    """Criterion 2: 30 restarts with a turn that survives them, each recovery refreshing its
    status message. The message gets at most `MAX_EDITS` edits, at least `GAP` apart but for
    the summary, which still comes; the edit before it says that updates are paused. With the
    counts in memory only, each start edited it at once: 33 edits, some 10 s apart."""
    assert len(LIFETIMES) == 30
    loop, mid = await begun(tmp_path)
    for lifetime in LIFETIMES:
        b = await loop.start()
        assert b.actors["native"].turn["status_message_id"] == mid  # the turn survived
        await loop.run(lifetime)
        await loop.crash()
    b = await loop.start()
    b.actors["native"].put(
        t.TurnCompleted(
            session_id="native", turn_id="turn1", status=t.TurnStatus.COMPLETED, final_text="ok"
        )
    )
    await loop.settle()
    times = [when for when, m, _ in loop.sent if m == mid]
    contents = [content for _, m, content in loop.sent if m == mid]
    assert len(times) == MAX_EDITS  # the cap, reached: the loop ran 25 minutes
    assert all(later - earlier >= GAP for earlier, later in pairwise(times[:-1]))
    assert contents[-2].endswith(PAUSED) and not any(PAUSED in c for c in contents[:-2])
    assert contents[-1].startswith("✅ **Done**") and times[-1] == loop.clock.now
    assert loop.fake.messages[mid]["content"] == contents[-1]
    # The turn's answer delivered, its status message is edited no more: its count goes
    assert b.store.edit_counts("bot") == []
    assert b.client.edit_bounds.edits(mid) == MAX_EDITS
    await loop.crash()


async def test_a_turn_revived_after_a_stop_timeout_keeps_its_count(tmp_path):
    """Codex round 1 on #795: a stop that times out ends the turn unconfirmed (`StopTimeout`,
    `release_slot=False`), and its status message gets the timeout's final edit, the 20th. The
    backend may still run it: after a restart, recovery takes the turn up again with the same
    status message (`_reconcile`). Its count must still be there, so that message never gets a
    21st edit; the summary, when the turn ends, goes in a new message."""
    loop, mid = await begun(tmp_path)
    for _ in range(MAX_EDITS - 2):  # each run makes its recovery's edit, a gap after the last
        await loop.start()
        await loop.run(65)
        await loop.crash()
    assert len([m for _, m, _ in loop.sent if m == mid]) == MAX_EDITS - 1  # the 19th: paused
    b = await loop.start()
    actor = b.actors["native"]
    actor.put(("!stop",))
    actor.put(StopTimeout("turn1"))  # no confirmation from the backend within 30 s
    await loop.settle()
    assert actor.unconfirmed_turn and loop.fake.messages[mid]["content"].startswith("⏹")
    await loop.crash()
    b = await loop.start()  # the backend still runs the turn: recovery takes it up again
    actor = b.actors["native"]
    assert actor.turn["status_message_id"] == mid and actor.row["state"] == "running"
    await loop.run(65)
    actor.put(
        t.TurnCompleted(
            session_id="native", turn_id="turn1", status=t.TurnStatus.COMPLETED, final_text="ok"
        )
    )
    await loop.settle()
    assert len([m for _, m, _ in loop.sent if m == mid]) == MAX_EDITS
    summary = actor.store.rows("turns")[0]["status_message_id"]
    assert summary != mid and loop.fake.messages[summary]["content"].startswith("✅ **Done**")
    await loop.crash()


class Hanging(MarkdownRenderer):
    """A renderer that never returns: the bridge dies delivering the answer, after the summary,
    at every start (as an out-of-memory kill or a signal would end it there; #806)."""

    async def render_agent_text(self, *args, **kwargs):
        await asyncio.Event().wait()


async def test_starts_that_die_delivering_the_answer_send_its_summary_a_gap_apart(tmp_path):
    """#806, criterion 3: a bridge that dies inside the answer's delivery at each of 60 starts,
    each start completing the turn again and sending its summary again. Every message gets its
    edits at least `GAP` apart once its summary went out (the first goes at once), a new status
    message comes only after 20 edits of the last, and its own first edit waits for its gap
    after the post too. Before #806 each start edited the message at once, some 10 s apart:
    this test then fails at its first gap."""
    loop, mid = await begun(tmp_path)
    loop.renderer, loop.hung = Hanging(), True
    b = await loop.start()  # the turn ends: its summary goes out, then the delivery hangs
    b.actors["native"].put(
        t.TurnCompleted(
            session_id="native", turn_id="turn1", status=t.TurnStatus.COMPLETED, final_text="ok"
        )
    )
    await loop.run(5)
    await loop.crash()
    loop.backend.open_session.return_value = loop.backend.create_session.return_value  # ended
    for lifetime in [5, 65] * 30:
        b = await loop.start()  # recovery completes the turn again, and hangs again
        assert b.actors["native"].completion
        await loop.run(lifetime)
        await loop.crash()
    summaries = [(when, m) for when, m, c in loop.sent if c.startswith("✅ **Done**")]
    first = summaries[0][0]
    anew = [(when, m) for when, m, c in loop.posted if c.startswith("✅ **Done**")]
    assert summaries and anew  # the cap was reached: the summary went in a new message
    messages = [mid] + [m for _, m in anew]
    for m in messages:
        times = [when for when, n, _ in loop.sent if n == m and when >= first]
        times = [when for when, n in anew if n == m] + times  # a post counts as its final
        assert all(later - earlier >= GAP for earlier, later in pairwise(times)), (m, times)
    edits = {m: sum(n == m for _, n, _ in loop.sent) for m in messages}
    assert all(edits[m] == MAX_EDITS for m in messages[:-1]), edits  # anew only past the cap
    assert len(loop.sent) <= (loop.clock.now - first) / GAP + 5


async def test_a_summary_counted_but_never_shown_lands_after_a_restart(tmp_path):
    """Codex round 2 on #813. The turn's summary is counted but never reaches Zulip (it
    failed). A start within its gap holds the repeat back, and the answer goes out, ending
    the turn. The bridge then stops before the gap has passed, and at the next start the held
    summary fails once more. It still lands: kept in the store, sent by that start once its
    gap has passed, and tried again a gap after the failure."""
    loop, mid = await begun(tmp_path)
    b = await loop.start()
    loop.fake.offline = True  # the summary's edit fails: counted, since it may have landed
    b.actors["native"].put(
        t.TurnCompleted(
            session_id="native", turn_id="turn1", status=t.TurnStatus.COMPLETED, final_text="ok"
        )
    )
    await loop.settle()
    loop.fake.offline = False
    tried = loop.clock.now
    assert b.store.rows("turns")[0]["state"] == "delivery_pending"
    loop.backend.open_session.return_value = loop.backend.create_session.return_value  # ended
    await loop.run(5)
    await loop.crash()
    b = await loop.start()  # within the summary's gap: held back, the answer delivered
    assert b.actors["native"].turn is None and b.store.rows("turns")[0]["state"] == "completed"
    assert any(m["content"] == "ok" for m in loop.fake.messages.values())
    assert not loop.fake.messages[mid]["content"].startswith("✅")
    await loop.run(5)
    await loop.crash()  # stopped while it is held
    b = await loop.start()  # sends it once its gap has passed
    await loop.run(int(tried + GAP - loop.clock.now) - 1)
    loop.fake.offline = True
    await loop.run(2)  # it fails
    loop.fake.offline = False
    assert not loop.fake.messages[mid]["content"].startswith("✅")
    await loop.run(int(GAP))
    assert loop.fake.messages[mid]["content"].startswith("✅ **Done**")
    assert b.store.held_finals("bot") == []
    times = [when for when, m, c in loop.sent if m == mid and c.startswith("✅")]
    assert all(later - earlier >= GAP for earlier, later in pairwise(times)), times
    await loop.crash()
