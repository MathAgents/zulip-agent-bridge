import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from websockets.asyncio.server import unix_serve

from zab.backends.codex import CodexBackend
from zab.backends.codex_usage import snapshot
from zab.config import BackendConfig
from zab.core import types as t
from zab.testing.fake_codex import FakeCodex

pytestmark = pytest.mark.contract


def backend():
    result = CodexBackend("c", BackendConfig(kind="codex", codex_home="/isolated"))
    result.rpc = AsyncMock()
    result.rpc.up = True
    return result


def window(percent=12, minutes=300):
    return dict(usedPercent=percent, windowDurationMins=minutes, resetsAt=2000)


def test_all_buckets_duration_labels_and_legacy_fallback():
    buckets = {
        "codex": {"primary": window(36, 10080), "secondary": window()},
        "model": {"limitName": "Special", "secondary": window(84, 10080)},
    }
    usage = snapshot({}, None, {"rateLimitsByLimitId": buckets, "rateLimits": {}}, 1000)
    assert [w.label for w in usage.windows] == ["Weekly", "Session (5-hour)", "Weekly · Special"]
    assert usage.windows[2].used_fraction == 0.84 and usage.windows[2].resets_at == 2000
    legacy = snapshot({}, None, {"rateLimits": buckets["codex"]}, 1000)
    assert len(legacy.windows) == 2


@pytest.mark.parametrize(
    "enabled,balance,shown",
    [
        (True, "1.25", True),
        (True, "0", False),
        (False, "1", False),
        (True, None, False),
        (True, "NaN", False),
        (True, "bad", False),
    ],
)
def test_only_known_nonzero_actual_credits(enabled, balance, shown):
    limits = {"rateLimits": {"credits": {"hasCredits": enabled, "balance": balance}, "costUSD": 32}}
    result = snapshot({}, None, limits, 1000)
    assert bool(result.credits) is shown
    assert not result.models and not result.windows and result.context_tokens is None


async def test_failed_limits_keep_session_observation_and_unknown_is_not_zero():
    b = backend()
    b.usage_observations["s"] = (
        {
            "last": {"totalTokens": 45},
            "modelContextWindow": 200,
            "total": {"inputTokens": 100, "cachedInputTokens": 0},
        },
        123,
    )
    b.rpc.call.side_effect = RuntimeError("sensitive detail")
    usage = await b.usage("s")
    b.rpc.call.assert_awaited_once_with("account/rateLimits/read", {})
    assert usage.as_of == 123 and usage.limits_as_of is None
    assert usage.context_tokens == 45 and usage.context_window == 200
    assert usage.models[0].input_tokens == 100 and usage.models[0].cache_read_tokens == 0
    assert usage.models[0].output_tokens is None
    assert "sensitive detail" not in str(usage) and usage.unavailable
    assert not (await b.usage("other")).models


async def test_fake_wire_caches_latest_usage_without_consuming_events(tmp_path):
    calls = []
    fixture = tmp_path / "usage.jsonl"
    records = []
    for total, last in [(100, 45), (300, 80)]:
        records.append(
            {
                "direction": "receive",
                "message": {
                    "method": "thread/tokenUsage/updated",
                    "params": {
                        "threadId": "s",
                        "turnId": "t",
                        "tokenUsage": {
                            "total": {
                                "totalTokens": total,
                                "inputTokens": total - 10,
                                "cachedInputTokens": 20,
                                "outputTokens": 10,
                                "reasoningOutputTokens": 5,
                            },
                            "last": {"totalTokens": last},
                            "modelContextWindow": 200,
                        },
                    },
                },
            }
        )
    fixture.write_text("\n".join(json.dumps(r) for r in records))
    fake = FakeCodex(fixture)

    async def handler(ws):
        async for raw in ws:
            message = json.loads(raw)
            if "id" not in message:
                continue
            calls.append(message["method"])
            if message["method"] == "initialize":
                result = {"codexHome": str(tmp_path)}
            else:
                assert message["method"] == "account/rateLimits/read"
                result = {"rateLimits": {"primary": window()}}
            await ws.send(json.dumps({"id": message["id"], "result": result}))
            if message["method"] == "initialize":
                async for notification in fake.messages():
                    await ws.send(json.dumps(notification))

    path = str(tmp_path / "usage.sock")
    async with unix_serve(handler, path):
        b = CodexBackend("c", BackendConfig(kind="codex", codex_home=str(tmp_path), socket=path))
        await b.start()
        try:
            usages = []
            async with asyncio.timeout(2):
                while len(usages) < 2:
                    event = await b.queue.get()
                    if isinstance(event, t.UsageUpdated):
                        usages.append(event)
            result = await b.usage("s")
            assert result.context_tokens == 80 and result.models[0].input_tokens == 290
            assert result.as_of is not None and result.limits_as_of >= result.as_of
            assert usages[-1].usage.total_tokens == 300
            assert calls == ["initialize", "account/rateLimits/read"]
        finally:
            await b.close()


async def test_cancel_usage_propagates():
    b = backend()
    b.rpc.call = AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        await b.usage("s")
