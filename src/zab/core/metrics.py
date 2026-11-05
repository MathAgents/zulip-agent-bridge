"""Bounded, metadata-only interval metrics (SPEC §14)."""

from __future__ import annotations

import logging
import math
import time
from collections import Counter, OrderedDict, deque

COUNTERS = (
    "turns_started",
    "turns_completed",
    "turns_failed",
    "turns_interrupted",
    "zulip_requests",
    "zulip_429",
    "reconnects",
    "ack_untracked",
    "ack_e2e_unavailable",
    "arrival_dropped",
)
LIMIT = 10000


class Metrics:
    def __init__(self, bot: str = "", backend: str = "") -> None:
        self.bot, self.backend = bot, backend
        self.counts: Counter[str] = Counter()
        self.arrivals: OrderedDict[int, tuple[float, int | None]] = OrderedDict()
        self.latencies: deque[float] = deque(maxlen=LIMIT)
        self.e2e_latencies: deque[float] = deque(maxlen=LIMIT)
        self.ack_count = 0
        self.started = time.monotonic()
        self.backend_up: bool | None = None

    def arrived(self, message_id: int, received_at: float, timestamp: object = None) -> None:
        # Zulip timestamps are integer Unix seconds. Never invent missing server time.
        sent_at = timestamp if type(timestamp) is int and timestamp > 0 else None
        self.arrivals.setdefault(message_id, (received_at, sent_at))
        if len(self.arrivals) > LIMIT:
            self.arrivals.popitem(last=False)
            self.counts["arrival_dropped"] += 1

    def acknowledged(self, message_id: int) -> None:
        arrival = self.arrivals.pop(message_id, None)
        if arrival is None:
            self.counts["ack_untracked"] += 1
            return  # Recovery cannot invent an arrival time or a latency sample.
        received_at, sent_at = arrival
        latency = max(0, time.monotonic() - received_at)
        e2e = time.time() - sent_at if sent_at is not None else None
        if e2e is None or e2e < 0:
            e2e = None  # Future timestamps indicate skew; do not turn them into a passing zero.
            self.counts["ack_e2e_unavailable"] += 1
        else:
            self.e2e_latencies.append(e2e)
        self.ack_count += 1
        self.latencies.append(latency)
        logging.getLogger("zab.metrics").info(
            "Eyes acknowledgement delivered",
            extra={
                "event": "ack",
                "bot": self.bot,
                "backend": self.backend,
                "message_id": message_id,
                "latency_s": latency,
                "latency_e2e_s": e2e,
            },
        )

    def connection(self, up: bool) -> None:
        if up and self.backend_up is False:
            self.counts["reconnects"] += 1
        self.backend_up = up

    def emit(self) -> None:
        samples = sorted(self.latencies)
        values = {key: self.counts[key] for key in COUNTERS}
        values.update(
            ack_count=self.ack_count,
            ack_samples=len(samples),
            ack_samples_dropped=self.ack_count - len(samples),
            ack_p50_s=samples[math.ceil(len(samples) * 0.5) - 1] if samples else None,
            ack_p95_s=samples[math.ceil(len(samples) * 0.95) - 1] if samples else None,
        )
        e2e = sorted(self.e2e_latencies)
        values.update(
            ack_e2e_samples=len(e2e),
            ack_e2e_samples_dropped=self.ack_count - self.counts["ack_e2e_unavailable"] - len(e2e),
            ack_e2e_p50_s=e2e[math.ceil(len(e2e) * 0.5) - 1] if e2e else None,
            ack_e2e_p95_s=e2e[math.ceil(len(e2e) * 0.95) - 1] if e2e else None,
        )
        now = time.monotonic()
        logging.getLogger("zab.metrics").info(
            "Bridge metrics",
            extra={
                "event": "metrics",
                "bot": self.bot,
                "backend": self.backend,
                "window_s": now - self.started,
                "metrics": values,
            },
        )
        self.counts.clear()
        self.latencies.clear()
        self.e2e_latencies.clear()
        self.ack_count = 0
        self.started = now
