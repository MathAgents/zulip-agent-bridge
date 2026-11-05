"""The waker's start-up under load (#717), offline: its peak memory, measured.

A fake Zulip over HTTP on 127.0.0.1, in a thread of this process, serves `bots` bots, two per
VM. Each VM's channel holds `messages` messages of `size` characters, a turn in three: the
owner's prompt, the first bot's status message and its answer. The last `new` come after the
bots' stored marks, within the catch-up's 5 minutes, so that the owner's newest, which mentions
the first bot, wakes the VM. A status message carries `edits` entries of edit history, as Zulip
returns a message the bridge edited every 10 seconds of a turn: each the earlier version
(`edit_size` characters) and its HTML (twice that). `GET /users` answers `members` members.

The waker runs in a child process, built as `zab.waker.__main__.run_fleets` builds it: one
`Waker` per workshop in one event loop, over real `ZulipClient`s (real httpx clients, and their
TLS contexts, as in production), pointed at the fake over plain HTTP, and an EC2 that shows
every VM stopped. Once every bot's catch-up has settled and the wakes are done, the child
prints its report: its peak resident memory (`VmHWM`, Linux), how long the catch-ups took, and
the starts; the parent adds the fake's requests by endpoint.

    uv run python tests/waker/load_rig.py --bots 150 [--edits 20] [--messages 101 --new 100]

The child imports the `zab` its interpreter finds: `PYTHONPATH=<a tree's src>` measures
another tree's waker (the "before" of #717). `tests/waker/test_startup_memory.py` asserts the
bound. What this leaves out: TLS on the wire (a few tens of KB per open connection), and the
cgroup's page cache.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

OWNER0, BOT0, STREAM0 = 1000, 10_000, 100
#: An agent's answer: mostly ASCII, with the symbols they use (BMP), and in one message of ten
#: an emoji outside it, which makes Python hold that string at 4 bytes a character.
PIECE = "Let K = Q(√\u22125): its class group has order h = 2, so h ≤ 2 → (2, 1 + √\u22125) — "
EMOJI = "✅ 🙂 "


def text(size: int, emoji: bool = False) -> str:
    head = EMOJI if emoji else ""
    return (head + PIECE * (size // len(PIECE) + 1))[:size]


def owner(k: int) -> int:
    return OWNER0 + k // 2


def uid(email: str) -> int | None:
    """The fake's user ID for an address it gave, else None."""
    name = email.partition("@")[0]
    for prefix, base in (("bot", BOT0), ("owner", OWNER0)):
        if name.startswith(prefix) and name[len(prefix) :].isdecimal():
            return base + int(name[len(prefix) :])
    return None


class Fleet:
    """What the fake serves: the bots, their owners, their VMs' channels."""

    def __init__(
        self, bots: int, messages: int, size: int, new: int, members: int, edits: int = 0,
        edit_size: int = 1500, foreign: bool = False, recent: bool = False,
    ) -> None:  # fmt: skip
        if not 0 <= new < messages:  # a mark is a message before the new ones (review of #727)
            raise ValueError(f"new must be from 0 to messages - 1, not {new}")
        self.bots, self.messages, self.new, self.foreign = bots, messages, new, foreign
        self.recent = recent  # every new message within the last 200 s (the review of #727)
        self.vms = (bots + 1) // 2
        self.now = time.time()
        self.lock = threading.Lock()
        self.requests: Counter[str] = Counter()
        self.next_id = 10 * messages * self.vms
        self.contents = [json.dumps(text(size, e)).encode() for e in (False, True)]
        history = [
            {"user_id": 0, "timestamp": int(self.now) - 10 * n, "prev_content": text(edit_size),
             "prev_rendered_content": f"<p>{text(2 * edit_size - 7)}</p>",
             "prev_rendered_content_version": 1}
            for n in range(edits)
        ]  # fmt: skip
        self.history = json.dumps(history).encode() if edits else None
        users = []
        for k in range(bots):
            users.append(self.user(BOT0 + k, f"bot{k}@example.test", f"Bot {k}", owner(k)))
        for vm in range(self.vms):
            users.append(self.user(OWNER0 + vm, f"owner{vm}@example.test", f"Owner {vm}"))
        for n in range(max(0, members - len(users))):
            users.append(self.user(100_000 + n, f"member{n}@example.test", f"Member {n}"))
        self.users = json.dumps({"result": "success", "msg": "", "members": users}).encode()

    @staticmethod
    def user(uid: int, email: str, name: str, bot_owner: int | None = None) -> dict:
        user = {
            "email": email, "user_id": uid, "avatar_version": 1, "is_admin": False,
            "is_owner": False, "is_guest": False, "is_billing_admin": False, "role": 400,
            "is_bot": bot_owner is not None, "full_name": name, "timezone": "America/New_York",
            "is_active": True, "date_joined": "2026-10-30T14:00:00.000000+00:00",
            "delivery_email": None, "avatar_url": None, "profile_data": {},
        }  # fmt: skip
        if bot_owner is not None:
            user |= {"bot_type": 1, "bot_owner_id": bot_owner}
        return user

    def mid(self, vm: int, i: int) -> int:
        """The ID of a VM's `i`-th message: IDs are the realm's, interleaved across channels."""
        return 1 + i * self.vms + vm

    def mark(self, k: int) -> int:
        """The bot's stored mark: its VM's last message before the `new` ones, always one of
        them, so never below 1 (a negative mark is none to the waker, `Waker.stored_mark`)."""
        return self.mid(k // 2, self.messages - self.new - 1)

    def newest(self, k: int) -> int:
        return self.mid(k // 2, self.messages - 1)

    def sender(self, k: int, i: int) -> int:
        """A turn in three, the newest an answer: the owner's prompt, then the first bot's; with
        `foreign`, the next VM's first bot's, as in a channel the VMs share."""
        if (self.messages - 1 - i) % 3 == 2:
            return OWNER0 + k // 2
        return BOT0 + (k // 2 + self.foreign) % self.vms * 2

    def message(self, k: int, i: int) -> bytes:
        vm, sender = k // 2, self.sender(k, i)
        old = i < self.messages - self.new
        step = 2 if self.recent else 5  # 5 s apart: the oldest new ones past the 5 minutes
        sent = self.now - 3600 + 10 * i if old else self.now - 60 + step * (i - self.messages)
        mentioned = sender < BOT0 and k % 2 == 0 and i == self.messages - 3
        status = sender >= BOT0 and (self.messages - 1 - i) % 3 == 1
        head = {
            "id": self.mid(vm, i), "sender_id": sender, "recipient_id": 5000 + vm,
            "timestamp": int(sent), "client": "website", "subject": "work", "topic_links": [],
            "is_me_message": False, "reactions": [], "submessages": [],
            "flags": ["read", "mentioned"] if mentioned else ["read"],
            "sender_full_name": f"User {sender}", "sender_email": f"user{sender}@example.test",
            "sender_realm_str": "example", "display_recipient": f"ws-vm{vm}", "type": "stream",
            "stream_id": STREAM0 + vm, "avatar_url": None, "content_type": "text/x-markdown",
        }  # fmt: skip
        parts = [json.dumps(head).encode()[:-1], b', "content": ', self.contents[i % 10 == 0]]
        if status and self.history:
            parts += [b', "edit_history": ', self.history]
        return b"".join([*parts, b"}"])

    def fetch(self, k: int, params: dict[str, str]) -> list[bytes]:
        """`GET /messages` for bot `k`, in pieces: its VM's channel, as Zulip anchors and narrows
        it (a sender, or not, by ID or address)."""
        anchor = params.get("anchor", "newest")
        before, after = int(params.get("num_before", 0)), int(params.get("num_after", 0))
        include = params.get("include_anchor", "true") == "true"
        keep = list(range(self.messages))
        for term in json.loads(params.get("narrow", "[]")):
            operand = term.get("operand")
            who = operand if isinstance(operand, int) else uid(str(operand))
            if term.get("operator") == "sender":
                keep = [i for i in keep if (self.sender(k, i) == who) != term.get("negated", False)]
        ids = {i: self.mid(k // 2, i) for i in keep}
        if anchor == "newest":
            picked = keep[-before:] if before else []
        else:
            at = int(anchor)
            older = [i for i in keep if ids[i] < at]
            same = [i for i in keep if ids[i] == at and include]
            picked = (
                (older[-before:] if before else [])
                + same
                + [i for i in keep if ids[i] > at][:after]
            )
        newest = anchor == "newest" or not keep or (picked and picked[-1] == keep[-1])
        head = {"result": "success", "msg": "", "found_newest": bool(newest),
                "found_oldest": not picked or picked[0] == keep[0], "found_anchor": False,
                "history_limited": False}  # fmt: skip
        pieces = [json.dumps(head).encode()[:-1] + b', "messages": [']
        for n, i in enumerate(picked):
            pieces.append((b"," if n else b"") + self.message(k, i))
        return [*pieces, b"]}"]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    fleet: Fleet
    stop: threading.Event

    def log_message(self, *args: Any) -> None:
        pass

    def bot(self) -> int:
        token = self.headers.get("Authorization", "").partition(" ")[2]
        email = base64.b64decode(token).decode().partition(":")[0]
        return int(email.removeprefix("bot").partition("@")[0])

    def answer(self, body: bytes | dict | list[bytes], status: int = 200) -> None:
        pieces = body if isinstance(body, list) else [body]
        pieces = [p if isinstance(p, bytes) else json.dumps(p).encode() for p in pieces]
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(sum(map(len, pieces))))
            self.end_headers()
            for piece in pieces:
                self.wfile.write(piece)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True  # the child is gone

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        params = {k: v[-1] for k, v in parse_qs(url.query, keep_blank_values=True).items()}
        self.route("GET", url.path.removeprefix("/api/v1/"), params)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode()
        params = {k: v[-1] for k, v in parse_qs(raw, keep_blank_values=True).items()}
        self.route("POST", urlsplit(self.path).path.removeprefix("/api/v1/"), params)

    def route(self, method: str, path: str, params: dict[str, str]) -> None:
        fleet, k = self.fleet, self.bot()
        name = "messages/*/reactions" if path.endswith("/reactions") else path
        with fleet.lock:
            fleet.requests[f"{method} {name}"] += 1
        ok: dict[str, Any] = {"result": "success", "msg": ""}
        if path == "register":
            realm = {f"realm_setting_{n}": n for n in range(400)}  # about 10 KB, as Zulip's
            queue = {"queue_id": f"q{k}-{time.monotonic_ns()}", "last_event_id": -1}
            if "message" in json.loads(params.get("fetch_event_types", "[]")):
                queue["max_message_id"] = fleet.newest(k)
            self.answer(ok | realm | queue)
        elif path == "events":
            self.stop.wait(30)
            self.answer(ok | {"events": []})
        elif path == "messages" and method == "GET":
            self.answer(fleet.fetch(k, params))
        elif path == "messages":
            with fleet.lock:
                fleet.next_id += 1
                self.answer(ok | {"id": fleet.next_id})
        elif path == "users/me":
            self.answer(ok | Fleet.user(BOT0 + k, f"bot{k}@example.test", f"Bot {k}", owner(k)))
        elif path == "users":
            self.answer(fleet.users)
        elif name == "messages/*/reactions":
            self.answer(ok)
        else:
            self.answer({"result": "error", "code": "NOT_FOUND", "msg": ""}, 404)


def measure(
    bots: int = 150,
    messages: int = 100,
    size: int = 10_000,
    new: int = 3,
    members: int = 600,
    edits: int = 0,
    edit_size: int = 1500,
    foreign: bool = False,
    recent: bool = False,
    workshops: int = 2,
    timeout: float = 240.0,
    python: str = sys.executable,
    at_once: int = 0,
    page: int = 0,
) -> dict[str, Any]:
    """The child's report, with the fake's requests by endpoint."""
    fleet = Fleet(bots, messages, size, new, members, edits, edit_size, foreign, recent)
    stop = threading.Event()
    handler = type("Bound", (Handler,), {"fleet": fleet, "stop": stop})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="zab-load-") as state:
            argv = [python, __file__, "--child", "--port", str(server.server_address[1])]
            argv += ["--bots", str(bots), "--messages", str(messages), "--new", str(new)]
            argv += ["--workshops", str(workshops), "--state", state, "--now", repr(fleet.now)]
            argv += ["--timeout", str(timeout), "--at-once", str(at_once), "--page", str(page)]
            done = subprocess.run(  # noqa: S603 - argument list: this interpreter, this file
                argv, capture_output=True, text=True, timeout=timeout + 30
            )
        if done.returncode:
            raise RuntimeError(f"the waker's child failed:\n{done.stderr[-4000:]}")
        report = json.loads(done.stdout.strip().splitlines()[-1])
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
    report["requests"] = dict(sorted(fleet.requests.items()))
    return report


# ------------------------------------------------------------------------- the child: the waker


class EC2:
    """Every VM stopped until its start; the starter's answers have this shape."""

    def __init__(self, workshop: str, instances: list[str]) -> None:
        self.tag = {"Key": "workshop", "Value": workshop}
        self.states = dict.fromkeys(instances, "stopped")
        self.starts = 0

    def describe_instances(self, *, Filters: list) -> dict:
        ids = next(f["Values"] for f in Filters if f["Name"] == "instance-id")
        instances = [
            {"InstanceId": i, "State": {"Name": self.states[i]}, "Tags": [self.tag]}
            for i in ids
            if i in self.states
        ]
        return {"Reservations": [{"Instances": instances}]}

    def start_instances(self, *, InstanceIds: list[str]) -> dict:
        (instance,) = InstanceIds
        self.states[instance] = "pending"
        self.starts += 1
        return {}

    def close(self) -> None:
        pass


def peak() -> dict[str, int]:
    values = {}
    with open("/proc/self/status") as status:
        for line in status:
            key, _, rest = line.partition(":")
            if key in ("VmHWM", "VmRSS"):
                values[key] = int(rest.split()[0])
    return values


async def child(args: argparse.Namespace) -> dict[str, Any]:
    from zab.config import BotConfig
    from zab.core.store import Store
    from zab.waker import queues, service
    from zab.waker.service import Target, Waker
    from zab.zulip.client import ZulipClient

    if args.at_once:
        queues.AT_ONCE = args.at_once
    if args.page:
        queues.PAGE = args.page
    store = Store(Path(args.state))
    fleet = Fleet(args.bots, args.messages, 1, args.new, 0)  # IDs and marks only
    for k in range(args.bots):
        if fleet.mark(k) < 1:
            raise RuntimeError(f"bot{k}'s mark would be {fleet.mark(k)}: no mark to the waker")
        store.set(f"{service.MARK}bot{k}", fleet.mark(k))
    base = f"http://127.0.0.1:{args.port}/api/v1/"
    groups: list[list[Any]] = [[] for _ in range(args.workshops)]
    for k in range(args.bots):
        bot = BotConfig(name=f"bot{k}", allow_user_ids=[owner(k)])
        client = ZulipClient("https://zulip.example", f"bot{k}@example.test", "decoy")
        client.http.base_url = base  # the fake, over plain HTTP; its TLS context stays built
        instance = f"i-{k // 2:017x}"
        groups[(k // 2) % args.workshops].append(Target(bot, instance, client))
    settled: set[str] = set()
    wakers, ec2s = [], []
    for n, targets in enumerate(groups):
        name = f"ws{n}"
        ec2 = EC2(name, sorted({t.instance for t in targets}))
        waker = Waker(targets, ec2, store, 1, name, f"{service.STATES}{name}", args.now - 3600)
        given = waker.source.settled

        def settle(target: Any, given: Any = given) -> None:
            given(target)
            settled.add(target.bot.name)

        waker.source.settled = settle
        wakers.append(waker)
        ec2s.append(ec2)

    async def run(waker: Any) -> None:
        await waker.refresh(force=True)
        await waker.run()

    started = time.monotonic()
    deadline = started + args.timeout
    tasks = [asyncio.create_task(run(w)) for w in wakers]
    while len(settled) < args.bots:
        if time.monotonic() > deadline or any(t.done() for t in tasks):
            raise RuntimeError(f"{len(settled)} of {args.bots} bots settled")
        await asyncio.sleep(0.05)
    caught_up = time.monotonic() - started
    while any(w.tasks for w in wakers) and time.monotonic() < deadline:  # noqa: ASYNC110
        await asyncio.sleep(0.05)  # the wakes in flight: no event says they are done
    await asyncio.sleep(1)
    report = {"bots": args.bots, "caught_up_s": round(caught_up, 2)}
    report |= {"starts": sum(e.starts for e in ec2s), "python": sys.version.split()[0]}
    report |= {"zab": str(Path(service.__file__).parents[2])}  # which tree's waker
    report |= {"peak_kb": (memory := peak())["VmHWM"], "rss_kb": memory["VmRSS"]}
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    store.close()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--state", help=argparse.SUPPRESS)
    parser.add_argument("--now", type=float, help=argparse.SUPPRESS)
    parser.add_argument("--bots", type=int, default=150)
    parser.add_argument("--messages", type=int, default=100)
    parser.add_argument("--size", type=int, default=10_000)
    parser.add_argument("--new", type=int, default=3)
    parser.add_argument("--members", type=int, default=600)
    parser.add_argument("--edits", type=int, default=0)
    parser.add_argument("--edit-size", type=int, default=1500)
    parser.add_argument("--foreign", action="store_true", help="the bots' messages: another VM's")
    parser.add_argument("--recent", action="store_true", help="every new message in 200 s")
    parser.add_argument("--workshops", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=240.0)
    parser.add_argument("--python", default=sys.executable, help="the waker's interpreter")
    parser.add_argument("--at-once", type=int, default=0, help="queues.AT_ONCE, if not its own")
    parser.add_argument("--page", type=int, default=0, help="queues.PAGE, if not its own")
    args = parser.parse_args()
    if args.child:
        print(json.dumps(asyncio.run(child(args))), flush=True)
        os._exit(0)  # not the clients' closes: the report is out
    keys = ("bots", "messages", "size", "new", "members", "edits", "edit_size", "foreign", "recent")
    keys += ("workshops",)
    options = {k: getattr(args, k) for k in (*keys, "timeout", "python", "at_once", "page")}
    print(json.dumps(measure(**options), indent=1))


if __name__ == "__main__":
    main()
