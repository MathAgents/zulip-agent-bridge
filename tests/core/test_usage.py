import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from test_session import actor as actor
from test_session import send

from zab.core import types as t
from zab.core.usage import render_usage, stamp
from zab.render.bars import bar

pytestmark = pytest.mark.integration


def test_tables_keep_limits_and_session_observations_separate():
    usage = t.Usage(
        context_tokens=45000,
        context_window=200000,
        as_of=1000,
        limits_as_of=2000,
        windows=(t.UsageWindow(label="Weekly | model", used_fraction=0.84, resets_at=3000),),
        models=(
            t.UsageTokens(
                input_tokens=2700, cache_read_tokens=2500, output_tokens=5200, reasoning_tokens=1200
            ),
        ),
        credits=(t.UsageCredit(label="credits", balance="3.5"),),
    )
    reply = render_usage(usage, "codex", "devbox", now=2500)
    assert "45k of 200k tokens (23%)" in reply
    assert "| Input | Cached | Output | Reasoning |" in reply
    assert "| 2.7k | 2.5k | 5.2k | 1.2k |" in reply
    assert r"| Weekly \| model |" in reply and stamp(3000) in reply
    # each table says when its own data was reported (issue #647)
    assert "**Account limits** · as of " + stamp(2000) in reply
    assert "**This session** · as of " + stamp(1000) in reply
    assert "Limits observed:" not in reply
    assert "Credits" not in reply and "3.5" not in reply


def test_claude_models_cache_hit_and_unknowns():
    usage = t.Usage(
        models=(
            t.UsageTokens(
                model="model-a",
                input_tokens=10,
                output_tokens=5,
                cache_read_tokens=80,
                cache_write_tokens=10,
            ),
            t.UsageTokens(model="model-b", input_tokens=0),
        ),
        credits=(t.UsageCredit(label="zero", balance="0"),),
    )
    reply = render_usage(usage, "claude", "laptop")
    assert "Cache hit: 80%" in reply and "model-a" in reply and "model-b" in reply
    assert "| Input | Output | Cache read | Cache write |" in reply
    assert "Context usage unavailable" in reply and "Account limits unavailable" in reply
    assert "Some token counters unavailable" in reply
    assert "`" not in reply.splitlines()[-1]
    assert "Model: ` model-a `" in reply
    assert "Credits" not in reply and "cost" not in reply.lower()
    assert "Context: 0" not in reply


CARD = t.Usage(
    as_of=1000,
    limits_as_of=900,
    windows=(
        t.UsageWindow(label="Session (5-hour)", used_fraction=0.08, resets_at=2000),
        t.UsageWindow(label="Weekly · all models", used_fraction=0.91, resets_at=5000),
    ),
)
PAST = "unknown until the next Claude turn"


def test_limits_whose_resets_have_all_passed_show_no_figure():
    """Issue #647: a week-old report, both windows reset since, the 5-hour one exactly now."""
    reply = render_usage(CARD, "claude", "box", now=5000)
    assert "**Account limits** · as of " + stamp(900) in reply
    assert f"| Session (5-hour) | {PAST} | reset at {stamp(2000)} |" in reply
    assert f"| Weekly · all models | {PAST} | reset at {stamp(5000)} |" in reply
    assert "8%" not in reply and "91%" not in reply and "🟥" not in reply and "⬜" not in reply


def test_limits_whose_resets_have_partly_passed():
    reply = render_usage(CARD, "claude", "box", now=2000.5)
    assert f"| Session (5-hour) | {PAST} | reset at {stamp(2000)} |" in reply
    assert "8%" not in reply
    assert f"| Weekly · all models | {bar(0.91)} | {stamp(5000)} |" in reply


def test_current_limits_show_their_bars_and_when_they_were_reported():
    reply = render_usage(CARD, "claude", "box", now=1999)
    assert "**Account limits** · as of " + stamp(900) in reply
    assert f"| Session (5-hour) | {bar(0.08)} | {stamp(2000)} |" in reply
    assert f"| Weekly · all models | {bar(0.91)} | {stamp(5000)} |" in reply
    assert PAST not in reply and "reset at" not in reply
    lines = reply.splitlines()  # the dating line is its own paragraph, before the table
    i = lines.index("**Account limits** · as of " + stamp(900))
    assert lines[i - 1] == "" and lines[i + 1] == "" and lines[i + 2].startswith("| Limit |")


def test_a_past_codex_reset_and_an_undated_report():
    """Codex reads its limits for each `!usage`, so a passed reset asks for another; limits
    with no reported time say so rather than borrowing the session's."""
    usage = t.Usage(
        as_of=1000, windows=(t.UsageWindow(label="Weekly", used_fraction=0.5, resets_at=10),)
    )
    reply = render_usage(usage, "codex", "box", now=20)
    assert f"| Weekly | unknown; try !usage again | reset at {stamp(10)} |" in reply
    assert "50%" not in reply and "Claude turn" not in reply
    assert "**Account limits** · as of unknown time" in reply


def test_unknown_limit_does_not_become_zero_and_invalid_time_is_safe():
    reply = render_usage(t.Usage(windows=(t.UsageWindow(label="Weekly"),)), "codex", "box")
    assert "utilization unavailable" in reply and "0%" not in reply
    assert stamp(float("inf")) == "unknown time"
    # a session's `as_of` from a hand-edited file: unknown, not "Usage unavailable" (#648)
    assert stamp("x") == "unknown time" and stamp([1]) == "unknown time"
    odd = render_usage(t.Usage(as_of="yesterday", context_tokens=5), "claude", "box")
    assert "**This session** · as of unknown time" in odd


async def test_usage_command_hides_a_passed_reset(actor):
    """Through `!usage` and `_account_usage`, on the real clock: no test-injected `now`
    (review of #648), so a default of 0 or the report's own time fails here."""
    a, fake, backend = actor
    backend.kind = "claude"
    backend.usage.return_value = t.Usage(
        limits_as_of=900,
        windows=(t.UsageWindow(label="Weekly · all models", used_fraction=0.91, resets_at=1000),),
    )
    await a.command("!usage")
    reply = list(fake.messages.values())[-1]["content"]
    assert "91%" not in reply and "unknown until the next Claude turn" in reply


async def test_usage_posts_to_topic_without_starting_turn(actor):
    a, fake, backend = actor
    backend.kind = "codex"
    backend.usage.return_value = t.Usage(context_tokens=0)
    await a.command("!usage")
    backend.usage.assert_awaited_once_with("native")
    backend.start_turn.assert_not_awaited()
    assert "Context: 0 tokens" in list(fake.messages.values())[-1]["content"]
    await a.command("!help")
    assert "!usage" in list(fake.messages.values())[-1]["content"]


@pytest.mark.parametrize("failure", [RuntimeError("private backend detail"), TimeoutError()])
async def test_usage_failure_preserves_running_turn(actor, failure):
    a, fake, backend = actor
    await send(a, fake, "start")
    backend.usage.side_effect = failure
    await a.command("!usage")
    assert a.row["state"] == "running" and a.turn["native_turn_id"] == "turn1"
    reply = list(fake.messages.values())[-1]["content"]
    assert "Usage unavailable" in reply and "private backend detail" not in reply
    a.post = AsyncMock(side_effect=RuntimeError("posting failed"))
    await a.command("!usage")
    assert a.row["state"] == "running"


def test_limit_labels_and_unavailable_reasons_are_plain_text():
    reply = render_usage(
        t.Usage(
            windows=(t.UsageWindow(label="Session (5-hour)", used_fraction=0.123),),
            unavailable=("No turn yet | try again later",),
        ),
        "claude",
        "box",
    )
    assert "| Session (5-hour) |" in reply
    assert r"No turn yet \| try again later" in reply.splitlines()[-1]
    assert "`" not in reply.splitlines()[-1]


@pytest.mark.parametrize("backend", ["claude", "codex"])
def test_fixture_usage_tables_end_before_following_paragraphs(backend):
    from zab.backends.claude_usage import UsageStore
    from zab.backends.codex_usage import snapshot

    fixtures = Path(__file__).parents[1] / "fixtures"
    if backend == "claude":
        store = UsageStore(None)
        for line in (fixtures / "claude/2.1.283/simple.jsonl").read_text().splitlines():
            record = json.loads(line)
            data = record.get("data", {})
            if data.get("type") == "rate_limit_event":
                store.record_limits(
                    "account", data["rate_limit_info"]["unifiedWindows"], None, 1000
                )
            elif data.get("type") == "result":
                store.record_turn("session", data, None, 1000)
        usage = store.usage("session", "account")
    else:
        tokens, limits = {}, {}
        for line in (fixtures / "codex/0.157.1/simple.jsonl").read_text().splitlines():
            message = json.loads(line).get("message", {})
            if message.get("method") == "thread/tokenUsage/updated":
                tokens = message["params"]["tokenUsage"]
            elif message.get("method") == "account/rateLimits/updated":
                limits = message["params"]
        usage = snapshot(tokens, 1000, limits, 2000)
    assert usage and usage.models and usage.windows
    now = min(w.resets_at for w in usage.windows if w.resets_at is not None) - 1
    reply = render_usage(usage, backend, "fixture-box", now=now)  # every window current
    assert "Limits observed:" not in reply and "Credits" not in reply
    assert "unknown" not in reply and "%" in reply
    lines = reply.splitlines()
    ends = [
        i
        for i, line in enumerate(lines)
        if line.startswith("|") and (i == len(lines) - 1 or not lines[i + 1].startswith("|"))
    ]
    assert len(ends) >= 2
    for i in ends:
        assert i == len(lines) - 1 or lines[i + 1] == ""
