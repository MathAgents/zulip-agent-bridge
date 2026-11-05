"""Record isolated app-server conversations for scrubbed replay (SPEC §15.2)."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import time
from pathlib import Path
from uuid import uuid4

from zab.backends.codex import PIN
from zab.backends.codex_rpc import CodexRpc


async def record(args: argparse.Namespace) -> None:
    records = [{"kind": "meta", "version": None, "scenario": args.scenario}]
    started = time.monotonic()

    def trace(direction: str, message: dict) -> None:
        records.append(
            {"direction": direction, "t": round(time.monotonic() - started, 6), "message": message}
        )

    rpc = CodexRpc(trace)
    try:
        await rpc.connect(home=args.codex_home)
        identity = rpc.identity.get("userAgent", "")
        match = re.match(r"[^/\s]+/([^\s]+)", identity)
        version = match.group(1) if match else None
        records[0]["version"] = version
        if version != PIN:
            raise ValueError(f"daemon version {version!r} does not match fixture pin {PIN}")
        if args.thread:
            sid = args.thread
            await rpc.call("thread/resume", {"threadId": sid, "excludeTurns": True})
        else:
            result = await rpc.call(
                "thread/start",
                {
                    "cwd": args.cwd,
                    "approvalPolicy": args.approval,
                    "sandbox": "workspace-write",
                    "developerInstructions": (
                        "This is a scratch protocol test. Never read credentials or secrets."
                    ),
                },
            )
            sid = result["thread"]["id"]
        result = await rpc.call(
            "turn/start",
            {
                "threadId": sid,
                "clientUserMessageId": str(uuid4()),
                "input": [{"type": "text", "text": args.prompt}],
            },
        )
        tid = result["turn"]["id"]
        acted = False
        async with asyncio.timeout(120):
            while True:
                message = await rpc.incoming.get()
                method = message.get("method", "")
                if "disconnected" in message:
                    raise ConnectionError(message["disconnected"])
                if "id" in message:
                    if method in (
                        "item/commandExecution/requestApproval",
                        "item/fileChange/requestApproval",
                    ):
                        await rpc.send({"id": message["id"], "result": {"decision": args.decision}})
                    else:
                        await rpc.send(
                            {
                                "id": message["id"],
                                "error": {"code": -32601, "message": "unsupported fixture request"},
                            }
                        )
                if (
                    method == "item/started"
                    and message["params"]["item"]["type"] == "commandExecution"
                    and not acted
                ):
                    acted = True
                    if args.scenario == "steer":
                        await rpc.call(
                            "turn/steer",
                            {
                                "threadId": sid,
                                "expectedTurnId": tid,
                                "clientUserMessageId": str(uuid4()),
                                "input": [
                                    {"type": "text", "text": "Finish with STEER_FIXTURE_OK."}
                                ],
                            },
                        )
                    elif args.scenario == "interrupt":
                        await rpc.call("turn/interrupt", {"threadId": sid, "turnId": tid})
                if method == "turn/completed" and message["params"]["turn"]["id"] == tid:
                    break
        await rpc.call("thread/resume", {"threadId": sid, "excludeTurns": True})
        await rpc.call("thread/turns/list", {"threadId": sid, "limit": 2, "itemsView": "summary"})
        print(json.dumps({"thread": sid, "turn": tid, "records": len(records)}))
    finally:
        await rpc.close()
        output = Path(args.output)
        await asyncio.to_thread(output.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(
            output.write_text, "\n".join(json.dumps(row) for row in records) + "\n"
        )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="zab dev record-codex")
    parser.add_argument("--codex-home", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument(
        "--output", required=True, help="raw JSONL: scrub and inspect before committing"
    )
    parser.add_argument(
        "--scenario",
        default="simple",
        choices=["simple", "tool_use", "approval", "steer", "interrupt", "resume"],
    )
    parser.add_argument("--thread")
    parser.add_argument("--approval", default="never", choices=["never", "on-request"])
    parser.add_argument(
        "--decision", default="decline", choices=["accept", "acceptForSession", "decline", "cancel"]
    )
    args = parser.parse_args(argv)
    if not os.environ.get("ZAB_LIVE"):
        parser.error("set ZAB_LIVE explicitly to record live fixtures")
    if Path(args.codex_home).expanduser().resolve() == Path.home() / ".codex":
        parser.error("use an isolated test home, never the agent's own ~/.codex")
    args.codex_home = str(Path(args.codex_home).expanduser().resolve())
    args.cwd = str(Path(args.cwd).resolve())
    asyncio.run(record(args))
    return 0
