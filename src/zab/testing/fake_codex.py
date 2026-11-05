"""Codex fakes for offline tests (SPEC §15.2).

`FakeCodex` replays a scrubbed JSON-RPC transcript. `FakeAppServer` is a scripted app-server: it
keeps threads and turns in memory and answers the requests zab sends, over the daemon's
unix-socket websocket, so the real `CodexBackend` runs against it. Its shapes follow the pinned
0.157.1 schemas (`docs/protocol/codex-0.157.1`), and its `thread/fork` the native behaviour the
#202 VERIFY recorded (DECISIONS, 2026-10-10): the child holds the parent's turns through
`lastTurnId`, a turn still in progress is refused as `lastTurnId` (-32600), the child runs in
the `cwd` it was given, and it inherits none of the parent's model, effort or sandbox. An
`ephemeral` fork (a side question, #716) is gone at its `thread/unsubscribe`; whether the real
one leaves any trace is the test VM's VERIFY (DECISIONS).
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import itertools
import json
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from zab.backends.codex_events import EventMapper
from zab.core.types import BackendEvent


class FakeCodex:
    def __init__(self, fixture: str | Path) -> None:
        self.records = [json.loads(line) for line in Path(fixture).read_text().splitlines()]

    async def messages(self) -> AsyncIterator[dict[str, Any]]:
        for record in self.records:
            if record.get("direction") == "receive":
                await asyncio.sleep(0)
                yield record["message"]

    async def events(self) -> AsyncIterator[BackendEvent]:
        mapper = EventMapper()
        async for message in self.messages():
            if "method" in message and "id" not in message:
                for event in mapper.notification(message["method"], message.get("params", {})):
                    yield event


def _ms() -> int:
    return int(time.time() * 1000)


class RpcFailure(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


#: What a new or forked thread runs with when the request names nothing (not the parent's).
DEFAULTS = {"model": "gpt-fake", "approval": "on-request", "sandbox": "workspace-write"}
SANDBOX_TYPES = {
    "read-only": "readOnly",
    "workspace-write": "workspaceWrite",
    "danger-full-access": "dangerFullAccess",
}


class FakeAppServer:
    """A scripted Codex app-server (SPEC §15.2) for the backend and the core's offline tests.

    `serve(path)` listens on a unix socket, as the daemon does; point `[backends.codex] socket`
    at it. Every request is kept in `calls` and every message both ways in `wire`, for schema
    checks. A turn answers at once (`answer(thread, text)`, by default `FAKE_OK <n>`, n being
    its number in its thread) unless `hold` is set: then it stays in progress until
    `finish(thread_id)` or `turn/interrupt`.
    """

    def __init__(self, home: str) -> None:
        self.home = home
        self.threads: dict[str, dict[str, Any]] = {}
        self.calls: list[dict[str, Any]] = []
        self.wire: list[tuple[str, dict[str, Any]]] = []
        self.hold = False
        self.defaults = dict(DEFAULTS)  # a test may change them, as a config.toml edit would
        self.answer: Callable[[dict[str, Any], str], str] = lambda thread, text: (
            f"FAKE_OK {len(thread['turns'])}"
        )
        self.socket: Any = None
        self.ids = itertools.count(1)
        # Side questions (`!btw`, #716): an ephemeral fork's turn is held by `hold_side`, not
        # `hold`; before its answer it starts `side_items` and sends a `side_request`, a
        # server request whose answer goes to `responses` (by request id).
        self.hold_side = False
        self.side_items: list[dict[str, Any]] = []
        self.side_request: str | None = None
        self.responses: dict[Any, dict[str, Any]] = {}
        # what `config/read` answers: the effective config.toml (its MCP servers, plugins, …)
        self.effective_config: dict[str, Any] = {}
        # `mcpServerStatus/list`: each server of the effective config is `connected` for a
        # thread, `disabled` where its `config` turned it off; `mcp_states` forces a state
        # (`None`: unknown), and lists a server that `config/read` leaves out
        self.mcp_states: dict[str, str | None] = {}

    @contextlib.asynccontextmanager
    async def serve(self, path: str) -> AsyncIterator[FakeAppServer]:
        from websockets.asyncio.server import unix_serve

        async with unix_serve(self._connection, path):
            yield self

    def requests(self, method: str) -> list[dict[str, Any]]:
        """The params of every `method` request, oldest first."""
        return [call["params"] for call in self.calls if call["method"] == method]

    async def finish(self, thread_id: str, text: str | None = None) -> None:
        """Complete the thread's turn in progress (one that `hold` kept running). A test calls it
        once that turn exists: a thread can be there before its `turn/start` (#748)."""
        thread = self.threads[thread_id]
        turn = next((t for t in thread["turns"] if t["status"] == "inProgress"), None)
        if turn is None:  # not a bare StopIteration, which a coroutine turns into a RuntimeError
            raise AssertionError(f"no turn in progress on {thread_id}")
        await self._complete(thread, turn, "completed", text)

    # ------------------------------------------------------------------------------ transport

    async def _connection(self, socket: Any) -> None:
        self.socket = socket
        async for raw in socket:
            message = json.loads(raw)
            self.wire.append(("send", message))
            if "id" not in message:
                continue  # `initialized`
            if "method" not in message:  # the answer to a server request (an approval)
                self.responses[message["id"]] = message
                continue
            self.calls.append({"method": message["method"], "params": message.get("params", {})})
            try:
                handler = getattr(self, "_" + message["method"].replace("/", "_"), None)
                if handler is None:
                    raise RpcFailure(-32601, f"unsupported method {message['method']}")
                result, after = await handler(message.get("params", {}))
            except RpcFailure as exc:
                await self._send(
                    {"id": message["id"], "error": {"code": exc.code, "message": str(exc)}}
                )
                continue
            await self._send({"id": message["id"], "result": result})
            for notification in after:
                await self._send(notification)

    async def _send(self, message: dict[str, Any]) -> None:
        self.wire.append(("receive", message))
        await self.socket.send(json.dumps(message))

    # ------------------------------------------------------------------------------- objects

    def _thread_json(self, thread: dict[str, Any]) -> dict[str, Any]:
        now = int(time.time())
        busy = any(t["status"] == "inProgress" for t in thread["turns"])
        return {
            "id": thread["id"],
            "sessionId": thread["id"],
            "cwd": thread["cwd"],
            "cliVersion": "0.157.1",
            "createdAt": thread["created"],
            "updatedAt": now,
            "ephemeral": thread.get("ephemeral", False),
            "forkedFromId": thread["forked_from"],
            "modelProvider": "openai",
            "model": thread["model"],
            "reasoningEffort": thread["effort"],
            "name": thread["name"],
            "preview": thread["preview"],
            "projectId": None,
            "source": "appServer",
            "status": {"type": "active", "activeFlags": []} if busy else {"type": "idle"},
            "turns": [],
        }

    def _settings_json(self, thread: dict[str, Any]) -> dict[str, Any]:
        return {
            "approvalPolicy": thread["approval"],
            "approvalsReviewer": "user",
            "cwd": thread["cwd"],
            "model": thread["model"],
            "modelProvider": "openai",
            "reasoningEffort": thread["effort"],
            "sandbox": {"type": SANDBOX_TYPES[thread["sandbox"]]},
            "thread": self._thread_json(thread),
        }

    def _new_thread(self, params: dict[str, Any], cwd: str, **extra: Any) -> dict[str, Any]:
        thread = {
            "id": f"thread-{next(self.ids)}",
            "cwd": cwd,
            "created": int(time.time()),
            "forked_from": None,
            "model": params.get("model") or self.defaults["model"],
            "effort": None,
            "approval": params.get("approvalPolicy") or self.defaults["approval"],
            "sandbox": params.get("sandbox") or self.defaults["sandbox"],
            "name": None,
            "preview": "",
            "turns": [],
            "developer_instructions": params.get("developerInstructions"),
            "config": params.get("config"),
        } | extra
        self.threads[thread["id"]] = thread
        return thread

    def _thread(self, params: dict[str, Any]) -> dict[str, Any]:
        thread = self.threads.get(params.get("threadId", ""))
        if thread is None:
            raise RpcFailure(-32600, f"invalid thread id: {params.get('threadId')}")
        return thread

    @staticmethod
    def _turn_json(turn: dict[str, Any], items: str = "summary") -> dict[str, Any]:
        return {
            "id": turn["id"],
            "items": copy.deepcopy(turn["items"]) if items != "notLoaded" else [],
            "itemsView": items,
            "status": turn["status"],
            "error": None,
            "startedAt": turn["started"],
            "completedAt": turn.get("completed"),
            "durationMs": None,
        }

    # -------------------------------------------------------------------------------- methods

    async def _initialize(self, params: dict[str, Any]) -> tuple[dict[str, Any], list]:
        result = {
            "userAgent": "fake/0.157.1",
            "codexHome": self.home,
            "platformFamily": "unix",
            "platformOs": "linux",
        }
        return result, []

    async def _mcpServerStatus_list(self, params: dict[str, Any]) -> tuple[dict[str, Any], list]:
        thread = self._thread(params) if params.get("threadId") else {}
        overrides = thread.get("config") or {}
        names = [*self.effective_config.get("mcp_servers", {}), *self.mcp_states]
        # 0.157.1 registers its own `codex_apps` for a ChatGPT sign-in while `features.apps`
        # is on, its default (the review of #739): listed, not started, until a turn needs it
        apps = overrides.get("features.apps") is not False
        data = []
        for name in dict.fromkeys([*(["codex_apps"] if apps else []), *names]):
            off = overrides.get(f"mcp_servers.{name}.enabled") is False
            default = "notStarted" if name == "codex_apps" else "disabled" if off else "connected"
            state = self.mcp_states.get(name, default)
            data.append(
                {
                    "name": name,
                    "authStatus": "unsupported",
                    "resources": [],
                    "resourceTemplates": [],
                    "tools": {},
                    "runtimeStatus": state,
                }
            )
        return {"data": data, "nextCursor": None}, []

    async def _config_read(self, params: dict[str, Any]) -> tuple[dict[str, Any], list]:
        return {"config": copy.deepcopy(self.effective_config), "origins": {}}, []

    async def _model_list(self, params: dict[str, Any]) -> tuple[dict[str, Any], list]:
        efforts = [{"reasoningEffort": e, "description": e} for e in ("low", "medium", "high")]
        models = [
            {
                "id": name,
                "model": name,
                "displayName": name,
                "description": name,
                "hidden": False,
                "isDefault": name == self.defaults["model"],
                "defaultReasoningEffort": "medium",
                "supportedReasoningEfforts": efforts,
            }
            for name in (self.defaults["model"], "gpt-other")
        ]
        return {"data": models, "nextCursor": None}, []

    async def _thread_start(self, params: dict[str, Any]) -> tuple[dict[str, Any], list]:
        thread = self._new_thread(params, params.get("cwd") or self.home)
        return self._settings_json(thread), []

    async def _thread_resume(self, params: dict[str, Any]) -> tuple[dict[str, Any], list]:
        return self._settings_json(self._thread(params)), []

    async def _thread_read(self, params: dict[str, Any]) -> tuple[dict[str, Any], list]:
        return {"thread": self._thread_json(self._thread(params))}, []

    async def _thread_loaded_list(self, params: dict[str, Any]) -> tuple[dict[str, Any], list]:
        return {"data": list(self.threads), "nextCursor": None}, []

    async def _thread_name_set(self, params: dict[str, Any]) -> tuple[dict[str, Any], list]:
        self._thread(params)["name"] = params["name"]
        return {}, []

    async def _thread_turns_list(self, params: dict[str, Any]) -> tuple[dict[str, Any], list]:
        turns = list(self._thread(params)["turns"])
        if params.get("sortDirection", "desc") == "desc":
            turns.reverse()
        if params.get("limit") is not None:
            turns = turns[: params["limit"]]
        view = params.get("itemsView") or "summary"
        return {"data": [self._turn_json(t, view) for t in turns], "nextCursor": None}, []

    async def _thread_fork(self, params: dict[str, Any]) -> tuple[dict[str, Any], list]:
        parent = self._thread(params)
        turns = parent["turns"]
        if (last := params.get("lastTurnId")) is not None:
            index = next((i for i, t in enumerate(turns) if t["id"] == last), None)
            if index is None:
                raise RpcFailure(-32600, f"turn not found: {last}")
            if turns[index]["status"] == "inProgress":
                raise RpcFailure(-32600, f"cannot fork through an in-progress turn: {last}")
            turns = turns[: index + 1]
        else:  # what a direct fork of a running thread keeps (the Claude VERIFY's analogue)
            turns = [
                t | {"status": "interrupted"} if t["status"] == "inProgress" else t for t in turns
            ]
        child = self._new_thread(
            params,
            params.get("cwd") or parent["cwd"],
            forked_from=parent["id"],
            preview=parent["preview"],
            turns=copy.deepcopy(turns),
            ephemeral=bool(params.get("ephemeral")),
        )
        return self._settings_json(child), []

    async def _thread_unsubscribe(self, params: dict[str, Any]) -> tuple[dict[str, Any], list]:
        """The last subscriber gone, an ephemeral thread is gone too, never on disk (#716)."""
        thread = self.threads.get(params.get("threadId", ""))
        if thread is None:
            return {"status": "notLoaded"}, []
        if not thread.get("ephemeral"):
            return {"status": "unsubscribed"}, []
        del self.threads[thread["id"]]
        return {"status": "unsubscribed"}, [self._note("thread/closed", threadId=thread["id"])]

    async def _thread_items_list(self, params: dict[str, Any]) -> tuple[dict[str, Any], list]:
        turns = self._thread(params)["turns"]
        items = [  # `ThreadItemsListResponse`: each item with its turn
            {"item": copy.deepcopy(item), "turnId": turn["id"]}
            for turn in turns
            if params.get("turnId") in (None, turn["id"])
            for item in turn["items"]
        ]
        if params.get("sortDirection", "asc") == "desc":
            items.reverse()
        if params.get("limit") is not None:
            items = items[: params["limit"]]
        return {"data": items, "nextCursor": None, "backwardsCursor": None}, []

    async def _turn_start(self, params: dict[str, Any]) -> tuple[dict[str, Any], list]:
        thread = self._thread(params)
        if any(t["status"] == "inProgress" for t in thread["turns"]):
            raise RpcFailure(-32600, "a turn is already in progress")
        for key, field in (
            ("model", "model"),
            ("effort", "effort"),
            ("approvalPolicy", "approval"),
        ):
            if params.get(key):
                thread[field] = params[key]  # for this turn and the next ones, as Codex does
        if policy := params.get("sandboxPolicy"):
            thread["sandbox"] = {v: k for k, v in SANDBOX_TYPES.items()}[policy["type"]]
        text = "\n".join(p["text"] for p in params["input"] if p["type"] == "text")
        thread["preview"] = thread["preview"] or text
        user = {
            "type": "userMessage",
            "id": f"item-{next(self.ids)}",
            "clientId": params.get("clientUserMessageId"),
            "content": [{"type": "text", "text": text, "text_elements": []}],
        }
        turn = {
            "id": f"turn-{next(self.ids)}",
            "status": "inProgress",
            "items": [user],
            "started": int(time.time()),
            "params": params,
        }
        thread["turns"].append(turn)
        sid = thread["id"]
        after = [
            self._note("turn/started", threadId=sid, turn=self._turn_json(turn, "notLoaded")),
            self._note(
                "item/completed", threadId=sid, turnId=turn["id"], item=user, completedAtMs=_ms()
            ),
        ]
        side = thread.get("ephemeral", False)
        if side:  # a side question's turn: what a test scripted, before its answer (#716)
            for scripted in self.side_items:
                item = {"id": f"item-{next(self.ids)}"} | copy.deepcopy(scripted)
                turn["items"].append(item)
                after.append(
                    self._note(
                        "item/started",
                        threadId=sid,
                        turnId=turn["id"],
                        item=item,
                        startedAtMs=_ms(),
                    )
                )
            if self.side_request:
                request = {
                    "threadId": sid,
                    "turnId": turn["id"],
                    "itemId": f"item-{next(self.ids)}",
                    "startedAtMs": _ms(),
                    "command": "touch decoy",
                    "cwd": thread["cwd"],
                }
                after.append(
                    {"id": f"srv-{next(self.ids)}", "method": self.side_request, "params": request}
                )
        if not (self.hold_side if side else self.hold):
            after += self._completion(thread, turn, "completed", self.answer(thread, text))
        return {"turn": self._turn_json(turn, "notLoaded")}, after

    async def _turn_steer(self, params: dict[str, Any]) -> tuple[dict[str, Any], list]:
        thread = self._thread(params)
        turn = thread["turns"][-1] if thread["turns"] else None
        if turn is None or turn["status"] != "inProgress" or turn["id"] != params["expectedTurnId"]:
            raise RpcFailure(-32600, "no active turn to steer")
        text = "\n".join(p["text"] for p in params["input"] if p["type"] == "text")
        user = {
            "type": "userMessage",
            "id": f"item-{next(self.ids)}",
            "clientId": params.get("clientUserMessageId"),
            "content": [{"type": "text", "text": text, "text_elements": []}],
        }
        turn["items"].append(user)
        # Codex reports the steer's item when the model takes it in, after the tool call that
        # runs (the `steer` fixture); with no tool call here, at once (#654).
        sid, tid = thread["id"], turn["id"]
        return {"turnId": tid}, [
            self._note(method, threadId=sid, turnId=tid, item=user, **{stamp: _ms()})
            for method, stamp in (
                ("item/started", "startedAtMs"),
                ("item/completed", "completedAtMs"),
            )
        ]

    async def _turn_interrupt(self, params: dict[str, Any]) -> tuple[dict[str, Any], list]:
        thread = self._thread(params)
        turn = next((t for t in thread["turns"] if t["id"] == params.get("turnId")), None)
        if turn is None or turn["status"] != "inProgress":
            return {}, []
        return {}, self._completion(thread, turn, "interrupted", None)

    # ----------------------------------------------------------------------------- completion

    @staticmethod
    def _note(method: str, **params: Any) -> dict[str, Any]:
        return {"method": method, "params": params}

    def _completion(
        self, thread: dict[str, Any], turn: dict[str, Any], status: str, text: str | None
    ) -> list[dict[str, Any]]:
        sid, after = thread["id"], []
        if text is not None:
            item = {
                "type": "agentMessage",
                "id": f"msg-{next(self.ids)}",
                "text": text,
                "phase": "final_answer",
                "memoryCitation": None,
                "delivery": None,
                "questions": None,
            }
            turn["items"].append(item)
            after.append(
                self._note(
                    "item/completed",
                    threadId=sid,
                    turnId=turn["id"],
                    item=item,
                    completedAtMs=_ms(),
                )
            )
        turn["status"] = status
        turn["completed"] = int(time.time())
        after.append(self._note("turn/completed", threadId=sid, turn=self._turn_json(turn)))
        return after

    async def _complete(
        self, thread: dict[str, Any], turn: dict[str, Any], status: str, text: str | None
    ) -> None:
        text = text if text is not None else self.answer(thread, "")
        for notification in self._completion(thread, turn, status, text):
            await self._send(notification)
