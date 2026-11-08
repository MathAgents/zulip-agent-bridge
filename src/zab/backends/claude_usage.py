"""Claude's `!usage` data (issue #36; DECISIONS, 2026-10-04 `!usage` row).

The CLI reports usage only during a turn, so this keeps what the last turns said:

- account limits per account directory: the last `rate_limit_event`'s `unifiedWindows`, each
  `{utilization: 0-1, resetsAt: Unix seconds}`, with the model of the turn that reported them
  and the time they were stored (`at`), which `!usage` shows as the limits' "as of"; a window
  whose reset has passed shows no figure (issue #647);
- per session: the context of the turn's last API call (the last `usage.iterations` entry,
  input + cache read + cache write) against that model's `contextWindow`, and cumulative
  per-model totals from each turn's `modelUsage`. Never `costUSD`: it is a list-price
  estimate (`costBasis: "list"`), not money spent on a subscription.

Everything is kept in a small JSON file (`ClaudeBackend` passes its path), so a bridge restart
does not lose it. It holds counts and percentages only, no prompts and no credentials.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import re
import tempfile
import threading
from collections.abc import Iterator, Mapping
from typing import Any

try:
    import fcntl
except ImportError:  # not POSIX: the in-process store still merges, without a file lock
    fcntl = None  # type: ignore[assignment]

from zab.core.types import Usage, UsageTokens, UsageWindow

log = logging.getLogger("zab.backends.claude")

#: Labels of the windows the CLI reports, as Claude on the web labels them (PM on issue #36).
WINDOW_LABELS = {"five_hour": "Session (5-hour)", "seven_day": "Weekly · all models"}
#: A weekly window for the reporting turn's model family: "Weekly · Fable" (measured with
#: `fable`, DECISIONS; Drew's Claude-web screenshot shows the same percentage under that label).
MODEL_WINDOWS = ("seven_day_overage_included",)
KEEP_SESSIONS = 500  # the newest sessions kept in the file


def model_family(model: str | None) -> str | None:
    """`claude-fable-5-1` → `Fable`, `claude-opus-5-5[1m]` → `Opus`, `claude-3-opus-…` → `Opus`."""
    m = re.match(r"claude-(?:\d+(?:[-.]\d+)*+-)?([a-z]+)", (model or "").lower())
    return m.group(1).capitalize() if m else None


def window_label(key: str, model: str | None) -> str:
    """The row label for a `unifiedWindows` key; unknown keys are humanized, never dropped."""
    if key in WINDOW_LABELS:
        return WINDOW_LABELS[key]
    if key in MODEL_WINDOWS:
        family = model_family(model)
        return f"Weekly · {family}" if family else "Weekly · model-specific"
    m = re.fullmatch(r"seven_day_([a-z0-9]+)", key)
    if m:
        return f"Weekly · {m.group(1).capitalize()}"
    return key.replace("_", " ").strip().capitalize() or "Limit"


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def _count(value: Any) -> int:
    number = _number(value)
    return int(number) if number is not None and number >= 0 else 0


def parse_windows(windows: Any) -> dict[str, list[float | None]]:
    """`unifiedWindows` → key → [utilization, resetsAt]; odd entries are skipped."""
    if not isinstance(windows, Mapping):
        return {}
    out: dict[str, list[float | None]] = {}
    for key, value in windows.items():
        if isinstance(key, str) and isinstance(value, Mapping):
            out[key] = [_number(value.get("utilization")), _number(value.get("resetsAt"))]
    return out


def parse_turn(result: Mapping[str, Any], model: str | None) -> dict[str, Any]:
    """Per-model counts, the context and its window from one turn's `result`."""
    models: dict[str, list[int]] = {}
    windows: dict[str, int] = {}
    canonical: dict[str, str] = {}
    usage_by_model = result.get("modelUsage")
    if isinstance(usage_by_model, Mapping):
        for name, mu in usage_by_model.items():
            if not isinstance(name, str) or not isinstance(mu, Mapping):
                continue
            models[name] = [
                _count(mu.get("inputTokens")),
                _count(mu.get("outputTokens")),
                _count(mu.get("cacheReadInputTokens")),
                _count(mu.get("cacheCreationInputTokens")),
                _count(mu.get("thinkingTokens")),
            ]
            if (w := _count(mu.get("contextWindow"))) > 0:
                windows[name] = w
            if isinstance(mu.get("canonicalModel"), str):
                canonical[name] = mu["canonicalModel"]
    context_window = None
    if model in windows:
        context_window = windows[model]
    else:  # `claude-opus-5-5[1m]` against a canonical name, or the only model of the turn
        base = (model or "").split("[")[0]
        matches = [w for n, w in windows.items() if base and canonical.get(n) == base]
        if len(matches) == 1:
            context_window = matches[0]
        elif len(windows) == 1:
            context_window = next(iter(windows.values()))
    context = None
    usage = result.get("usage")
    iterations = usage.get("iterations") if isinstance(usage, Mapping) else None
    if isinstance(iterations, list) and iterations and isinstance(iterations[-1], Mapping):
        last = iterations[-1]  # the turn's last API call (DECISIONS: one entry per turn)
        context = sum(
            _count(last.get(k))
            for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
        )
    return {"models": models, "context_tokens": context, "context_window": context_window}


def _pair(value: Any) -> tuple[float | None, float | None]:
    """[utilization, resetsAt] as stored; anything odd (a hand-edited file) is unknown."""
    items = list(value)[:2] if isinstance(value, list) else []
    items += [None] * (2 - len(items))
    return _number(items[0]), _number(items[1])


def _five(value: Any) -> list[int]:
    items = list(value)[:5] if isinstance(value, list) else []
    return [_count(v) for v in items] + [0] * (5 - len(items))


@contextlib.contextmanager
def _locked(path: str) -> Iterator[None]:
    """An exclusive lock on `path` (created 0600), released on exit; none without fcntl."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing releases the lock


_STORES: dict[str | None, UsageStore] = {}
_STORES_GUARD = threading.Lock()


def shared_store(path: str | None) -> UsageStore:
    """The process's one store for `path`, shared by every Claude backend that uses it."""
    with _STORES_GUARD:
        if path not in _STORES or path is None:
            _STORES[path] = UsageStore(path)
        return _STORES[path]


class UsageStore:
    """The latest observations, per account and per session, mirrored to a JSON file.

    One store per file and process (`shared_store`): every Claude backend of a bridge records
    into the same object. `save` merges only the entries this store changed into what the file
    holds now, under an exclusive lock, so another process's entries are never lost either
    (codex review on #122).
    """

    def __init__(self, path: str | None) -> None:
        self.path = path
        self.accounts: dict[str, dict[str, Any]] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        self._loaded = path is None
        self._changed_accounts: set[str] = set()
        self._changed_sessions: set[str] = set()
        self._removed_sessions: set[str] = set()
        self._guard = threading.Lock()  # saves run in worker threads

    def _read(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """The file's accounts and sessions; a missing or unreadable file is empty (logged)."""
        try:
            with open(self.path or "", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return {}, {}
        except (OSError, ValueError) as exc:
            log.warning("claude usage file unreadable (%s); starting empty", type(exc).__name__)
            return {}, {}
        if not isinstance(data, dict):
            return {}, {}
        accounts, sessions = data.get("accounts"), data.get("sessions")
        return (
            accounts if isinstance(accounts, dict) else {},
            sessions if isinstance(sessions, dict) else {},
        )

    def load(self) -> None:
        """Read the file once, before the first change or read."""
        if self._loaded:
            return
        self._loaded = True
        accounts, sessions = self._read()
        self.accounts = accounts | self.accounts
        self.sessions = sessions | self.sessions

    def save(self) -> None:
        """Merge this store's changes into the file, atomically, mode 0600, keeping the newest
        `KEEP_SESSIONS` sessions. Nothing changed (a turn that never started): no write."""
        if self.path is None:
            return
        with self._guard:
            if not (self._changed_accounts or self._changed_sessions or self._removed_sessions):
                return
            directory = os.path.dirname(self.path) or "."
            os.makedirs(directory, mode=0o700, exist_ok=True)
            with _locked(self.path + ".lock"):
                accounts, sessions = self._read()
                for key in self._changed_accounts:
                    accounts[key] = self.accounts[key]
                for key in self._removed_sessions:
                    sessions.pop(key, None)
                for key in self._changed_sessions:
                    if key in self.sessions:
                        sessions[key] = self.sessions[key]
                if len(sessions) > KEEP_SESSIONS:
                    newest = sorted(sessions, key=lambda k: _number(sessions[k].get("as_of")) or 0)
                    for old in newest[:-KEEP_SESSIONS]:
                        del sessions[old]
                fd, tmp = tempfile.mkstemp(prefix=".claude-usage-", dir=directory)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        json.dump({"accounts": accounts, "sessions": sessions}, f)
                    os.replace(tmp, self.path)
                except BaseException:
                    with contextlib.suppress(OSError):
                        os.unlink(tmp)
                    raise
            self.accounts, self.sessions = accounts, sessions  # others' entries too
            self._changed_accounts.clear()
            self._changed_sessions.clear()
            self._removed_sessions.clear()

    def record_limits(self, account: str, windows: Any, model: str | None, at: float) -> None:
        parsed = parse_windows(windows)
        if parsed:
            with self._guard:
                self.load()
                self.accounts[account] = {"windows": parsed, "model": model, "at": at}
                self._changed_accounts.add(account)

    def record_turn(self, session_id: str, result: Mapping[str, Any], model: str | None, at: float):
        turn = parse_turn(result, model)
        if not turn["models"] and turn["context_tokens"] is None:
            return
        with self._guard:
            self._record_turn(session_id, turn, at)

    def _record_turn(self, session_id: str, turn: dict[str, Any], at: float) -> None:
        self.load()
        entry = self.sessions.setdefault(session_id, {"models": {}})
        totals = entry.setdefault("models", {})
        for name, counts in turn["models"].items():
            totals[name] = [a + b for a, b in zip(_five(totals.get(name)), counts, strict=True)]
        if turn["context_tokens"] is not None:
            entry["context_tokens"] = turn["context_tokens"]
            entry["context_window"] = turn["context_window"]
        entry["as_of"] = at
        self._changed_sessions.add(session_id)

    def rename(self, old: str, new: str) -> None:
        """The CLI replaced the session id (`SessionChanged`, §9.3)."""
        with self._guard:
            self._rename(old, new)

    def _rename(self, old: str, new: str) -> None:
        self.load()
        if old in self.sessions and new not in self.sessions:
            self.sessions[new] = self.sessions.pop(old)
            self._removed_sessions.add(old)
            self._changed_sessions.discard(old)
            self._changed_sessions.add(new)

    def usage(self, session_id: str, account: str) -> Usage | None:
        """What `!usage` shows; None when nothing at all has been reported."""
        with self._guard:
            return self._usage(session_id, account)

    def _usage(self, session_id: str, account: str) -> Usage | None:
        self.load()
        limits = self.accounts.get(account)
        entry = self.sessions.get(session_id)
        if limits is None and entry is None:
            return None
        windows: list[UsageWindow] = []
        if limits is not None:
            raw = limits.get("windows") or {}
            known = [k for k in WINDOW_LABELS if k in raw]
            for key in known + [k for k in raw if k not in WINDOW_LABELS]:
                fraction, resets = _pair(raw[key])
                windows.append(
                    UsageWindow(
                        label=window_label(key, limits.get("model")),
                        used_fraction=fraction,
                        resets_at=resets,
                    )
                )
        models: list[UsageTokens] = []
        unavailable: list[str] = []
        if entry is not None:
            for name, counts in (entry.get("models") or {}).items():
                inp, out, read, write, thinking = _five(counts)
                models.append(
                    UsageTokens(
                        model=name,
                        input_tokens=inp,
                        output_tokens=out,
                        cache_read_tokens=read,
                        cache_write_tokens=write,
                        reasoning_tokens=thinking,
                    )
                )
        else:
            unavailable.append("No turn has finished in this session yet")
        return Usage(
            context_tokens=entry.get("context_tokens") if entry else None,
            context_window=entry.get("context_window") if entry else None,
            windows=tuple(windows),
            models=tuple(models),
            as_of=entry.get("as_of") if entry else None,
            limits_as_of=_number(limits.get("at")) if limits else None,
            unavailable=tuple(unavailable),
        )
