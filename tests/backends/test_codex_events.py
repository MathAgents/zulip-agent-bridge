import itertools

import pytest

from zab.backends.codex_events import (
    EventMapper,
    error_info,
    item_info,
    subagent_note,
    turn_summary,
)
from zab.core import types as t

pytestmark = pytest.mark.contract


def test_final_tracks_completed_item_not_empty_turn_items():
    mapper = EventMapper()
    mapper.notification("turn/started", {"threadId": "s", "turn": {"id": "t"}})
    mapper.notification(
        "item/completed",
        {
            "threadId": "s",
            "turnId": "t",
            "item": {"id": "a", "type": "agentMessage", "text": "answer"},
        },
    )
    mapper.notification("turn/diff/updated", {"threadId": "s", "turnId": "t", "diff": "patch"})
    (event,) = mapper.notification(
        "turn/completed", {"threadId": "s", "turn": {"id": "t", "status": "completed", "items": []}}
    )
    assert event.final_text == "answer"
    assert event.native_diff == "patch"
    assert not mapper.active


@pytest.mark.parametrize(
    "code,kind",
    [
        ("usageLimitExceeded", t.ErrorKind.USAGE_LIMIT),
        ("unauthorized", t.ErrorKind.AUTH),
        ({"responseStreamDisconnected": {"httpStatusCode": 500}}, t.ErrorKind.NETWORK),
        ("sandboxError", t.ErrorKind.SANDBOX),
        ("newCode", t.ErrorKind.OTHER),
        (None, t.ErrorKind.OTHER),
    ],
)
def test_schema_error_shapes(code, kind):
    assert error_info({"codexErrorInfo": code, "message": "failure"}).kind == kind


def test_user_id_echo_and_recovery_summary():
    item = item_info(
        {
            "id": "i",
            "type": "userMessage",
            "clientId": "echo",
            "content": [{"type": "text", "text": "hi"}],
        }
    )
    assert item.client_id == "echo"
    summary = turn_summary(
        {"id": "t", "status": "interrupted", "items": [{"type": "agentMessage", "text": "partial"}]}
    )
    assert summary.status == t.TurnStatus.INTERRUPTED
    assert summary.final_text == "partial"


def test_usage_is_per_turn_difference():
    mapper = EventMapper()

    def usage(total):
        return {
            "threadId": "s",
            "turnId": "t",
            "tokenUsage": {"total": {"totalTokens": total}, "modelContextWindow": 1000},
        }

    mapper.notification("thread/tokenUsage/updated", usage(100))
    mapper.notification("turn/started", {"threadId": "s", "turn": {"id": "t"}})
    mapper.notification("thread/tokenUsage/updated", usage(140))
    (event,) = mapper.notification(
        "turn/completed", {"threadId": "s", "turn": {"id": "t", "status": "completed", "items": []}}
    )
    assert event.usage.total_tokens == 40


@pytest.mark.parametrize("status", ["completed", "failed", "interrupted"])
def test_unknown_usage_and_retry_error(status):
    mapper = EventMapper()
    mapper.notification("turn/started", {"threadId": "s", "turn": {"id": "t"}})
    mapper.notification(
        "thread/tokenUsage/updated",
        {"threadId": "s", "turnId": "t", "tokenUsage": {"total": {"totalTokens": 1040}}},
    )
    mapper.notification(
        "error",
        {
            "threadId": "s",
            "turnId": "t",
            "willRetry": True,
            "error": {"message": "retry", "codexErrorInfo": "responseStreamDisconnected"},
        },
    )
    (event,) = mapper.notification(
        "turn/completed", {"threadId": "s", "turn": {"id": "t", "status": status, "error": None}}
    )
    assert event.usage is None
    assert bool(event.error) == (status == "failed")


def test_an_error_outside_a_turn_is_a_notice_that_names_codex():
    """§8.5: an `error` not tied to a turn is a Notice, which says who reports it (#677)."""
    (notice,) = EventMapper().notification(
        "error", {"threadId": "s", "error": {"message": "stream lost"}}
    )
    assert notice.level == "error"
    assert notice.message == "Codex reported an error outside a turn: stream lost"
    # redacted before the prefix: the zuliprc rule matches whole lines only (§11.6, review)
    key = "Q7xK" + "a1B2" * 7
    for raw in (f"key={key}", f"key={key[:16]}\x00{key[16:]}"):  # NULs dropped first (#678)
        (notice,) = EventMapper().notification(
            "error", {"threadId": "s", "error": {"message": raw}}
        )
        assert key[:8] not in notice.message.replace("\x00", "")
        assert "[redacted" in notice.message


def test_a_command_title_is_redacted_before_it_is_cut():
    """Review of #658: the title is cut at 120 characters, which can leave `key=` with fewer
    than the 32 characters the zuliprc rule needs (SPEC §11.6); so it is redacted first."""
    key = "Q7xK" + "a1B2" * 7
    command = "echo " + "y" * 100 + "\nkey=" + key + "\n"
    item = item_info({"type": "commandExecution", "id": "c", "command": command}, False)
    assert len(item.title) <= 120 and key[:4] not in item.title


def collab(tool: str, states: dict[str, str], receivers=()) -> dict:
    """A `collabAgentToolCall` item of the parent's, in 0.157.1's schema."""
    return {
        "id": f"call-{tool}",
        "type": "collabAgentToolCall",
        "tool": tool,
        "status": "completed",
        "senderThreadId": "s",
        "receiverThreadIds": list(receivers),
        "agentsStates": {c: {"status": s, "message": None} for c, s in states.items()},
    }


def activity(child: str, kind: str) -> dict:
    """A `subAgentActivity` item of the parent's (0.157.1's schema)."""
    return {"id": f"act-{child}", "type": "subAgentActivity", "agentThreadId": child,
            "agentPath": "/root/a", "kind": kind}  # fmt: skip


def end_turn(mapper, items: list[dict], *, answer: str | None = "ok"):
    """One turn of thread `s` with these items, then its answer; its `TurnCompleted`."""
    mapper.notification("turn/started", {"threadId": "s", "turn": {"id": "t"}})
    final = [{"id": "a", "type": "agentMessage", "text": answer}] if answer else []
    for item in [*items, *final]:
        mapper.notification("item/completed", {"threadId": "s", "turnId": "t", "item": item})
    (event,) = mapper.notification(
        "turn/completed", {"threadId": "s", "turn": {"id": "t", "status": "completed"}}
    )
    return event


def test_a_turn_that_ends_with_a_running_subagent_says_so():
    """#660 case 2: Codex starts no turn when a sub-agent left running finishes, so its answer
    never reached Zulip (DECISIONS, 2026-10-25, B2). The turn's answer now says it runs."""
    mapper = EventMapper()
    done = end_turn(mapper, [collab("spawnAgent", {"c1": "pendingInit"}, ["c1"])])
    assert done.final_text == "ok\n\n" + subagent_note(1)
    assert done.final_text.startswith("ok\n\n⚠️ **Bridge** (not Codex): A sub-agent was still")
    assert mapper.running_subagents("s") == ["c1"]
    # stopped by the model, or seen to complete by a wait: no note
    for last in (
        collab("interruptAgent", {"c1": "interrupted"}, ["c1"]),
        collab("wait", {"c1": "completed"}, ["c1"]),
    ):
        mapper = EventMapper()
        done = end_turn(mapper, [collab("spawnAgent", {"c1": "running"}, ["c1"]), last])
        assert done.final_text == "ok" and not mapper.running_subagents("s")


def test_a_subagent_reported_by_its_activity_or_its_own_turn():
    """The parent's `subAgentActivity` items, and the child's own `turn/completed` should the
    daemon send it, update its state; two left running are counted; a turn with no answer
    gets the note alone, and one still running is noted again at the next turn's end."""
    mapper = EventMapper()
    spawn = collab("spawnAgent", {}, ["c1", "c2", "c3"])
    done = end_turn(mapper, [spawn, activity("c1", "completed")], answer=None)
    assert done.final_text == subagent_note(3 - 1)
    assert "2 sub-agents were still running" in done.final_text
    mapper.notification("turn/started", {"threadId": "c2", "turn": {"id": "u"}})
    mapper.notification(
        "turn/completed", {"threadId": "c2", "turn": {"id": "u", "status": "interrupted"}}
    )
    assert mapper.running_subagents("s") == ["c3"]
    assert end_turn(mapper, []).final_text == "ok\n\n" + subagent_note(1)
    mapper.notification("turn/started", {"threadId": "s", "turn": {"id": "t"}})
    mapper.notification("turn/started", {"threadId": "c3", "turn": {"id": "v"}})  # still c3's
    item = activity("c3", "interrupted")
    mapper.notification("item/completed", {"threadId": "s", "turnId": "t", "item": item})
    assert mapper.running_subagents("s") == ["c3"]  # its own turn runs until it says it ended
    mapper.notification(
        "turn/completed", {"threadId": "c3", "turn": {"id": "v", "status": "interrupted"}}
    )
    assert not mapper.running_subagents("s")
    mapper.notification("turn/started", {"threadId": "c3", "turn": {"id": "w"}})  # messaged again
    assert mapper.running_subagents("s") == ["c3"]
    mapper.notification(
        "turn/completed", {"threadId": "c3", "turn": {"id": "w", "status": "completed"}}
    )
    item = activity("c3", "completed")
    mapper.notification("item/completed", {"threadId": "s", "turnId": "t", "item": item})
    mapper.notification(
        "turn/completed", {"threadId": "s", "turn": {"id": "t", "status": "completed"}}
    )
    assert not mapper.running_subagents("s")
    assert list(mapper.subagents["s"]) == ["c1", "c2", "c3"]  # known, never dropped


def test_messaging_a_finished_subagent_does_not_mark_it_running():
    """Codex round 1 on #822: `interacted` covers `send_message`, which only queues a message
    for an idle sub-agent (codex-rs 0.157.1, `MessageDeliveryMode::QueueOnly`), so it says
    nothing of its state; marked running, a finished one was noted at every later turn. A
    `followup_task` starts it again, which its own `turn/started` shows."""
    mapper = EventMapper()
    assert (
        end_turn(mapper, [activity("c1", "started"), activity("c1", "completed")]).final_text
        == "ok"
    )
    assert end_turn(mapper, [activity("c1", "interacted")]).final_text == "ok"
    assert end_turn(mapper, []).final_text == "ok"
    mapper.notification("turn/started", {"threadId": "s", "turn": {"id": "t"}})
    item = activity("c1", "interacted")  # a follow-up this time: c1 starts a turn
    mapper.notification("item/completed", {"threadId": "s", "turnId": "t", "item": item})
    mapper.notification("turn/started", {"threadId": "c1", "turn": {"id": "u"}})
    (done,) = mapper.notification(
        "turn/completed", {"threadId": "s", "turn": {"id": "t", "status": "completed"}}
    )
    assert done.final_text == subagent_note(1)


def test_a_followup_turn_noted_whatever_comes_first():
    """Review of #822, B1: codex-rs queues a `followup_task` on the sub-agent before it sends
    the parent's `interacted`, so the sub-agent's `turn/started` can come first. When finished
    sub-agents were dropped at a turn's end, that one was unknown then, and the parent's turn
    ended without the note while it ran. They are kept now."""
    mapper = EventMapper()
    mapper.notification("turn/started", {"threadId": "s", "turn": {"id": "t"}})
    mapper.notification("turn/started", {"threadId": "c1", "turn": {"id": "u"}})
    mapper.notification(
        "turn/completed", {"threadId": "c1", "turn": {"id": "u", "status": "completed"}}
    )
    item = activity("c1", "started")
    mapper.notification("item/completed", {"threadId": "s", "turnId": "t", "item": item})
    item = activity("c1", "completed")
    mapper.notification("item/completed", {"threadId": "s", "turnId": "t", "item": item})
    (done,) = mapper.notification(
        "turn/completed", {"threadId": "s", "turn": {"id": "t", "status": "completed"}}
    )
    assert done.final_text is None  # c1 was done
    mapper.notification("turn/started", {"threadId": "s", "turn": {"id": "t2"}})
    mapper.notification("turn/started", {"threadId": "c1", "turn": {"id": "u2"}})  # first
    item = activity("c1", "interacted")
    mapper.notification("item/completed", {"threadId": "s", "turnId": "t2", "item": item})
    (done,) = mapper.notification(
        "turn/completed", {"threadId": "s", "turn": {"id": "t2", "status": "completed"}}
    )
    assert done.final_text == subagent_note(1)


@pytest.mark.parametrize(
    "child_turn, running",
    [("failed", False), ("completed", False), ("interrupted", False), (None, True)],
)
def test_a_subagents_own_turns_before_its_spawn_item_count(child_turn, running):
    """Codex round 2 on #822: a sub-agent's own `turn/started`, and its `turn/completed`, can
    come before the parent's `subAgentActivity` `started`. Its end was then ignored, and the
    `started` marked it running: a failed one, which sends the parent no `completed`, was
    noted at every later turn. Its observed turns now set its state at that item; a turn
    still running, or none yet, means running."""
    mapper = EventMapper()
    mapper.notification("turn/started", {"threadId": "s", "turn": {"id": "t"}})
    mapper.notification("turn/started", {"threadId": "c1", "turn": {"id": "u"}})
    if child_turn:
        turn = {"id": "u", "status": child_turn, "error": None}
        mapper.notification("turn/completed", {"threadId": "c1", "turn": turn})
    item = activity("c1", "started")
    mapper.notification("item/completed", {"threadId": "s", "turnId": "t", "item": item})
    assert mapper.running_subagents("s") == (["c1"] if running else [])
    (done,) = mapper.notification(
        "turn/completed", {"threadId": "s", "turn": {"id": "t", "status": "completed"}}
    )
    assert done.final_text == (subagent_note(1) if running else None)
    if child_turn == "failed":
        assert mapper.subagents["s"]["c1"] == "errored"
    # v1's spawn item likewise, with the snapshot it always carries (review round 2: its
    # `pendingInit` or `running` is older than the sub-agent's own turns)
    for snapshot in ("pendingInit", "running"):
        mapper = EventMapper()
        mapper.notification("turn/started", {"threadId": "c2", "turn": {"id": "v"}})
        if child_turn:
            turn = {"id": "v", "status": child_turn, "error": None}
            mapper.notification("turn/completed", {"threadId": "c2", "turn": turn})
        mapper.notification("turn/started", {"threadId": "s", "turn": {"id": "t"}})
        item = collab("spawnAgent", {"c2": snapshot}, ["c2"])
        mapper.notification("item/completed", {"threadId": "s", "turnId": "t", "item": item})
        assert mapper.running_subagents("s") == (["c2"] if running else [])


def test_only_a_spawn_makes_a_subagent():
    """Codex round 3, on #825: an interrupt or a wait can target any thread by id, another
    thread's sub-agent included. Thread B's `interrupted` (v2) or `agentsStates` (v1) for A's
    sub-agent C made C B's too: C's own turns then gave B the note, and B's `!stop` walked into
    C. They now update only a sub-agent the thread spawned."""
    mapper = EventMapper()
    for thread, turn in (("a", "ta"), ("b", "tb")):
        mapper.notification("turn/started", {"threadId": thread, "turn": {"id": turn}})
    mapper.notification(
        "item/completed", {"threadId": "a", "turnId": "ta", "item": activity("c", "started")}
    )
    for item in (activity("c", "interrupted"), collab("wait", {"c": "running"}, ["c"])):
        mapper.notification("item/completed", {"threadId": "b", "turnId": "tb", "item": item})
    assert not mapper.subagents.get("b")
    assert mapper.subagents["a"] == {"c": "running"}  # B's interrupt is not A's item either
    mapper.notification("turn/started", {"threadId": "c", "turn": {"id": "u"}})  # A resumes it
    (done_b,) = mapper.notification(
        "turn/completed", {"threadId": "b", "turn": {"id": "tb", "status": "completed"}}
    )
    (done_a,) = mapper.notification(
        "turn/completed", {"threadId": "a", "turn": {"id": "ta", "status": "completed"}}
    )
    assert done_b.final_text is None and done_a.final_text == subagent_note(1)


def test_a_message_to_a_thread_outside_the_tree_tracks_nothing():
    """Review of #822, N5: `send_message` can name any thread of the daemon by id; its
    `interacted` makes it no sub-agent, and its own turns give no note."""
    mapper = EventMapper()
    mapper.notification("turn/started", {"threadId": "s", "turn": {"id": "t"}})
    item = activity("other-root", "interacted")
    mapper.notification("item/completed", {"threadId": "s", "turnId": "t", "item": item})
    mapper.notification("turn/started", {"threadId": "other-root", "turn": {"id": "o"}})
    (done,) = mapper.notification(
        "turn/completed", {"threadId": "s", "turn": {"id": "t", "status": "completed"}}
    )
    assert done.final_text is None and not mapper.subagents.get("s")


def test_a_reconnection_forgets_the_subagents_states_not_their_parents():
    """Review of #822, B2: after a new connection to the daemon, none of the old sub-agents'
    events will come, so one that ran would be noted at every later turn. Its state is
    forgotten (no note), and who spawned whom is kept (for `!stop`)."""
    mapper = EventMapper()
    assert end_turn(mapper, [activity("c1", "started")]).final_text == "ok\n\n" + subagent_note(1)
    mapper.reconnected()
    assert end_turn(mapper, []).final_text == "ok"
    assert mapper.subagents == {"s": {"c1": "unknown"}}
    mapper.notification("turn/started", {"threadId": "c1", "turn": {"id": "u"}})  # reloaded
    assert mapper.running_subagents("s") == ["c1"]


def test_a_reconnection_forgets_turn_ids_and_v1_waits_until_heard_again():
    """Review round 2, N3: the old sub-agents' turn ids went stale with the connection, and
    `!stop` (#825) sent one instead of asking the daemon for the live turn: they are
    forgotten too. N2: on v1, a `wait` reporting an old sub-agent running after a reconnection
    made it running though its end would never come here. Its own `turn/started` makes it
    running again (the daemon subscribes this connection when it loads it again), and its own
    turns then win over any snapshot (Codex round 4)."""
    mapper = EventMapper()
    mapper.notification("turn/started", {"threadId": "s", "turn": {"id": "t"}})
    item = collab("spawnAgent", {"c1": "pendingInit"}, ["c1"])
    mapper.notification("item/completed", {"threadId": "s", "turnId": "t", "item": item})
    mapper.notification("turn/started", {"threadId": "c1", "turn": {"id": "old"}})
    mapper.reconnected()
    assert "c1" not in mapper.active and "s" in mapper.active  # the parent's isn't a sub-agent's
    item = collab("wait", {"c1": "running"}, ["c1"])
    mapper.notification("item/completed", {"threadId": "s", "turnId": "t", "item": item})
    assert not mapper.running_subagents("s")
    mapper.notification("turn/started", {"threadId": "c1", "turn": {"id": "new"}})
    assert mapper.running_subagents("s") == ["c1"]  # heard again
    mapper.notification(
        "turn/completed", {"threadId": "c1", "turn": {"id": "new", "status": "completed"}}
    )
    mapper.notification("item/completed", {"threadId": "s", "turnId": "t", "item": item})
    assert not mapper.running_subagents("s")  # the wait's `running` is older than that end


def test_a_late_v1_snapshot_never_overrides_the_subagents_own_turns():
    """Codex round 4: a v1 `wait` (or `close_agent`) item's `agentsStates` is a snapshot taken
    when the call ran, which can come after the sub-agent's own `turn/completed`. A `running`
    captured before it failed revived it, and with no further end to come, every later turn
    noted it. The sub-agent's own turns, once seen, now win over any snapshot: a stale
    `running` after its end, as a stale `completed` while a newer turn of its runs."""
    mapper = EventMapper()
    mapper.notification("turn/started", {"threadId": "s", "turn": {"id": "t"}})
    spawn = collab("spawnAgent", {"c": "pendingInit"}, ["c"])
    mapper.notification("item/completed", {"threadId": "s", "turnId": "t", "item": spawn})
    mapper.notification("turn/started", {"threadId": "c", "turn": {"id": "u"}})
    turn = {"id": "u", "status": "failed", "error": None}
    mapper.notification("turn/completed", {"threadId": "c", "turn": turn})
    late = collab("wait", {"c": "running"}, ["c"])  # captured before it failed
    mapper.notification("item/completed", {"threadId": "s", "turnId": "t", "item": late})
    assert not mapper.running_subagents("s")
    (done,) = mapper.notification(
        "turn/completed", {"threadId": "s", "turn": {"id": "t", "status": "completed"}}
    )
    assert done.final_text is None and end_turn(mapper, []).final_text == "ok"
    mapper.notification("turn/started", {"threadId": "s", "turn": {"id": "t3"}})
    mapper.notification("turn/started", {"threadId": "c", "turn": {"id": "u2"}})  # resumed
    stale = collab("wait", {"c": "completed"}, ["c"])  # the previous turn's end
    mapper.notification("item/completed", {"threadId": "s", "turnId": "t3", "item": stale})
    assert mapper.running_subagents("s") == ["c"]


def test_a_late_end_of_an_earlier_turn_leaves_a_newer_one_running():
    """Review round 2, N1: the parent's `completed` item for a sub-agent's turn can come after
    that sub-agent's next turn started (a follow-up): it no longer marks the running one done.
    Its own `turn/completed` says when that one ends."""
    mapper = EventMapper()
    mapper.notification("turn/started", {"threadId": "s", "turn": {"id": "t"}})
    for event, params in (
        ("item/completed", {"threadId": "s", "turnId": "t", "item": activity("c", "started")}),
        ("turn/started", {"threadId": "c", "turn": {"id": "u1"}}),
        ("turn/completed", {"threadId": "c", "turn": {"id": "u1", "status": "completed"}}),
        ("turn/started", {"threadId": "c", "turn": {"id": "u2"}}),  # the follow-up's
        ("item/completed", {"threadId": "s", "turnId": "t", "item": activity("c", "completed")}),
    ):
        mapper.notification(event, params)
    assert mapper.running_subagents("s") == ["c"]
    mapper.notification(
        "turn/completed", {"threadId": "c", "turn": {"id": "u2", "status": "failed"}}
    )
    assert not mapper.running_subagents("s")


def merges(a: list, b: list):
    """Every interleaving of a and b that keeps each one's own order."""
    for slots in itertools.combinations(range(len(a) + len(b)), len(a)):
        left, right = iter(a), iter(b)
        yield [next(left) if i in slots else next(right) for i in range(len(a) + len(b))]


@pytest.mark.parametrize("end", ["completed", "failed", "interrupted", None])
def test_every_order_of_a_subagents_events_against_its_parents(end):
    """Review round 2: the parent's items and the sub-agent's own turns come on two threads'
    channels, each in order, interleaved any way. Whatever the order, a sub-agent whose turn
    ended isn't running, and one whose turn runs is."""
    started = activity("c", "started")
    parent = [
        ("item/started", {"threadId": "s", "turnId": "t", "item": started}),
        ("item/completed", {"threadId": "s", "turnId": "t", "item": started}),
    ]
    if end == "completed":  # the only end codex-rs reports to the parent
        parent.append(
            ("item/completed", {"threadId": "s", "turnId": "t", "item": activity("c", end)})
        )
    child = [("turn/started", {"threadId": "c", "turn": {"id": "u"}})]
    if end:
        child.append(("turn/completed", {"threadId": "c", "turn": {"id": "u", "status": end}}))
    for order in merges(parent, child):
        mapper = EventMapper()
        mapper.notification("turn/started", {"threadId": "s", "turn": {"id": "t"}})
        for event, params in order:
            mapper.notification(event, params)
        assert mapper.running_subagents("s") == ([] if end else ["c"]), order
