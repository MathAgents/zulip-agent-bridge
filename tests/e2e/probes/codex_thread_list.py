"""#858's probe: how Codex 0.157.1's `thread/list` lists a spawned sub-agent thread, if at all.

Run on the test VM as `agent`, with the installed zab's Python, which reads this file from
stdin (nothing is copied to the VM):

    ssh … ubuntu@VM 'sudo -u agent -H env -C /home/agent \\
        /home/agent/.local/share/uv/tools/zulip-agent-bridge/bin/python -I -' \\
        < tests/e2e/probes/codex_thread_list.py

It connects to the Codex daemon's socket as the bridge does (`CodexRpc`), starts a thread of
its own in a new directory, and runs one turn that asks the model to spawn a sub-agent with a
long command and to end at once. Then, in each phase, it lists threads with each variant of
`thread/list` #858 names, and reads the parent and the sub-agent (`thread/read`,
`thread/loaded/list`):

1. the sub-agent's command running (it waits for that, and aborts after 120 s without);
2. the parent's turn ended, the sub-agent running;
3. on a new connection, which is what the daemon sees of a bridge restart;
4. after the daemon's unload delay (60 s), the first connection gone;
5. on another new connection, the sub-agent still running.

Then it interrupts the sub-agent's turn and ends the command it started. It prints JSON lines
of thread ids, source kinds, statuses, parent ids, whether a thread shares the parent's session
tree (`sessionId`), counts and seconds only: never a message's text, and it reads nothing of
`~/.codex` but the socket. In each phase it also reads every other loaded thread, which a
running sub-agent's turn keeps loaded, and prints those of the probe's session tree: the
fallback if no `thread/list` variant lists the sub-agent.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import signal
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from zab.backends.codex_rpc import CodexRpc, RpcError

#: `ThreadSourceKind` in the pinned 0.157.1 schema
KINDS = [
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
PAGES, LIMIT = 20, 100
#: Scenario 17 part 3's prompt, which runs the sub-agent's command live, with a longer one
PROMPT = (
    "This is a test of the bridge (`{marker}`). Start a sub-agent with spawn_agent. Its task: "
    "run the shell command `python3 -c 'import time; time.sleep(400)' {marker}`, then reply "
    "with exactly `SUB-{marker}`. Leave it running: don't wait for it, don't stop it, and end "
    "your turn at once with exactly `STARTED-{marker}`."
)
#: The VM bridge's Codex defaults (`workshop/admin/push_config.py`): the sub-agent's command
#: ran under them in scenario 17, and didn't under `workspace-write` in the probe's first run
THREAD = {"approvalPolicy": "never", "sandbox": "danger-full-access"}
COMMAND_S, COMMAND_POLL_S = 120.0, 2.0


class Aborted(Exception):
    """The probe can't test what it is for: its reason goes out as the last line."""


def variants(cwd: str) -> list[tuple[str, dict[str, Any]]]:
    """Each `thread/list` the probe sends: the bridge's own first."""
    out: list[tuple[str, dict[str, Any]]] = [
        ("the bridge's query", {"sourceKinds": ["subAgentThreadSpawn"], "sortKey": "updated_at"}),
        ("no sourceKinds", {}),
    ]
    out += [(f"sourceKinds [{kind}]", {"sourceKinds": [kind]}) for kind in KINDS]
    out += [
        ("all kinds", {"sourceKinds": KINDS}),
        (
            "subAgentThreadSpawn, useStateDbOnly",
            {"sourceKinds": ["subAgentThreadSpawn"], "useStateDbOnly": True},
        ),
        ("all kinds, useStateDbOnly", {"sourceKinds": KINDS, "useStateDbOnly": True}),
        ("all kinds, cwd", {"sourceKinds": KINDS, "cwd": cwd}),
        ("no sourceKinds, cwd", {"cwd": cwd}),
    ]
    return out


def kind(source: Any) -> str:
    """A thread's `source`, as its kind only: `appServer`, or `subAgent.thread_spawn` for an
    object, without the values inside it."""
    if isinstance(source, dict) and source:
        key = next(iter(source))
        inner = source[key]
        if isinstance(inner, dict) and inner:
            return f"{key}.{next(iter(inner))}"
        return f"{key}.{inner}" if isinstance(inner, str) else str(key)
    return str(source)


def emit(**fields: Any) -> None:
    print(json.dumps(fields, sort_keys=True), flush=True)


class Probe:
    def __init__(
        self, home: str, socket: str, cwd: str, marker: str, wait_s: float, unload_s: float
    ) -> None:
        self.home, self.socket, self.cwd, self.marker = home, socket, cwd, marker
        self.wait_s, self.unload_s = wait_s, unload_s
        self.rpc: CodexRpc | None = None
        self.pump: asyncio.Task | None = None
        self.parent = ""
        self.session = ""  # the parent's `sessionId`, which its session tree shares
        self.children: dict[str, str | None] = {}  # sub-agent thread → its turn in progress
        self.parent_turn = ""
        self.parent_ended = asyncio.Event()
        self.child_seen = asyncio.Event()
        # whether the sub-agent's command runs: `marked` on the VM, a stub in the offline test
        self.command_running: Callable[[], bool] = lambda: bool(marked(self.marker))

    async def connect(self) -> None:
        self.rpc = CodexRpc()
        await self.rpc.connect(home=self.home, socket=self.socket)
        self.pump = asyncio.create_task(self._pump(self.rpc))

    async def disconnect(self) -> None:
        if self.pump:
            self.pump.cancel()
        if self.rpc:
            await self.rpc.close()
        self.rpc = self.pump = None

    async def _pump(self, rpc: CodexRpc) -> None:
        """The events that name the probe's threads: no text is kept."""
        while True:
            message = await rpc.incoming.get()
            method, params = message.get("method"), message.get("params") or {}
            thread = params.get("threadId")
            if method == "turn/started" and thread:
                turn = (params.get("turn") or {}).get("id")
                if thread == self.parent:
                    self.parent_turn = turn or ""
                elif thread in self.children:
                    self.children[thread] = turn
                    self.child_seen.set()
                else:  # a new thread's own turn may come before the parent's item names it
                    self.children.setdefault(f"?{thread}", turn)
            elif method == "turn/completed" and thread:
                status = (params.get("turn") or {}).get("status")
                if thread == self.parent:
                    self.parent_ended.set()
                elif thread in self.children:
                    self.children[thread] = None
                emit(event="turn/completed", thread=thread, status=status)
            elif method in ("item/started", "item/completed") and thread == self.parent:
                item = params.get("item") or {}
                ids = []
                if item.get("type") == "subAgentActivity":
                    ids = [item.get("agentThreadId")]
                    emit(event="subAgentActivity", child=ids[0], kind=item.get("kind"))
                elif item.get("type") == "collabAgentToolCall":
                    ids = list(item.get("receiverThreadIds") or [])
                for child in filter(None, ids):
                    turn = self.children.pop(f"?{child}", self.children.get(child))
                    self.children[child] = turn
                    if turn:
                        self.child_seen.set()

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        assert self.rpc is not None
        return await self.rpc.call(method, params)

    def mine(self) -> set[str]:
        return {self.parent, *(c for c in self.children if not c.startswith("?"))}

    async def snapshot(self, phase: str) -> None:
        """Each `thread/list` variant, then the probe's threads read one by one."""
        mine = self.mine()
        emit(
            phase=phase,
            state={
                "parent_turn_ended": self.parent_ended.is_set(),
                "command_running": await asyncio.to_thread(self.command_running),
            },
        )
        for name, params in variants(self.cwd):
            started, count, pages, found, cursor, error = time.monotonic(), 0, 0, [], None, None
            try:
                while pages < PAGES:
                    page = await self.call(
                        "thread/list",
                        {**params, "limit": LIMIT, **({"cursor": cursor} if cursor else {})},
                    )
                    pages += 1
                    data = page.get("data") or []
                    count += len(data)
                    found += [
                        self.brief(t)
                        for t in data
                        if t.get("id") in mine
                        or t.get("parentThreadId") in mine
                        or t.get("sessionId") == self.session
                    ]
                    cursor = page.get("nextCursor")
                    if not cursor:
                        break
            except RpcError as exc:
                error = f"{exc.code}: {str(exc)[:160]}"
            emit(
                phase=phase,
                query=name,
                listed=count,
                pages=pages,
                more=bool(cursor),
                seconds=round(time.monotonic() - started, 2),
                probe_threads=found,
                error=error,
            )
        loaded, cursor = set(), None
        try:
            for _ in range(PAGES):
                page = await self.call(
                    "thread/loaded/list", {"limit": LIMIT, **({"cursor": cursor} if cursor else {})}
                )
                loaded |= set(page.get("data") or [])
                if not (cursor := page.get("nextCursor")):
                    break
        except RpcError as exc:
            emit(phase=phase, read="thread/loaded/list", error=f"{exc.code}")
        for thread_id in sorted(mine):
            try:
                thread = (
                    await self.call("thread/read", {"threadId": thread_id, "includeTurns": False})
                )["thread"]
                emit(
                    phase=phase,
                    read=thread_id,
                    role="parent" if thread_id == self.parent else "sub-agent",
                    **self.brief(thread),
                    cwd_is_probe=thread.get("cwd") == self.cwd,
                    loaded=thread_id in loaded,
                )
            except RpcError as exc:
                emit(phase=phase, read=thread_id, error=f"{exc.code}: {str(exc)[:160]}")
        await self.loaded_tree(phase, loaded - {self.parent})

    def brief(self, thread: dict[str, Any]) -> dict[str, Any]:
        """A thread as the probe prints it: never its text."""
        return {
            "id": thread.get("id"),
            "source": kind(thread.get("source")),
            "status": (thread.get("status") or {}).get("type"),
            "parent": thread.get("parentThreadId"),
            "same_session": thread.get("sessionId") == self.session,
        }

    async def loaded_tree(self, phase: str, others: set[str]) -> None:
        """The fallback #858 needs, as the bridge would run it: every loaded thread but the
        parent read (`thread/read` loads nothing), those that are sub-agents or of the
        probe's session tree printed, and which of them reach the parent by `parentThreadId`,
        reading each unloaded ancestor, 8 levels at most."""
        started, tree, errors, reached = time.monotonic(), [], 0, []
        for thread_id in sorted(others)[:200]:
            try:
                thread = (
                    await self.call("thread/read", {"threadId": thread_id, "includeTurns": False})
                )["thread"]
            except RpcError:
                errors += 1
                continue
            if thread.get("sessionId") != self.session and not thread.get("parentThreadId"):
                continue
            tree.append(self.brief(thread))
            up, depth = thread.get("parentThreadId"), 0
            while up and up != self.parent and depth < 8:
                try:
                    up = (await self.call("thread/read", {"threadId": up, "includeTurns": False}))[
                        "thread"
                    ].get("parentThreadId")
                except RpcError:
                    errors += 1
                    break
                depth += 1
            if up == self.parent:
                reached.append(thread_id)
        emit(
            phase=phase,
            read="loaded threads but the parent",
            loaded=len(others),
            read_errors=errors,
            seconds=round(time.monotonic() - started, 2),
            sub_agents_or_same_session=tree,
            reach_the_parent=reached,
        )

    async def run(self) -> None:
        await self.connect()
        thread = (await self.call("thread/start", {"cwd": self.cwd, **THREAD}))["thread"]
        self.parent, self.session = thread["id"], thread.get("sessionId") or ""
        emit(
            event="thread/start",
            parent=self.parent,
            source=kind(thread.get("source")),
            session_is_id=self.session == self.parent,
        )
        text = PROMPT.format(marker=self.marker)
        await self.call(
            "turn/start",
            {
                "threadId": self.parent,
                "input": [{"type": "text", "text": text}],
                "effort": "high",
            },
        )
        deadline = time.monotonic() + COMMAND_S
        while not await asyncio.to_thread(self.command_running):
            if time.monotonic() > deadline:
                raise Aborted(f"the sub-agent's command didn't run within {COMMAND_S:.0f} s")
            await asyncio.sleep(COMMAND_POLL_S)
        emit(event="the sub-agent's command runs")
        await self.snapshot("1 the sub-agent's command running")
        try:
            await asyncio.wait_for(self.parent_ended.wait(), self.wait_s)
        except TimeoutError:
            emit(event="timeout", waiting="the parent's turn to end")
        emit(event="children", known=sorted(self.mine() - {self.parent}))
        await self.snapshot("2 parent's turn ended")
        await self.disconnect()
        left = time.monotonic()
        await self.connect()
        await self.snapshot("3 new connection")
        await asyncio.sleep(max(0.0, self.unload_s - (time.monotonic() - left)))
        await self.snapshot("4 after the unload delay")
        await self.disconnect()  # a second restart, the sub-agent still running
        await self.connect()
        await self.snapshot("5 another new connection")

    @staticmethod
    async def _first(*events: asyncio.Event) -> None:
        waits = [asyncio.ensure_future(e.wait()) for e in events]
        try:
            await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for w in waits:
                w.cancel()

    async def clean_up(self) -> None:
        """Interrupt each sub-agent's turn in progress, then end the command it started."""
        if self.rpc is None:
            try:
                await self.connect()
            except Exception as exc:  # a probe's clean-up reports and goes on
                emit(event="clean-up", error=type(exc).__name__)
        for child in sorted(self.mine() - {self.parent}) if self.rpc else []:
            try:
                turns = await self.call(
                    "thread/turns/list",
                    {
                        "threadId": child,
                        "limit": 1,
                        "sortDirection": "desc",
                        "itemsView": "notLoaded",
                    },
                )
                data = turns.get("data") or []
                if data and data[0].get("status") == "inProgress":
                    await self.call("turn/interrupt", {"threadId": child, "turnId": data[0]["id"]})
                    emit(event="clean-up", interrupted=child)
            except RpcError as exc:
                emit(event="clean-up", child=child, error=f"{exc.code}")
        emit(event="clean-up", ended=await asyncio.to_thread(end_marked, self.marker))
        await self.disconnect()


def marked(marker: str) -> list[int]:
    """The `python3` processes of this user whose arguments hold `marker`, from /proc."""
    pids = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        words = [w.decode(errors="replace") for w in argv if w]
        if words and Path(words[0]).name.startswith("python3") and marker in words[1:]:
            pids.append(int(entry.name))
    return pids


def end_marked(marker: str) -> list[int]:
    ended = []
    for sig in (signal.SIGTERM, signal.SIGKILL):
        pids = marked(marker)
        for pid in pids:
            try:
                os.kill(pid, sig)
                ended.append(pid)
            except OSError:
                pass
        if pids:
            time.sleep(2)
    return sorted(set(ended))


def probe(argv: list[str], *, workdir: Path | None = None) -> Probe:
    """The probe the command line asks for, in a new directory under `workdir` (the home)."""
    parser = argparse.ArgumentParser(description="#858's probe of Codex's thread/list")
    parser.add_argument("--home", default=os.environ.get("CODEX_HOME", "~/.codex"))
    parser.add_argument("--socket", default="", help="default: the daemon's, under --home")
    parser.add_argument("--wait", type=float, default=300.0, help="seconds per wait")
    parser.add_argument("--unload", type=float, default=75.0, help="seconds after disconnect")
    args = parser.parse_args(argv)
    nonce = secrets.token_hex(3)
    cwd = (workdir or Path.home()) / f"zab-probe-858-{nonce}"
    cwd.mkdir()
    home = str(Path(args.home).expanduser())
    return Probe(home, args.socket, str(cwd), f"P858-{nonce}", args.wait, args.unload)


async def main(found: Probe) -> int:
    emit(event="probe", marker=found.marker, kinds=KINDS)
    try:
        await found.run()
    except Aborted as exc:
        emit(event="aborted", reason=str(exc))
        return 3
    finally:
        await found.clean_up()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(probe(sys.argv[1:]))))
