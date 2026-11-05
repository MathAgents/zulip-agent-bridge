"""A turn that starts while the bridge is starting is never judged by an older snapshot.

Drew's VM, 2026-10-14 (zab 0.2.2): the backend queued `BackendStatus(up=True)` before the
bridges started, catch-up replayed a mention in a new topic, and the new session's actor
began its turn. The pump then took the backend's snapshot (no turn yet) while `begin()` was
still waiting on git and Zulip, and recovery applied it after the turn had started: the live
turn was completed "interrupted by bridge restart" (Claude) or "… no longer active"
(Codex), and its answer was dropped (SPEC §7.8.1).
"""

import asyncio

import pytest

from zab.commands import run
from zab.commands.run import Runtime
from zab.config import BackendConfig, BotConfig, BridgeConfig, Config, RenderConfig
from zab.core import types as t
from zab.core.session import NEEDS_RECOVERY, WAITING, SessionActor
from zab.testing.fake_zulip import FakeZulip
from zab.workspace.local import LocalWorkspace
from zab.zulip.client import ZulipClient

pytestmark = pytest.mark.integration

NOTICE = "interrupted by bridge restart"
WARNING = "no longer active"


class Backend:
    """A backend that answers `open_session` from its own state, as the real ones do: an
    active turn once `start_turn` returned, the last completed turn after it finished."""

    def __init__(self, kind: str, run: str, *, hold_up: bool = False) -> None:
        self.kind, self.run, self.hold_up = kind, run, hold_up
        self.config = BackendConfig(kind=kind)
        self.capabilities = t.Capabilities(
            steer=False, approvals=False, live_attach=False, rename=False
        )
        self.queue: asyncio.Queue = asyncio.Queue()
        self.sessions = 0
        self.turns = 0
        self.active: dict[str, str] = {}
        self.last: dict[str, t.TurnSummary] = {}
        self.opened: list[tuple[str, str | None]] = []  # (session, active turn) per snapshot
        self.starting = asyncio.Event()  # a begin() is underway
        self.up_handled = asyncio.Event()  # the pump has handled BackendStatus(up=True)

    async def start(self) -> None:
        self.queue.put_nowait(t.BackendStatus(session_id="", up=True))  # before any bridge

    async def close(self) -> None:
        pass

    def events(self):
        return self._events()

    async def _events(self):
        while True:
            event = await self.queue.get()
            up = isinstance(event, t.BackendStatus) and event.up
            if up and self.hold_up:
                await self.starting.wait()
            yield event
            if up:
                self.up_handled.set()  # the pump asks for the next event only now

    def workspace(self, root: str, cwd: str) -> LocalWorkspace:
        return LocalWorkspace(root, cwd)

    async def create_session(self, spec: t.SessionSpec) -> t.SessionInfo:
        self.sessions += 1
        native = f"{self.run}-session{self.sessions}"
        return t.SessionInfo(
            native_id=native, cwd=spec.cwd, settings=spec.settings, resume_hint=f"resume {native}"
        )

    async def update_settings(self, native_id: str, settings: t.SessionSettings):
        return settings

    async def rename(self, native_id: str, name: str) -> None:
        pass

    async def open_session(self, native_id: str, spec: t.SessionSpec) -> t.SessionInfo:
        self.opened.append((native_id, self.active.get(native_id)))
        return t.SessionInfo(
            native_id=native_id,
            cwd=spec.cwd,
            settings=spec.settings,
            resume_hint=f"resume {native_id}",
            active_turn_id=self.active.get(native_id),
            last_turn=self.last.get(native_id),
        )

    async def start_turn(self, native_id: str, turn_input: t.UserInput) -> str:
        self.turns += 1
        tid = f"{self.run}-turn{self.turns}"
        self.active[native_id] = tid
        self.queue.put_nowait(t.TurnStarted(session_id=native_id, turn_id=tid))
        return tid

    def finish(self, native_id: str, text: str) -> None:
        tid = self.active.pop(native_id)
        self.last[native_id] = t.TurnSummary(
            turn_id=tid, status=t.TurnStatus.COMPLETED, final_text=text
        )
        self.queue.put_nowait(
            t.TurnCompleted(
                session_id=native_id, turn_id=tid, status=t.TurnStatus.COMPLETED, final_text=text
            )
        )


async def until(predicate) -> None:
    async with asyncio.timeout(10):
        while not predicate():  # noqa: ASYNC110 - the state of several tasks
            await asyncio.sleep(0.01)


def texts(fake: FakeZulip, topic: str) -> list[str]:
    """Everything the bridge ever posted or edited in `topic`, edits included."""
    ids = {mid for mid, m in fake.messages.items() if m["subject"] == topic}
    found = []
    for method, path, params in fake.requests:
        if "content" not in params:
            continue
        if (method == "POST" and path == "messages" and params.get("topic") == topic) or (
            method == "PATCH" and path.startswith("messages/") and int(path.split("/")[1]) in ids
        ):
            found.append(params["content"])
    return found


async def start(config: Config, backend: Backend, monkeypatch) -> Runtime:
    monkeypatch.setattr(run, "make_backend", lambda name, config: backend)
    runtime = Runtime(config)
    await runtime.start()
    for bridge in runtime.bridges:  # tests drive Zulip events themselves
        bridge.listener.cancel()
        with pytest.raises(asyncio.CancelledError):
            await bridge.listener
    return runtime


async def setup(tmp_path, monkeypatch, kind: str) -> tuple[Config, FakeZulip]:
    ws = LocalWorkspace(str(tmp_path))
    await ws.run(["git", "init", "-q"])
    fake = FakeZulip()
    monkeypatch.setattr(
        ZulipClient,
        "from_zuliprc",
        lambda _: ZulipClient("https://zulip.example", "bot", "decoy", transport=fake.transport),
    )
    config = Config(
        tmp_path / "config.toml",
        BridgeConfig(state_dir=str(tmp_path / "state"), default_project=str(tmp_path)),
        [BotConfig("bot", "decoy", kind, [20])],
        {kind: BackendConfig(kind=kind)},
        [],
        RenderConfig(),
    )
    return config, fake


async def mention(bridge, fake: FakeZulip, topic: str, text: str) -> None:
    mid = await bridge.client.send(1, topic, f"@**Test Bot** {text}")
    message = fake.messages[mid] | {"sender_id": 20}
    await bridge.handle({"type": "message", "message": message, "flags": ["mentioned"]})


@pytest.mark.parametrize("kind", ["claude", "codex"])
async def test_turn_started_during_startup_survives_recovery(tmp_path, monkeypatch, kind):
    config, fake = await setup(tmp_path, monkeypatch, kind)

    # First run: a turn is running when the bridge goes down.
    first = Backend(kind, "r1")
    runtime = await start(config, first, monkeypatch)
    try:
        bridge = runtime.bridges[0]
        await asyncio.wait_for(first.up_handled.wait(), 5)
        await mention(bridge, fake, "old topic", "long job")
        actor = bridge.actors["r1-session1"]
        for _ in range(2):  # the delivery, then the "begin" its slot waiter queued
            await actor.inbox.join()
            await asyncio.sleep(0)
        stale = dict(actor.turn)
        assert stale["status_message_id"]
    finally:
        await runtime.close()

    # While it is down, a mention opens a new topic: the catch-up backlog of the next start.
    fake.post_as(20, "@**Test Bot** what is 1+1", topic="new topic")

    # Second run. The pump takes the queued up event only once begin() is underway, and
    # begin() reaches the backend only once the pump handled it: the order on Drew's VM.
    second = Backend(kind, "r2", hold_up=True)
    prepare_input = SessionActor.prepare_input

    async def underway(actor, *args, **kwargs):
        second.starting.set()
        await second.up_handled.wait()
        return await prepare_input(actor, *args, **kwargs)

    monkeypatch.setattr(SessionActor, "prepare_input", underway)
    runtime = await start(config, second, monkeypatch)
    try:
        bridge = runtime.bridges[0]
        await asyncio.wait_for(second.up_handled.wait(), 5)
        await asyncio.wait_for(runtime.recoveries[id(bridge)], 10)
        new = bridge.actors["r2-session1"]
        old = bridge.actors["r1-session1"]

        # The new topic's turn is live, and recovery adopted it instead of ending it.
        assert not [text for text in texts(fake, "new topic") if NOTICE in text or WARNING in text]
        assert new.turn and new.turn["native_turn_id"] == "r2-turn1"
        assert new.row["state"] == "running" and new.slot_owned
        assert ("r2-session1", "r2-turn1") in second.opened  # the snapshot followed the start
        second.finish("r2-session1", "1+1 = 2")
        await until(lambda: new.turn is None)
        new_texts = texts(fake, "new topic")
        assert any("1+1 = 2" in text for text in new_texts)
        assert not [text for text in new_texts if NOTICE in text or WARNING in text]

        # The turn that really died with the first run still gets the restart treatment.
        assert old.turn is None
        row = runtime.store.rows("turns", id=stale["id"])[0]
        assert row["state"] == "interrupted"
        old_texts = texts(fake, "old topic")
        if kind == "claude":
            assert fake.messages[stale["status_message_id"]]["content"] == (
                "⏹ interrupted by bridge restart; send a message to continue"
            )
        else:
            ended = "⚠️ **Bridge** (not Codex): The stored turn is no longer active."
            assert any(ended in text for text in old_texts)
    finally:
        await runtime.close()


class Refusing(Backend):
    """The Codex daemon kept the thread loaded across the restart, and the bot's commit
    identity changed: it refuses that one session (§5.5)."""

    async def open_session(self, native_id: str, spec: t.SessionSpec) -> t.SessionInfo:
        raise t.SessionRefused("Codex thread is already loaded with an unverified git identity")


async def test_a_message_caught_up_for_a_refused_session_says_how_to_recover(tmp_path, monkeypatch):
    """Review of #504: catch-up holds a restored session's message under "⏳ Waiting…" before
    the startup round, which then refuses the session. The notice must not wait forever: it
    says that `!status` retries, and the message stays held for that retry (§7.3, #306)."""
    config, fake = await setup(tmp_path, monkeypatch, "codex")
    first = Backend("codex", "r1")
    runtime = await start(config, first, monkeypatch)
    try:
        bridge = runtime.bridges[0]
        await asyncio.wait_for(first.up_handled.wait(), 5)
        await mention(bridge, fake, "topic", "first job")
        actor = bridge.actors["r1-session1"]
        await until(lambda: actor.turn is not None)
        first.finish("r1-session1", "done")
        await until(lambda: actor.turn is None)
    finally:
        await runtime.close()

    fake.post_as(20, "@**Test Bot** follow-up", topic="topic")  # while zab is down
    second = Refusing("codex", "r2")
    runtime = await start(config, second, monkeypatch)
    try:
        bridge = runtime.bridges[0]
        await asyncio.wait_for(second.up_handled.wait(), 5)
        await asyncio.wait_for(runtime.recoveries[id(bridge)], 10)
        actor = bridge.actors["r1-session1"]
        assert actor.row["state"] == "error" and not actor.ready
        assert [m["content"] for m in runtime.store.held(actor.row["id"])] == ["follow-up"]
        assert texts(fake, "topic")[-2:] == [WAITING, actor.note(NEEDS_RECOVERY)]
        assert fake.messages[actor.waiting_message_id]["content"] == actor.note(NEEDS_RECOVERY)
    finally:
        await runtime.close()
