"""A VM's boot or wake loads no Codex thread until its topic is used (issue #632, SPEC §7.8.1).

The real `Runtime` on the fake Zulip and `FakeAppServer`, stopped and started again on the same
state with the app-server restarted in between, as a VM's boot or wake does (`Restarts`, from
the `!fork` tests). Each thread the app-server holds loaded runs its own `lean-lsp-mcp` on the
VMs, so the loaded threads after the reopen are what the boot costs."""

from __future__ import annotations

import asyncio

import pytest
from test_fork import OWNER
from test_fork import restarts as restarts  # the fixture

pytestmark = pytest.mark.integration


def thread(rig, topic: str) -> str:
    return rig.store().rows("sessions", topic=topic)[0]["native_id"]


async def recovered(runtime) -> None:
    """The backend's reopen round and what follows it (`Runtime.finish_recovery`) are done."""
    async with asyncio.timeout(30):
        while not runtime.recoveries:  # noqa: ASYNC110 - the pump's first BackendStatus(up)
            await asyncio.sleep(0.05)
        await asyncio.gather(*runtime.recoveries.values())


async def test_a_boot_loads_only_the_thread_of_the_topic_that_woke_it(restarts):
    """Three Codex topics; the VM stops, and a message in one wakes it: that topic is
    answered, its thread alone is loaded, and the others load at their next message."""
    rig = await restarts("codex")
    await rig.start()
    topics = ("one", "two", "three")
    for topic in topics:
        rig.fake.post_as(OWNER, f"@**Test Bot** remember {topic}", topic)
        await rig.turns(1, topic)
    assert rig.server.loaded == {thread(rig, t) for t in topics}
    await rig.stop()
    rig.server.restart()  # the boot: the app-server starts with no thread loaded

    rig.fake.post_as(OWNER, "@**Test Bot** what did I ask?", "two")  # wakes the VM
    runtime = await rig.start()
    await rig.turns(2, "two")
    await recovered(runtime)
    assert rig.server.loaded == {thread(rig, "two")}
    assert not [p for t in topics for p in rig.posts(t) if "⚠️" in p]
    assert {r["state"] for r in runtime.store.rows("sessions")} == {"idle"}

    rig.fake.post_as(OWNER, "@**Test Bot** and you?", "three")
    await rig.turns(2, "three")
    assert rig.server.loaded == {thread(rig, "two"), thread(rig, "three")}
    assert rig.posts("three")[-1] == "FAKE_OK 2"


async def test_a_turn_the_boot_ended_is_settled_without_loading_its_thread(restarts):
    """A turn running when the VM went down (a crash; an idle stop waits for idle) is listed
    `interrupted` from the rollout, as a resume would list it, and recovery ends it so
    (§7.8.1) with its thread unloaded; the topic's next message then runs as usual."""
    rig = await restarts("codex")
    await rig.start()
    rig.fake.post_as(OWNER, "@**Test Bot** remember orchid", "topic")
    await rig.turns(1)
    rig.server.hold = True
    rig.fake.post_as(OWNER, "@**Test Bot** long job", "topic")
    async with asyncio.timeout(30):
        while not any(r["state"] == "running" for r in rig.store().rows("sessions")):  # noqa: ASYNC110
            await asyncio.sleep(0.05)
    await rig.stop()
    rig.server.hold = False
    rig.server.restart()

    runtime = await rig.start()
    await rig.turns(2)
    await recovered(runtime)
    first, cut = rig.ended("topic")
    assert (first["state"], cut["state"]) == ("completed", "interrupted")
    assert rig.server.loaded == set()
    assert rig.posts("topic")[-2:] == [
        "⏹ **Stopped** · 0m 00s · 0 actions · ` gpt-fake ` · effort default",
        "(no final message)",
    ]
    rig.fake.post_as(OWNER, "@**Test Bot** again", "topic")
    await rig.turns(3)
    assert rig.server.loaded == {thread(rig, "topic")}


async def test_a_bridge_restart_after_a_wake_refuses_the_thread_a_terminal_loaded(restarts):
    """Review of #818 (B1, §5.5), through the store: after a wake, one topic's thread is loaded
    by the bridge (a message), the other's by a terminal's client first. The bridge alone
    restarts: the first topic is answered, the identity its load applied having been persisted;
    the second is refused, its message held for `!status`, and no turn runs there."""
    rig = await restarts("codex")
    await rig.start()
    for topic in ("mine", "terminal"):
        rig.fake.post_as(OWNER, f"@**Test Bot** remember {topic}", topic)
        await rig.turns(1, topic)
    await rig.stop()
    rig.server.restart()  # the wake
    await recovered(await rig.start())
    rig.fake.post_as(OWNER, "@**Test Bot** still there?", "mine")
    await rig.turns(2, "mine")
    rig.server.loaded.add(thread(rig, "terminal"))  # a terminal's `codex resume`
    await rig.stop()  # the bridge alone restarts: the app-server holds both threads

    runtime = await rig.start()
    await recovered(runtime)
    rig.fake.post_as(OWNER, "@**Test Bot** and now?", "mine")
    await rig.turns(3, "mine")
    rig.fake.post_as(OWNER, "@**Test Bot** hello", "terminal")
    async with asyncio.timeout(30):
        while not any("needs recovery" in p for p in rig.posts("terminal")):  # noqa: ASYNC110
            await asyncio.sleep(0.05)
    (row,) = runtime.store.rows("sessions", topic="terminal")
    assert row["state"] == "error" and runtime.store.held(row["id"])
    other = thread(rig, "terminal")
    assert all(r["threadId"] != other for r in rig.server.requests("turn/start")[2:])


async def until_true(check) -> None:
    async with asyncio.timeout(30):
        while not check():  # noqa: ASYNC110 - the runtime's tasks, on the fake Zulip
            await asyncio.sleep(0.05)


async def test_a_boot_refuses_the_thread_a_terminal_loaded_before_the_reopen(restarts):
    """#830 (§5.5): the VM boots and a terminal's client loads one topic's thread before the
    bridge reopens it. The identity record its load left in the earlier run vouches for
    nothing: that topic is refused, its message held and no turn run there; the other topic
    is answered."""
    rig = await restarts("codex")
    await rig.start()
    for topic in ("mine", "terminal"):
        rig.fake.post_as(OWNER, f"@**Test Bot** remember {topic}", topic)
        await rig.turns(1, topic)
    other = thread(rig, "terminal")
    await rig.stop()
    rig.server.restart()  # the boot
    rig.server.loaded.add(other)  # the terminal's `codex resume`, first

    runtime = await rig.start()
    await recovered(runtime)
    rig.fake.post_as(OWNER, "@**Test Bot** still there?", "mine")
    await rig.turns(2, "mine")
    rig.fake.post_as(OWNER, "@**Test Bot** hello", "terminal")
    await until_true(lambda: any("needs recovery" in p for p in rig.posts("terminal")))
    (row,) = runtime.store.rows("sessions", topic="terminal")
    assert row["state"] == "error" and runtime.store.held(row["id"])
    assert all(r["threadId"] != other for r in rig.server.requests("thread/resume"))


async def test_attach_after_a_detach_checks_the_thread_as_unknown(restarts):
    """#830 (§8.7, §5.5): `!attach` doesn't carry the identity record the detached binding
    left. Detached, the bridge restarts; a minute after it left, the app-server unloads the
    thread, and a terminal's client loads it, in its own environment but in the same run of
    the app-server, for which that record would have vouched. The attach is refused and loads
    nothing; once the terminal quits and the thread unloads, `!attach` binds it, and the
    topic's next message loads it, checked."""
    rig = await restarts("codex")
    await rig.start()
    rig.fake.post_as(OWNER, "@**Test Bot** remember orchid", "one")
    await rig.turns(1, "one")
    sid = thread(rig, "one")
    rig.fake.post_as(OWNER, "!detach", "one")
    await until_true(lambda: rig.store().rows("sessions", topic="one")[0]["state"] == "detached")
    await rig.stop()
    rig.server.unload(sid)  # a minute after the bridge left
    rig.server.loaded.add(sid)  # the terminal's `codex resume`

    await recovered(await rig.start())
    resumes = len(rig.server.requests("thread/resume"))
    rig.fake.post_as(OWNER, f"@**Test Bot** !attach {sid}", "two")
    await until_true(lambda: any(p.startswith("Attach failed") for p in rig.posts("two")))
    assert "unverified git identity" in rig.posts("two")[-1]
    assert len(rig.server.requests("thread/resume")) == resumes
    assert not rig.store().rows("sessions", topic="two")

    rig.server.unload(sid)  # the terminal quit
    rig.fake.post_as(OWNER, f"@**Test Bot** !attach {sid}", "two")
    await until_true(lambda: rig.store().rows("sessions", topic="two"))
    rig.fake.post_as(OWNER, "@**Test Bot** what did I ask?", "two")
    await rig.turns(1, "two")
    assert rig.server.loaded == {sid}
    assert rig.server.requests("thread/resume")[-1]["threadId"] == sid


async def test_attach_right_after_a_detach_trusts_the_bridges_own_load(restarts):
    """#830: within one run of the bridge, the thread its connection started is still loaded
    by that load, so `!attach` after `!detach` resumes it, without a refusal."""
    rig = await restarts("codex")
    await rig.start()
    rig.fake.post_as(OWNER, "@**Test Bot** remember orchid", "one")
    await rig.turns(1, "one")
    sid = thread(rig, "one")
    rig.fake.post_as(OWNER, "!detach", "one")
    await until_true(lambda: rig.store().rows("sessions", topic="one")[0]["state"] == "detached")
    rig.fake.post_as(OWNER, f"@**Test Bot** !attach {sid}", "two")
    await until_true(lambda: rig.store().rows("sessions", topic="two"))
    rig.fake.post_as(OWNER, "@**Test Bot** what did I ask?", "two")
    await rig.turns(1, "two")
    assert not any(p.startswith("Attach failed") for p in rig.posts("two"))
    assert not rig.server.requests("thread/loaded/list")


async def test_a_topic_whose_turn_ran_through_a_refused_reopen_says_what_unblocks_it(restarts):
    """Review of #854, B1 (§7.3): a topic's Codex turn runs when the bridge stops, and the new
    bridge's reopen refuses its thread, loaded under a record that vouches for nothing now (one
    from before #830 at the rollout). The session is in `error` with its turn's row still
    running: its next message is held with the needs-recovery note, not with ⏳ alone."""
    rig = await restarts("codex")
    await rig.start()
    rig.fake.post_as(OWNER, "@**Test Bot** remember orchid", "busy")
    await rig.turns(1, "busy")
    rig.server.hold = True
    rig.fake.post_as(OWNER, "@**Test Bot** long job", "busy")
    await until_true(lambda: rig.store().rows("sessions", topic="busy")[0]["state"] == "running")
    await rig.stop()
    rig.server.instance = "another run"  # the turn runs on; the record vouches for nothing

    runtime = await rig.start()
    await recovered(runtime)
    rig.fake.post_as(OWNER, "@**Test Bot** and then?", "busy")
    await until_true(lambda: any("needs recovery" in p for p in rig.posts("busy")))
    (row,) = runtime.store.rows("sessions", topic="busy")
    assert row["state"] == "error" and runtime.store.held(row["id"])
    assert runtime.store.rows("turns", session_id=row["id"], state="running")
