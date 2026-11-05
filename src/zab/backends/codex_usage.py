"""Normalize Codex 0.157.1 usage observations without reading account files."""

from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from typing import Any

from zab.core import types as t


def count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def snapshot(
    tokens: dict[str, Any],
    observed: float | None,
    limits: dict[str, Any] | None,
    limits_observed: float | None,
) -> t.Usage:
    total = tokens.get("total") or {}
    last = tokens.get("last") or {}
    models = (
        (
            t.UsageTokens(
                input_tokens=count(total.get("inputTokens")),
                cache_read_tokens=count(total.get("cachedInputTokens")),
                output_tokens=count(total.get("outputTokens")),
                reasoning_tokens=count(total.get("reasoningOutputTokens")),
            ),
        )
        if total
        else ()
    )
    windows, credits, unavailable = [], [], []
    buckets = (limits or {}).get("rateLimitsByLimitId") or {}
    if not buckets and limits and limits.get("rateLimits"):
        bucket = limits["rateLimits"]
        buckets = {bucket.get("limitId") or "codex": bucket}
    if limits is None:
        unavailable.append("Account limits read failed; try !usage again")
    for key, bucket in buckets.items():
        name = bucket.get("limitName") or key
        suffix = f" · {name}" if key != "codex" else ""
        for field in ("primary", "secondary"):
            window = bucket.get(field)
            if not window:
                continue
            duration = count(window.get("windowDurationMins"))
            label = {300: "Session (5-hour)", 10080: "Weekly"}.get(
                duration, f"{duration}-minute" if duration else "Limit (duration unknown)"
            )
            percent = window.get("usedPercent")
            fraction = (
                percent / 100
                if type(percent) in (int, float) and math.isfinite(percent) and percent >= 0
                else None
            )
            windows.append(
                t.UsageWindow(
                    label=label + suffix,
                    used_fraction=fraction,
                    resets_at=count(window.get("resetsAt")),
                )
            )
        credit = bucket.get("credits") or {}
        if credit.get("hasCredits") is True and credit.get("balance") is not None:
            try:
                balance = Decimal(str(credit["balance"]))
                if balance.is_finite() and balance != 0:
                    credits.append(t.UsageCredit(label=name, balance=str(balance)))
            except InvalidOperation:
                unavailable.append("Credit balance unavailable")
    return t.Usage(
        context_tokens=count(last.get("totalTokens")),
        context_window=count(tokens.get("modelContextWindow")),
        models=models,
        windows=tuple(windows),
        credits=tuple(credits),
        as_of=observed,
        limits_as_of=limits_observed,
        unavailable=tuple(unavailable),
    )
