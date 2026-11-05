import pytest

from zab.backends.codex_events import EventMapper, error_info, item_info, turn_summary
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
