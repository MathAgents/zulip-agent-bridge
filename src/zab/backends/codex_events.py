"""Pure mapping of pinned Codex notification shapes to the shared contract (§8.5)."""

from __future__ import annotations

from typing import Any

from zab.core import types as t
from zab.render.redact import redact_secrets

KINDS = {
    "userMessage": t.ItemKind.USER_MESSAGE,
    "agentMessage": t.ItemKind.AGENT_MESSAGE,
    "reasoning": t.ItemKind.REASONING,
    "commandExecution": t.ItemKind.COMMAND,
    "fileChange": t.ItemKind.FILE_CHANGE,
    "mcpToolCall": t.ItemKind.TOOL_CALL,
    "collabAgentToolCall": t.ItemKind.SUBAGENT,
    "collabToolCall": t.ItemKind.SUBAGENT,
    "webSearch": t.ItemKind.WEB_SEARCH,
    "contextCompaction": t.ItemKind.COMPACTION,
    "plan": t.ItemKind.PLAN,
}


def error_info(error: dict[str, Any] | None) -> t.ErrorInfo | None:
    if not error:
        return None
    code = error.get("codexErrorInfo") or "other"
    if isinstance(code, dict):
        code = next(iter(code), "other")
    kinds = {
        "usageLimitExceeded": t.ErrorKind.USAGE_LIMIT,
        "sessionBudgetExceeded": t.ErrorKind.USAGE_LIMIT,
        "rateLimitExceeded": t.ErrorKind.USAGE_LIMIT,
        "contextWindowExceeded": t.ErrorKind.CONTEXT_WINDOW,
        "unauthorized": t.ErrorKind.AUTH,
        "sandboxError": t.ErrorKind.SANDBOX,
    }
    kind = kinds.get(code, t.ErrorKind.OTHER)
    if code.startswith(("httpConnection", "responseStream", "responseTooMany")):
        kind = t.ErrorKind.NETWORK
    return t.ErrorInfo(kind=kind, message=error.get("message", "Codex turn failed"))


def item_info(item: dict[str, Any], completed: bool = True) -> t.ItemInfo:
    kind = KINDS.get(item["type"], t.ItemKind.OTHER)
    text = item.get("text")
    title = item["type"]
    detail = None
    if kind == t.ItemKind.USER_MESSAGE:
        text = "\n".join(x["text"] for x in item.get("content", []) if x["type"] == "text")
    elif kind == t.ItemKind.REASONING:
        text = "\n".join(item.get("summary", []))
    elif kind == t.ItemKind.COMMAND:
        title = item["command"]
        detail = item.get("aggregatedOutput")
    elif kind == t.ItemKind.FILE_CHANGE:
        title = ", ".join(x["path"] for x in item.get("changes", []))
        detail = "\n".join(x.get("diff", "") for x in item.get("changes", []))
    elif kind == t.ItemKind.TOOL_CALL:
        title = f"{item.get('server', '')}.{item.get('tool', '')}"
    elif kind == t.ItemKind.WEB_SEARCH:
        title = item.get("query", "web search")
    if text:
        title = text.splitlines()[0]
    status = {
        "inProgress": t.ItemStatus.IN_PROGRESS,
        "completed": t.ItemStatus.COMPLETED,
        "failed": t.ItemStatus.FAILED,
        "declined": t.ItemStatus.DECLINED,
    }.get(item.get("status"), t.ItemStatus.COMPLETED if completed else t.ItemStatus.IN_PROGRESS)
    return t.ItemInfo(
        item_id=item["id"],
        kind=kind,
        status=status,
        # redacted before the cut, which can leave a key too short for its rule (§11.6)
        title=redact_secrets(title.replace("\x00", ""))[:120],
        text=text,
        detail=detail,
        exit_code=item.get("exitCode"),
        duration_ms=item.get("durationMs"),
        client_id=item.get("clientId"),
    )


def turn_summary(turn: dict[str, Any]) -> t.TurnSummary:
    messages = [x["text"] for x in turn.get("items", []) if x["type"] == "agentMessage"]
    return t.TurnSummary(
        turn_id=turn["id"],
        status=t.TurnStatus(turn["status"]),
        final_text=messages[-1] if messages else None,
        error=error_info(turn.get("error")),
    )


class EventMapper:
    def __init__(self) -> None:
        self.active: dict[str, str] = {}
        self.revisions: dict[str, int] = {}
        self.last_text: dict[tuple[str, str], str] = {}
        self.diffs: dict[tuple[str, str], str] = {}
        self.errors: dict[tuple[str, str], t.ErrorInfo] = {}
        self.totals: dict[str, dict[str, int]] = {}
        self.baselines: dict[tuple[str, str], dict[str, int]] = {}
        self.items: dict[str, t.ItemInfo] = {}
        self.closed: set[str] = set()
        # The last turn/completed per thread, with the revision it made: open_session reports
        # it when the turn list it read predates that completion (§7.8.1).
        self.completed: dict[str, tuple[int, t.TurnSummary]] = {}

    def notification(self, method: str, p: dict[str, Any]) -> list[t.BackendEvent]:
        sid = p.get("threadId", "")
        tid = p.get("turnId", "")
        key = (sid, tid)
        if method in ("turn/started", "turn/completed"):
            self.revisions[sid] = self.revisions.get(sid, 0) + 1
        if method == "turn/started":
            tid = p["turn"]["id"]
            self.active[sid] = tid
            if sid in self.totals:
                self.baselines[sid, tid] = self.totals[sid].copy()
            return [t.TurnStarted(session_id=sid, turn_id=tid)]
        if method in ("item/started", "item/completed"):
            item = item_info(p["item"], method == "item/completed")
            if item.kind == t.ItemKind.FILE_CHANGE and method == "item/started":
                self.items[item.item_id] = item
            elif method == "item/completed":
                self.items.pop(item.item_id, None)
            if item.kind == t.ItemKind.AGENT_MESSAGE and method == "item/completed":
                self.last_text[key] = item.text or ""
            cls = t.ItemCompleted if method == "item/completed" else t.ItemStarted
            return [cls(session_id=sid, turn_id=tid, item=item)]
        if method == "turn/plan/updated":
            statuses = {"pending": "pending", "inProgress": "in_progress", "completed": "completed"}
            return [
                t.PlanUpdated(
                    session_id=sid,
                    turn_id=tid,
                    steps=tuple((x["step"], statuses[x["status"]]) for x in p["plan"]),
                )
            ]
        if method == "turn/diff/updated":
            self.diffs[key] = p["diff"]
        elif method == "thread/tokenUsage/updated":
            usage = p["tokenUsage"]
            self.totals[sid] = usage["total"]
            return [
                t.UsageUpdated(
                    session_id=sid,
                    usage=self._usage(usage["total"], usage.get("modelContextWindow")),
                )
            ]
        elif method == "error":
            error = error_info(p.get("error"))
            if error and tid:
                self.errors[key] = error
            elif error:
                # redacted, NULs dropped first, before the prefix, which would defeat the
                # line-anchored rules (§11.6; reviews of #678)
                safe = redact_secrets(error.message.replace("\x00", ""))
                message = "Codex reported an error outside a turn: " + safe
                return [t.Notice(session_id=sid, level="error", message=message)]
        elif method == "turn/completed":
            turn = p["turn"]
            tid = turn["id"]
            key = sid, tid
            self.active.pop(sid, None)
            base = self.baselines.pop(key, None)
            remembered_error = self.errors.pop(key, None)
            usage = (
                {k: max(0, v - base.get(k, 0)) for k, v in self.totals.get(sid, {}).items()}
                if base is not None
                else {}
            )
            summary = t.TurnSummary(
                turn_id=tid,
                status=t.TurnStatus(turn["status"]),
                final_text=self.last_text.pop(key, None),
                error=(error_info(turn.get("error")) or remembered_error)
                if turn["status"] == "failed"
                else None,
            )
            self.completed[sid] = self.revisions[sid], summary
            return [
                t.TurnCompleted(
                    session_id=sid,
                    turn_id=tid,
                    status=summary.status,
                    final_text=summary.final_text,
                    error=summary.error,
                    native_diff=self.diffs.pop(key, None),
                    usage=self._usage(usage) if usage else None,
                )
            ]
        elif method == "thread/name/updated":
            return [t.SessionChanged(session_id=sid, title=p.get("threadName"))]
        elif method == "thread/closed":
            self.closed.add(sid)
        return []

    @staticmethod
    def _usage(value: dict[str, int], window: int | None = None) -> t.TokenUsage:
        return t.TokenUsage(
            input_tokens=value.get("inputTokens"),
            output_tokens=value.get("outputTokens"),
            total_tokens=value.get("totalTokens"),
            context_window=window,
        )
