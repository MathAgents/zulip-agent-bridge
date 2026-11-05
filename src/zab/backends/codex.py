"""Codex backend using the pinned app-server protocol (SPEC §8)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
import shlex
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from zab.backends.codex_events import EventMapper, error_info, item_info, turn_summary
from zab.backends.codex_rpc import CodexRpc, RpcError
from zab.backends.codex_settings import ModelCatalog
from zab.backends.codex_usage import snapshot
from zab.config import BackendConfig
from zab.core import presentation as view
from zab.core import types as t
from zab.core.reason import must_verify
from zab.render.redact import redact_secrets

PIN = "0.157.1"
SOURCES = [
    "cli",
    "vscode",
    "exec",
    "appServer",
    "subAgent",
    "subAgentReview",
    "subAgentCompact",
    "subAgentThreadSpawn",
    "subAgentOther",
    "unknown",
]
#: `SandboxPolicy.type` (a response) to `SandboxMode` (a request), for the modes zab sets.
SANDBOX_MODES = {
    "readOnly": "read-only",
    "workspaceWrite": "workspace-write",
    "dangerFullAccess": "danger-full-access",
}
#: `!btw` (#716, SPEC §8.10): the side question's frame, as developer instructions and on its
#: input (0.157.1 doesn't reliably apply new instructions to a copied history, §8.4).
SIDE_QUESTION_FRAME = (
    "This is a side question from your user (`!btw`), asked while you work. Answer it in one "
    "reply from what this conversation already holds: the messages, your replies and the tool "
    "results so far. Don't run commands, edit files, search or call any tool for it: answer "
    "only from the conversation, and say so if it doesn't tell. The question and your answer "
    "stay out of the conversation, and your work goes on as it was."
)
#: The items a side question's turn may have; any other (a command, a file change, a tool
#: call, a web search, a sub-agent) stops it: the read-only sandbox is only the floor. A hook's
#: prompt is text the hook's own run added, not a call of the turn's (review of #739).
SIDE_ITEMS = frozenset(
    {"userMessage", "hookPrompt", "agentMessage", "reasoning", "plan", "contextCompaction"}
)
#: What a side question's fork runs without (`config` on `thread/fork`; the keys of
#: `codex-rs/core/config.schema.json` at rust-v0.157.1): the apps (connectors) and web search
#: here, and every MCP server, plugin and app of the cwd's effective config by name
#: (`_side_config`). `features.apps = false` drops the built-in `codex_apps` MCP server, which
#: 0.157.1 registers for a ChatGPT sign-in whenever that feature is on (its default), whatever
#: `apps._default` says (review of #739).
SIDE_CONFIG = {"apps._default.enabled": False, "features.apps": False, "web_search": "disabled"}
#: How much of the running turn a side question is told, newest kept (`_progress`).
PROGRESS_ITEMS, PROGRESS_CHARS, PROGRESS_ITEM_CHARS = 50, 6000, 600
#: How many pages of `mcpServerStatus/list` a side question reads; more left refuses it.
SIDE_MCP_PAGES = 10
DECISIONS = {
    t.ApprovalDecision.ACCEPT: "accept",
    t.ApprovalDecision.ACCEPT_FOR_SESSION: "acceptForSession",
    t.ApprovalDecision.DECLINE: "decline",
    t.ApprovalDecision.CANCEL: "cancel",
}


def device_code_reply(url: str, code: str) -> str:
    """`!login`'s message (SPEC §4.6): the ChatGPT setting as ChatGPT names it ("Security and
    login", "Enable device code sign-in for Codex…", as of Nov 1, 2026), then the link and code,
    and that nothing goes back into the DM: the bridge answers ✅ when the daemon reports the
    sign-in (#709, as "Start here" says)."""
    return (
        "First, in ChatGPT's settings, under **Security and login**, turn on device code sign-in "
        f"for Codex (*Enable device code sign-in for Codex*). Then open {url} and enter {code}. "
        "Nothing to send back here: I'll answer ✅ when you're done."
    )


class CodexBackend:
    kind = "codex"

    def __init__(self, name: str, config: BackendConfig) -> None:
        self.name = name
        self.config = config
        self.home = str(
            Path(config.codex_home or os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
        )
        self.capabilities = t.Capabilities(
            steer=True,
            approvals=True,
            live_attach=config.connect == "daemon",
            rename=True,
            fork=True,  # thread/fork, verified on 0.157.1 (#202, DECISIONS 2026-10-10)
            side_question=True,  # `!btw`: an ephemeral thread/fork, one read-only turn (#716)
        )
        # `!btw` (#716): the messages of each side question's ephemeral thread, by thread id,
        # and the threads of the last ones answered, whose stragglers are dropped
        self.side: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self.side_ended: deque[str] = deque(maxlen=64)
        self.side_cleanups: set[asyncio.Future[Any]] = set()  # for forks that answered late
        self.rpc: CodexRpc | None = None
        self.mapper = EventMapper()
        self.queue: asyncio.Queue[t.BackendEvent] = asyncio.Queue()
        self.task: asyncio.Task | None = None
        self.ready = asyncio.Event()
        self.settings: dict[str, t.SessionSettings] = {}
        self.defaults: dict[str, t.SessionSettings] = {}
        self.catalog = ModelCatalog()
        self.observed: dict[str, t.SessionSettings] = {}
        # The approval and sandbox a thread's start or resume reported, for a fork whose parent
        # has no explicit ones: a native fork inherits neither (#202, DECISIONS 2026-10-10).
        self.policies: dict[str, t.SessionSettings] = {}
        self.settings_pending: dict[str, t.SessionSettings] = {}
        self.specs: dict[str, t.SessionSpec] = {}
        self.pending_context: dict[str, str] = {}
        self.own_turns: set[tuple[str, str]] = set()
        self.starting: set[str] = set()
        self.pending_requests: dict[str, list[dict[str, Any]]] = {}
        self.client_ids: set[str] = set()
        self.requests: dict[str, tuple[Any, str]] = {}
        self.usage_observations: dict[str, tuple[dict[str, Any], float]] = {}
        self.epoch = ""
        self.login_events: asyncio.Queue[dict[str, Any] | None] | None = None

    async def start(self) -> None:
        if not self.task:
            self.task = asyncio.create_task(self._run())
        await asyncio.sleep(0)

    async def close(self) -> None:
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
            self.task = None
        self.ready.clear()

    async def _run(self) -> None:
        delay = 1
        while True:
            rpc = CodexRpc()
            try:
                await rpc.connect(
                    home=self.home,
                    socket=self.config.socket,
                    binary=self.config.binary or "codex",
                    spawn=self.config.connect == "spawn",
                )
                logging.getLogger(__name__).info(
                    "Connected: %s; home=%s", rpc.identity.get("userAgent", "Codex"), self.home
                )
                self.rpc = rpc
                self.observed.clear()
                self.catalog = ModelCatalog()
                self.epoch = uuid4().hex
                self.ready.set()
                delay = 1
                self.queue.put_nowait(t.BackendStatus(session_id="", up=True))
                while True:
                    message = await rpc.incoming.get()
                    if "disconnected" in message:
                        raise t.BackendUnavailable(message["disconnected"])
                    if "id" in message:
                        await self._approval(message)
                    elif (side := message.get("params", {}).get("threadId")) in self.side:
                        self.side[side].put_nowait(message)  # a side question's (#716)
                    elif side in self.side_ended:
                        pass  # its stragglers (`thread/closed`): no history, no event
                    elif message["method"] == "thread/settings/updated":
                        p = message["params"]
                        values = p["threadSettings"]
                        self.observed[p["threadId"]] = t.SessionSettings(
                            model=values["model"], effort=values.get("effort")
                        )
                        self._remember_policy(
                            p["threadId"], values.get("approvalPolicy"), values.get("sandboxPolicy")
                        )
                    elif message["method"] == "account/login/completed":
                        if self.login_events is not None:
                            self.login_events.put_nowait(message["params"])
                    elif message["method"] == "serverRequest/resolved":
                        raw_id = message["params"]["requestId"]
                        for pending in self.pending_requests.values():
                            pending[:] = [m for m in pending if m["id"] != raw_id]
                        for key, (rid, sid) in list(self.requests.items()):
                            if raw_id == rid:
                                self.requests.pop(key)
                                self.queue.put_nowait(
                                    t.ApprovalResolved(session_id=sid, request_id=key)
                                )
                    else:
                        if message["method"] == "thread/tokenUsage/updated":
                            params = message["params"]
                            self.usage_observations[params["threadId"]] = (
                                params["tokenUsage"],
                                time.time(),
                            )
                        for event in self.mapper.notification(
                            message["method"], message.get("params", {})
                        ):
                            if (
                                isinstance(event, t.ItemCompleted)
                                and event.item.kind == t.ItemKind.USER_MESSAGE
                            ):
                                if event.item.client_id not in self.client_ids:
                                    event = replace(event, item=replace(event.item, client_id=None))
                            if isinstance(event, t.TurnStarted):
                                observed = self.observed.get(event.session_id)
                                if observed:
                                    event = replace(
                                        event, model=observed.model, effort=observed.effort
                                    )
                            self.queue.put_nowait(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.queue.put_nowait(
                    t.BackendStatus(session_id="", up=False, message=type(exc).__name__)
                )
            finally:
                if self.login_events is not None:
                    self.login_events.put_nowait(None)
                self.ready.clear()
                self.rpc = None
                for key, (_, sid) in self.requests.items():
                    self.queue.put_nowait(t.ApprovalResolved(session_id=sid, request_id=key))
                self.requests.clear()
                self.pending_requests.clear()
                await rpc.close()
            await asyncio.sleep(delay * random.uniform(0.8, 1.2))  # noqa: S311
            delay = min(60, delay * 2)

    async def usage(self, session_id: str) -> t.Usage:
        limits, limits_observed = None, None
        try:
            async with asyncio.timeout(5):
                limits = await self._call("account/rateLimits/read")
            limits_observed = time.time()
        except Exception as exc:
            logging.getLogger(__name__).warning("Usage limits read failed: %s", type(exc).__name__)
        tokens, observed = self.usage_observations.get(session_id, ({}, None))
        return snapshot(tokens, observed, limits, limits_observed)

    def events(self) -> AsyncIterator[t.BackendEvent]:
        return self._events()

    async def _events(self) -> AsyncIterator[t.BackendEvent]:
        while True:
            yield await self.queue.get()

    async def _call(self, method: str, **params: Any) -> dict[str, Any]:
        if not self.rpc or not self.rpc.up:
            raise t.BackendUnavailable("Codex daemon unavailable")
        return await self.rpc.call(method, params)

    async def _kept_call(self, method: str, **params: Any) -> dict[str, Any]:
        """`_call` without the RPC's deadline: a side question's fork or turn, whose answer
        `_kept` cleans up however late it comes (codex round 4 on #739)."""
        if not self.rpc or not self.rpc.up:
            raise t.BackendUnavailable("Codex daemon unavailable")
        return await self.rpc.call(method, params, keep=True)

    async def _approval(self, message: dict[str, Any]) -> None:
        assert self.rpc
        method, p = message["method"], message.get("params", {})
        sid, tid = p.get("threadId", ""), p.get("turnId", "")
        if sid in self.side or sid in self.side_ended:  # a side question never asks (#716)
            if method in (
                "item/commandExecution/requestApproval",
                "item/fileChange/requestApproval",
            ):
                await self.rpc.send({"id": message["id"], "result": {"decision": "decline"}})
            else:
                error = {"code": -32601, "message": "not supported by zab"}
                await self.rpc.send({"id": message["id"], "error": error})
            if sid in self.side:  # declined, never shown; one after the answer, too
                self.side[sid].put_nowait({"method": "zab/declined", "params": {"request": method}})
            return
        if not (self.config.answer_external_approvals or (sid, tid) in self.own_turns):
            if sid in self.starting:
                self.pending_requests.setdefault(sid, []).append(message)
            return
        if method not in (
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        ):
            await self.rpc.send(
                {"id": message["id"], "error": {"code": -32601, "message": "not supported by zab"}}
            )
            self.queue.put_nowait(  # nothing failed for the user: information (#677)
                t.Notice(
                    session_id=sid,
                    level="info",
                    message="Codex asked for something the bridge can't do from Zulip "
                    f"({view.code(str(method))}), so the bridge declined it. Codex was told and "
                    "can carry on.",
                )
            )
            return
        key = f"{self.name}:{self.epoch}:{message['id']}"
        self.requests[key] = message["id"], sid
        item = self.mapper.items.get(p.get("itemId"))
        command = method == "item/commandExecution/requestApproval"
        self.queue.put_nowait(
            t.ApprovalRequested(
                session_id=sid,
                turn_id=tid,
                request_id=key,
                kind="command" if command else "file_change",
                summary=p.get("command") or (item.title if item else "edit files"),
                detail="\n".join(
                    filter(None, [p.get("cwd"), p.get("reason"), item.detail if item else None])
                ),
                decisions=tuple(DECISIONS),
            )
        )

    def _info(self, thread: dict[str, Any], settings: t.SessionSettings) -> t.SessionInfo:
        sid = thread["id"]
        resume = f"CODEX_HOME={shlex.quote(self.home)} codex resume {shlex.quote(sid)}"
        return t.SessionInfo(
            native_id=sid,
            cwd=thread["cwd"],
            settings=settings,
            resume_hint=resume,
            title=thread.get("name"),
            preview=thread.get("preview"),
            updated_at=thread.get("updatedAt"),
        )

    async def _identity_config(self, spec: t.SessionSpec) -> dict:
        # Resolve the cwd's effective layers through Codex, without opening config files.
        # Never replace the user's existing secret exclusions or unrelated overrides.
        result = await self._call("config/read", cwd=spec.cwd, includeLayers=False)
        return identity_config(spec, result["config"].get("shell_environment_policy") or {})

    async def create_session(self, spec: t.SessionSpec) -> t.SessionInfo:
        if spec.settings.model is not None or spec.settings.effort is not None:
            if await self.catalog.load(self) is not None:
                self.catalog.validate(spec.settings, t.SessionSettings(), t.SessionSettings())
        params = {
            "config": await self._identity_config(spec),
            "cwd": spec.cwd,
            "serviceName": "zab",
            "developerInstructions": spec.context_prompt,
        }
        for field, key in (
            ("model", "model"),
            ("approval", "approvalPolicy"),
            ("sandbox", "sandbox"),
        ):
            if value := getattr(spec.settings, field):
                selected = self.catalog.find(value) if field == "model" else None
                params[key] = selected["model"] if selected else value
        response = await self._call("thread/start", **params)
        spec = remember_identity(spec)
        info = self._info(response["thread"], spec.settings)
        self.settings[info.native_id] = spec.settings
        self._remember_defaults(info.native_id, response, spec.settings)
        self.specs[info.native_id] = spec
        self.mapper.totals[info.native_id] = {}
        await self.rename(info.native_id, spec.name)
        return info

    async def open_session(self, native_id: str, spec: t.SessionSpec) -> t.SessionInfo:
        revision = self.mapper.revisions.get(native_id, 0)
        # Restore only proven bridge-owned turns before resume can replay approvals.
        self.own_turns.update(
            (native_id, tid) for tid in spec.settings.extra.get("owned_turn_ids", [])
        )
        self.client_ids.update(spec.settings.extra.get("client_ids", []))
        # Loaded threads ignore resume's shell_environment_policy on 0.157.1.
        # Never silently use a participant's identity or interrupt another client.
        previous = self.specs.get(native_id)
        known = (
            previous is not None and previous.git_identity == spec.git_identity
        ) or spec.settings.extra.get("applied_git_identity") == identity_marker(spec)
        if not known:
            cursor = None
            while True:
                page = await self._call("thread/loaded/list", cursor=cursor, limit=100)
                if native_id in page["data"]:
                    # This thread only: the daemon is up, and its other threads reopen.
                    raise t.SessionRefused(
                        "Codex thread is already loaded with an unverified git identity; "
                        "restart its app-server when all turns are idle, then retry"
                    )
                cursor = page.get("nextCursor")
                if not cursor:
                    break
        params = {
            "threadId": native_id,
            "excludeTurns": True,
            "config": await self._identity_config(spec),
        }
        if spec.context_prompt:
            # Also install the bridge instructions when attaching a native CLI thread.
            params["developerInstructions"] = spec.context_prompt
        response = await self._call("thread/resume", **params)
        spec = remember_identity(spec)
        info = self._info(response["thread"], spec.settings)
        turns = await self._call(
            "thread/turns/list",
            threadId=native_id,
            limit=2,
            sortDirection="desc",
            itemsView="summary",
        )
        users = [
            item
            for turn in turns["data"]
            for item in reversed(turn.get("items", []))
            if item["type"] == "userMessage"
        ]
        if users:
            info.preview = item_info(users[0]).text
        for turn in turns["data"]:
            if turn["status"] == "inProgress":
                info.active_turn_id = turn["id"]
            elif info.last_turn is None:
                info.last_turn = turn_summary(turn)
        if self.mapper.revisions.get(native_id, 0) != revision:
            info.active_turn_id = self.mapper.active.get(native_id)
            # A turn whose completion the stream handled during this call, and which the list
            # shows still running or not at all, is taken as newer than the list: report it,
            # or recovery ends it as no longer active and drops its answer (§7.8.1). The
            # revision guard leaves out completions handled before the call. A turn that fell
            # off the list's two entries, behind two newer ones from another client within
            # this one call, is not told apart; recovery matches turns by id, so no answer
            # is ever given to another turn.
            seen, done = self.mapper.completed.get(native_id, (0, None))
            listed = {turn["id"] for turn in turns["data"] if turn["status"] != "inProgress"}
            if done and seen > revision and done.turn_id not in listed:
                info.last_turn = done
        elif info.active_turn_id:
            self.mapper.active[native_id] = info.active_turn_id
        else:
            self.mapper.active.pop(native_id, None)
        self.own_turns.update(
            (native_id, tid) for tid in spec.settings.extra.get("owned_turn_ids", [])
        )
        self.client_ids.update(spec.settings.extra.get("client_ids", []))
        self._remember_defaults(native_id, response, spec.settings)
        self.settings[native_id] = spec.settings
        self.specs[native_id] = spec
        # 0.157.1 accepts resume developerInstructions but does not reliably inject
        # them into existing history. Use the §8.4 first-input fallback as well.
        if spec.context_prompt:
            self.pending_context[native_id] = spec.context_prompt
        self.mapper.closed.discard(native_id)
        return info

    async def fork_session(self, native_id: str, spec: t.SessionSpec) -> t.SessionInfo:
        """An independent child thread through the parent's last ended turn (#202, SPEC §8.8).

        The boundary is the parent's newest turn no longer in progress (completed, or ended by
        `!stop` or a restart: DECISIONS, "The fork boundary"), read with `thread/turns/list`;
        `thread/fork` copies through it as `lastTurnId`, so a turn running meanwhile stays out.
        A native fork inherits neither model, effort nor sandbox (DECISIONS, 2026-10-10): the
        child gets the parent's effective settings with the spec's explicit values on top, model,
        approval and sandbox on the fork itself, then on each of its turns as the parent's own
        turns send them, effort included (`ThreadForkParams` has no effort). An approval or
        sandbox the parent runs with without having set it goes on the fork only. The parent is
        only read: no resume beyond the reopen its own next turn would make, no rename, setting
        or turn.
        """
        parent = self.specs.get(native_id)
        if parent is None:
            raise ValueError("The session to fork from isn't open.")
        if native_id in self.mapper.closed:
            await self.open_session(native_id, parent)  # as start_turn would, with its own spec
        listed = await self._call(
            "thread/turns/list",
            threadId=native_id,
            limit=2,  # one turn at most is in progress
            sortDirection="desc",
            itemsView="notLoaded",
        )
        last = next((turn["id"] for turn in listed["data"] if turn["status"] != "inProgress"), None)
        if last is None:
            raise ValueError("There is no completed turn to fork from yet.")
        base = self.effective_settings(native_id) or t.SessionSettings()
        given = spec.settings
        settings = t.SessionSettings(
            model=given.model or base.model,
            effort=given.effort or base.effort,
            approval=given.approval or base.approval,
            sandbox=given.sandbox or base.sandbox,
        )
        if settings.sandbox:
            self._sandbox_policy(settings.sandbox)  # refused before anything is created
        # What the parent runs with without having set it goes on the fork alone, where the
        # server builds the whole policy from the mode and config.toml as for thread/start; on
        # the child's turns a bare `sandboxPolicy` type would drop its network access and
        # writable roots (review of #568). Its turns send what the parent's own turns send.
        policy = self.policies.get(native_id, t.SessionSettings())
        fallback = {"approval": policy.approval, "sandbox": policy.sandbox}
        params: dict[str, Any] = {
            "threadId": native_id,
            "lastTurnId": last,
            "cwd": spec.cwd,
            "excludeTurns": True,
            "config": await self._identity_config(spec),  # the child commits as the agent (#214)
            "developerInstructions": spec.context_prompt,
        }
        for field, key in (
            ("model", "model"),
            ("approval", "approvalPolicy"),
            ("sandbox", "sandbox"),
        ):
            if value := getattr(settings, field) or fallback.get(field):
                params[key] = value
        response = await self._call("thread/fork", **params)
        spec = remember_identity(replace(spec, settings=settings))
        info = self._info(response["thread"], spec.settings)
        child = info.native_id
        self.settings[child] = spec.settings
        self._remember_defaults(child, response, spec.settings)
        self.specs[child] = spec
        # No zero usage baseline, as for a resumed thread (open_session): the child's totals may
        # include the history it copied, which its first turn must not count (review of #568).
        if spec.context_prompt:
            # As for a resumed thread: the history is the parent's, so the bridge context also
            # goes with the first input (§8.4 fallback).
            self.pending_context[child] = spec.context_prompt
        await self.rename(child, spec.name)
        return info

    async def side_question(self, native_id: str, question: str, *, timeout_s: float) -> str:
        """`!btw` (#716, SPEC §8.10): an ephemeral `thread/fork` through the last ended turn,
        then one turn on it, read-only, approval `never`, with the session's model and effort.

        The fork copies through the newest turn not in progress, as `fork_session` does; a
        running turn can't be copied (`lastTurnId`), so what it has done so far is told in the
        frame (`_progress`). The fork's messages go to the side question, never to the event
        stream or the parent's bookkeeping; an approval it asks for is declined, and an item
        other than `SIDE_ITEMS` stops the turn. Then the fork is unsubscribed and forgotten.
        """
        parent = self.specs.get(native_id)
        if parent is None:
            raise ValueError("This session isn't open yet; send !btw again in a moment.")
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        child = turn = None
        answered = False
        try:
            async with asyncio.timeout(timeout_s):
                if native_id in self.mapper.closed:
                    await self.open_session(native_id, parent)  # as its next turn would
                listed = await self._call(
                    "thread/turns/list",
                    threadId=native_id,
                    limit=2,  # one turn at most is in progress
                    sortDirection="desc",
                    itemsView="notLoaded",
                )
                turns = listed["data"]
                last = next((x["id"] for x in turns if x["status"] != "inProgress"), None)
                if last is None:
                    raise ValueError("There is no completed turn to ask about yet.")
                running = next((x["id"] for x in turns if x["status"] == "inProgress"), None)
                frame = SIDE_QUESTION_FRAME + (await self._progress(native_id, running))
                settings = self.effective_settings(native_id) or t.SessionSettings()
                instructions = "\n\n".join(filter(None, (parent.context_prompt, frame)))
                params: dict[str, Any] = {
                    "threadId": native_id,
                    "lastTurnId": last,
                    "ephemeral": True,  # "should not be materialized on disk" (0.157.1)
                    "excludeTurns": True,
                    "cwd": parent.cwd,
                    "approvalPolicy": "never",
                    "sandbox": "read-only",
                    "developerInstructions": instructions,
                    "config": await self._side_config(parent.cwd),  # no MCP servers, …
                }
                if settings.model:
                    params["model"] = settings.model
                fork = asyncio.ensure_future(self._kept_call("thread/fork", **params))
                made = await self._kept(fork, lambda r: self._end_side(r["thread"]["id"], None))
                child = made["thread"]["id"]
                self.side[child] = queue
                await self._check_side_mcp(child)  # before any turn: fail closed
                start: dict[str, Any] = {
                    "threadId": child,
                    "input": self._input(t.UserInput(text=question, client_id=""), frame),
                    "approvalPolicy": "never",
                    "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
                }
                if settings.model:
                    start["model"] = settings.model
                if settings.effort:
                    start["effort"] = settings.effort
                begun = asyncio.ensure_future(self._kept_call("turn/start", **start))
                thread: str = child
                started = await self._kept(begun, lambda r: self._end_side(thread, r["turn"]["id"]))
                turn = started["turn"]["id"]
                answer = await self._side_answer(queue)
                answered = True
                return answer
        finally:
            if child is not None:
                await self._end_side(child, None if answered else turn)

    async def _side_config(self, cwd: str) -> dict[str, Any]:
        """`SIDE_CONFIG`, and `enabled = false` for every MCP server, plugin and app entry the
        cwd's effective config names (`config/read`, as for the identity policy): MCP tools run
        outside the exec sandbox, and the item that shows one arrives once the call has gone
        out (review of #739). A plugin may bring MCP servers, hooks and skills of its own, and
        an app entry may turn an app on over `_default`. A name a dotted key can't address
        refuses the side question, rather than leave it on. These tables are no typed field of
        `config/read`'s answer: a server that leaves them out turns nothing off, which
        `_check_side_mcp` then catches. What was found is logged (names only, SPEC §14)."""
        config = (await self._call("config/read", cwd=cwd, includeLayers=False))["config"]
        overrides: dict[str, Any] = dict(SIDE_CONFIG)
        found: dict[str, list[str]] = {}
        for table in ("mcp_servers", "plugins", "apps"):
            names = [
                n for n in config.get(table) or {} if not (table == "apps" and n == "_default")
            ]
            found[table] = names
            for name in names:
                if not name or "." in name:
                    raise ValueError(
                        f"A side question can't turn off this agent's {table} entry "
                        f"{view.code(name)}, so the bridge didn't ask it."
                    )
                overrides[f"{table}.{name}.enabled"] = False
        logging.getLogger(__name__).info(
            "Side question config: config/read %s mcp_servers; off: %s",
            "reported" if "mcp_servers" in config else "did not report",
            "; ".join(f"{table} {', '.join(names) or '-'}" for table, names in found.items()),
            extra={"event": "side_question_config"},
        )
        return overrides

    async def _check_side_mcp(self, child: str) -> None:
        """Fail closed (review of #739): the fork's MCP servers as `mcpServerStatus/list`
        reports them for its thread; any that isn't `disabled` (running, starting, not started
        yet, failed, unknown) refuses the side question before its turn, as does a listing
        that can't be read. Logged with each server's state (names only)."""
        servers: list[dict[str, Any]] = []
        cursor = None
        try:
            for _ in range(SIDE_MCP_PAGES):
                page = await self._call(
                    "mcpServerStatus/list", threadId=child, detail="toolsAndAuthOnly", cursor=cursor
                )
                servers += page.get("data", [])
                if not (cursor := page.get("nextCursor")):
                    break
            else:  # pages left unread: an incomplete listing proves nothing (codex on #739)
                raise RuntimeError("incomplete listing")
        except Exception as exc:
            raise ValueError(
                "The bridge couldn't check that this agent's MCP servers are off for the side "
                f"question ({type(exc).__name__}), so it didn't ask it."
            ) from exc
        states = {str(s.get("name")): s.get("runtimeStatus") for s in servers}
        logging.getLogger(__name__).info(
            "Side question MCP servers: %s",
            ", ".join(f"{name}={state}" for name, state in states.items()) or "none",
            extra={"event": "side_question_mcp"},
        )
        on = {name: state for name, state in states.items() if state != "disabled"}
        if on:
            listed = ", ".join(
                f"{view.code(name)} ({state or 'unknown'})" for name, state in on.items()
            )
            raise ValueError(
                f"This agent's MCP servers aren't off for the side question: {listed}, so the "
                "bridge didn't ask it."
            )

    async def _kept(
        self, call: asyncio.Future[dict[str, Any]], late: Callable[[dict[str, Any]], Awaitable[Any]]
    ) -> dict[str, Any]:
        """`call`'s result. Should the side question give up first (its timeout, a shutdown),
        the request goes on, and `late` runs on its result once it comes: the fork or turn the
        server made is still stopped and unsubscribed (codex round 2 on #739). Also when the
        request has just answered and the cancel wins the race to wake this task: the callback
        then runs at once, and `_late` skips a request that failed (codex round 3)."""
        try:
            return await asyncio.shield(call)
        except BaseException:
            call.add_done_callback(lambda done: self._late(done, late))
            raise

    def _late(
        self, done: asyncio.Future[dict[str, Any]], late: Callable[[dict[str, Any]], Awaitable[Any]]
    ) -> None:
        if done.cancelled() or done.exception() is not None:
            return  # nothing was made, or the daemon is gone
        task = asyncio.ensure_future(late(done.result()))
        self.side_cleanups.add(task)
        task.add_done_callback(self.side_cleanups.discard)

    async def _side_answer(self, queue: asyncio.Queue[dict[str, Any]]) -> str:
        """The side question's last agent message once its turn completes; ValueError with a
        user-facing reason for a tool the turn reached for, or a turn that didn't complete."""
        text, error = "", None
        while True:
            message = await queue.get()
            method, p = message["method"], message.get("params", {})
            item = p.get("item") or {}
            if method == "item/started" and item.get("type") not in SIDE_ITEMS:
                raise ValueError(
                    f"Codex reached for a tool ({view.code(str(item.get('type')))}) to answer "
                    "the side question, so the bridge stopped it; ask in the conversation."
                )
            if method == "item/completed" and item.get("type") == "agentMessage":
                text = item.get("text") or text
            elif method == "error":
                error = error_info(p.get("error")) or error
            elif method == "turn/completed":
                status = p["turn"]["status"]
                if status == "completed":
                    return text
                error = error_info(p["turn"].get("error")) or error
                if error is not None and error.kind == t.ErrorKind.AUTH:
                    raise ValueError(
                        "This agent isn't connected to your ChatGPT subscription yet: send me "
                        "!login in a direct message."
                    )
                detail = redact_secrets(error.message.replace("\x00", "")) if error else ""
                line = (detail.splitlines() or [status])[0][:300]
                kind = error.kind.value if error else status
                raise ValueError(f"Codex couldn't answer ({kind}): {line}")

    async def _progress(self, sid: str, turn_id: str | None) -> str:
        """What the running turn has done so far, in words, for a side question's frame: the
        user's messages and the agent's whole, the rest by title, newest kept. "" when there is
        no running turn or its items can't be read."""
        if turn_id is None:
            return ""
        try:
            page = await self._call(
                "thread/items/list",
                threadId=sid,
                turnId=turn_id,
                limit=PROGRESS_ITEMS,
                sortDirection="desc",
            )
            infos = [item_info(entry["item"], completed=False) for entry in page.get("data", [])]
        except Exception as exc:  # the answer goes on through the last ended turn
            logging.getLogger(__name__).info("Side question progress: %s", type(exc).__name__)
            return ""
        lines, left = [], PROGRESS_CHARS
        for info in infos:  # newest first
            if info.kind == t.ItemKind.REASONING:
                continue
            who = {t.ItemKind.USER_MESSAGE: "your user", t.ItemKind.AGENT_MESSAGE: "you"}
            body = info.text if info.kind in who else info.title
            line = f"- {who.get(info.kind, info.kind.value)}: {' '.join((body or '').split())}"
            line = line[:PROGRESS_ITEM_CHARS]
            if len(line) > left:
                break
            lines.append(line)
            left -= len(line) + 1
        if not lines:
            return ""
        return (
            "\n\nThe turn still running in the conversation, which this copy doesn't hold, has "
            "done this so far (the bridge's summary, oldest first):\n" + "\n".join(reversed(lines))
        )

    async def _end_side(self, child: str, turn: str | None) -> None:
        """Stop the side question's turn if it still runs, unsubscribe from its ephemeral
        thread (best effort), and forget it: nothing of it stays in this backend's state. Each
        call has its own limit and failure: a turn that ended meanwhile may refuse its
        interrupt, and the unsubscribe is tried anyway (codex round 1 on #739)."""
        calls = [("thread/unsubscribe", {"threadId": child})]
        if turn is not None:
            calls.insert(0, ("turn/interrupt", {"threadId": child, "turnId": turn}))
        for method, params in calls:
            try:
                async with asyncio.timeout(10):
                    await self._call(method, **params)
            except Exception as exc:
                logging.getLogger(__name__).info("Side question %s: %s", method, type(exc).__name__)
        self.side.pop(child, None)
        self.side_ended.append(child)
        for state in (
            self.observed,
            self.policies,
            self.usage_observations,
            self.mapper.active,
            self.mapper.revisions,
            self.mapper.totals,
            self.mapper.completed,
        ):
            state.pop(child, None)
        self.mapper.closed.discard(child)

    async def lookup_session(self, native_id: str) -> t.SessionInfo | None:
        """Read native metadata without opening a thread (SPEC §8.7)."""
        try:
            result = await self._call("thread/read", threadId=native_id, includeTurns=False)
        except RpcError as exc:
            if exc.code == -32600 and str(exc).startswith(
                ("thread not loaded:", "invalid thread id:")
            ):
                return None
            raise
        return self._info(result["thread"], self.settings.get(native_id, t.SessionSettings()))

    async def list_sessions(self, cwd: str | None, limit: int = 20) -> list[t.SessionInfo]:
        result = await self._call(
            "thread/list", cwd=cwd, limit=limit, sortKey="updated_at", sourceKinds=SOURCES
        )
        return [self._info(x, t.SessionSettings()) for x in result["data"]]

    @staticmethod
    def _input(value: t.UserInput, context: str = "") -> list[dict[str, str]]:
        text = (
            f"<zab-context>\n{context}\n</zab-context>\n\n{value.text}" if context else value.text
        )
        parts = [{"type": "text", "text": text}]
        parts.extend(
            {"type": "localImage", "path": a.path}
            for a in value.attachments
            if a.mime_type.startswith("image/")
        )
        return parts

    async def start_turn(self, session_id: str, turn_input: t.UserInput) -> str:
        if session_id in self.mapper.closed:
            await self.open_session(session_id, self.specs[session_id])
        settings = self.effective_settings(session_id) or self.settings[session_id]
        params = {
            k: v
            for k, v in {
                "model": settings.model,
                "effort": settings.effort,
                "approvalPolicy": settings.approval,
            }.items()
            if v
        }
        if settings.sandbox:
            params["sandboxPolicy"] = self._sandbox_policy(settings.sandbox)
        self.client_ids.add(turn_input.client_id)
        self.starting.add(session_id)
        try:
            result = await self._call(
                "turn/start",
                threadId=session_id,
                clientUserMessageId=turn_input.client_id,
                input=self._input(turn_input, self.pending_context.get(session_id, "")),
                **params,
            )
            self.pending_context.pop(session_id, None)
            self.settings_pending.pop(session_id, None)
            tid = result["turn"]["id"]
            self.own_turns.add((session_id, tid))
            self.mapper.active[session_id] = tid
            self.starting.discard(session_id)
            for message in self.pending_requests.pop(session_id, []):
                await self._approval(message)
            return tid
        finally:
            self.starting.discard(session_id)
            self.pending_requests.pop(session_id, None)

    async def steer(self, session_id: str, turn_id: str, turn_input: t.UserInput) -> None:
        self.client_ids.add(turn_input.client_id)
        try:
            await self._call(
                "turn/steer",
                threadId=session_id,
                expectedTurnId=turn_id,
                clientUserMessageId=turn_input.client_id,
                input=self._input(turn_input, self.pending_context.get(session_id, "")),
            )
            self.pending_context.pop(session_id, None)
        except RpcError as exc:
            raise t.SteerRejected(str(exc)) from exc

    async def interrupt(self, session_id: str) -> None:
        if tid := self.mapper.active.get(session_id):
            await self._call("turn/interrupt", threadId=session_id, turnId=tid)

    async def respond(self, request_id: str, decision: t.ApprovalDecision) -> None:
        if not self.rpc or request_id not in self.requests:
            raise t.BackendUnavailable("approval no longer pending")
        rid, _ = self.requests[request_id]
        await self.rpc.send({"id": rid, "result": {"decision": DECISIONS[decision]}})
        self.requests.pop(request_id, None)

    @staticmethod
    def _sandbox_policy(mode: str) -> dict[str, str]:
        types = {
            "read-only": "readOnly",
            "workspace-write": "workspaceWrite",
            "danger-full-access": "dangerFullAccess",
        }
        if mode not in types:
            raise ValueError("unsupported Codex sandbox policy")
        return {"type": types[mode]}

    def _remember_defaults(
        self, sid: str, response: dict[str, Any], overrides: t.SessionSettings
    ) -> None:
        resolved = t.SessionSettings(
            model=response.get("model"), effort=response.get("reasoningEffort")
        )
        self.observed[sid] = resolved
        self._remember_policy(sid, response.get("approvalPolicy"), response.get("sandbox"))
        if sid not in self.defaults:
            self.defaults[sid] = t.SessionSettings(
                model=resolved.model if overrides.model is None else None,
                effort=resolved.effort if overrides.effort is None else None,
            )
        if any(
            getattr(overrides, key) is not None
            and getattr(overrides, key) != getattr(resolved, key)
            for key in ("model", "effort")
        ):
            self.settings_pending[sid] = self.catalog.effective(overrides, self.defaults[sid])

    def _remember_policy(self, sid: str, approval: Any, sandbox: Any) -> None:
        """The approval and sandbox mode a thread runs with, from its start or resume response
        or a `thread/settings/updated`: a fork's fallback when its parent has none set (#202)."""
        self.policies[sid] = t.SessionSettings(
            approval=approval if isinstance(approval, str) else None,  # not a granular policy
            sandbox=SANDBOX_MODES.get(sandbox.get("type")) if isinstance(sandbox, dict) else None,
        )

    def effective_settings(self, session_id: str) -> t.SessionSettings | None:
        settings = self.settings.get(session_id)
        if settings is None:
            return None
        if session_id in self.settings_pending:
            return replace(self.settings_pending[session_id])
        effective = self.catalog.effective(
            settings, self.defaults.get(session_id, t.SessionSettings())
        )
        observed = self.observed.get(session_id)
        if observed and session_id not in self.settings_pending:
            effective = replace(
                effective,
                model=observed.model or effective.model,
                effort=observed.effort or effective.effort,
            )
        return effective

    async def update_settings(
        self, session_id: str, settings: t.SessionSettings
    ) -> t.SessionSettings:
        if settings.sandbox:
            self._sandbox_policy(settings.sandbox)
        previous = self.settings.get(session_id, t.SessionSettings())
        current = self.effective_settings(session_id)
        default = self.defaults.get(session_id, t.SessionSettings())
        candidate = settings
        # An effort-only change must keep a terminal-selected (or pending) model.
        if settings.model == previous.model and current and current.model:
            candidate = replace(settings, model=current.model)
            previous = replace(previous, model=current.model)
        if await self.catalog.load(self) is not None:
            candidate = self.catalog.validate(candidate, previous, default)
        elif must_verify.get():
            # An unmentioned `!model`/`!effort` (#713): unverified, it may be Claude's value.
            original = self.settings.get(session_id, t.SessionSettings())
            field = "model" if settings.model != original.model else "effort"
            value = settings.model if field == "model" else settings.effort
            raise t.InvalidSetting(field, value, [], "Codex's model list is unavailable.")
        accepted = replace(
            settings,
            model=candidate.model if settings.model is not None else None,
            effort=candidate.effort,
            extra=dict(settings.extra),
        )
        if "applied_git_identity" in previous.extra:
            accepted.extra["applied_git_identity"] = previous.extra["applied_git_identity"]
        self.settings[session_id] = accepted
        self.settings_pending[session_id] = self.catalog.effective(candidate, default)
        if session_id in self.specs:
            self.specs[session_id] = replace(self.specs[session_id], settings=accepted)
        return accepted

    async def rename(self, session_id: str, name: str) -> None:
        await self._call("thread/name/set", threadId=session_id, name=name.removeprefix("✔ "))

    def workspace(self, root: str, cwd: str) -> t.Workspace:
        from zab.workspace.local import LocalWorkspace

        return LocalWorkspace(root, cwd)

    async def login(
        self,
        account: str | None,
        send: Callable[[str], Awaitable[None]],
        receive: Callable[[float], Awaitable[str | None]],
    ) -> bool:
        if self.login_events is not None or account is not None:
            return False  # One Codex account/home; named accounts are Claude-only.
        rpc = self.rpc
        if rpc is None or not rpc.up:
            raise t.BackendUnavailable("Codex daemon unavailable")
        events: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.login_events = events  # Install before start: completion can race the response.
        login_id = None
        completed = False
        start = asyncio.create_task(rpc.call("account/login/start", {"type": "chatgptDeviceCode"}))
        try:
            result = await asyncio.shield(start)
            login_id = result["loginId"]
            await send(device_code_reply(result["verificationUrl"], result["userCode"]))
            while (event := await events.get()) is not None:
                if event.get("loginId") == login_id:
                    completed = True
                    return event["success"] is True
            return False
        finally:
            try:
                # Cancellation may arrive before start returns its ID. Finish that RPC
                # (transport-bounded) so its newly allocated flow can still be cancelled.
                if login_id is None:
                    with contextlib.suppress(Exception):
                        login_id = (await start)["loginId"]
                if login_id and not completed:
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(
                            rpc.call("account/login/cancel", {"loginId": login_id}), 10
                        )
            finally:
                self.login_events = None

    async def signed_in(self) -> bool | None:
        """Whether the daemon has an account (#687): the doctor's `codex:login`, a local
        `account/read` that refreshes no token; None when the daemon can't answer. A daemon
        still connecting (a VM just woken) is waited for, as long as the caller allows."""
        await self.ready.wait()
        try:
            account = await self._call("account/read", refreshToken=False)
        except (t.BackendUnavailable, RpcError):
            return None
        return account.get("account") is not None

    async def doctor(self) -> list[t.Check]:
        results = []
        try:
            account = await self._call("account/read", refreshToken=False)
            results.extend(
                [
                    t.Check(name="codex:connection", ok=True, detail=self.home),
                    t.Check(
                        name="codex:login",
                        ok=account.get("account") is not None,
                        detail="signed in"
                        if account.get("account") is not None
                        else "not signed in: send !login to this bot in a direct message",
                    ),
                ]
            )
        except (t.BackendUnavailable, RpcError):
            results.append(t.Check(name="codex:connection", ok=False, detail="daemon unavailable"))
        env = dict(os.environ, CODEX_HOME=self.home)
        for key in ("OPENAI_API_KEY", "CODEX_ACCESS_TOKEN"):
            env.pop(key, None)
        for name, argv in (
            ("version", [self.config.binary or "codex", "--version"]),
            ("daemons", ["pgrep", "-u", str(os.getuid()), "-fc", r"(^|/)codex app-server( |$)"]),
        ):
            proc = None
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                output, _ = await asyncio.wait_for(proc.communicate(), 5)
                value = output.decode().strip()
                if name == "version":
                    results.append(
                        t.Check(
                            name="codex:version",
                            ok=proc.returncode == 0,
                            warning=value != f"codex-cli {PIN}",
                            detail=f"{value}; pinned {PIN}",
                        )
                    )
                else:
                    count = int(value)
                    results.append(
                        t.Check(
                            name="codex:daemons",
                            ok=True,
                            warning=count > 1,
                            detail=f"{count} app-server processes; verify separate homes",
                        )
                    )
            except (OSError, ValueError, TimeoutError):
                results.append(
                    t.Check(
                        name=f"codex:{name}",
                        ok=name == "daemons",
                        warning=name == "daemons",
                        detail="check unavailable",
                    )
                )
            finally:
                if proc and proc.returncode is None:
                    proc.kill()
                    await proc.wait()
        return results


GIT_KEYS = ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL")
BOT_KEY = "ZAB_BOT"  # the session's bot, for `zab history` (issue #248)


def identity_marker(spec: t.SessionSpec) -> dict[str, str] | str:
    who = spec.git_identity
    return {"name": who.name, "email": who.email} if who else "off"


def remember_identity(spec: t.SessionSpec) -> t.SessionSpec:
    """Record only after start/resume succeeded; core persists returned settings."""
    return replace(
        spec,
        settings=replace(
            spec.settings,
            extra=spec.settings.extra | {"applied_git_identity": identity_marker(spec)},
        ),
    )


def identity_config(spec: t.SessionSpec, policy: dict | None = None) -> dict:
    # config/read reports unset policy fields as explicit null, not as missing keys.
    policy = policy or {}
    who = spec.git_identity
    values = dict(policy.get("set") or {})
    if who:
        values.update(zip(GIT_KEYS, (who.name, who.email, who.name, who.email), strict=True))
    if spec.bot:
        values[BOT_KEY] = spec.bot
    # Explicit overrides apply after exclusions; off also strips inherited identity, and no
    # bot strips an inherited ZAB_BOT. A thread that is already loaded keeps the environment
    # it was loaded with (DECISIONS, 2026-10-11). Unlike an unknown git identity, that is not
    # refused for ZAB_BOT: it only picks a default (any agent may pass `zab history --bot`),
    # and without it `zab history` takes the only bot or asks for --bot, naming each bot's
    # backend.
    return {
        "shell_environment_policy.set": values,
        "shell_environment_policy.exclude": list(
            dict.fromkeys([*(policy.get("exclude") or []), *GIT_KEYS, BOT_KEY])
        ),
    }
