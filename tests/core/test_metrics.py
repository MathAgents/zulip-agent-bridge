import asyncio
import json
import logging
import time
from types import SimpleNamespace

import pytest
from test_bridge import bridge as bridge
from test_session import actor as actor
from test_session import send

from zab.core import bridge as bridge_module
from zab.core import metrics as metrics_module
from zab.core import types as t
from zab.core.logging import JsonFormatter
from zab.core.metrics import LIMIT, Metrics

pytestmark = pytest.mark.integration


def set_clock(monkeypatch, *, monotonic=time.monotonic, wall=time.time):
    # Replace only these modules' reference, never the event loop's time module.
    clock = SimpleNamespace(monotonic=monotonic, time=wall)
    monkeypatch.setattr(bridge_module, "time", clock)
    monkeypatch.setattr(metrics_module, "time", clock)


def test_metrics_percentiles_bounds_reset_and_private_fields(caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="zab.metrics")
    clock = [0.0]
    set_clock(monkeypatch, monotonic=lambda: clock[0], wall=lambda: 100 + clock[0])
    metrics = Metrics("bot", "codex")
    for mid in range(1, 21):
        metrics.arrived(mid, 0, 90)
        clock[0] = mid
        metrics.acknowledged(mid)
    metrics.connection(True)  # initial connection isn't a reconnect
    metrics.connection(False)
    metrics.connection(False)
    metrics.connection(True)
    metrics.emit()
    record = json.loads(JsonFormatter().format(caplog.records[-1]))
    assert record["backend"] == "codex" and record["bot"] == "bot"
    assert record["metrics"]["ack_p50_s"] == 10
    assert record["metrics"]["ack_p95_s"] == 19
    assert record["metrics"]["ack_samples"] == 20
    assert record["metrics"]["ack_e2e_p50_s"] == 20
    assert record["metrics"]["ack_e2e_p95_s"] == 29
    assert record["metrics"]["ack_e2e_samples"] == 20
    assert record["metrics"]["ack_e2e_samples_dropped"] == 0
    ack = next(r for r in caplog.records if r.event == "ack")
    assert json.loads(JsonFormatter().format(ack))["latency_e2e_s"] == 11
    assert record["metrics"]["reconnects"] == 1
    assert len([r for r in caplog.records if r.event == "ack"]) == 20
    metrics.emit()
    assert caplog.records[-1].metrics["ack_p95_s"] is None
    assert caplog.records[-1].metrics["ack_count"] == 0
    assert caplog.records[-1].metrics["ack_e2e_p95_s"] is None
    assert caplog.records[-1].metrics["ack_e2e_samples"] == 0
    for mid in range(LIMIT + 1):
        metrics.arrived(mid, 0)
    assert len(metrics.arrivals) == LIMIT and metrics.counts["arrival_dropped"] == 1
    metrics.acknowledged(0)
    assert metrics.counts["ack_untracked"] == 1
    # Arbitrary extras never enter structured logs.
    caplog.records[-1].private_text = "PRIVATE_LOGIN_CODE"
    assert "PRIVATE_LOGIN_CODE" not in JsonFormatter().format(caplog.records[-1])


async def test_turn_metrics_count_outcomes_once_despite_delivery_retry(actor):
    a, fake, _ = actor
    metrics = a.poster.client.metrics
    await send(a, fake, "first")
    original = a.publish_completion

    async def fail(event):
        raise RuntimeError("private")

    a.publish_completion = fail
    event = t.TurnCompleted(session_id="native", turn_id="turn1", status=t.TurnStatus.FAILED)
    await a.complete(event)
    a.publish_completion = original
    await a.complete(event)
    await a.complete(event)  # duplicate native notification after delivery
    assert metrics.counts["turns_started"] == 1
    assert metrics.counts["turns_failed"] == 1
    await send(a, fake, "second")
    await a.complete(
        t.TurnCompleted(
            session_id="native", turn_id="turn2", status=t.TurnStatus.COMPLETED, final_text="done"
        )
    )
    assert metrics.counts["turns_started"] == 2
    assert metrics.counts["turns_completed"] == 1


async def test_listener_arrival_survives_processing_delay_until_successful_eyes(bridge, caplog):
    b, fake, _ = bridge
    caplog.set_level(logging.INFO, logger="zab.metrics")
    mid = await b.client.send(1, "topic", "@**Test Bot** PRIVATE_PROMPT")
    message = fake.messages[mid] | {"sender_id": 20}
    for queue in fake.queues.values():
        queue.clear()  # Remove the fake bot's send event; deliver the human event only.
    fake.emit({"type": "message", "message": message, "flags": ["mentioned"]})
    acknowledged = asyncio.Event()
    original_ack = b.client.metrics.acknowledged

    def ack(mid):
        original_ack(mid)
        acknowledged.set()

    b.client.metrics.acknowledged = ack
    original_events = b.client.events
    first = True

    async def events(*args):
        nonlocal first
        if first:
            first = False
            return await original_events(*args)
        await asyncio.Event().wait()  # Model the server holding an idle long poll.

    b.client.events = events
    b.listener = asyncio.create_task(b.listen())
    await asyncio.wait_for(acknowledged.wait(), 2)
    b.listener.cancel()
    assert b.client.metrics.ack_count == 1
    assert b.client.metrics.latencies[0] >= 0
    assert "PRIVATE_PROMPT" not in caplog.text
    actor = b.actors["native"]
    # A recovered duplicate reaction must not invent another latency sample.
    fake.failures.append((400, {"code": "REACTION_ALREADY_EXISTS"}))
    await actor.react(mid, "eyes")
    assert b.client.metrics.ack_count == 1


async def test_periodic_metrics_use_ten_minute_interval(bridge, monkeypatch, caplog):
    b, _, _ = bridge
    caplog.set_level(logging.INFO, logger="zab.metrics")
    b.metric_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await b.metric_task
    delays = []

    async def sleep(delay):
        delays.append(delay)
        if len(delays) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(bridge_module, "metrics_sleep", sleep)
    b.client.metrics.counts["zulip_requests"] = 7
    with pytest.raises(asyncio.CancelledError):
        await b.report_metrics()
    assert delays == [600, 600]
    assert caplog.records[-1].metrics["zulip_requests"] == 7


async def test_batch_arrivals_precede_retry_of_an_earlier_event(bridge, monkeypatch):
    from unittest.mock import AsyncMock

    from zab.zulip.client import ZulipError

    b, _, _ = bridge
    clock = [0.0]
    set_clock(monkeypatch, monotonic=lambda: clock[0])

    async def sleep(delay):
        clock[0] += delay
        await asyncio.sleep(0)

    monkeypatch.setattr(bridge_module, "sleep", sleep)
    events = [dict(id=i, type="message", message=dict(id=i, type="stream")) for i in (1, 2)]
    b.client.events = AsyncMock(side_effect=[events, events, asyncio.CancelledError()])
    failed = False

    async def handle(event):
        nonlocal failed
        if not failed:
            failed = True
            raise ZulipError("RATE_LIMIT_HIT", 429, 10)
        if event["id"] == 2:
            clock[0] += 1
            b.client.metrics.acknowledged(2)

    b.handle = handle
    with pytest.raises(asyncio.CancelledError):
        await b.listen()
    assert list(b.client.metrics.latencies) == [11]  # not 1: include the batch's retry delay


@pytest.mark.parametrize("timestamp", [None, "100", True, 0, -1, float("nan"), 102])
def test_invalid_server_time_never_becomes_a_passing_sample(timestamp, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="zab.metrics")
    set_clock(monkeypatch, wall=lambda: 101.5)
    metrics = Metrics()
    metrics.arrived(1, 0, timestamp)
    metrics.acknowledged(1)
    assert caplog.records[-1].latency_e2e_s is None
    metrics.emit()
    assert caplog.records[-1].metrics["ack_e2e_unavailable"] == 1
    assert caplog.records[-1].metrics["ack_e2e_p95_s"] is None
    assert caplog.records[-1].metrics["ack_e2e_samples_dropped"] == 0
    assert caplog.records[-1].metrics["ack_samples"] == 1  # Local sample remains usable.


def test_e2e_sample_cap_and_second_resolution(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="zab.metrics")
    set_clock(monkeypatch, wall=lambda: 101.75)
    metrics = Metrics()
    for mid in range(LIMIT + 1):
        metrics.arrived(mid, 0, 100)
        metrics.acknowledged(mid)
    metrics.emit()
    assert caplog.records[-1].metrics["ack_e2e_samples"] == LIMIT
    assert caplog.records[-1].metrics["ack_e2e_samples_dropped"] == 1
    assert caplog.records[-1].metrics["ack_e2e_p95_s"] == 1.75


async def test_e2e_includes_budget_stall_before_poll_returns(bridge, monkeypatch):
    b, _, _ = bridge
    clock = [100.0]
    set_clock(monkeypatch, monotonic=lambda: clock[0], wall=lambda: clock[0])
    polled = False

    async def events(*args):
        nonlocal polled
        if polled:
            raise asyncio.CancelledError
        polled = True
        clock[0] += 60  # Shared-budget stall before receiving an already-sent message.
        return [dict(id=1, type="message", message=dict(id=1, type="stream", timestamp=100))]

    async def handle(event):
        clock[0] += 1
        b.client.metrics.acknowledged(event["message"]["id"])

    b.client.events, b.handle = events, handle
    with pytest.raises(asyncio.CancelledError):
        await b.listen()
    assert list(b.client.metrics.latencies) == [1]
    assert list(b.client.metrics.e2e_latencies) == [61]
