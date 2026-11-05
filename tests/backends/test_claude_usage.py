"""Claude's `Backend.usage()` (issue #36, PM Sun 18:10; DECISIONS 2026-10-04 `!usage` row): the
latest limits per account, context and per-model totals per session, kept across restarts."""

from __future__ import annotations

import json
import sys
from dataclasses import replace

import pytest

from backends.claude_backend_helpers import FAKE, FIXTURES, drain
from zab.backends import claude as cb
from zab.backends import claude_usage as cu
from zab.core.types import SessionSettings, SessionSpec, UserInput
from zab.core.usage import render_usage, stamp

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(sys.platform == "win32", reason="fake binary needs a POSIX shebang"),
]


def records(name: str) -> list[dict]:
    lines = (FIXTURES / f"{name}.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


def result_of(name: str) -> dict:
    return next(
        r["data"]
        for r in records(name)
        if r.get("kind") == "stdout" and r["data"]["type"] == "result"
    )


@pytest.mark.parametrize(
    ("key", "model", "label"),
    [
        ("five_hour", "claude-opus-5-5", "Session (5-hour)"),
        ("seven_day", "claude-opus-5-5", "Weekly · all models"),
        # the PM's label: the reporting turn's model family, as Claude on the web shows it
        ("seven_day_overage_included", "claude-fable-5-1", "Weekly · Fable"),
        ("seven_day_overage_included", "claude-opus-5-5[1m]", "Weekly · Opus"),
        ("seven_day_overage_included", None, "Weekly · model-specific"),
        ("seven_day_overage_included", "banana", "Weekly · model-specific"),
        ("seven_day_opus", None, "Weekly · Opus"),
        ("monthly_spend_cap", None, "Monthly spend cap"),
    ],
)
def test_window_labels(key, model, label):
    assert cu.window_label(key, model) == label


def test_model_family():
    assert cu.model_family("claude-haiku-4-5-20251001") == "Haiku"
    assert cu.model_family("claude-3-opus-20240229") == "Opus"
    assert cu.model_family("claude-sonnet-5-5") == "Sonnet"
    assert cu.model_family(None) is None


def test_a_multi_call_turn_takes_the_context_of_its_last_call():
    turn = cu.parse_turn(result_of("tool_use"), "claude-haiku-4-5-20251001")
    assert turn["context_tokens"] == 8 + 28_848 + 153  # the last iterations entry, not the sum
    assert turn["context_window"] == 200_000
    assert turn["models"] == {"claude-haiku-4-5-20251001": [60, 1388, 179_471, 15_312, 343]}
    # a `[1m]` model finds its entry through canonicalModel; odd shapes are tolerated
    odd = {
        "modelUsage": {
            "x": "y",
            "claude-opus-5-5": {"contextWindow": 10**6, "canonicalModel": "claude-opus-5-5"},
        },
        "usage": "z",
    }
    assert cu.parse_turn(odd, "claude-opus-5-5[1m]")["context_window"] == 10**6
    assert cu.parse_turn(odd, "claude-opus-5-5[1m]")["context_tokens"] is None


def backend_for(home, **defaults):
    config = cb.ClaudeConfig(
        binary=str(FAKE),
        accounts={"test": str(home["account"])},
        default_account="test",
        defaults=SessionSettings(**({"model": "haiku"} | defaults)),
    )
    return cb.ClaudeBackend("claude", config)


async def started(backend):
    await backend.start()
    await backend._events.get()  # BackendStatus
    return backend


async def run_turn(backend, sid):
    await backend.start_turn(sid, UserInput(text="Reply with pong.", client_id="c"))
    await drain(backend, extra=1)  # through UsageUpdated: the turn's bookkeeping is done


async def test_usage_after_turns_and_after_a_restart(home):
    backend = await started(backend_for(home))
    sid = (await backend.create_session(SessionSpec(cwd=str(home["work"]), name="t"))).native_id
    assert await backend.usage(sid) is None  # nothing reported yet: no invented zeros
    await run_turn(backend, sid)
    await run_turn(backend, sid)
    usage = await backend.usage(sid)
    assert [(w.label, w.used_fraction, w.resets_at) for w in usage.windows] == [
        ("Session (5-hour)", 0.0, 1790577600.0),
        ("Weekly · all models", 0.36, 1790910000.0),
    ]
    [tokens] = usage.models  # cumulative over both turns
    assert tokens.model == "claude-haiku-4-5-20251001"
    assert (tokens.input_tokens, tokens.output_tokens) == (20, 92)
    assert (tokens.cache_read_tokens, tokens.cache_write_tokens) == (2 * 13_689, 2 * 11_511)
    assert usage.context_tokens == 10 + 13_689 + 11_511 and usage.context_window == 200_000
    assert usage.as_of and usage.limits_as_of and not usage.unavailable
    await backend.close()
    # a restarted bridge reads the same numbers from the file
    cu._STORES.clear()  # a new process: no shared store in memory
    again = await started(backend_for(home))
    await again.open_session(sid, SessionSpec(cwd=str(home["work"]), name="t"))
    assert await again.usage(sid) == usage
    await again.close()


async def test_the_card_renders_with_the_core_formatter(home, tmp_path, monkeypatch):
    """A fable turn reporting `seven_day_overage_included`, through `zab.core.usage`."""
    recs = records("simple")
    for r in recs:
        if r.get("kind") == "stdout" and r["data"].get("type") == "rate_limit_event":
            windows = r["data"]["rate_limit_info"]["unifiedWindows"]
            windows["five_hour"]["utilization"] = 0.03
            windows["seven_day"]["utilization"] = 0.23
            windows["seven_day_overage_included"] = {"utilization": 0.43, "resetsAt": 1790910000}
    fixture = tmp_path / "fable.jsonl"
    fixture.write_text("".join(json.dumps(r) + "\n" for r in recs))
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_FIXTURE", str(fixture))
    backend = await started(backend_for(home, model="fable"))
    sid = (await backend.create_session(SessionSpec(cwd=str(home["work"]), name="t"))).native_id
    await run_turn(backend, sid)
    usage = await backend.usage(sid)
    text = render_usage(usage, "claude", "workstation", now=1790577600 - 1)  # before the resets
    assert "**Account limits** · as of " + stamp(usage.limits_as_of) in text
    assert "| Session (5-hour) | 🟩⬜⬜⬜⬜⬜⬜⬜⬜⬜ 3% |" in text
    assert "| Weekly · all models | 🟩🟩⬜⬜⬜⬜⬜⬜⬜⬜ 23% |" in text
    assert "| Weekly · Fable | 🟩🟩🟩🟩⬜⬜⬜⬜⬜⬜ 43% |" in text
    assert "Context: 25.2k of 200k tokens (13%)" in text
    assert "Cache hit:" in text and "cost" not in text.lower() and "$" not in text
    await backend.close()


async def test_limits_from_another_session_and_from_a_model_check(home):
    backend = await started(backend_for(home))
    one = (await backend.create_session(SessionSpec(cwd=str(home["work"]), name="a"))).native_id
    two = (await backend.create_session(SessionSpec(cwd=str(home["work"]), name="b"))).native_id
    await run_turn(backend, one)
    usage = await backend.usage(two)  # the account's limits, but no turn in this session
    assert [w.label for w in usage.windows] == ["Session (5-hour)", "Weekly · all models"]
    assert usage.models == () and usage.context_tokens is None
    assert usage.unavailable == ("No turn has finished in this session yet",)
    # a model check is a short turn on the account: it refreshes the limits too
    before = usage.limits_as_of
    stored = SessionSettings(approval=None, account="test", model="haiku")
    await backend.update_settings(two, replace(stored, model="opus"))
    assert (await backend.usage(two)).limits_as_of >= before
    await backend.close()


def test_a_file_from_an_older_release_renders_its_passed_resets_as_unknown(tmp_path):
    """Issue #647: the store's file, as v0.2.7 wrote it, read now. Each account's report keeps
    the time it was stored (`at`), which dates the limits table; an entry without one, or
    with an odd one, still reads, as of an unknown time."""
    path = tmp_path / "claude-usage.json"
    week_ago, reset_5h, reset_7d = 1_792_000_000.0, 1_792_010_000.0, 1_792_030_000.0
    old = {
        "accounts": {
            "/home/u/.claude": {
                "windows": {"five_hour": [0.08, reset_5h], "seven_day": [0.91, reset_7d]},
                "model": "claude-opus-5-5",
                "at": week_ago,
            },
            "/home/u/.claude-b": {"windows": {"five_hour": [0.5, reset_7d]}, "at": "yesterday"},
            "/home/u/.claude-c": {"windows": {"seven_day": [0.25, reset_7d]}},
        },
        "sessions": {
            "s1": {
                "models": {"claude-opus-5-5": [10, 46, 13689, 11511, 0]},
                "context_tokens": 25210,
                "context_window": 200000,
                "as_of": week_ago - 60,
            }
        },
    }
    path.write_text(json.dumps(old))
    store = cu.UsageStore(str(path))
    usage = store.usage("s1", "/home/u/.claude")
    assert usage.limits_as_of == week_ago and usage.as_of == week_ago - 60
    text = render_usage(usage, "claude", "vm", now=reset_7d + 7 * 86400)
    assert "**Account limits** · as of " + stamp(week_ago) in text
    assert (
        f"| Session (5-hour) | unknown until the next Claude turn | reset at {stamp(reset_5h)} |"
        in text
    )
    assert (
        f"| Weekly · all models | unknown until the next Claude turn | reset at {stamp(reset_7d)} |"
        in text
    )
    assert "8%" not in text and "91%" not in text
    assert "Context: 25.2k of 200k tokens (13%)" in text  # the session's own figures still show
    current = render_usage(usage, "claude", "vm", now=reset_5h - 1)
    assert "| Session (5-hour) | 🟩⬜⬜⬜⬜⬜⬜⬜⬜⬜ 8% |" in current
    for account in ("/home/u/.claude-b", "/home/u/.claude-c"):
        undated = store.usage("s1", account)
        assert undated.limits_as_of is None and undated.windows
        assert "**Account limits** · as of unknown time" in render_usage(
            undated, "claude", "vm", now=week_ago
        )
    # a new report keeps the format, its storage time included
    store.record_limits(
        "/home/u/.claude",
        {"five_hour": {"utilization": 0.1, "resetsAt": 9e9}},
        None,
        1_800_000_000.0,
    )
    store.save()
    saved = json.loads(path.read_text())["accounts"]["/home/u/.claude"]
    assert saved == {"windows": {"five_hour": [0.1, 9e9]}, "model": None, "at": 1_800_000_000.0}


def test_store_rename_prune_and_a_corrupt_file(tmp_path, caplog):
    path = tmp_path / "u.json"
    store = cu.UsageStore(str(path))
    store.record_turn("old", result_of("simple"), "claude-haiku-4-5-20251001", 1.0)
    store.rename("old", "new")
    assert store.usage("new", "acct").models[0].input_tokens == 10
    assert store.usage("old", "acct") is None
    store.save()
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    path.write_text("{not json")
    fresh = cu.UsageStore(str(path))
    assert fresh.usage("new", "acct") is None and "unreadable" in caplog.text
    many = cu.UsageStore(str(tmp_path / "many.json"))
    for i in range(cu.KEEP_SESSIONS + 5):
        many.record_turn(f"s{i}", result_of("simple"), None, float(i))
    many.save()
    kept = json.loads((tmp_path / "many.json").read_text())["sessions"]
    assert len(kept) == cu.KEEP_SESSIONS and "s0" not in kept and f"s{cu.KEEP_SESSIONS + 4}" in kept


def test_a_save_with_nothing_recorded_keeps_the_file(tmp_path):
    """Codex review on #122: a first turn after a restart that records nothing (it never
    started) must not overwrite the persisted usage with an empty store."""
    path = tmp_path / "u.json"
    first = cu.UsageStore(str(path))
    first.record_turn("s1", result_of("simple"), None, 1.0)
    first.save()
    before = path.read_text()
    fresh = cu.UsageStore(str(path))
    fresh.save()  # nothing recorded, nothing loaded
    assert path.read_text() == before
    fresh.record_limits("acct", {"five_hour": {"utilization": 0.1, "resetsAt": 5}}, None, 2.0)
    fresh.save()
    data = json.loads(path.read_text())
    assert "s1" in data["sessions"] and "acct" in data["accounts"]


def test_two_stores_on_one_file_keep_each_others_entries(tmp_path):
    """Codex review on #122: two backends (or processes) saving to one file lose nothing."""
    path = str(tmp_path / "u.json")
    a, b = cu.UsageStore(path), cu.UsageStore(path)
    b.load()  # B's snapshot predates A's save
    a.record_turn("session-a", result_of("simple"), None, 1.0)
    a.save()
    b.record_turn("session-b", result_of("simple"), None, 2.0)
    b.save()
    sessions = json.loads((tmp_path / "u.json").read_text())["sessions"]
    assert set(sessions) == {"session-a", "session-b"}
    assert b.usage("session-a", "x") is not None  # B adopted A's entry on save
    assert cu.shared_store(path) is cu.shared_store(path)  # one store per file per process
    b.rename("session-b", "session-c")
    b.save()
    assert set(json.loads((tmp_path / "u.json").read_text())["sessions"]) == {
        "session-a",
        "session-c",
    }
