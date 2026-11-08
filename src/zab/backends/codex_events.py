"""Pure mapping of pinned Codex notification shapes to the shared contract (§8.5)."""

from __future__ import annotations

from typing import Any

from zab.core import presentation as view
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
    "subAgentActivity": t.ItemKind.SUBAGENT,
    "webSearch": t.ItemKind.WEB_SEARCH,
    "contextCompaction": t.ItemKind.COMPACTION,
    "plan": t.ItemKind.PLAN,
}


#: A sub-agent's `CollabAgentStatus` values while it runs (0.157.1).
RUNNING_AGENT = frozenset({"pendingInit", "running"})
#: zab's own status for a sub-agent whose state was lost with the daemon's connection.
UNKNOWN_AGENT = "unknown"
#: A `subAgentActivity` item's `kind` (0.157.1) → the status it reports. `interacted` reports
#: none: `send_message` only queues a message for an idle sub-agent, and `followup_task`, which
#: starts it again, shows in its own `turn/started` (codex-rs 0.157.1, `MessageDeliveryMode`;
#: Codex round 1 on #822).
ACTIVITY_STATUS = {"started": "running", "interrupted": "interrupted", "completed": "completed"}
#: A sub-agent's own `turn/completed` status → its `CollabAgentStatus`.
CHILD_TURN_STATUS = {"completed": "completed", "interrupted": "interrupted", "failed": "errored"}


def subagent_note(count: int) -> str:
    """Appended to a turn that ends with sub-agents still running (#660): Codex starts no turn
    when one finishes, so its answer would be lost without a word (DECISIONS, 2026-10-25)."""
    which = "A sub-agent was" if count == 1 else f"{count} sub-agents were"
    its, it = ("Its answer", "it") if count == 1 else ("Their answers", "them")
    return view.bridge_note(
        "codex",
        f"{which} still running when this turn ended. {its} won't be posted here: ask Codex "
        f"about {it} in a new message, or send !stop to stop {it}.",
    )


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
    elif item["type"] == "subAgentActivity":  # multi-agent v2's spawn, message, stop or end
        title = f"sub-agent {item.get('agentPath') or ''} {item.get('kind') or ''}"
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
        # Each thread's sub-agents (#660), by child thread id: the last status reported
        # (`CollabAgentStatus`), in the order they were spawned. Never dropped: a sub-agent that
        # ended can work again, and its own `turn/started` may come before the parent's item
        # that says it was messaged (review of #822); and `!stop` follows them, whatever their
        # state, to a running grandchild under an idle child (Codex on #825)
        self.subagents: dict[str, dict[str, str]] = {}
        # sub-agents known before a reconnection whose events this connection doesn't hear yet
        self.unheard: set[str] = set()

    def running_subagents(self, sid: str) -> list[str]:
        """The thread's sub-agents still running, as the events last reported them."""
        return [c for c, s in self.subagents.get(sid, {}).items() if s in RUNNING_AGENT]

    def reconnected(self) -> None:
        """A new connection to the daemon: what ran meanwhile, and whether it still runs, is
        unknown, and none of the old sub-agents' events will come (review of #822). Their
        states are forgotten, so that no turn notes them running for ever, and so are their
        turn ids, which `!stop` would use instead of asking the daemon for the live one; who
        spawned whom is kept. Until one's own `turn/started` comes again (the daemon
        subscribes this connection when it loads it again), a v1 `wait` reporting it running
        doesn't make it so: its end would never come (review round 2)."""
        for children in self.subagents.values():
            children.update(dict.fromkeys(children, UNKNOWN_AGENT))
            for child in children:
                self.active.pop(child, None)
                self.unheard.add(child)

    def _subagent_item(self, sid: str, item: dict[str, Any]) -> None:
        """A sub-agent's state, from the parent's items (codex-rs 0.157.1; DECISIONS,
        2026-11-05). Multi-agent v2, the VM's model's, sends a `subAgentActivity` for a spawn
        (`started`), a message to it (`interacted`, no state: `ACTIVITY_STATUS`), an
        `interrupt_agent` (`interrupted`) and its completion (`completed`, even after the
        parent's turn ended); a sub-agent's own `turn/started` says it works again. v1 sends
        `collabAgentToolCall` items: a spawn's `receiverThreadIds` are new children, any call's
        `agentsStates` their last known status.

        Only a spawn makes a sub-agent of the thread: v2's `started`, v1's `spawnAgent`. Any
        other item updates the state of a sub-agent the thread spawned, and names no new one:
        a message, an interrupt or a wait can target any thread of the daemon by id
        (`resolve_agent_target`), another thread's sub-agent included, and `!stop` walks these
        relations (Codex round 3, on #825)."""
        children = self.subagents.setdefault(sid, {})
        if item.get("type") == "collabAgentToolCall":
            spawned = [str(c) for c in item.get("receiverThreadIds") or []]
            if item.get("tool") != "spawnAgent":
                spawned = []
            for child in spawned:
                children.setdefault(child, "pendingInit")
            for child, state in (item.get("agentsStates") or {}).items():
                status = state.get("status") if isinstance(state, dict) else None
                if str(child) not in children or not isinstance(status, str):
                    continue
                if status in RUNNING_AGENT and str(child) in self.unheard:
                    continue  # its end would never reach this connection (`reconnected`)
                # a snapshot from when the call ran: the sub-agent's own turns, once seen, are
                # newer (a `wait`'s `running` after it failed: Codex round 4)
                children[str(child)] = self._observed(str(child), status)
            for child in spawned:  # likewise for one the snapshot leaves out
                children[child] = self._observed(child, children[child])
        elif item.get("type") == "subAgentActivity" and item.get("agentThreadId"):
            child = str(item["agentThreadId"])
            if item.get("kind") == "started":  # a spawn
                children[child] = self._observed(child, "running")
            elif child in self.active:
                pass  # a late end of an earlier turn: its newer one runs (review round 2)
            elif child in children and (status := ACTIVITY_STATUS.get(item.get("kind"))):
                children[child] = status  # `interacted` has none: `ACTIVITY_STATUS`

    def _observed(self, child: str, default: str) -> str:
        """A sub-agent's state from its own turns, which can come before the parent's item that
        names it or carries a snapshot of its state: running while one runs, else how its last
        one ended (a failure sends the parent no activity: Codex rounds 2 and 4 on #822);
        `default`, the item's word, if none came yet."""
        if child in self.active:
            return "running"
        if (last := self.completed.get(child)) is not None:
            return CHILD_TURN_STATUS.get(last[1].status.value, default)
        return default

    def notification(self, method: str, p: dict[str, Any]) -> list[t.BackendEvent]:
        sid = p.get("threadId", "")
        tid = p.get("turnId", "")
        key = (sid, tid)
        if method in ("turn/started", "turn/completed"):
            self.revisions[sid] = self.revisions.get(sid, 0) + 1
        if method == "turn/started":
            tid = p["turn"]["id"]
            self.active[sid] = tid
            self.unheard.discard(sid)
            for children in self.subagents.values():
                if sid in children:  # a sub-agent's own turn: the daemon sends it too (#660)
                    children[sid] = "running"
            if sid in self.totals:
                self.baselines[sid, tid] = self.totals[sid].copy()
            return [t.TurnStarted(session_id=sid, turn_id=tid)]
        if method in ("item/started", "item/completed"):
            if p["item"].get("type") in ("collabAgentToolCall", "subAgentActivity"):
                self._subagent_item(sid, p["item"])
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
            for children in self.subagents.values():
                if sid in children:  # a sub-agent's own turn: an error or a stop sends no activity
                    children[sid] = CHILD_TURN_STATUS.get(turn["status"], children[sid])
            final_text = self.last_text.pop(key, None)
            if running := self.running_subagents(sid):
                note = subagent_note(len(running))
                final_text = f"{final_text.rstrip()}\n\n{note}" if final_text else note
            summary = t.TurnSummary(
                turn_id=tid,
                status=t.TurnStatus(turn["status"]),
                final_text=final_text,
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
