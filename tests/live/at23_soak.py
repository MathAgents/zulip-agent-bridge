"""AT-23 (SPEC §17): 5 bridges x 3 concurrent sessions x 30 min against the real zab-dev channel,
driven by fake backends. Pass: no lost or duplicated messages, no unhandled exceptions, stable
memory, no 429 failures that go unrecovered.

    ZAB_LIVE=claude uv run python tests/live/at23_soak.py      # AT23_SECONDS, AT23_DIR optional

A standalone script, not collected by pytest (its name does not start with `test_`): the run takes
30 minutes plus wind-down. Each bridge is its own `zab run` (state dir, config, one bot: soak-1 ..
soak-5) with the Claude backend pointed at the fake `claude` (src/zab/testing/fake_claude.py,
fixture `simple`, recorded timing), so no model is called. zab-tester only POSTs during the run
(about 55 messages a minute, under its own 200/min); everything is verified afterwards from the
bridges' stores and the topic histories. Credentials stay in ~/.config/zab (paths only).
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import signal
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
NS = time.time_ns()
HERE = Path(os.environ.get("AT23_DIR") or tempfile.mkdtemp(prefix="zab-at23-"))  # logs, state
sys.path.insert(0, str(REPO / "src"))
from zab.workspace.local import LocalWorkspace  # noqa: E402
from zab.zulip.client import ZulipClient  # noqa: E402

CREDENTIALS = Path.home() / ".config/zab"
STREAM = 639106  # zab-dev (private)
TESTER_ID, TESTER2_ID = 1207964, 1207965
BOTS = {  # name -> (zuliprc, mention, user id)
    f"soak-{i}": (f"soak-{i}.zuliprc", f"@**Soak {i}**", 1207973 + i) for i in range(1, 6)
}
DURATION_S = float(os.environ.get("AT23_SECONDS", "1800"))
FAKE = REPO / "src/zab/testing/fake_claude.py"
FIXTURE = REPO / "tests/fixtures/claude/2.1.283/simple.jsonl"
ANSWER = "pong"  # the fixture's result
#: A topic `!status` reply (core/session.py `report_status`): one of these four forms, which no
#: answer ("pong") or "⏳ **Working**" status message matches.
STATUS_REPLY = re.compile(r"(?:Idle|Running) · \d|Waiting for approval|Error: ")
#: "Stable memory": RSS is sampled every minute; after a 5-minute warm-up it may grow by at most
#: 25 % of the warmed value, and at least 10 samples are required.
RSS_WARMUP_SAMPLES, RSS_MIN_SAMPLES, RSS_MAX_GROWTH = 5, 10, 0.25
LOG = HERE / "at23.log"


def ev(kind: str, **data) -> None:
    rec = {"t": round(time.time(), 3), "kind": kind, **data}
    with LOG.open("a") as f:
        f.write(json.dumps(rec, default=str) + "\n")
    print(f"AT23-{kind} " + " ".join(f"{k}={v}" for k, v in data.items()), flush=True)


async def zreq(client: ZulipClient, method: str, path: str, **params):
    for attempt in range(8):
        try:
            return await client.request(method, path, **params)
        except Exception as exc:
            if "RATE_LIMIT" not in str(exc) or attempt == 7:
                raise
            ev("TESTER-429", attempt=attempt)
            await asyncio.sleep(5 + 5 * attempt)


class Bridge:
    def __init__(self, bot: str, repo: Path) -> None:
        self.bot = bot
        self.dir = HERE / bot
        self.state = self.dir / "state"
        self.account = self.dir / "claude-config"
        self.log = self.dir / "bridge.log"
        self.config = self.dir / "config.toml"
        self.repo = repo
        self.proc: asyncio.subprocess.Process | None = None
        self.rss: list[int] = []

    def write_config(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.account.mkdir(exist_ok=True)
        rc = CREDENTIALS / BOTS[self.bot][0]
        self.config.write_text(
            f"""[bridge]
default_project={json.dumps(str(self.repo))}
state_dir={json.dumps(str(self.state))}
max_concurrent_turns=3
default_workspace="shared"
[[bots]]
name={json.dumps(self.bot)}
zuliprc={json.dumps(str(rc))}
backend="claude"
allow_user_ids=[{TESTER_ID}, {TESTER2_ID}]
[backends.claude]
kind="claude"
binary={json.dumps(str(FAKE))}
accounts={{test={json.dumps(str(self.account))}}}
default_account="test"
defaults={{model="haiku", approval="bypassPermissions"}}
steer=false
"""
        )  # steer=false: a pair's second message is held, as AT-23 was run (#310 made it on)

    async def start(self) -> None:
        self.write_config()
        fd = os.open(self.log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        env = {
            **os.environ,
            "PYTHONPATH": str(REPO / "src"),
            "ZAB_FAKE_CLAUDE_FIXTURE": str(FIXTURE),
            "ZAB_FAKE_CLAUDE_REALTIME": "1",
        }
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "zab",
            "run",
            "--config",
            str(self.config),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=fd,
            cwd=str(REPO),
            env=env,
        )
        os.close(fd)
        for _ in range(120):
            if self.proc.returncode is not None:
                raise RuntimeError(f"{self.bot}: bridge exited {self.proc.returncode}")
            if "zab ready" in self.log.read_text(errors="replace"):
                ev("BRIDGE-UP", bot=self.bot, pid=self.proc.pid)
                return
            await asyncio.sleep(1)
        raise RuntimeError(f"{self.bot}: not ready in 120 s")

    def sample_rss(self) -> None:
        try:
            for line in Path(f"/proc/{self.proc.pid}/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    self.rss.append(int(line.split()[1]))
        except (OSError, AttributeError):
            pass

    async def stop(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(self.proc.wait(), 60)
            except TimeoutError:
                self.proc.kill()
                await self.proc.wait()
                ev("BRIDGE-KILLED", bot=self.bot)
        ev("BRIDGE-DOWN", bot=self.bot, code=self.proc.returncode if self.proc else None)

    def db(self) -> sqlite3.Connection:
        con = sqlite3.connect(f"file:{self.state / 'zab.db'}?mode=ro", uri=True, timeout=10)
        con.row_factory = sqlite3.Row
        return con


async def driver(tester: ZulipClient, bot: str, index: int, deadline: float, sent: list) -> None:
    topic = f"zab-at23-{NS}-{bot}-{index}"
    mention = BOTS[bot][1]
    rng = random.Random(f"{bot}-{index}")  # noqa: S311 - a reproducible schedule, not secrets
    await asyncio.sleep(rng.uniform(0, 15))  # spread the starts
    k = 0

    async def post(text: str, kind: str) -> None:
        data = await zreq(
            tester, "POST", "messages", type="stream", to=STREAM, topic=topic, content=text
        )
        sent.append(
            {"bot": bot, "topic": topic, "id": int(data["id"]), "kind": kind, "t": time.time()}
        )

    await post(f"{mention} AT-23 session {index} starts.", "prompt")
    while time.time() < deadline:
        await asyncio.sleep(rng.uniform(14, 26))
        k += 1
        r = rng.random()
        if r < 0.1:
            await post("!status", "status")
        elif r < 0.35:  # a pair: the second arrives while the first runs, so it is held
            await post(f"Message {k}a.", "prompt")
            await asyncio.sleep(1)
            await post(f"Message {k}b.", "prompt")
        else:
            await post(f"Message {k}.", "prompt")


async def scratch_repo() -> Path:
    repo = HERE / "ws" / "repo"
    if not repo.exists():
        repo.mkdir(parents=True)
        (repo / "README.md").write_text("# at23\n")
        ws = LocalWorkspace(str(repo))
        for args in (
            ["git", "init", "-q", "-b", "main"],
            ["git", "add", "README.md"],
            [
                "git",
                "-c",
                "user.name=Zab Test",
                "-c",
                "user.email=zab@example.invalid",
                "commit",
                "-qm",
                "init",
            ],
        ):
            assert (await ws.run(args)).exit_code == 0
    return repo


async def quiesced(bridges: list[Bridge]) -> bool:
    for b in bridges:
        try:
            con = b.db()
        except sqlite3.OperationalError:
            return False
        try:
            running = con.execute(
                "SELECT COUNT(*) FROM turns WHERE state IN ('running','delivery_pending')"
            ).fetchone()[0]
            held = (
                con.execute("SELECT COUNT(*) FROM held").fetchone()[0]
                if con.execute("SELECT name FROM sqlite_master WHERE name='held'").fetchone()
                else 0
            )
        finally:
            con.close()
        if running or held:
            return False
    return True


def match_status(commands: list[dict], replies: list[dict]) -> tuple[list[dict], list[int]]:
    """Match each !status, in order, to the first unclaimed status reply after it. Returns the
    unanswered commands and the ids of replies no command claimed (a command processed twice)."""
    used: set[int] = set()
    unanswered = []
    for m in sorted(commands, key=lambda m: m["id"]):
        reply = next((h for h in replies if h["id"] > m["id"] and h["id"] not in used), None)
        if reply is None:
            unanswered.append(m)
        else:
            used.add(reply["id"])
    return unanswered, [h["id"] for h in replies if h["id"] not in used]


async def verify(tester: ZulipClient, bridges: list[Bridge], sent: list) -> dict:
    result = {
        "prompts": 0,
        "lost": [],
        "duplicated": [],
        "turns": 0,
        "not_completed": [],
        "answers_expected": 0,
        "answers_seen": 0,
        "duplicate_answers": [],
        "status_sent": 0,
        "status_unanswered": [],
        "status_duplicates": [],
        "memory_unstable": [],
        "tracebacks": {},
        "errors": {},
        "rss_kb": {},
    }
    triggers: dict[str, dict[int, int]] = {}
    turns_by_topic: dict[str, list] = {}
    for b in bridges:
        con = b.db()
        try:
            sessions = {r["id"]: r["topic"] for r in con.execute("SELECT id, topic FROM sessions")}
            for t in con.execute("SELECT * FROM turns"):
                topic = sessions.get(t["session_id"], "?")
                turns_by_topic.setdefault(topic, []).append(dict(t))
                for mid in json.loads(t["trigger_msg_ids"] or "[]"):
                    triggers.setdefault(b.bot, {}).setdefault(int(mid), 0)
                    triggers[b.bot][int(mid)] += 1
        finally:
            con.close()
        text = b.log.read_text(errors="replace")
        result["tracebacks"][b.bot] = text.count("Traceback")
        result["errors"][b.bot] = sum(
            1 for line in text.splitlines() if line.startswith(("ERROR", "CRITICAL"))
        )
        result["rss_kb"][b.bot] = b.rss
        if len(b.rss) < RSS_MIN_SAMPLES:
            result["memory_unstable"].append({"bot": b.bot, "why": f"{len(b.rss)} samples"})
        else:
            warm, last = b.rss[RSS_WARMUP_SAMPLES], b.rss[-1]
            if last > warm * (1 + RSS_MAX_GROWTH):
                result["memory_unstable"].append({"bot": b.bot, "warm_kb": warm, "last_kb": last})
    for m in sent:
        if m["kind"] == "prompt":
            result["prompts"] += 1
            n = triggers.get(m["bot"], {}).get(m["id"], 0)
            if n == 0:
                result["lost"].append(m)
            elif n > 1:
                result["duplicated"].append(m)
    topics = sorted({m["topic"] for m in sent})
    for topic in topics:
        bot = next(m["bot"] for m in sent if m["topic"] == topic)
        bot_id = BOTS[bot][2]
        history, anchor = [], 0
        while True:
            data = await zreq(
                tester,
                "GET",
                "messages",
                anchor=anchor,
                num_before=0,
                num_after=1000,
                include_anchor="false" if anchor else "true",
                narrow=json.dumps(
                    [
                        {"operator": "stream", "operand": "zab-dev"},
                        {"operator": "topic", "operand": topic},
                    ]
                ),
            )
            history += data["messages"]
            if data.get("found_newest", True) or not data["messages"]:
                break
            anchor = data["messages"][-1]["id"]
        turns = turns_by_topic.get(topic, [])
        result["turns"] += len(turns)
        final_ids = []
        for t in turns:
            if t["state"] != "completed":
                result["not_completed"].append(
                    {"topic": topic, "turn": t["id"], "state": t["state"]}
                )
            final_ids += [int(x) for x in json.loads(t["final_message_ids"] or "[]")]
        answers = [
            h
            for h in history
            if h["sender_id"] == bot_id and h["content"].strip() == f"<p>{ANSWER}</p>"
        ]
        result["answers_expected"] += len([t for t in turns if t["state"] == "completed"])
        result["answers_seen"] += len(answers)
        extra = sorted({h["id"] for h in answers} - set(final_ids))
        if extra or len(final_ids) != len(set(final_ids)):
            result["duplicate_answers"].append({"topic": topic, "extra_ids": extra})
        # each !status needs a status reply of its own, matched in order: a later answer, or one
        # reply after several commands, does not count for a dropped command
        replies = [
            h for h in history if h["sender_id"] == bot_id and STATUS_REPLY.search(h["content"])
        ]
        commands = [m for m in sent if m["topic"] == topic and m["kind"] == "status"]
        result["status_sent"] += len(commands)
        unanswered, surplus = match_status(commands, replies)
        result["status_unanswered"] += unanswered
        result["status_duplicates"] += [{"topic": topic, "reply": r} for r in surplus]
    return result


async def main() -> int:
    repo = await scratch_repo()
    tester = ZulipClient.from_zuliprc(CREDENTIALS / "tester.zuliprc")
    bridges = [Bridge(bot, repo) for bot in BOTS]
    sent: list[dict] = []
    loop = asyncio.get_running_loop()
    current = asyncio.current_task()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, current.cancel)
    try:
        assert (await tester.request("GET", "users/me"))["user_id"] == TESTER_ID
        stream = await tester.request("GET", f"streams/{STREAM}")
        assert stream["stream"]["name"] == "zab-dev" and stream["stream"]["invite_only"]
        for b in bridges:
            await b.start()
        start = time.time()
        deadline = start + DURATION_S
        ev("START", duration_s=DURATION_S, ns=NS, bridges=len(bridges), sessions_per_bridge=3)
        drivers = [
            asyncio.create_task(driver(tester, b.bot, i, deadline, sent))
            for b in bridges
            for i in (1, 2, 3)
        ]

        async def sampler():
            while True:
                for b in bridges:
                    b.sample_rss()
                await asyncio.sleep(60)

        samp = asyncio.create_task(sampler())
        await asyncio.gather(*drivers)
        ev("SENDING-DONE", sent=len(sent), minutes=round((time.time() - start) / 60, 1))
        for _ in range(60):  # wind-down: up to 10 minutes for the last turns and held messages
            await asyncio.sleep(10)
            if await quiesced(bridges):
                break
        await asyncio.sleep(30)  # let the final posts and the last !status replies land
        for b in bridges:
            b.sample_rss()
        samp.cancel()
    finally:
        for b in bridges:
            await asyncio.shield(b.stop())
    res = await verify(tester, bridges, sent)
    await tester.close()
    ok = (
        not res["lost"]
        and not res["duplicated"]
        and not res["not_completed"]
        and not res["duplicate_answers"]
        and not res["status_unanswered"]
        and not res["status_duplicates"]
        and not res["memory_unstable"]
        and res["answers_seen"] == res["answers_expected"]
        and not any(res["tracebacks"].values())
        and not any(res["errors"].values())
    )
    shown = {k: (len(v) if isinstance(v, list) else v) for k, v in res.items()}
    shown["rss_kb"] = {  # first, after warm-up, last, max
        bot: (r[0], r[min(RSS_WARMUP_SAMPLES, len(r) - 1)], r[-1], max(r)) if r else None
        for bot, r in res["rss_kb"].items()
    }
    ev("RESULT", ok=ok, **shown)
    (HERE / "result.json").write_text(json.dumps({**res, "sent": sent}, indent=2, default=str))
    return 0 if ok else 1


if __name__ == "__main__":
    if os.environ.get("ZAB_LIVE") != "claude":
        raise SystemExit("AT-23 posts in zab-dev: set ZAB_LIVE=claude (SPEC §15.3)")
    raise SystemExit(asyncio.run(main()))
