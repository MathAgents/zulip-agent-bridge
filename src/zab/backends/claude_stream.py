"""Claude Code stream-json → contract events (SPEC §9.4).

Pure: `StreamParser` is fed one parsed stdout object at a time and returns the events to emit,
so it can be exercised against recorded fixtures without a process (`tests/backends/`). The
shapes were checked against Claude Code 2.1.283 (docs/DECISIONS.md, §9.4 rows).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from zab.backends.claude import WAITED_TASK_TYPES, StreamInput, run_turn, shorten
from zab.core import presentation as view
from zab.core.types import (
    BackendEvent,
    ErrorInfo,
    ErrorKind,
    ItemCompleted,
    ItemInfo,
    ItemKind,
    ItemStarted,
    ItemStatus,
    Notice,
    PlanUpdated,
    SessionChanged,
    TokenUsage,
    TurnCompleted,
    TurnStarted,
    TurnStatus,
)
from zab.render.redact import redact_secrets

log = logging.getLogger("zab.backends.claude")

PlanStep = tuple[str, str]  # (text, "pending" | "in_progress" | "completed")


# ------------------------------------------------------------------------ item mapping (§9.4)

_FILE_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}
_READ_TOOLS = {"Read", "Grep", "Glob", "LS"}
_WEB_TOOLS = {"WebSearch", "WebFetch"}
_SUBAGENT_TOOLS = {"Task", "Agent"}
_PLAN_TOOLS = {"TodoWrite", "TaskCreate", "TaskUpdate"}
_TITLE_MAX = 120


def _one_line(text: str, limit: int = _TITLE_MAX) -> str:
    return shorten(text, limit)


def _relative(path: str, cwd: str | None) -> str:
    if cwd and path.startswith(cwd.rstrip("/") + "/"):
        return path[len(cwd.rstrip("/")) + 1 :]
    return path


def classify_tool(
    name: str, tool_input: Mapping[str, Any], cwd: str | None = None
) -> tuple[ItemKind, str]:
    """Tool name and input → (`ItemKind`, one-line title), as in the §9.4 table."""
    inp = tool_input or {}
    if name == "Bash":
        return ItemKind.COMMAND, _one_line(inp.get("command") or "")
    if name in _FILE_TOOLS:
        path = inp.get("file_path") or inp.get("notebook_path") or ""
        return ItemKind.FILE_CHANGE, _one_line(_relative(str(path), cwd))
    if name in _READ_TOOLS:
        target = inp.get("file_path") or inp.get("pattern") or inp.get("path") or ""
        return ItemKind.TOOL_CALL, _one_line(f"{name} {_relative(str(target), cwd)}".rstrip())
    if name in _WEB_TOOLS:
        return ItemKind.WEB_SEARCH, _one_line(inp.get("query") or inp.get("url") or name)
    if name in _SUBAGENT_TOOLS:
        return ItemKind.SUBAGENT, _one_line(inp.get("description") or inp.get("prompt") or name)
    if name in _PLAN_TOOLS:
        return ItemKind.PLAN, _one_line(inp.get("subject") or "plan")
    m = re.fullmatch(r"mcp__(.+?)__(.+)", name)
    if m:
        return ItemKind.TOOL_CALL, _one_line(f"{m.group(1)}.{m.group(2)}")
    return ItemKind.OTHER, _one_line(name)


_AUTH_RE = re.compile(
    r"not logged in|login expired|please run /login|authentication[_ ]failed|invalid api key"
    r"|oauth token|\b401\b|unauthori[sz]ed",
    re.I,
)
#: The one refresh failure the CLI calls transient: the access token had expired and another
#: process held the OAuth refresh lock, or exited holding it. In `-p` mode the CLI reports it as
#: an API error (`error: "server_error"`) with "Failed to refresh OAuth token: another Claude
#: Code process is refreshing it or exited mid-refresh. This is usually transient; retry in a
#: minute, …"; the interactive text says "Could not refresh your login because another Claude
#: Code process is refreshing it (or exited mid-refresh)", and the internal error "… another
#: Claude Code process is holding the refresh lock" (the same in 2.1.251, 2.1.283 and 2.1.299,
#: DECISIONS). Checked before `_AUTH_RE`, which its "OAuth token" also matches: the account is
#: still signed in, so the answer is to retry, never `!login`.
_REFRESH_RACE_RE = re.compile(
    r"another claude code process is (?:refreshing|holding the refresh lock)|exited mid-refresh",
    re.I,
)
#: What the user reads when a turn still loses the refresh race after the backend's retries.
REFRESH_RACE_MESSAGE = (
    "Claude could not refresh its sign-in because another Claude process was in the middle of "
    "refreshing it; send your message again in a minute."
)
_USAGE_RE = re.compile(
    r"usage limit|rate limit|limit reached|too many requests|\b429\b|overloaded", re.I
)
_CONTEXT_RE = re.compile(r"context window|prompt is too long|context length|too many tokens", re.I)
_NETWORK_RE = re.compile(
    r"\b(?:econnreset|econnrefused|enotfound|etimedout|eai_again)\b|fetch failed|network error"
    r"|socket hang up|\b50[234]\b",
    re.I,
)
_RESET_RE = re.compile(
    r"(?:resets?|try again|available)(?: at| in)?\s+(?:in\s+)?(\d+)\s*(second|minute|hour)s?", re.I
)


def classify_error(
    code: str | None,
    text: str,
    stderr: str,
    *,
    reset_at: float | None = None,
    now: float | None = None,
) -> ErrorInfo:
    """`ErrorInfo` from the API error code, the result text and the stderr tail (§9.4, §7.9)."""
    haystack = f"{code or ''}\n{text}\n{stderr}"
    if is_refresh_race(haystack):
        # retry advice (§7.9): the CLI's own lock is stale after 60 s
        return ErrorInfo(
            kind=ErrorKind.BACKEND_UNAVAILABLE, message=REFRESH_RACE_MESSAGE, retry_after_s=60.0
        )
    kind = ErrorKind.OTHER
    retry: float | None = None
    if code in {"authentication_failed", "invalid_api_key", "unauthorized"} or _AUTH_RE.search(
        haystack
    ):
        kind = ErrorKind.AUTH
    elif code in {"rate_limit", "rate_limit_error", "usage_limit"} or _USAGE_RE.search(haystack):
        kind = ErrorKind.USAGE_LIMIT
        if reset_at is not None:
            retry = max(0.0, reset_at - (time.time() if now is None else now))
        else:
            m = _RESET_RE.search(haystack)
            if m:
                unit = {"second": 1, "minute": 60, "hour": 3600}[m.group(2).lower()]
                retry = float(m.group(1)) * unit
    elif _CONTEXT_RE.search(haystack):
        kind = ErrorKind.CONTEXT_WINDOW
    elif code in {"network", "connection_error"} or _NETWORK_RE.search(haystack):
        kind = ErrorKind.NETWORK
    # Redacted, NULs dropped first as the renderer does, before a line is taken out of it and
    # before the cut: either can leave too little of a secret for its rule (SPEC §11.6, #661).
    # Stripped before it, too: `.strip()` takes blanks that end no line (U+3000, U+00A0), so
    # `boom\nkey=<32>` + U+3000 is a whole zuliprc line only after it (#666).
    message = redact_secrets(text.replace("\x00", "").strip())
    lines = redact_secrets(stderr.replace("\x00", "").strip()).splitlines()  # may be none
    if not message and lines:
        message = lines[-1]
    if not message:
        message = f"claude exited without a result ({code or 'unknown error'})"
    return ErrorInfo(kind=kind, message=message[:1000], retry_after_s=retry)


def is_refresh_race(text: str) -> bool:
    """Whether an error text is the CLI's transient OAuth refresh failure (`_REFRESH_RACE_RE`)."""
    return bool(_REFRESH_RACE_RE.search(text))


def model_answered(obj: Mapping[str, Any]) -> bool:
    """Whether a stdout object shows the model answering, so the CLI is past its sign-in: it
    refreshes the OAuth token before its first request. An API-error message or an error
    `result` does not count, as a lost refresh race ends that way (#307, #450)."""
    kind = obj.get("type")
    if kind == "assistant":
        return not (obj.get("error") or obj.get("is_api_error_message"))
    if kind == "result":
        return not obj.get("is_error")
    if kind == "system":
        return obj.get("subtype") == "thinking_tokens"
    return kind in ("stream_event", "rate_limit_event")


def _as_int(value: Any) -> int:
    """A count from the CLI, or 0 when the field is missing or not a number."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _usage_from_result(obj: Mapping[str, Any]) -> TokenUsage:
    """Token counts, context window and cost from a `result`; an odd shape in any field (a
    string `usage`, a list `modelUsage`, a non-numeric cost) leaves that part empty instead of
    failing the turn (PM review on PR #44)."""
    u = obj.get("usage")
    u = u if isinstance(u, Mapping) else {}
    inp = _as_int(u.get("input_tokens"))
    cached = _as_int(u.get("cache_read_input_tokens")) + _as_int(
        u.get("cache_creation_input_tokens")
    )
    out = _as_int(u.get("output_tokens"))
    model_usage = obj.get("modelUsage")
    windows = [
        _as_int(m.get("contextWindow"))
        for m in (model_usage.values() if isinstance(model_usage, Mapping) else [])
        if isinstance(m, Mapping)
    ]
    cost = obj.get("total_cost_usd")
    try:
        cost_usd = float(cost) if cost is not None and not isinstance(cost, bool) else None
    except (TypeError, ValueError):
        cost_usd = None
    return TokenUsage(
        input_tokens=inp + cached,
        output_tokens=out,
        total_tokens=inp + cached + out,
        context_window=max(windows) if windows else None,
        cost_usd=cost_usd,
    )


def _sum_usage(total: TokenUsage | None, more: TokenUsage) -> TokenUsage:
    """Two results of one process: `usage` counts each result's own loop, while
    `total_cost_usd` and `modelUsage` already add up over the process (2.1.283, DECISIONS)."""
    if total is None:
        return more

    def add(a: int | None, b: int | None) -> int | None:
        return None if a is None and b is None else (a or 0) + (b or 0)

    return TokenUsage(
        input_tokens=add(total.input_tokens, more.input_tokens),
        output_tokens=add(total.output_tokens, more.output_tokens),
        total_tokens=add(total.total_tokens, more.total_tokens),
        context_window=more.context_window or total.context_window,
        cost_usd=more.cost_usd if more.cost_usd is not None else total.cost_usd,
    )


# ---------------------------------------------------------------------------- parser (§9.4)


def is_model_unavailable(obj: Mapping[str, Any]) -> bool:
    """Whether a failed `result` means the model does not exist or the account cannot use it.

    The CLI answers a missing model with exit 1, `api_error_status` 404 and "There's an issue
    with the selected model (X)…" (2.1.283 and 2.1.289, DECISIONS; the `error` fixture). Its
    stderr `[claude-code:unrecognized_model]` tag is no evidence: it also marks models that work.
    """
    text = obj.get("result")
    return bool(obj.get("is_error")) and (
        obj.get("api_error_status") == 404
        or (isinstance(text, str) and "issue with the selected model" in text)
    )


def model_unavailable_message(model: str | None) -> str:
    """The error text for a turn that failed on its model (§7.9)."""
    name = f"`{model.replace('`', '')}`" if model else "The selected model"
    return (
        f"Model {name} isn't available on this Claude account, so the turn did not run. "
        'Pick another with `!model` (for example `!model opus`), or `!model ""` for the default.'
    )


@dataclass(slots=True)
class _Pending:
    name: str
    kind: ItemKind
    title: str
    started: float


@dataclass(slots=True)
class TurnInfo:
    """What the parser learned about the turn, for cards, logs and `zab dev claude-turn`."""

    session_id: str
    turn_id: str
    model: str | None = None  # as `system/init` reports it: resolved, e.g. claude-opus-5-5
    effort: str | None = None  # the `--effort` of the argv; None = the CLI's default
    effort_supported: bool | None = None  # `per_turn_effort_active` of `system/init`
    model_unavailable: bool = False  # the turn failed on its model (is_model_unavailable)
    refresh_race: bool = False  # the turn failed on the CLI's transient refresh race
    permission_mode: str | None = None
    cli_version: str | None = None
    final_text: str | None = None
    status: TurnStatus | None = None
    error: ErrorInfo | None = None
    usage: TokenUsage | None = None
    rate_limits: Mapping[str, Any] | None = None  # the last `rate_limit_event`'s unifiedWindows
    result: Mapping[str, Any] | None = None  # the `result` object, for `claude_usage`
    num_tool_calls: int = 0
    denials: list[str] = field(default_factory=list)


#: Appended to a completed turn that ends with a background job still running (PM on issue #36,
#: Wed 17:40).
BACKGROUND_NOTE = view.bridge_note(
    "claude",
    "A background job was still running when this turn ended and has been stopped; "
    "ask again to run it in the foreground.",
)


class StreamParser:
    """Maps stream-json objects to contract events (§9.4). Pure: no I/O, no clock reads except
    `time.monotonic()` for item durations (injectable through `clock`).

    Feed each parsed stdout object to `feed()`, then call `finish()` with the exit code and
    stderr tail; it emits the final `TurnCompleted` if the CLI never produced a `result`.
    Events inside subagents (`parent_tool_use_id` set) are folded into their `SUBAGENT` item
    and not reported individually.

    `multi_result` is for steered turns (`StreamInput`), which can run several loops, each
    ending in its own `result`: every `result` then emits a `TurnCompleted` for the whole turn so
    far, with the texts of all results in order and their usage summed, and the caller keeps the
    last one. A loop that starts after a `result` and never ends in one fails the turn.

    `steers` maps the uuid of each steer written (`StreamInput.client_ids`, filled as they are
    written) to the core's `client_id`: the CLI's echo of one (`isReplay`), which comes when the
    model takes it in, is reported as `ItemCompleted(USER_MESSAGE)` with that id (#654), once;
    so is a steer that a `result` lists in `user_message_uuids` without its echo.
    """

    def __init__(
        self,
        session_id: str,
        turn_id: str,
        *,
        pgid: int | None = None,
        cwd: str | None = None,
        clock: Callable[[], float] = time.monotonic,
        plan: Mapping[str, PlanStep] | None = None,
        effort: str | None = None,
        multi_result: bool = False,
        steers: Mapping[str, str] | None = None,
    ) -> None:
        self.info = TurnInfo(session_id=session_id, turn_id=turn_id, effort=effort)
        self._steers: Mapping[str, str] = steers if steers is not None else {}
        self._taken_in: set[str] = set()  # the steers reported taken in (#654)
        #: called once, at the first object that shows the model answering (`model_answered`)
        self.on_answer: Callable[[], None] | None = None
        self._multi = multi_result
        self._texts: list[str] = []  # multi_result: the text of each result so far
        self._loop_text: str | None = None  # the last agent text since the previous result
        self._loop_open = False  # multi_result: a loop started after the last result
        self._usage_total: TokenUsage | None = None  # multi_result: summed over the results
        self._providers: set[str] = set()  # third-party providers already reported
        self._reasons: dict[str, str] = {}  # tool_use_id → Claude Code's reason for a denial
        self._denied: set[str] = set()  # the tool_use_ids of denials already reported
        self._expected_session = session_id
        self.pgid = pgid
        self._cwd = cwd
        self._clock = clock
        self._started = False
        self._completed = False
        self._pending: dict[str, _Pending] = {}
        # task id (or index) → step; seeded from the previous turn so the plan outlives a turn
        self.plan: dict[str, PlanStep] = dict(plan or {})
        self._last_agent_text: str | None = None
        self._output = False  # an item, a plan step or a successful result was reported
        self._model_output = False  # a main-thread assistant message was seen in this process
        self._held_result: Mapping[str, Any] | None = None  # see `_result`
        self._background_tasks = 0  # the CLI's own count, from `background_tasks_changed`
        self._api_error: str | None = None
        self._api_error_text: str | None = None
        self._last_user_text: str | None = None
        self._usage_reset_at: float | None = None

    @property
    def completed(self) -> bool:
        return self._completed

    @property
    def output_seen(self) -> bool:
        """Whether the turn did or said anything: a tool call, agent text, a plan step, or a
        successful result (a steered turn's earlier loop). An API error message is none of these.
        """
        return self._output

    # --- entry points

    def feed(self, obj: Mapping[str, Any]) -> list[BackendEvent]:
        """Map one stdout object to events; a malformed object is logged by type and skipped.

        The CLI's output is treated as untrusted input (§9.1): a valid but unexpected JSON
        shape must never end the turn, so every branch runs under one guard and only type
        names, never content, reach the log (§14).
        """
        if not isinstance(obj, Mapping):
            log.warning("claude: skipped a non-object stdout record (%s)", type(obj).__name__)
            return []
        if self.on_answer is not None and model_answered(obj):
            answered, self.on_answer = self.on_answer, None
            answered()
        kind = obj.get("type")
        try:
            if kind == "system":
                return self._system(obj)
            if kind == "assistant":
                return self._assistant(obj)
            if kind == "user":
                return self._user(obj)
            if kind == "result":
                return self._result(obj)
            if kind == "rate_limit_event":
                self._rate_limit(obj)
        except Exception as exc:  # deliberate breadth: the turn must survive (§9.1)
            log.warning("claude: skipped a malformed %r event (%s)", kind, type(exc).__name__)
        return []

    def finish(self, exit_code: int | None, stderr_tail: str) -> list[BackendEvent]:
        """Called after the process exited (§9.4 last row)."""
        events: list[BackendEvent] = []
        if self._completed and not self._loop_open:
            return events
        if self._held_result is not None and exit_code == 0 and not self._model_output:
            # an empty result that nothing followed (a lone `/compact`): it was the turn's end
            return self._result(self._held_result, hold=False)
        if not self._started:
            events.extend(self._start())
        events.extend(self._abandon_pending())
        classified = classify_error(
            self._api_error, self._api_error_text or "", stderr_tail, reset_at=self._usage_reset_at
        )
        message = f"claude exited with code {exit_code} without a result"
        tail = redact_secrets((self._api_error_text or stderr_tail).replace("\x00", ""))  # #661
        last_line = tail.strip().splitlines()[-1:]
        if last_line:
            # the line again, alone: after this prefix no whole-line rule can match it
            message = f"{message}: {redact_secrets(last_line[0])[:500]}"
        error = ErrorInfo(
            kind=classified.kind, message=message, retry_after_s=classified.retry_after_s
        )
        self.info.refresh_race = is_refresh_race(f"{self._api_error_text or ''}\n{stderr_tail}")
        if self.info.refresh_race:
            error = classified  # the clear message, not the CLI's "sign in again"
        self._completed = True
        self._loop_open = False
        self.info.status = TurnStatus.FAILED
        self.info.error = error
        final_text = self._last_agent_text
        parts = [*self._texts, *([self._loop_text] if self._loop_text else [])]
        if self._multi and parts:
            final_text = "\n\n".join(parts)  # the results so far, and the unfinished loop's text
        events.append(
            TurnCompleted(
                session_id=self.info.session_id,  # the id after any SessionChanged (§9.3)
                turn_id=self.info.turn_id,
                status=TurnStatus.FAILED,
                final_text=final_text,
                error=error,
                usage=self._usage_total,
            )
        )
        return events

    # --- helpers

    def start(self) -> list[BackendEvent]:
        """Emit `TurnStarted` now, with the pgid, because the process exists (§9.6, §7.8.1).

        Called from the spawn callback so that the core can persist the process group before
        any output arrives. `system/init` then repeats `TurnStarted` with the resolved model
        (SPEC B.2 amendment, issue #36).
        """
        return [] if self._started else self._start()

    def _start(self) -> list[BackendEvent]:
        self._started = True
        return [self._turn_started()]

    def _turn_started(self) -> TurnStarted:
        """The turn's actual settings so far: the argv's effort, then init's model (B.2)."""
        return TurnStarted(
            session_id=self._expected_session,
            turn_id=self.info.turn_id,
            pgid=self.pgid,
            model=self.info.model,
            effort=self.info.effort,
            effort_supported=self.info.effort_supported,
        )

    def _system(self, obj: Mapping[str, Any]) -> list[BackendEvent]:
        if obj.get("subtype") == "background_tasks_changed" and not obj.get("parent_tool_use_id"):
            # The CLI lists the running tasks and drops a finished one (2.1.283, checked live for
            # the PM on #193: `[task]` → `[]` when it completes); its entries have no status. Should
            # a later version keep finished ones with a status, only running ones count. A
            # background sub-agent is left out: the CLI waits for it, and its notification's loop
            # ends in a further `result`, so it is never stopped at the turn's end (#660).
            tasks = obj.get("tasks")
            self._background_tasks = sum(
                1
                for task in (tasks if isinstance(tasks, list) else [])
                if not isinstance(task, Mapping)
                or (
                    task.get("status") in (None, "running", "pending")
                    and task.get("task_type") not in WAITED_TASK_TYPES
                )
            )
            return []
        if obj.get("subtype") != "init":
            return []  # hook_started, hook_response, thinking_tokens, … (DECISIONS §9.4)
        if self._multi and self._completed:
            self._loop_open = True  # a steer started a further loop
        events: list[BackendEvent] = []
        model = obj.get("model")
        self.info.model = model if isinstance(model, str) and model else None
        active = obj.get("per_turn_effort_active")
        self.info.effort_supported = active if isinstance(active, bool) else None
        # One TurnStarted if the spawn callback has not sent it yet; otherwise a repeat for the
        # same turn, now with the model, which the core folds into the turn's snapshot (B.2).
        events.extend(self._start() if not self._started else [self._turn_started()])
        self.info.permission_mode = obj.get("permissionMode")
        self.info.cli_version = obj.get("claude_code_version")
        events.extend(self._check_session(obj.get("session_id")))
        source = obj.get("apiKeySource")
        if source not in (None, "none"):
            # Phase 1 runs on the account's own login (D5); a key in the environment or the
            # settings would bill someone else and bypass the account directory.
            events.append(
                Notice(
                    session_id=self.info.session_id,
                    level="warning",
                    message=f"{CLAUDE_CODE} is using an API key (source: "
                    f"{view.code(str(source))}) instead of a Claude account sign-in.",
                )
            )
        return events

    def _check_session(self, reported: Any) -> list[BackendEvent]:
        if isinstance(reported, str) and reported and reported != self.info.session_id:
            old, self.info.session_id = self.info.session_id, reported
            return [SessionChanged(session_id=old, new_session_id=reported)]
        return []

    def _assistant(self, obj: Mapping[str, Any]) -> list[BackendEvent]:
        if obj.get("parent_tool_use_id"):
            return []
        self._model_output = True
        if self._multi and self._completed:
            self._loop_open = True
        events: list[BackendEvent] = []
        if not self._started:
            events.extend(self._start())
        if obj.get("error"):
            self._api_error = str(obj["error"])
        message = obj.get("message")
        if not isinstance(message, Mapping):
            message = {}
        content = message.get("content")
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, Mapping):
                continue
            try:  # one odd block must not drop the rest of the message (PM review on PR #44)
                events.extend(self._assistant_block(obj, message, block, len(events)))
            except Exception as exc:  # deliberate breadth, as in feed() (§9.1)
                log.warning("claude: skipped a malformed assistant block (%s)", type(exc).__name__)
        return events

    def _assistant_block(
        self, obj: Mapping[str, Any], message: Mapping[str, Any], block: Mapping[str, Any], n: int
    ) -> list[BackendEvent]:
        btype = block.get("type")
        if btype == "text":
            text = block.get("text") or ""
            if not isinstance(text, str) or not text.strip():
                return []
            if obj.get("is_api_error_message") or obj.get("error"):
                self._api_error_text = text
                return []
            self._output = True
            self._last_agent_text = self._loop_text = text
            return [
                ItemCompleted(
                    session_id=self.info.session_id,
                    turn_id=self.info.turn_id,
                    item=ItemInfo(
                        item_id=f"{message.get('id') or 'msg'}:{n}",
                        kind=ItemKind.AGENT_MESSAGE,
                        status=ItemStatus.COMPLETED,
                        title=_one_line(text.splitlines()[0] if text.splitlines() else text),
                        text=text,
                    ),
                )
            ]
        if btype == "tool_use":
            return self._tool_use(block)
        return []

    def _tool_use(self, block: Mapping[str, Any]) -> list[BackendEvent]:
        self._output = True
        name = str(block.get("name") or "")
        tool_id = str(block.get("id") or f"tool{self.info.num_tool_calls}")
        tool_input = block.get("input") or {}
        kind, title = classify_tool(name, tool_input, self._cwd)
        self._pending[tool_id] = _Pending(name=name, kind=kind, title=title, started=self._clock())
        events: list[BackendEvent] = []
        if kind is not ItemKind.PLAN:  # plan tools only move the plan (§9.4 TodoWrite row)
            self.info.num_tool_calls += 1
            events.append(
                ItemStarted(
                    session_id=self.info.session_id,
                    turn_id=self.info.turn_id,
                    item=ItemInfo(
                        item_id=tool_id, kind=kind, status=ItemStatus.IN_PROGRESS, title=title
                    ),
                )
            )
        if name == "TodoWrite":
            self.plan = {
                str(i): (str(t.get("content") or ""), _plan_status(t.get("status")))
                for i, t in enumerate(tool_input.get("todos") or [])
            }
            events.append(self._plan_event())
        elif name == "TaskUpdate":
            task_id = str(tool_input.get("taskId") or "")
            status = tool_input.get("status")
            if task_id in self.plan and status in {"deleted", "removed"}:
                del self.plan[task_id]
            elif task_id:
                text = str(tool_input.get("subject") or self.plan.get(task_id, ("",))[0])
                current = self.plan.get(task_id, (text, "pending"))[1]
                self.plan[task_id] = (text or f"task {task_id}", _plan_status(status, current))
            events.append(self._plan_event())
        return events

    def _plan_event(self) -> PlanUpdated:
        steps = tuple(self.plan[k] for k in sorted(self.plan, key=_task_sort_key))
        return PlanUpdated(session_id=self.info.session_id, turn_id=self.info.turn_id, steps=steps)  # type: ignore[arg-type]

    def _user(self, obj: Mapping[str, Any]) -> list[BackendEvent]:
        if obj.get("isReplay") and not obj.get("parent_tool_use_id"):
            return self._echo(obj)
        if obj.get("parent_tool_use_id") or obj.get("isReplay"):
            return []  # a subagent's, or the CLI's echo of a line zab wrote (`StreamInput`)
        events: list[BackendEvent] = []
        message = obj.get("message")
        if not isinstance(message, Mapping):
            return events
        content = message.get("content")
        if not isinstance(content, list):
            return events
        structured = obj.get("tool_use_result")
        for block in content:
            if not isinstance(block, Mapping):
                continue
            try:  # one odd block must not drop the rest of the message (PM review on PR #44)
                events.extend(self._user_block(block, structured))
            except Exception as exc:  # deliberate breadth, as in feed() (§9.1)
                log.warning("claude: skipped a malformed user block (%s)", type(exc).__name__)
        return events

    def _echo(self, obj: Mapping[str, Any]) -> list[BackendEvent]:
        """The CLI's echo of a line zab wrote: the model has taken it in. A steer's is the
        moment the core marks its message read (#654); the prompt's, and any other, is no event.
        """
        message = obj.get("message")
        content = message.get("content") if isinstance(message, Mapping) else None
        if isinstance(content, list):
            content = "\n".join(
                str(b.get("text") or "")
                for b in content
                if isinstance(b, Mapping) and b.get("type") == "text"
            )
        return self._taken(str(obj.get("uuid")), content if isinstance(content, str) else "")

    def _taken(self, uid: str, text: str | None) -> list[BackendEvent]:
        """A steer taken in, once: its echo, or a result listing it (`user_message_uuids`)."""
        client_id = self._steers.get(uid)
        if not client_id or uid in self._taken_in:
            return []
        self._taken_in.add(uid)
        return [
            ItemCompleted(
                session_id=self.info.session_id,
                turn_id=self.info.turn_id,
                item=ItemInfo(
                    item_id=uid,
                    kind=ItemKind.USER_MESSAGE,
                    status=ItemStatus.COMPLETED,
                    title=_one_line(text or "") or "message",
                    text=text,
                    client_id=client_id,
                ),
            )
        ]

    def _user_block(self, block: Mapping[str, Any], structured: Any) -> list[BackendEvent]:
        events: list[BackendEvent] = []
        if block.get("type") == "text" and str(block.get("text") or "").strip():
            self._last_user_text = str(block["text"]).strip()  # e.g. "[Request interrupted]"
        if block.get("type") != "tool_result":
            return events
        tool_id = str(block.get("tool_use_id") or "")
        if block.get("is_error") and (reason := _approval_reason(block.get("content"))):
            self._reasons[tool_id] = reason  # for the denial notice at the `result` (#677)
        pending = self._pending.pop(tool_id, None)
        if pending is None:
            return events
        is_error = bool(block.get("is_error"))
        detail, exit_code = _result_detail(block.get("content"), structured)
        if pending.name == "TaskCreate" and isinstance(structured, Mapping):
            task = structured.get("task") or {}
            task_id = str(task.get("id") or len(self.plan) + 1)
            self.plan[task_id] = (str(task.get("subject") or pending.title), "pending")
            events.append(self._plan_event())
        if pending.kind is ItemKind.PLAN:
            return events
        events.append(
            ItemCompleted(
                session_id=self.info.session_id,
                turn_id=self.info.turn_id,
                item=ItemInfo(
                    item_id=tool_id,
                    kind=pending.kind,
                    status=ItemStatus.FAILED if is_error else ItemStatus.COMPLETED,
                    title=pending.title,
                    detail=detail,
                    exit_code=exit_code,
                    duration_ms=int((self._clock() - pending.started) * 1000),
                ),
            )
        )
        return events

    def _rate_limit(self, obj: Mapping[str, Any]) -> None:
        info = obj.get("rate_limit_info") or {}
        if isinstance(info.get("unifiedWindows"), Mapping):  # account limits, for `!usage`
            self.info.rate_limits = info["unifiedWindows"]
        reset = info.get("resetsAt")
        if isinstance(reset, int | float) and info.get("status") not in (None, "allowed"):
            self._usage_reset_at = float(reset)

    def _result(self, obj: Mapping[str, Any], *, hold: bool = True) -> list[BackendEvent]:
        subtype = str(obj.get("subtype") or "")
        failed = bool(obj.get("is_error")) or subtype.startswith("error")
        text = obj.get("result")
        # a steer the result lists as taken in, if its echo never came: `StreamInput` counts it
        # delivered, and the core marks it read (#654)
        listed = obj.get("user_message_uuids")
        taken = [
            e
            for u in (listed if isinstance(listed, list) else [])
            for e in self._taken(str(u), None)
        ]
        if (
            hold
            and not failed
            and obj.get("num_turns") == 0
            and not (isinstance(text, str) and text.strip())
            and not self._model_output
        ):
            # An empty result before any model output is not necessarily the turn's end. When a
            # resumed session has a background task's notification left from the previous
            # process, Claude Code (2.1.283) reports it first: `task_notification`, `init`, then
            # this result (`num_turns: 0`, no model call), and only then runs the prompt, with a
            # second `init` and the real result. Completing here posted "(no final message)"
            # and dropped the answer (issue #36, Drew's end-to-end test, Wed Oct 7). A lone
            # `/compact` ends with the same shape, so the result is held, not dropped: `finish`
            # uses it when the process exits without anything else.
            self._held_result = obj
            return taken
        events: list[BackendEvent] = []
        if not self._started:
            events.extend(self._start())
        events.extend(taken)
        events.extend(self._check_session(obj.get("session_id")))
        events.extend(self._abandon_pending())
        # On failure `result` holds the error text, which the core renders from ErrorInfo (§7.9);
        # the final text is then the last real agent message, if any.
        final_text = self._last_agent_text
        if not failed and isinstance(text, str) and text.strip():
            final_text = text
        usage = _usage_from_result(obj)
        if self._multi:
            # every result of a steered turn, in order (PM on issue #36, Fri Oct 9): a steer
            # that came too late for one loop gets its own loop and result. A failed loop keeps
            # its last agent text, as a failed turn does, never the result's error text
            has_text = isinstance(text, str) and bool(text.strip())
            part = text if has_text and not failed else self._loop_text
            if part:
                self._texts.append(part)
            final_text = "\n\n".join(self._texts) if self._texts else self._last_agent_text
            usage = _sum_usage(self._usage_total, usage)
            self._usage_total = usage
            self._loop_text = None
            self._loop_open = False
        if not failed and self._background_tasks:
            # `-p` stops background jobs about 5 s after the result, and nothing can wake the
            # agent for them (PM on issue #36, Wed 17:40; the instructions say so, #189)
            final_text = (final_text or "").rstrip() + "\n\n" + BACKGROUND_NOTE
        model_usage = obj.get("modelUsage")
        providers = sorted(
            {
                str(m.get("provider"))
                for m in (model_usage.values() if isinstance(model_usage, Mapping) else [])
                if isinstance(m, Mapping) and m.get("provider") not in (None, "firstParty")
            }
            - self._providers  # `modelUsage` adds up over a process's results
        )
        self._providers.update(providers)
        if providers:
            events.append(
                Notice(
                    session_id=self.info.session_id,
                    level="warning",
                    message=f"{CLAUDE_CODE} used a third-party provider for this turn: "
                    + ", ".join(view.code(p) for p in providers)
                    + ".",
                )
            )
        error: ErrorInfo | None = None
        if failed:
            message = text if isinstance(text, str) else (self._api_error_text or "")
            if not message.strip() and subtype.startswith("error"):
                message = f"claude reported {subtype}"
                if self._last_user_text:
                    message += f": {self._last_user_text}"
            error = classify_error(
                self._api_error or (subtype if subtype.startswith("error") else None),
                message,
                "",
                reset_at=self._usage_reset_at,
            )
            # the race from the result text or the API-error message, as `finish` reads it
            race_text = f"{message}\n{self._api_error_text or ''}"
            self.info.refresh_race = is_refresh_race(race_text)
            if self.info.refresh_race:
                error = classify_error(None, race_text, "")
            if is_model_unavailable(obj):
                self.info.model_unavailable = True
                error = replace(error, message=model_unavailable_message(self.info.model))
        denials = obj.get("permission_denials") or []
        if not isinstance(denials, list):
            denials = []
        # once each, should a later result of the process list them again (as `modelUsage`)
        fresh = [d for d in denials if not _denial_id(d) or _denial_id(d) not in self._denied]
        self._denied.update(_denial_id(d) for d in fresh)
        self.info.denials.extend(_denial_text(d) for d in fresh)
        if fresh:  # one notice for all of this result's denials
            events.append(
                Notice(
                    session_id=self.info.session_id,
                    level="warning",
                    message=_denial_notice(fresh, self._reasons),
                )
            )
        self._completed = True
        self._output = self._output or not failed
        self.info.result = obj
        self.info.status = TurnStatus.FAILED if failed else TurnStatus.COMPLETED
        self.info.final_text = final_text
        self.info.error = error
        self.info.usage = usage
        events.append(
            TurnCompleted(
                session_id=self.info.session_id,
                turn_id=self.info.turn_id,
                status=self.info.status,
                final_text=final_text,
                error=error,
                usage=usage,
            )
        )
        return events

    def _abandon_pending(self) -> list[BackendEvent]:
        events: list[BackendEvent] = []
        for tool_id, pending in self._pending.items():
            if pending.kind is ItemKind.PLAN:
                continue
            events.append(
                ItemCompleted(
                    session_id=self.info.session_id,
                    turn_id=self.info.turn_id,
                    item=ItemInfo(
                        item_id=tool_id,
                        kind=pending.kind,
                        status=ItemStatus.FAILED,
                        title=pending.title,
                        detail="no result before the turn ended",
                        duration_ms=int((self._clock() - pending.started) * 1000),
                    ),
                )
            )
        self._pending.clear()
        return events


def _plan_status(value: Any, default: str = "pending") -> str:
    if value in ("pending", "in_progress", "completed"):
        return str(value)
    if value in ("done", "complete", "resolved"):
        return "completed"
    if value in ("active", "started"):
        return "in_progress"
    return default


def _task_sort_key(k: str) -> tuple[int, int | str]:
    return (0, int(k)) if k.isdigit() else (1, k)


_EXIT_RE = re.compile(r"exit code[: ]+(\d+)", re.I)
_DETAIL_MAX = 2000


def _result_detail(content: Any, structured: Any) -> tuple[str | None, int | None]:
    """An output tail for the status log and the exit code when one is reported."""
    text = ""
    if isinstance(structured, Mapping) and ("stdout" in structured or "stderr" in structured):
        out, err = str(structured.get("stdout") or ""), str(structured.get("stderr") or "")
        text = out if not err else f"{out}\n[stderr]\n{err}" if out else err
    elif isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "\n".join(str(c.get("text") or "") for c in content if isinstance(c, Mapping))
    text = text.strip()
    exit_code: int | None = None
    m = _EXIT_RE.search(text)
    if m:
        exit_code = int(m.group(1))
    if not text:
        return None, exit_code
    return (text if len(text) <= _DETAIL_MAX else "…" + text[-_DETAIL_MAX:]), exit_code


def _denial_text(d: Any) -> str:
    if not isinstance(d, Mapping):
        return str(d)
    name = str(d.get("tool_name") or d.get("tool") or "tool")
    inp = d.get("tool_input") or {}
    _kind, title = classify_tool(name, inp if isinstance(inp, Mapping) else {})
    return f"{name}({title})" if title and title != name else name


def _denial_id(d: Any) -> str:
    """A denial's `tool_use_id`, "" without one."""
    return str((d.get("tool_use_id") if isinstance(d, Mapping) else None) or "")


def _denied_call(d: Any) -> str:
    """What a denial blocked, as its notice shows it: the command's start, else as in logs."""
    if isinstance(d, Mapping) and d.get("tool_name") == "Bash":
        inp = d.get("tool_input")
        return classify_tool("Bash", inp if isinstance(inp, Mapping) else {})[1] or "Bash"
    return _denial_text(d)


#: Who blocks a call, for a participant who has never used Claude Code (issue #677).
CLAUDE_CODE = "Claude Code, the program Claude runs in,"
#: Where Claude Code's reason starts in a denied call's `tool_result` (2.1.283, issue #677).
_APPROVAL = "What required approval: "
_REASON_MAX = 300


def _approval_reason(content: Any) -> str | None:
    """Claude Code's reason for a denial, from the denied call's `tool_result`: the first two
    lines after "What required approval: ", on one line and clipped, back to the last whole
    sentence when there is one; None without it (#677). NULs go before the redaction (§11.6)."""
    if isinstance(content, list):
        content = "\n".join(str(c.get("text") or "") for c in content if isinstance(c, Mapping))
    if not isinstance(content, str) or _APPROVAL not in content:
        return None
    rest = content.replace("\x00", "").split(_APPROVAL, 1)[1]
    lines = [line for line in rest.splitlines() if line.strip()]
    reason = view.clip(redact_secrets("\n".join(lines[:2])), _REASON_MAX)
    if reason.endswith("…") and (end := reason.rfind(". ")) > 0:
        reason = reason[: end + 1]  # not a fragment of the next sentence (review of #678)
    return reason or None


def _denial_notice(denials: list[Any], reasons: Mapping[str, str]) -> str:
    """One notice for a result's denied calls, in plain words (SPEC §9.4, issue #677): who
    blocked them and why they did not run, then each call's start and, when its `tool_result`
    gave one, Claude Code's reason."""
    n = len(denials)
    bash = all(isinstance(d, Mapping) and d.get("tool_name") == "Bash" for d in denials)
    what = ("command" if bash else "action") + ("" if n == 1 else "s")
    it = "It" if n == 1 else "They"
    these = "this" if n == 1 else f"these {n}"
    lines = [
        f"{CLAUDE_CODE} blocked {these} {what} under its safety rules. {it} needed approval, "
        f"which can't be given from Zulip, so {it.lower()} did not run. Claude was told and "
        "can carry on."
    ]
    for d in denials:
        lines.append("- " + view.code(_denied_call(d)))
        if reason := reasons.get(_denial_id(d)):
            lines.append("  Claude Code's reason: " + view.code(reason))
    return "\n".join(lines)


async def run_parsed_turn(
    argv: list[str],
    *,
    env: Mapping[str, str],
    cwd: str,
    prompt: str,
    parser: StreamParser,
    on_event: Callable[[BackendEvent], Any] | None = None,
    on_raw: Callable[[str, str, float], None] | None = None,
    on_start: Callable[[Any], Any] | None = None,
    turn_holder: list[Any] | None = None,
    stream_input: StreamInput | None = None,
) -> tuple[TurnInfo, int]:
    """`run_turn` with the parser attached: every stdout object becomes events (§9.4).

    `on_event` may be a coroutine function or a plain callable; `on_start` receives the
    `ClaudeTurn` once its process exists; `stream_input` makes the turn steerable. Returns
    (`TurnInfo`, exit code).
    """

    async def emit(events: list[BackendEvent]) -> None:
        for ev in events:
            if on_event is None:
                continue
            res = on_event(ev)
            if asyncio.iscoroutine(res):
                await res

    async def started(turn: Any) -> None:
        parser.pgid = turn.pgid
        if on_start is not None:
            res = on_start(turn)
            if asyncio.iscoroutine(res):
                await res
        await emit(parser.start())  # TurnStarted carries the pgid before any output (§7.8.1)

    async def on_object(obj: dict[str, Any]) -> None:
        await emit(parser.feed(obj))

    code, stderr_tail = await run_turn(
        argv,
        env=env,
        cwd=cwd,
        prompt=prompt,
        on_object=on_object,
        on_raw=on_raw,
        on_start=started,
        turn_holder=turn_holder,
        stream_input=stream_input,
    )
    await emit(parser.finish(code, stderr_tail))
    return parser.info, code
