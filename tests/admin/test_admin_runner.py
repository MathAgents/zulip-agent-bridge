"""The runner (docs/ADMIN.md §2.3, §2.4, §2.7, §2.8; #342): the bots of `botkit` with the real
`Runner`, running fake scripts from a temporary release tree. A plan's dry runs and status
read, and `!status`; apply → result; busy; two workshops at once; a restart and a SIGTERM
mid-apply; the environment; process groups and the timeout (shortened); and a secret across
every cut the output goes through, progress edits included."""

import asyncio
import contextlib
import json
import logging
import os
import signal
import stat
import sys
import textwrap
import time
from dataclasses import replace
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from zab.admin import bot as botmod
from zab.admin import provider, starter
from zab.admin import runner as runner_mod
from zab.admin.audit import verify
from zab.admin.bot import serve
from zab.admin.fleet import ApplyEnd
from zab.admin.planning import Job, Planning
from zab.admin.plans import (
    APPLYING,
    APPROVED,
    DONE,
    EXPIRED,
    FAILED,
    INTERRUPTED,
    PENDING,
    REFUSED,
    STALE,
)
from zab.admin.runner import Capture, Runner
from zab.zulip import client as zulip_client

from . import botkit
from .botkit import (
    ALICE,
    DREW,
    HARVARD,
    HARVARD_CH,
    OLGA,
    OSCAR,
    TESS,
    TEST,
    TEST_CH,
    ZULIP_ITALIC,
    _write,
    audit_text,
    incoming,
    make_site,
    until,
)

VMS = {"alice": "i-1", "bob": "i-2", "erin": "i-5"}
LATE = 10**6  # a message ID after every post of the fake realm
TOKEN = "github_pat_" + "Z" * 40  # botkit's GitHub token: a registered secret
PRELUDE = """\
import json, os, pathlib, signal, subprocess, sys, time
DRY = "--dry-run" in sys.argv
TAG = sys.argv[sys.argv.index("--tag") + 1] if "--tag" in sys.argv else ""
with open({calls!r}, "a") as f:
    f.write(json.dumps({{"script": {name!r}, "argv": sys.argv[1:], "dry": DRY}}) + "\\n")
"""
BEGIN, END = "-----BEGIN OPENSSH PRIVATE KEY-----", "-----END OPENSSH PRIVATE KEY-----"
#: A script and its child that ignore SIGTERM (inherited across exec), and a token's head
#: whose tail never comes.
#: The pids are written after the output, so that a test which kills them once it reads the pids
#: finds the output whole (a CI flake at 3f98a2a2).
STUBBORN = """\
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    print("working", flush=True)
    print("ghp_" + "Q" * 20, end="", flush=True)
    pathlib.Path({pids!r}).write_text(f"{{os.getpid()}} {{child.pid}}")  # after its output
    time.sleep(60)
"""


@pytest.fixture
def site(tmp_path, monkeypatch):
    site = make_site(tmp_path)
    layout = replace(site.paths.layout, release=str(tmp_path / "release"), python=sys.executable)
    site.paths = replace(site.paths, layout=layout)
    for name, value in (("PROGRESS", 0.05), ("GRACE", 0.3), ("STOP_GRACE", 0.3), ("DRAIN", 1.0)):
        monkeypatch.setattr(runner_mod, name, value)
    monkeypatch.setattr(zulip_client, "GAP", 0.0)  # the edits' 60 s, scaled as PROGRESS is
    fake(site, "aws/power.py", 'print("alice: would stop i-1" if DRY else "alice: stopped")')
    fake(site, "admin/status.py", 'print(json.dumps({"alice": "running"}))')
    return site


def fake(site, name, body):
    """A fake `workshop/<name>` that logs each call to `calls.jsonl`, then runs `body`."""
    path = Path(site.paths.layout.release) / "workshop" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    calls = str(site.tmp / "calls.jsonl")
    path.write_text(PRELUDE.format(calls=calls, name=name) + textwrap.dedent(body))


def calls(site, name=None, dry=None):
    path = site.tmp / "calls.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    return [r for r in rows if name in (None, r["script"]) and dry in (None, r["dry"])]


def vms(fleet, names):
    return {name: VMS[name] for name in names if name in VMS}


async def start(site, **kwargs):
    srv = site.server()
    srv.planner = Planning(Runner(srv, **({"vms": vms} | kwargs)))
    await srv.start()
    return srv


@pytest.fixture
async def server(site):
    srv = await start(site)
    yield srv
    await srv.close()


def bot_of(srv, uid):
    return next(b for b in srv.bots if b.user_id == uid)


async def settle(srv):
    while srv.planner.tasks:
        await asyncio.gather(*list(srv.planner.tasks), return_exceptions=True)


async def request(srv, text, sender=OLGA, bot=HARVARD, stream=HARVARD_CH, mid=5000):
    await bot_of(srv, bot).on_message(incoming(sender, text, stream=stream, mid=mid))
    await settle(srv)
    ids = [row[0] for row in srv.store.db.execute("SELECT id FROM plans")]
    return srv.store.get(max(ids))


async def approve(srv, plan, sender=OLGA, bot=HARVARD, stream=HARVARD_CH, mid=LATE, phrase=""):
    text = f"!approve {plan.name} {plan.hash[:6]} {phrase}".strip()
    await bot_of(srv, bot).on_message(incoming(sender, text, stream=stream, mid=mid))


async def status(srv, text, mid=6000):
    await bot_of(srv, HARVARD).on_message(incoming(OLGA, text, mid=mid))
    await settle(srv)


def posts(site, by=HARVARD):
    return [p["content"] for p in site.realm.posts(by)]


def records(site, event=None):
    rows = [json.loads(line) for line in audit_text(site.audit).splitlines()]
    return [r for r in rows if event is None or r["event"] == event]


def everything(site):
    """Every text that left the runner: posts, edits, attachments, saved runs, the audit log."""
    texts = [p["content"] for p in site.realm.posts()] + [e[2] for e in site.realm.edits]
    texts += [v.decode("utf-8", "replace") for v in site.realm.uploads.values()]
    texts += [p.read_text() for p in Path(site.paths.layout.runs).rglob("*.txt")]
    return "\n".join([*texts, audit_text(site.audit)])


def gone(pid):
    """The process has ended (a zombie waiting for init counts). One reaped between the open
    of its `stat` and the read raises ESRCH (`ProcessLookupError`): ended too."""
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError):
        return True
    return state in ("Z", "X")


def pids(path):
    return [int(p) for p in path.read_text().split()] if path.exists() else []


# ------------------------------------------------------------------------------- plans


async def test_a_plan_shows_its_dry_run_and_status_and_saves_them_scrubbed(site, server):
    fake(site, "aws/power.py", f'print("alice: would stop i-1", {TOKEN!r})')
    plan = await request(server, "!stop alice")
    assert plan.state == PENDING, plan.note
    text = posts(site)[-2]
    framed = "── aws/power.py\nalice: would stop i-1 [redacted secret]\n── aws/power.py exited 0"
    assert framed in text
    assert '{"alice": "running"}' in text
    saved = Path(site.paths.layout.runs) / plan.name / "dry-run.txt"
    assert "[redacted secret]" in saved.read_text() and TOKEN not in everything(site)
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600
    assert not (Path(site.paths.tmp) / plan.name).exists()  # its TMPDIR is gone
    assert [c["script"] for c in calls(site)] == ["aws/power.py", "admin/status.py"]
    assert "--dry-run" in calls(site)[0]["argv"]


#: `status.py --json`'s answer for alice (schema 1), with her running turns.
STATUS = """\
import json, os
turns = [{{"bot": "claude", "backend": "claude", "started_at": None}}] * {turns}
vm = {{"slug": "alice", "host": "198.51.100.1", "bridge": "active", "zab_version": "0.2.3",
       "running_turns": turns, "idle_stop": {{"enabled": True, "active": True, "minutes": 15}}}}
print("EC2 lookup failed (NoCredentialsError: none)", flush=True)
print(json.dumps({{"schema": 1, "vms": [vm], "summary": {{}}, "ec2_error": None}}, indent=2))
"""


async def test_the_status_read_is_a_table_apart_from_the_dry_run_and_never_hashed(site, server):
    """#361: the status read is kept apart from the dry runs: the plan shows it as a table, one
    line per VM, outside the dry-run block, and saves it whole after the dry run. Its running
    turns change by the minute: a hashed dry run's plan hashes alike whatever it says."""
    fake(site, "admin/push_config.py", 'print("dry-run alice (198.51.100.1): idle stop installed")')
    fake(site, "admin/status.py", STATUS.format(turns=0))
    one = await request(server, "!idle-stop 30 alice")
    text = posts(site)[-2]
    dry = text.split("**Dry run**", 1)[1].split("**Status**", 1)[0]
    assert "idle stop installed" in dry and "schema" not in dry and "alice  active" not in dry
    table = text.split("**Status** of the targets, read for this plan (shown, not hashed):\n")[1]
    assert table.startswith(
        "```text\nVM     state   zab    idle stop  running turns\n"
        "alice  active  0.2.3  15 min     0\nEC2 lookup failed (NoCredentialsError: none)\n```"
    )
    saved = (Path(site.paths.layout.runs) / one.name / "dry-run.txt").read_text()
    assert saved.index("idle stop installed") < saved.index('"running_turns": []')
    fake(site, "admin/status.py", STATUS.format(turns=2))
    two = await request(server, "!idle-stop 30 alice", mid=5001)
    assert "alice  active  0.2.3  15 min     2 claude" in posts(site)[-2]
    assert one.state == two.state == PENDING and one.hash == two.hash


async def test_a_token_split_inside_the_status_json_is_redacted(site, server):
    """The review of #380: `json.dumps` writes a VM's U+200B as `\\u200b`, so a token it splits
    is out of a scan's sight in the escaped text. The plan's status read (saved, shown) and
    `!status` are scrubbed by their JSON's values (`Scrubber.json_text`)."""
    token = "ghp_" + "Z" * 36
    fake(site, "admin/status.py", f"""\
        vm = {{"slug": "alice", "bridge": "active", "last_error": {token[:9]!r} + "\\u200b" +
              {token[9:]!r}, "doctor_failures": [], "idle_stop": None, "running_turns": []}}
        print(json.dumps({{"schema": 1, "vms": [vm]}}, indent=2))
    """)  # fmt: skip
    plan = await request(server, "!stop alice")
    assert plan.state == PENDING, plan.note
    await status(server, "!status alice")
    assert "\n- last error: `[redacted github token]`\n" in posts(site)[-1]  # #439: no JSON
    saved = (Path(site.paths.layout.runs) / plan.name / "dry-run.txt").read_text()
    assert '"last_error": "[redacted' in saved
    assert "Z" * 20 not in everything(site)


async def test_a_failed_dry_run_refuses_the_plan(site, server):
    fake(site, "aws/power.py", 'print("alice: no such instance"); sys.exit(2)')
    plan = await request(server, "!stop alice")
    assert plan.state == REFUSED and plan.note == "the dry run failed: aws/power.py exited 2"
    assert "alice: no such instance" in posts(site)[-1]
    assert calls(site, "admin/status.py") == []  # nothing after a failed dry run


async def test_a_hashed_dry_run_is_deterministic(site, server):
    """§2.4: two plans of one command hash alike, whatever the time and their TMPDIRs; a change
    in what the dry run prints changes the hash."""
    state = site.tmp / "fleet-state"
    state.write_text("idle stop off")
    fake(site, "admin/push_config.py", f"""\
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        print("alice:", open({str(state)!r}).read(), os.environ["TMPDIR"] + "/zab-push-x1", stamp)
    """)  # fmt: skip
    one = await request(server, "!idle-stop 30 alice")
    await asyncio.sleep(1.1)  # another second in the timestamp
    two = await request(server, "!idle-stop 30 alice", mid=5001)
    state.write_text("idle stop 15")
    three = await request(server, "!idle-stop 30 alice", mid=5002)
    assert one.hash == two.hash != three.hash


async def test_a_hashed_dry_run_is_shown_whole_or_refused(site, server, monkeypatch):
    """Codex round 1 on #353, and the review of #353: every character a plan's hash covers is
    shown. Past `KEEP // 2`, or once the output shown is cut, a hashed dry run refuses the plan,
    whatever its unseen tail; a target verb's long dry run is only shown, so it still plans."""
    monkeypatch.setattr(runner_mod, "KEEP", 3000)
    for tail in ("A", "B"):  # the review's probe: 29 lines of 100, then 2979 more characters
        long = f'print(("x" * 99 + "\\n") * 29 + "command {tail * 2971}")'
        fake(site, "admin/push_config.py", long)
        plan = await request(server, "!idle-stop 30 alice", mid=5000 + ord(tail))
        assert plan.state == REFUSED
        assert plan.note == "the dry run printed over 1500 characters: too long to hash"
    fake(site, "admin/push_config.py", 'print(("x" * 99 + "\\n") * 20)')  # 2000: shown whole
    plan = await request(server, "!idle-stop 30 alice", mid=5099)
    assert plan.state == REFUSED and "over 1500 characters" in plan.note  # hashed.full alone
    fake(site, "admin/push_config.py", 'print(("x" * 99 + "\\n") * 14)')  # 1400: whole
    assert (await request(server, "!idle-stop 30 alice", mid=5100)).state == PENDING
    fake(site, "aws/power.py", 'print(("x" * 99 + "\\n") * 40, "command A")')
    assert (await request(server, "!stop alice", mid=5101)).state == PENDING


async def test_a_hashed_dry_run_whose_shown_output_is_cut_is_refused(site, server, monkeypatch):
    """The other half of the guard: should the output shown be cut first (its frame lines, a
    smaller limit), the plan is refused too, before the hashed part fills."""

    class SmallShown(runner_mod.Capture):
        def __init__(self, limit=None):
            super().__init__(200 if limit is None else limit)

    monkeypatch.setattr(runner_mod, "Capture", SmallShown)
    fake(site, "admin/push_config.py", 'print(("x" * 99 + "\\n") * 3)')  # 300: under KEEP // 2
    plan = await request(server, "!idle-stop 30 alice")
    assert plan.state == REFUSED and plan.note.endswith("too long to hash")


async def test_target_verbs_hash_the_vms(site, monkeypatch):
    srv = await start(site)
    try:
        one = await request(srv, "!stop alice")
        monkeypatch.setitem(VMS, "alice", "i-9")
        two = await request(srv, "!stop alice", mid=5001)
    finally:
        await srv.close()
    assert one.hash != two.hash
    srv = await start(site, vms=None)
    try:
        plan = await request(srv, "!stop alice", mid=5002)
    finally:
        await srv.close()
    assert plan.state == REFUSED and plan.note == "this server can't list its VMs yet: nothing runs"


async def test_status_reads_through_the_runner(site, server):
    await status(server, "!status alice")
    assert '"alice": "running"' in posts(site)[-1] and "──" not in posts(site)[-1]
    fake(site, "admin/status.py", 'print("bob: unreachable"); sys.exit(1)')
    await status(server, "!status bob", mid=6001)
    assert "bob: unreachable\n── admin/status.py exited 1" in posts(site)[-1]
    assert _listed(site.paths.tmp) == []  # each read's TMPDIR is gone


# ----------------------------------------------------------------------- apply → result


async def test_apply_to_result(site, server):
    plan = await request(server, "!stop alice")
    await approve(server, plan)
    await settle(server)
    assert server.store.get(plan.id).state == DONE
    progress, result = posts(site)[-2:]
    assert progress.startswith(f"⏳ **{plan.name}** · `!stop alice`: applying")
    assert result.startswith(f"✅ **{plan.name}** · `!stop alice`: done in ")
    assert "alice: stopped\n── aws/power.py exited 0" in result
    head = verify(site.audit).head
    assert verify(site.audit).ok and result.endswith(f"`{head}`")
    assert [r["event"] for r in records(site)][-3:] == ["approval", "apply_start", "apply_end"]
    start, end = records(site)[-2:]
    assert start["hash"] == plan.hash and start["approvers"] == [OLGA]
    assert (end["plan"], end["verb"], end["args"], end["exit"]) == (plan.name, "stop", ["alice"], 0)
    assert end["outcome"] == "done"
    (step,) = end["steps"]  # with its start and its seconds (#570)
    assert step == {"script": "aws/power.py", "exit": 0, "ended": "", "at": 1800000000.0,
                    "s": step["s"]}  # fmt: skip
    saved = Path(site.paths.state) / end["output"]
    assert "alice: stopped" in saved.read_text() and stat.S_IMODE(saved.stat().st_mode) == 0o600
    # the dry run at plan time only: a target verb's hash covers the VMs, not its dry run
    assert [c["dry"] for c in calls(site, "aws/power.py")] == [True, False]
    assert not (Path(site.paths.tmp) / plan.name).exists()
    assert server.planner.runner.busy("harvard-2026") is None


async def test_start_all_never_waits(site, server):
    """#334: `!start all` passes no `--wait`."""
    fake(site, "aws/power.py", 'print("started")')
    plan = await request(server, "!start all")
    await approve(server, plan)  # a start takes no phrase
    await settle(server)
    assert server.store.get(plan.id).state == DONE
    argv = calls(site, "aws/power.py", dry=False)[0]["argv"]
    assert "--start" in argv and "--all" in argv and "--wait" not in argv


async def test_a_failing_step_stops_the_rest(site, server):
    fake(site, "aws/launch.py", 'print("launching carol"); sys.exit(0 if DRY else 3)')
    fake(site, "admin/scan_hosts.py", 'print("scanned")')
    plan = await request(server, "!launch carol")
    await approve(server, plan)
    await settle(server)
    assert server.store.get(plan.id).state == FAILED
    assert posts(site)[-1].startswith(
        f"❌ **{plan.name}** · `!launch carol`: failed at step 1 of 2: aws/launch.py exited 3."
    )
    assert calls(site, "admin/scan_hosts.py") == []
    end = records(site, "apply_end")[-1]
    assert end["exit"] == 3 and end["outcome"] == "failed"


#: `aws/adopt_spare.py` as the real script's `main` against a fake EC2 (#455): harvard-2026's
#: spare `zab-spare-1` with its Elastic IP, in the state and with the port 22 that the JSON file
#: `STATE` gives (`adopt_spare` sets `AWS` and `STATE` before it). A retag fails, saying
#: `RETAGGED`.
ADOPT = """
sys.path.insert(0, AWS)
import adopt_spare

SPARE = json.loads(pathlib.Path(STATE).read_text())
OWN = {"workshop": "harvard-2026", "zab-scope": "mathagents", "participant": "zab-spare-1"}
TAGS = [{"Key": k, "Value": v} for k, v in OWN.items()]


class Ec2:
    def __init__(self, region, **config):
        pass

    def found(self, wanted, resource):
        return [resource] if all(OWN.get(k) == v for k, v in wanted.items()) else []

    def find_instances(self, wanted, states, **bound):
        state = {"Name": SPARE["state"]}
        return self.found(wanted, {"InstanceId": "i-0spare1", "State": state, "Tags": TAGS})

    def find_addresses(self, wanted):
        eip = {"AllocationId": "eipalloc-1", "PublicIp": "198.51.100.9", "InstanceId": "i-0spare1"}
        return self.found(wanted, {**eip, "Tags": TAGS})

    def create_tags(self, ids, new):
        raise SystemExit("the fake EC2 retagged")


adopt_spare.Ec2 = Ec2
adopt_spare.wait_for_ssh = lambda ip, timeout_s: SPARE["ssh"]
sys.exit(adopt_spare.main(sys.argv[1:]))
"""
RETAGGED = "the fake EC2 retagged"


def adopt_spare(site, srv) -> Path:
    """`!adopt-spare carol zab-spare-1` made possible on harvard-2026 (`srv` started): the spare
    in its `spares.json`, the fake `ADOPT`, and in the ledger, what `!adopt-spare` replays (a
    held `!provision zulip all` and `!rollout v0.2.5 all`, whose wheel the server must have: a
    stand-in unless there is one). The JSON file that gives the spare's state and port 22, which
    the caller writes."""
    wheel = Path(site.paths.layout.wheels) / "zulip_agent_bridge-0.2.5-py3-none-any.whl"
    if not wheel.exists():
        wheel.parent.mkdir(exist_ok=True)
        wheel.write_bytes(b"wheel")
    entry = {"name": "zab-spare-1", "instance_id": "i-0spare1", "ip": "198.51.100.9"}
    spares = {"harvard-2026/us-west-2/zab-spare-1": {**entry, "host_keys": "SHA256:s1"}}
    (site.tmp / "ws" / "harvard-2026" / "out" / "spares.json").write_text(json.dumps(spares))
    state, aws = site.tmp / "spare-state.json", Path(__file__).parents[2] / "workshop" / "aws"
    fake(site, "aws/adopt_spare.py", f"AWS, STATE = {str(aws)!r}, {str(state)!r}\n{ADOPT}")
    for verb, args in (("provision zulip", ("hold", "all")), ("rollout", ("v0.2.5", "all"))):
        end = ApplyEnd("harvard-2026", "P-3", verb, args, 0)
        srv.ledger.observe({"event": "apply_end", **end.fields()})
    return state


async def test_a_spare_stopping_after_its_plan_fails_the_first_step_before_the_retag(site):
    """#455: the plan's dry run passes while the spare runs; stopping before the approval (the
    hash covers its instance ID, not its state; a spare stops itself since #715), the apply's
    first step, `adopt_spare.py` itself, refuses it before the retag, and nothing after it
    runs. (Stopped by then, it would be started: tests/workshop/test_adopt_spare.py.)"""
    srv = await start(site)
    try:
        state = adopt_spare(site, srv)
        state.write_text(json.dumps({"state": "running", "ssh": True}))
        plan = await request(srv, "!adopt-spare carol zab-spare-1")
        assert plan.state == PENDING, plan.note
        state.write_text(json.dumps({"state": "stopping", "ssh": True}))
        await approve(srv, plan)
        await settle(srv)
        assert srv.store.get(plan.id).state == FAILED
    finally:
        await srv.close()
    assert posts(site)[-1].startswith(
        f"❌ **{plan.name}** · `!adopt-spare carol zab-spare-1`: failed at step 1 of 6: "
        "aws/adopt_spare.py exited 1."
    )
    assert "refusing: zab-spare-1 is stopping: request again in a minute or two" in posts(site)[-1]
    assert [c["dry"] for c in calls(site)] == [True, False]  # adopt_spare.py's alone
    assert RETAGGED not in everything(site)


async def test_the_recheck_makes_a_changed_plan_stale(site, server, monkeypatch):
    """§2.4 step 5: the dry run again for a verb that hashes it, the VMs for a target verb."""
    state = site.tmp / "fleet-state"
    state.write_text("idle stop off")
    fake(site, "admin/push_config.py", f"""\
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        print("alice:", open({str(state)!r}).read(), os.environ["TMPDIR"] + "/zab-push-x1", stamp)
    """)  # fmt: skip
    one = await request(server, "!idle-stop 30 alice")
    two = await request(server, "!idle-stop 30 bob", mid=5001)
    three = await request(server, "!stop alice", mid=5002)
    await approve(server, one)
    await settle(server)
    assert server.store.get(one.id).state == DONE  # the same, but for the time and the TMPDIR
    state.write_text("idle stop 15")
    await approve(server, two, mid=LATE + 1)
    await settle(server)
    stale = server.store.get(two.id)
    assert stale.state == STALE and stale.note.startswith("hash now ")
    assert posts(site)[-1].startswith(f"❌ {two.name} is stale (hash now ")
    assert "; since the plan: the dry run)" in posts(site)[-1]  # the review of #354: what changed
    assert len(calls(site, "admin/push_config.py", dry=False)) == 1
    monkeypatch.setitem(VMS, "alice", "i-9")
    monkeypatch.delitem(VMS, "bob")
    await approve(server, three, mid=LATE + 2)
    await settle(server)
    assert server.store.get(three.id).state == STALE
    assert "; since the plan: alice: i-1 → i-9)" in posts(site)[-1]
    assert calls(site, "aws/power.py", dry=False) == [] and records(site)[-1]["event"] == "stale"
    assert records(site)[-1]["changes"] == ["alice: i-1 → i-9"]


async def test_an_unreadable_token_fails_the_apply_and_says_why(site, server):
    fake(site, "admin/provision_repos.py", 'print("would invite alice")')
    plan = await request(server, "!provision repos alice")
    _remove(site.paths.github_token)
    await approve(server, plan)
    await settle(server)
    assert server.store.get(plan.id).state == FAILED
    text = posts(site)[-1]
    assert text.startswith(f"❌ **{plan.name}** · `!provision repos alice`: the runner failed")
    assert "── the runner failed (the GitHub token can't be read): nothing more ran" in text
    end = records(site, "apply_end")[-1]
    assert end["exit"] == -1 and end["outcome"] == "error"
    assert calls(site, "admin/provision_repos.py", dry=False) == []


def _failing(server, event):
    real = server.audit

    async def audit(name, **fields):
        if name == event:
            raise OSError("no space left on device")
        return await real(name, **fields)

    return audit


async def test_a_failed_apply_end_is_said_and_the_output_kept(site, server, monkeypatch):
    """The review of #354: the result keeps its output, and says that `apply_end` is missing,
    since `!adopt-spare` replays only what the audit log holds."""
    monkeypatch.setattr(server, "audit", _failing(server, "apply_end"))
    plan = await request(server, "!stop alice")
    await approve(server, plan)
    await settle(server)
    assert server.store.get(plan.id).state == DONE and records(site, "apply_end") == []
    result = posts(site)[-1]
    assert "alice: stopped" in result and "`audit #" not in result
    assert result.endswith(
        "⚠ `apply_end` was not written to the audit log: tell a deployer. Until it is fixed, "
        "`!adopt-spare` replays the fleet's settings as they were before this apply."
    )
    assert "alice: stopped" in (Path(site.paths.layout.runs) / plan.name / "apply.txt").read_text()


async def test_nothing_runs_when_apply_start_cant_be_written(site, server, monkeypatch):
    monkeypatch.setattr(server, "audit", _failing(server, "apply_start"))
    plan = await request(server, "!stop alice")
    await approve(server, plan)
    await settle(server)
    assert server.store.get(plan.id).state == FAILED
    assert calls(site, "aws/power.py", dry=False) == []
    assert posts(site)[-1] == f"❌ {plan.name} failed: the audit log can't be written."


async def test_the_result_post_is_tried_again(site, server, monkeypatch):
    """The review of #354: a result whose post surely never landed (no connection was made) is
    tried again; down for every try, the outcome is still in the audit log."""
    monkeypatch.setattr(runner_mod, "BACKOFF", 0.01)
    harvard = bot_of(server, HARVARD)
    real = harvard.client.request
    down = [0]

    async def request_(method, endpoint, **params):
        if endpoint == "messages" and params.get("content", "").startswith("✅ **") and down[0]:
            down[0] -= 1
            raise httpx.ConnectError("connection refused")
        return await real(method, endpoint, **params)

    monkeypatch.setattr(harvard.client, "request", request_)
    one = await request(server, "!stop alice")
    two = await request(server, "!stop bob", mid=5001)
    down[0] = 2
    await approve(server, one)
    await settle(server)
    assert sum(p.startswith(f"✅ **{one.name}**") for p in posts(site)) == 1  # the third try
    down[0] = runner_mod.POSTS
    await approve(server, two, mid=LATE + 1)
    await settle(server)
    assert not any(p.startswith(f"✅ **{two.name}**") for p in posts(site))
    assert server.store.get(two.id).state == DONE
    assert records(site, "apply_end")[-1]["plan"] == two.name


async def test_a_post_stored_then_answered_badly_is_never_posted_twice(site, server, monkeypatch):
    """The re-review of #354 at 1c0862a: Zulip stores the post, then the bot gets a proxy's 502
    page, or a 200 whose body is no JSON. Either may hide a post that landed: never sent again."""
    monkeypatch.setattr(runner_mod, "BACKOFF", 0.01)
    harvard = bot_of(server, HARVARD)
    real = harvard.client.http.request
    answers: list[int] = []

    async def request_(method, url, **kwargs):
        response = await real(method, url, **kwargs)  # it lands
        content = str((kwargs.get("data") or {}).get("content", ""))
        if url == "messages" and content.startswith("✅ **") and answers:
            page = "<html><body>bad gateway</body></html>"
            return httpx.Response(answers.pop(0), text=page, request=response.request)
        return response

    monkeypatch.setattr(harvard.client.http, "request", request_)
    for n, status in enumerate((502, 200)):
        plan = await request(server, f"!stop {('alice', 'bob')[n]}", mid=5000 + n)
        answers[:] = [status]
        await approve(server, plan, mid=LATE + n)
        await settle(server)
        assert sum(p.startswith(f"✅ **{plan.name}**") for p in posts(site)) == 1, status


async def test_a_result_whose_response_was_lost_is_never_posted_twice(site, server, monkeypatch):
    """The re-review of #354: Zulip took the post, and the response was lost (a read timeout):
    it is not sent again. A connection never made is, and posts once."""
    monkeypatch.setattr(runner_mod, "BACKOFF", 0.01)
    harvard = bot_of(server, HARVARD)
    real = harvard.client.request
    failures: list[type[Exception]] = []

    async def request_(method, endpoint, **params):
        if endpoint == "messages" and params.get("content", "").startswith("✅ **") and failures:
            error = failures.pop(0)
            if error is httpx.ReadTimeout:
                await real(method, endpoint, **params)  # it lands
            raise error("the network")
        return await real(method, endpoint, **params)

    monkeypatch.setattr(harvard.client, "request", request_)
    one = await request(server, "!stop alice")
    two = await request(server, "!stop bob", mid=5001)
    failures[:] = [httpx.ReadTimeout]
    await approve(server, one)
    await settle(server)
    assert sum(p.startswith(f"✅ **{one.name}**") for p in posts(site)) == 1  # once, not twice
    failures[:] = [httpx.ConnectError]
    await approve(server, two, mid=LATE + 1)
    await settle(server)
    assert sum(p.startswith(f"✅ **{two.name}**") for p in posts(site)) == 1  # the second try


async def test_an_unreadable_plan_material_never_silences_a_stale(site, server, monkeypatch):
    """The re-review of #354: a `plan_material` row it can't read (another format) leaves the
    stale record and its post as they would be, only without what changed."""
    plan = await request(server, "!stop alice")
    with server.store.db:
        server.store.db.execute(
            "UPDATE plan_material SET material = ? WHERE plan = ?", ('{"release": 1}', plan.id)
        )
    monkeypatch.setitem(VMS, "alice", "i-9")
    await approve(server, plan)
    await settle(server)
    assert server.store.get(plan.id).state == STALE
    assert records(site)[-1]["event"] == "stale" and "changes" not in records(site)[-1]
    assert posts(site)[-1].startswith(f"❌ {plan.name} is stale (hash now ")
    assert "since the plan" not in posts(site)[-1]


ZULIPRC_KEY = "Kq7RtY2uVb9NmZ4wLp8sXc3dFg6hJk1A"  # a bot key a step writes, printed bare


def _writes_a_key(site, then=""):
    """A script that writes a new participant bot zuliprc into `out/`, prints its key bare (no
    pattern knows it: only the registry does), then runs `then`."""
    path = Path(site.paths.state).parent / "ws" / "harvard-2026" / "out" / "alice"
    return f"""\
        if not DRY:
            pathlib.Path({str(path)!r}).mkdir(parents=True, exist_ok=True)
            rc = pathlib.Path({str(path)!r}, "alice-claude.zuliprc")
            rc.write_text("[api]\\nemail=alice-bot@example.com\\nkey={ZULIPRC_KEY}\\n")
            print("made", {ZULIPRC_KEY!r}, flush=True)
            {then}
    """


async def test_the_secrets_are_reloaded_while_a_step_runs(site, server):
    """§2.7, the ticker's reload (`watch_secrets`): a progress edit while the step that wrote a
    key still runs never shows it."""
    fake(site, "aws/power.py", _writes_a_key(site, then="time.sleep(0.5)"))
    plan = await request(server, "!stop alice")
    await approve(server, plan)
    await settle(server)
    assert any("last line: `made [redacted secret]`" in e[2] for e in site.realm.edits)
    assert ZULIPRC_KEY not in everything(site)


async def test_a_key_written_during_the_tickers_check_never_shows(site, server, monkeypatch):
    """The re-review of #354 (its probe): the step writes a key and prints it just after the
    ticker's check. The snapshot comes before the check, so an edit that shows the line has
    reloaded the key first."""
    gate, out = site.tmp / "gate", site.tmp / "ws" / "harvard-2026" / "out" / "alice"
    gate.mkdir()
    rc = f"[api]\\nkey={ZULIPRC_KEY}\\n"
    fake(site, "aws/power.py", f"""\
        if not DRY:
            g = pathlib.Path({str(gate)!r})
            while not (g / "go").exists():
                time.sleep(0.005)
            pathlib.Path({str(out)!r}).mkdir(parents=True, exist_ok=True)
            pathlib.Path({str(out)!r}, "alice-claude.zuliprc").write_text("{rc}")
            print("made", {ZULIPRC_KEY!r}, flush=True)
            (g / "printed").touch()
            time.sleep(1.0)
    """)  # fmt: skip
    real = server.watch_secrets

    async def watch():  # the step writes and prints just after the real check
        await real()
        if not (gate / "go").exists():
            (gate / "go").touch()
            await until(lambda: (gate / "printed").exists())
            await asyncio.sleep(0.1)  # the pump feeds the line

    monkeypatch.setattr(server, "watch_secrets", watch)
    plan = await request(server, "!stop alice")
    await approve(server, plan)
    await settle(server)
    assert site.realm.edits and ZULIPRC_KEY not in everything(site)


async def test_the_secrets_are_reloaded_before_the_result(site, server, monkeypatch):
    """§2.7, the reload in `_end`: with the ticker's reload blind (a key rewritten in a way its
    file signature misses), the result and `apply.txt`, after a later step, never show it. No
    progress edit either: a blinded ticker could show the key (re-review of #354)."""
    monkeypatch.setattr(runner_mod, "PROGRESS", 100.0)

    async def blind():
        return None

    monkeypatch.setattr(server, "watch_secrets", blind)
    fake(site, "aws/launch.py", _writes_a_key(site))
    fake(site, "admin/scan_hosts.py", "time.sleep(0 if DRY else 0.5); print('scanned')")
    plan = await request(server, "!launch carol")
    await approve(server, plan)
    await settle(server)
    assert server.store.get(plan.id).state == DONE
    result, backup = posts(site)[-2:]  # the step wrote out/: a backup follows, and fails here
    assert "made [redacted secret]" in result and backup.startswith("⚠ **P-1**: the encrypted")
    assert ZULIPRC_KEY not in everything(site)


async def test_the_secrets_are_reloaded_before_apply_end(site, monkeypatch):
    """§2.7, the reload in `_end`: a step cut short by a stop never reaches the reload after it,
    and no progress edit came first; `apply.txt` still never holds the key."""
    monkeypatch.setattr(runner_mod, "PROGRESS", 100.0)
    pidfile = site.tmp / "pids"
    then = f"pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid())); time.sleep(60)"
    fake(site, "aws/power.py", _writes_a_key(site, then=then))
    srv = await start(site)
    try:
        plan = await request(srv, "!stop alice")
        await approve(srv, plan)
        await until(lambda: len(pids(pidfile)) == 1)
    finally:
        await srv.close()
    saved = (Path(site.paths.layout.runs) / plan.name / "apply.txt").read_text()
    assert "made [redacted secret]" in saved and ZULIPRC_KEY not in everything(site)


async def test_busy_reads_the_plans_applying_in_state_db(site, server):
    """The review of #354: a plan APPLYING in `state.db` only (no apply in this process) keeps
    its workshop busy."""
    one = await request(server, "!stop alice")
    two = await request(server, "!stop bob", mid=5001)
    server.store.move(one.id, (PENDING,), APPLYING)
    assert server.planner.runner.applying == {}
    await approve(server, two)
    assert posts(site)[-1].startswith(f"busy: {one.name} is applying")
    assert server.store.get(two.id).state == PENDING


async def test_a_plan_whose_ttl_passes_during_the_recheck_expires(site):
    """`begin_apply` past the TTL: EXPIRED, recorded as `expired`, never `stale`."""
    calls_made = []

    def slow(fleet, names):
        calls_made.append(names)
        if len(calls_made) == 2:  # the recheck, after the approval
            site.clock.now += 10 * 60
        return {name: VMS[name] for name in names if name in VMS}

    srv = await start(site, vms=slow)
    try:
        plan = await request(srv, "!stop alice")
        await approve(srv, plan)
        await settle(srv)
        assert srv.store.get(plan.id).state == EXPIRED
    finally:
        await srv.close()
    assert records(site)[-1]["event"] == "expired"
    assert posts(site)[-1].startswith(f"❌ {plan.name} is expired (not applied within the TTL)")
    assert calls(site, "aws/power.py", dry=False) == []


async def test_the_runners_own_lock(site, server):
    """A second line behind the planner's busy mark: an apply handed over while another plan of
    its workshop applies goes STALE, and nothing runs."""
    one = await request(server, "!stop alice")
    two = await request(server, "!stop bob", mid=5001)
    runner = server.planner.runner
    runner.applying["harvard-2026"] = one
    with server.store.db:
        server.store.db.execute(
            "UPDATE plans SET state = ?, approved = ? WHERE id = ?",
            (APPROVED, site.clock(), two.id),
        )
    harvard = bot_of(server, HARVARD)
    command, action, fleet = await server.planner.rebuild(harvard, server.store.get(two.id))
    job = Job(server.store.get(two.id), harvard, HARVARD_CH, "ops", command, action, fleet)
    await runner.apply(job)
    stale = server.store.get(two.id)
    assert (stale.state, stale.note) == (STALE, f"{one.name} was applying")
    assert runner.applying == {"harvard-2026": one}
    assert calls(site, "aws/power.py", dry=False) == []


async def test_a_server_plan_holds_every_workshops_lock(site, server):
    """#418, §2.4 step 5: while a server plan (`!new-workshop`, workshop "") applies, no
    workshop's plan does, and it doesn't while any workshop's does: the runner's own line."""
    one = await request(server, "!stop alice")
    two = await request(server, "!stop bob", mid=5001)
    runner = server.planner.runner
    with server.store.db:
        server.store.db.execute("UPDATE plans SET workshop = '', state = ? WHERE id = ?",
                                (APPLYING, one.id))  # fmt: skip
        server.store.db.execute(
            "UPDATE plans SET state = ?, approved = ? WHERE id = ?",
            (APPROVED, site.clock(), two.id),
        )
    assert runner._blocking("harvard-2026").id == one.id  # the server's, in state.db
    assert runner._blocking("").id == one.id
    harvard = bot_of(server, HARVARD)
    command, action, fleet = await server.planner.rebuild(harvard, server.store.get(two.id))
    job = Job(server.store.get(two.id), harvard, HARVARD_CH, "ops", command, action, fleet)
    await runner.apply(job)
    stale = server.store.get(two.id)
    assert (stale.state, stale.note) == (STALE, f"{one.name} was applying")
    assert calls(site, "aws/power.py", dry=False) == []


# ------------------------------------------------------------------------------- the lock


GATED = """\
    if not DRY:
        pathlib.Path({gate!r}, TAG + ".started").touch()
        for _ in range(2000):
            if pathlib.Path({gate!r}, "go").exists():
                break
            time.sleep(0.01)
        else:
            sys.exit(9)
    print(TAG, "done")
"""


async def test_busy_and_two_workshops_at_once(site, server):
    gate = site.tmp / "gate"
    gate.mkdir()
    fake(site, "aws/power.py", GATED.format(gate=str(gate)))
    one = await request(server, "!stop alice")
    two = await request(server, "!stop bob", mid=5001)
    on_test = {"sender": TESS, "bot": TEST, "stream": TEST_CH}
    other = await request(server, "!stop erin", mid=5002, **on_test)
    await approve(server, one)
    await until(lambda: (gate / "workshop=harvard-2026.started").exists())
    runner = server.planner.runner
    assert runner.busy("harvard-2026").id == one.id and runner.busy("zab-test") is None
    await approve(server, two, sender=OSCAR, mid=LATE + 1)
    assert posts(site)[-1].startswith(f"busy: {one.name} is applying")
    assert server.store.get(two.id).state == PENDING
    # another workshop applies meanwhile: its script waits at the same gate
    await approve(server, other, mid=LATE + 2, phrase="stop 1", **on_test)
    await until(lambda: (gate / "workshop=zab-test.started").exists())
    assert server.store.get(one.id).state == server.store.get(other.id).state == APPLYING
    (gate / "go").touch()
    await settle(server)
    assert server.store.get(one.id).state == server.store.get(other.id).state == DONE
    assert runner.busy("harvard-2026") is None and runner.applying == {}
    await approve(server, two, sender=OSCAR, mid=LATE + 3)
    await settle(server)
    assert server.store.get(two.id).state == DONE


#: `roster_sync.py --rename OLD NEW`, as the real one checks and writes: the new slug against
#: the other workshops' rosters, then (applying) the gate, then its own roster rewritten. The
#: check comes before the gate: two applies running at once would both pass it.
RENAME = """\
    args = sys.argv[1:]
    roster = pathlib.Path(args[args.index("--roster") + 1])
    old, new = args[args.index("--rename") + 1 : args.index("--rename") + 3]
    for key, spec in zip(args, args[1:]):
        if key == "--workshop-roster":
            ws, path = spec.split("=", 1)
            rows = pathlib.Path(path).read_text().splitlines()[1:]
            if new in [row.split(",")[0] for row in rows]:
                print("refused: " + new + " is in " + ws + "; a slug is never shared")
                sys.exit(1)
    if DRY:
        print("~ " + old + " -> " + new)
        sys.exit(0)
    gate = pathlib.Path(GATE)
    (gate / (TAG + ".started")).touch()
    for _ in range(2000):
        if (gate / "go").exists():
            break
        time.sleep(0.01)
    else:
        sys.exit(9)
    lines = roster.read_text().splitlines()
    lines = [new + line[len(old):] if line.startswith(old + ",") else line for line in lines]
    roster.write_text("\\n".join(lines) + "\\n")
    print("renamed " + old + " to " + new)
"""


def slugs(site, workshop):
    rows = (site.tmp / "ws" / workshop / "roster.csv").read_text().splitlines()[1:]
    return [row.split(",")[0] for row in rows]


async def test_two_workshops_renaming_to_one_slug_at_once_never_both_take_it(site, server):
    """#646: each apply holds only its workshop's lock, so without the server's roster lock two
    workshops' renames to one free slug, approved within seconds, would both pass the check and
    both write it. Under it, harvard's applies; zab-test's waits, says so, then rechecks: its
    hash covers harvard's roster, which harvard's apply changed, so it is STALE and runs
    nothing."""
    gate = site.tmp / "gate"
    gate.mkdir()
    fake(site, "admin/roster_sync.py", RENAME.replace("GATE", repr(str(gate))))
    on_test = {"sender": TESS, "bot": TEST, "stream": TEST_CH}
    one = await request(server, "!roster rename carol dora")
    other = await request(server, "!roster rename erin dora", mid=5001, **on_test)
    assert one.state == other.state == PENDING, (one.note, other.note)
    await approve(server, one)
    await until(lambda: (gate / "workshop=harvard-2026.started").exists())
    runner = server.planner.runner
    assert runner.rosters.locked() and [p.id for p in runner.queued] == [one.id]
    await approve(server, other, mid=LATE + 1, **on_test)
    waits = f"⏳ {other.name} waits for {one.name} of harvard-2026: slugs are unique"
    started = gate / "workshop=zab-test.started"
    await until(lambda: started.exists() or any(p.startswith(waits) for p in posts(site, TEST)))
    assert runner.busy("zab-test").id == other.id  # its workshop's lock first, then the wait
    assert server.store.get(other.id).state == APPROVED and not started.exists()
    (gate / "go").touch()
    await settle(server)
    assert server.store.get(one.id).state == DONE
    stale = server.store.get(other.id)
    assert stale.state == STALE and stale.note.startswith("hash now ")
    said = posts(site, TEST)[-1]
    assert said.startswith(f"❌ {other.name} is stale") and "roster.csv changed" in said, said
    assert slugs(site, "harvard-2026") == ["alice", "bob", "dora"]
    assert slugs(site, "zab-test") == ["erin"]
    applied = [c["argv"] for c in calls(site, "admin/roster_sync.py", dry=False)]
    assert len(applied) == 1 and "workshop=harvard-2026" in applied[0]
    assert not runner.rosters.locked() and runner.queued == [] and runner.applying == {}
    # requested again, the rename is refused at its dry run: harvard has the slug now
    again = await request(server, "!roster rename erin dora", mid=5002, **on_test)
    assert again.state == REFUSED and "dora is in harvard-2026" in posts(site, TEST)[-1]


MIT, MIT_CH, MIA = 903, 47, 108  # a third workshop's bot, its channel and its organizer


def third_workshop(site, monkeypatch):
    """mit-2027, as botkit's zab-test: its bot, channel, organizer, and a roster of one row."""
    text = site.config_path.read_text()
    block = text[text.index('[[workshop]]\nname = "zab-test"') :]
    for old, new in (("zab-test", "mit-2027"), (f"user_id = {TEST}", f"user_id = {MIT}"),
                     (f"channels = [{TEST_CH}]", f"channels = [{MIT_CH}]"),
                     (f"organizers = [{TESS}]", f"organizers = [{MIA}]")):  # fmt: skip
        block = block.replace(old, new)
    site.config_path.write_text(f"{text}\n{block}")
    for uid, name, bot in ((MIA, "Mia Organizer", False), (MIT, "Workshop admin · mit-2027", True)):
        site.realm.person(uid, name, bot=bot)
        monkeypatch.setitem(botkit.BOTS if bot else botkit.NAMES, uid, name)
    site.realm.channel(MIT_CH, DREW, ALICE, MIA, MIT)
    _write(site.tmp / "etc" / "secrets" / "bots" / "mit-2027.zuliprc", site.realm.zuliprc(MIT),
           0o640)  # fmt: skip
    roster = "slug,first_name,email,ip,region\nfred,Fred,fred@example.com,203.0.113.6,us-east-1\n"
    _write(site.tmp / "ws" / "mit-2027" / "roster.csv", roster, 0o600)
    (site.tmp / "ws" / "mit-2027" / "out").mkdir(mode=0o700)


async def test_a_third_workshops_plan_that_comes_as_the_lock_passes_on_says_it_waits(
    site, monkeypatch
):
    """N1 of #793's review: harvard's rename holds the roster lock and zab-test's waits. A
    third workshop's comes just as harvard's releases it, before zab-test's has resumed: it
    waits behind zab-test's (an `asyncio.Lock` hands the lock to its first waiter), and says
    so, naming harvard's, which is still on its way out, and the one more before it. Then each
    waiter, whose inputs harvard's apply changed, is STALE; only harvard's script applied."""
    third_workshop(site, monkeypatch)
    gate = site.tmp / "gate"
    gate.mkdir()
    fake(site, "admin/roster_sync.py", RENAME.replace("GATE", repr(str(gate))))
    srv = await start(site)
    try:
        on_test = {"sender": TESS, "bot": TEST, "stream": TEST_CH}
        on_mit = {"sender": MIA, "bot": MIT, "stream": MIT_CH}
        one = await request(srv, "!roster rename carol dora")
        two = await request(srv, "!roster rename erin dora", mid=5001, **on_test)
        three = await request(srv, "!roster rename fred dora", mid=5002, **on_mit)
        assert one.state == two.state == three.state == PENDING, (one.note, two.note, three.note)
        runner = srv.planner.runner
        await approve(srv, one)
        await until(lambda: (gate / "workshop=harvard-2026.started").exists())
        await approve(srv, two, mid=LATE + 1, **on_test)
        await until(lambda: [p.id for p in runner.queued] == [one.id, two.id])
        # mit-2027's plan, approved; its apply begins as the lock is released, at once
        with srv.store.db:
            srv.store.db.execute("UPDATE plans SET state = ?, approved = ? WHERE id = ?",
                                 (APPROVED, site.clock(), three.id))  # fmt: skip
        mit = bot_of(srv, MIT)
        command, action, fleet = await srv.planner.rebuild(mit, srv.store.get(three.id))
        job = Job(srv.store.get(three.id), mit, MIT_CH, "ops", command, action, fleet)
        release, late, loop = runner.rosters.release, [], asyncio.get_running_loop()

        def passing_on():
            release()
            if not late:  # before zab-test's resumes: it is queued, so the lock isn't free
                late.append(asyncio.Task(runner.apply(job), loop=loop, eager_start=True))
                assert not runner.rosters.locked()

        monkeypatch.setattr(runner.rosters, "release", passing_on)
        (gate / "go").touch()
        await until(lambda: bool(late))
        await asyncio.gather(late[0], *list(srv.planner.tasks))
        await settle(srv)
        states = [srv.store.get(p.id).state for p in (one, two, three)]
    finally:
        await srv.close()
    assert states == [DONE, STALE, STALE], states
    said = posts(site, MIT)
    waits = f"⏳ {three.name} waits for {one.name} of harvard-2026 and 1 more: slugs are unique"
    assert said[-2].startswith(waits), said[-2:]
    assert said[-1].startswith(f"❌ {three.name} is stale"), said[-1]
    assert [slugs(site, ws) for ws in ("harvard-2026", "zab-test", "mit-2027")] == [
        ["alice", "bob", "dora"], ["erin"], ["fred"]
    ]  # fmt: skip
    applied = [c["argv"] for c in calls(site, "admin/roster_sync.py", dry=False)]
    assert len(applied) == 1 and "workshop=harvard-2026" in applied[0]
    assert runner.queued == [] and runner.applying == {}


async def test_a_roster_plan_whose_ttl_passes_while_it_waits_expires_before_its_recheck(
    site, server, monkeypatch
):
    """N3 of #793's review: a waiter past its TTL is EXPIRED, whatever the first apply
    changed, and is told so once it has the lock, without its recheck under it."""
    gate = site.tmp / "gate"
    gate.mkdir()
    fake(site, "admin/roster_sync.py", RENAME.replace("GATE", repr(str(gate))))
    on_test = {"sender": TESS, "bot": TEST, "stream": TEST_CH}
    one = await request(server, "!roster rename carol dora")
    other = await request(server, "!roster rename erin eve", mid=5001, **on_test)
    runner = server.planner.runner
    rechecked = []
    live = runner._live

    async def recheck(plan, *args, **kwargs):
        rechecked.append(plan.id)
        return await live(plan, *args, **kwargs)

    monkeypatch.setattr(runner, "_live", recheck)
    await approve(server, one)
    await until(lambda: (gate / "workshop=harvard-2026.started").exists())
    await approve(server, other, mid=LATE + 1, **on_test)
    await until(lambda: [p.id for p in runner.queued] == [one.id, other.id])
    site.clock.now += server.config.plan_ttl_minutes * 60
    (gate / "go").touch()
    await settle(server)
    assert server.store.get(one.id).state == DONE
    expired = server.store.get(other.id)
    assert (expired.state, expired.note) == (EXPIRED, "not applied within the TTL")
    assert rechecked == [one.id]  # harvard's recheck alone: zab-test's never ran
    said = posts(site, TEST)[-1]
    assert said.startswith(f"❌ {other.name} is expired (not applied within the TTL): nothing "
                           "was done. Request again.\n`audit #"), said  # fmt: skip
    assert records(site, "expired")[-1]["plan"] == other.name
    assert slugs(site, "zab-test") == ["erin"] and runner.queued == []


# ----------------------------------------------------------------------- restarts, signals


HANGING = """\
    if not DRY:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        print("working", flush=True)
        pathlib.Path({pids!r}).write_text(f"{{os.getpid()}} {{child.pid}}")  # after its output
        time.sleep(60)
"""


async def test_a_restart_mid_apply_interrupts_it_for_good(site):
    pidfile = site.tmp / "pids"
    fake(site, "aws/power.py", HANGING.format(pids=str(pidfile)))
    srv = await start(site)
    try:
        plan = await request(srv, "!stop alice")
        await approve(srv, plan)
        await until(lambda: len(pids(pidfile)) == 2)
        assert srv.store.get(plan.id).state == APPLYING
    finally:
        await srv.close()  # cancels the apply, as SIGTERM does
    await until(lambda: all(gone(pid) for pid in pids(pidfile)))
    end = records(site, "apply_end")[-1]
    assert end["outcome"] == "interrupted" and end["exit"] == -15
    assert end["steps"][0]["ended"] == "stopped"
    assert "working" in (Path(site.paths.state) / end["output"]).read_text()
    srv = await start(site)
    try:
        assert srv.store.get(plan.id).state == INTERRUPTED
    finally:
        await srv.close()
    assert records(site)[-1]["event"] == "interrupted"
    assert posts(site)[-1].startswith(f"⏹ {plan.name} was interrupted")
    assert len(calls(site, "aws/power.py", dry=False)) == 1  # never run again
    assert verify(site.audit).ok


async def test_sigterm_mid_apply(site):
    """systemd's stop: `serve` cancels the bots, and the runner stops the apply's group."""
    pidfile = site.tmp / "pids"
    fake(site, "aws/power.py", HANGING.format(pids=str(pidfile)))
    srv = site.server()
    srv.planner = Planning(Runner(srv, vms=vms))
    task = asyncio.create_task(serve(srv))
    await until(lambda: len({q.owner for q in site.realm.queues.values()}) == 3)
    site.realm.say(OLGA, HARVARD_CH, "!stop alice")
    await until(lambda: any("widget_content" in p for p in site.realm.posts()))
    approval = max(m for m, msg in site.realm.messages.items() if msg.get("widget"))
    site.realm.click(OSCAR, approval, "✅ Approve")  # #366's label
    await until(lambda: len(pids(pidfile)) == 2)
    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(task, 10)
    await until(lambda: all(gone(pid) for pid in pids(pidfile)))
    end = records(site, "apply_end")[-1]
    assert end["outcome"] == "interrupted" and end["steps"][0]["ended"] == "stopped"
    assert not any(p.startswith(("✅ **", "❌ **", "⏱ **")) for p in posts(site))  # no result
    assert verify(site.audit).ok


async def test_a_stop_during_the_recheck_leaves_it_approved(site):
    """Cancelled before `begin_apply`: nothing ran, no `apply_start`; the next start marks it."""
    pidfile = site.tmp / "pids"
    fake(site, "admin/push_config.py", f"""\
        if len(open({str(site.tmp / "calls.jsonl")!r}).readlines()) > 2:
            pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid()))
            time.sleep(60)
        print("would set idle stop")
    """)  # fmt: skip
    srv = await start(site)
    try:
        plan = await request(srv, "!idle-stop 30 alice")
        await approve(srv, plan)
        await until(lambda: len(pids(pidfile)) == 1)
    finally:
        await srv.close()
    await until(lambda: gone(pids(pidfile)[0]))
    assert records(site, "apply_start") == []
    srv = await start(site)
    try:
        assert srv.store.get(plan.id).state == INTERRUPTED
    finally:
        await srv.close()


async def test_the_timeout_at_apply(site, server, monkeypatch):
    """§2.4: 15 minutes (shortened) for all of an apply's steps; the result says how it ended."""
    monkeypatch.setattr(runner_mod, "TIMEOUT", 1.0)
    pidfile = site.tmp / "pids"
    stubborn = textwrap.dedent(STUBBORN.format(pids=str(pidfile)))
    fake(site, "aws/power.py", "if not DRY:\n" + textwrap.indent(stubborn, "    "))
    plan = await request(server, "!stop alice")
    await approve(server, plan)
    await settle(server)
    assert server.store.get(plan.id).state == FAILED
    await until(lambda: all(gone(pid) for pid in pids(pidfile)))
    result = posts(site)[-1]
    assert result.startswith(
        f"⏱ **{plan.name}** · `!stop alice`: timed out after 1 s, at step 1 of 1: aws/power.py "
        "timed out: SIGTERM, then SIGKILL; exit -9"
    )
    assert "working\n[its last line, cut short by the kill, is not shown]" in result
    end = records(site, "apply_end")[-1]
    assert (end["outcome"], end["exit"], end["steps"][0]["ended"]) == ("timed out", -9, "timeout")
    assert "QQQQ" not in everything(site)


async def test_a_step_that_exits_0_at_its_timeout_still_timed_out(site, server, monkeypatch):
    """Codex round 2 on #354: no step runs after it, and the apply is not a success (exit -1)."""
    monkeypatch.setattr(runner_mod, "TIMEOUTS", {"launch": 1.0})
    fake(site, "aws/launch.py", """\
        if not DRY:
            signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
            for _ in range(6000):  # 60 s, never in one sleep (why: above `CHILD`)
                time.sleep(0.01)
    """)  # fmt: skip
    fake(site, "admin/scan_hosts.py", 'print("scanned")')
    plan = await request(server, "!launch carol")
    await approve(server, plan)
    await settle(server)
    assert server.store.get(plan.id).state == FAILED
    assert posts(site)[-1].startswith(
        f"⏱ **{plan.name}** · `!launch carol`: timed out after 1 s, at step 1 of 2: "
        "aws/launch.py timed out: SIGTERM; exit 0"
    )
    assert calls(site, "admin/scan_hosts.py") == []
    end = records(site, "apply_end")[-1]
    assert (end["outcome"], end["exit"], end["steps"][0]["exit"]) == ("timed out", -1, 0)


# ------------------------------------------------------------------- progress, the result


async def test_progress_shows_only_whole_lines_of_everything_scrubbed(site, server):
    """A progress edit scrubs everything so far, never a chunk, and never shows a line still
    arriving: neither a key's body line after its BEGIN nor a token's head shows."""
    fake(site, "aws/power.py", f"""\
        if not DRY:
            print({BEGIN!r}); print("BODY" + "A" * 60, flush=True); time.sleep(0.5)
            print({END!r}); print("between", flush=True); time.sleep(0.5)
            print("ghp_" + "Q" * 20, end="", flush=True); time.sleep(0.5)
            print("Q" * 30, flush=True); print("end")
    """)  # fmt: skip
    plan = await request(server, "!stop alice")
    await approve(server, plan)
    await settle(server)
    assert server.store.get(plan.id).state == DONE
    edits = [e[2] for e in site.realm.edits]
    assert any("last line: `[redacted private key]`" in e for e in edits)
    assert any("last line: `between`" in e for e in edits)
    head = f"⏳ **{plan.name}** · `!stop alice`: applying, step 1 of 1, aws/power.py\nlast line: "
    assert all(e.startswith(head) for e in edits)
    assert "BODY" not in everything(site) and "QQQQ" not in everything(site)


async def test_progress_edits_at_most_every_period(site, server, monkeypatch):
    """At most one edit a `PROGRESS`, and only for a change (#754): a step printing a line
    every 0.3 s for 1.2 s gets an edit for each tick that sees a new one."""
    monkeypatch.setattr(runner_mod, "PROGRESS", 0.25)
    fake(site, "aws/power.py", """\
        if not DRY:
            for n in range(4):
                print("line", n, flush=True); time.sleep(0.3)
    """)  # fmt: skip
    plan = await request(server, "!stop alice")
    await approve(server, plan)
    await settle(server)
    edits = [e[2] for e in site.realm.edits]
    assert 2 <= len(edits) <= 5 and all(a != b for a, b in pairwise(edits))


async def test_a_quiet_step_edits_its_progress_once(site, server, monkeypatch):
    """#754: the progress shows no time, so a step that prints nothing for a second edits it
    once, with the step's frame line, over its ten `PROGRESS` ticks; once more at most, if a
    tick sees its last line before it ends. The result says how long."""
    monkeypatch.setattr(runner_mod, "PROGRESS", 0.1)
    fake(site, "aws/power.py", 'time.sleep(0 if DRY else 1.0); print("ok")')
    plan = await request(server, "!stop alice")
    await approve(server, plan)
    await settle(server)
    head = f"⏳ **{plan.name}** · `!stop alice`: applying, step 1 of 1, aws/power.py\nlast line: "
    edits = [e[2] for e in site.realm.edits]
    assert edits[:1] == [head + "`── aws/power.py`"] and edits[1:] in ([], [head + "`ok`"])
    assert "done in" in posts(site)[-1]


async def test_a_long_apply_edits_its_progress_within_the_bounds(site, server, monkeypatch):
    """#754: the progress message's edits, a gap apart at least, though `PROGRESS` ticks more
    often, and one short of the cap, since its result is a post of its own; the last one says
    that the progress is paused, and the result still comes. Both bounds scaled down, as
    `PROGRESS` is here: 0.1 s for 60 s, and 5 edits for 20."""
    monkeypatch.setattr(zulip_client, "GAP", 0.1)
    monkeypatch.setattr(zulip_client, "MAX_EDITS", 5)
    sent, edit = [], zulip_client.ZulipClient.edit

    async def timed(self, message_id, content, *, final=False):
        sent.append(time.monotonic())
        return await edit(self, message_id, content, final=final)

    monkeypatch.setattr(zulip_client.ZulipClient, "edit", timed)
    fake(site, "aws/power.py", """\
        if not DRY:
            for n in range(30):
                print("line", n, flush=True); time.sleep(0.05)
    """)  # fmt: skip  # a new last line at each of its 30 ticks
    plan = await request(server, "!stop alice")
    await approve(server, plan)
    await settle(server)
    edits = [e[2] for e in site.realm.edits]
    assert len(edits) == len(sent) == 4 and all(b - a >= 0.1 for a, b in pairwise(sent))
    assert edits[-1].endswith(botmod.PAUSED) and not any(botmod.PAUSED in e for e in edits[:-1])
    assert "done in" in posts(site)[-1]


async def test_the_result_is_cut_after_the_whole_is_scrubbed(site, server, monkeypatch):
    """The result's 40 lines and its attachment's bytes: a key block across either never
    shows."""
    monkeypatch.setattr(runner_mod, "ATTACHMENT_BYTES", 600)  # inside the key's body, raw
    key = BEGIN + "\n" + "\n".join(f"BODY{n}" + "A" * 60 for n in range(20)) + "\n" + END
    fake(site, "aws/power.py", f"""\
        if not DRY:
            print("line\\n" * 30 + {key!r}); print("tail\\n" * 30, end="")
    """)  # fmt: skip
    plan = await request(server, "!stop alice")
    await approve(server, plan)
    await settle(server)
    result = posts(site)[-1]
    assert "[redacted private key]" in result and "tail" in result  # the key crossed line 40
    assert f"[{plan.name}-apply.txt](/user_uploads/" in result
    (note,) = [line for line in result.split("\n") if f"{plan.name}-apply.txt" in line]
    assert note.startswith("*The last lines; ") and ZULIP_ITALIC.fullmatch(note), note  # #651
    attached = site.realm.uploads[next(k for k in site.realm.uploads if k.endswith("-apply.txt"))]
    assert b"[redacted private key]" in attached and len(attached) <= 600
    assert "BODY" not in everything(site)


# ----------------------------------------------------------------------------- the VMs


class FakeEc2Client:
    """DescribeInstances, in two pages, as `starter.Ec2` calls it."""

    def __init__(self, instances):
        self.instances, self.filters = instances, []

    def describe_instances(self, Filters, NextToken=None):
        self.filters.append(Filters)
        page = self.instances[1:] if NextToken else self.instances[:1]
        return {"Reservations": [{"Instances": page}], **({} if NextToken else {"NextToken": "2"})}


def _vm(iid, tags, ip="", zone="us-west-2a", state="running"):
    tags = [{"Key": k, "Value": v} for k, v in tags.items()]
    return {"InstanceId": iid, "Tags": tags, "Placement": {"AvailabilityZone": zone},
            "State": {"Name": state}, "PublicIpAddress": ip}  # fmt: skip


def test_a_targets_vms_are_found_by_tag_and_by_address(site):
    """Codex on #354: `power.py` acts on the VM at the roster's `ip`, which a hand-over by hand
    left tagged as a spare; `launch.py` and `protect.py` on the VM tagged with the slug. A
    target's material is both, so a new VM at the address changes the hash."""
    from zab.admin import fleet as fleetmod

    srv = site.server()
    ws = srv.config.workshop("harvard-2026")
    (Path(ws.out_dir) / "spares.json").write_text(json.dumps({
        "harvard-2026/us-west-2/zab-spare-2": {"name": "zab-spare-2", "ip": "198.51.100.9"},
    }))  # fmt: skip
    current = fleetmod.read_fleet(srv.config, ws)
    scope = {"workshop": "harvard-2026", "zab-scope": "mathagents"}  # #422: the fleet's too

    def found(*instances):
        client = FakeEc2Client(list(instances))
        return provider.targets(current, ("alice", "bob", "carol", "zab-spare-2"),
                                starter.Ec2(client))  # fmt: skip

    alice_ip = "198.51.100.1"  # botkit's roster
    assert found(
        _vm("i-old", scope | {"participant": "zab-spare-1"}, ip=alice_ip),  # handed over
        _vm("i-b2", scope | {"participant": "Bob"}),  # a legacy tag, in another case
        _vm("i-b1", scope | {"participant": "bob"}),
        _vm("i-s", scope | {"participant": "zab-spare-2"}, ip="198.51.100.9"),
        _vm("i-x", scope, ip=alice_ip),  # no participant tag: never
        _vm("i-y", scope | {"participant": "carol", "Zab-Admin": "x"}),  # the server's
        _vm("i-v", scope | {"participant": "carol", "ZAB-ADMIN": "x"}),  # in any case
        _vm("i-z", scope | {"participant": "carol"}, zone="us-east-1a"),  # another region
        _vm("i-w", {"workshop": "zab-test", "participant": "carol"}),  # another workshop
        _vm("i-u", {"workshop": "harvard-2026", "participant": "carol"}),  # not in the scope
    ) == {"alice": "i-old", "bob": "i-b1,i-b2", "zab-spare-2": "i-s"}
    swapped = _vm("i-new", scope | {"participant": "zab-spare-1"}, ip=alice_ip)
    assert found(swapped) == {"alice": "i-new"}  # another VM at alice's address


def test_main_hosts_the_runner_with_the_ec2_lookup(site, monkeypatch):
    server, seen = site.server(), []

    async def check(srv):
        seen.append(srv.planner)
        return 0

    monkeypatch.setattr(botmod, "load_admin_config", lambda path: server.config)
    monkeypatch.setattr(botmod, "AdminServer", lambda config, config_path, **_: server)
    monkeypatch.setattr(botmod, "check", check)
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    try:
        assert botmod.main(["--config", str(site.config_path), "--check"]) == 0
    finally:
        root.handlers[:], root.level = handlers, level
    (planner,) = seen
    assert isinstance(planner.runner, Runner) and planner.runner.vms is provider.targets
    assert planner.runner.tagged is provider.tagged  # !retire's exact-tag lookup (#559)
    assert planner.runner.addressed is provider.addressed


# ------------------------------------------------------------------------- the processes


async def test_the_environment_is_built_never_inherited(site, server, monkeypatch, tmp_path):
    """ADMIN §2.3 and CONTRIBUTING.md, rule 2: the operator's HOME, with its agents' credential
    files, and every inherited variable stay out; GH_TOKEN goes only to a step that names it."""
    home = tmp_path / "operator"
    for decoy in (".codex/auth.json", ".claude/.credentials.json", ".aws/credentials"):
        (home / decoy).parent.mkdir(parents=True, exist_ok=True)
        (home / decoy).write_text("decoy")
    monkeypatch.setenv("HOME", str(home))
    for name in ("AWS_SECRET_ACCESS_KEY", "ANTHROPIC_API_KEY", "CODEX_HOME", "PYTHONPATH"):
        monkeypatch.setenv(name, "/decoy")
    report = site.tmp / "report.jsonl"
    probe = f"""\
        home = pathlib.Path(os.path.expanduser("~"))
        import stat
        st = os.fstat(0)
        null = stat.S_ISCHR(st.st_mode) and st.st_rdev == os.stat("/dev/null").st_rdev
        decoys = (".codex/auth.json", ".claude/.credentials.json")
        with open({str(report)!r}, "a") as f:
            f.write(json.dumps({{
                "keys": sorted(os.environ), "home": str(home), "tmp": os.environ["TMPDIR"],
                "cwd": os.getcwd(), "flags": sys.orig_argv[1:5], "stdin_null": null,
                "leader": os.getpgid(0) == os.getpid() == os.getsid(0),
                "found": [p for p in decoys if (home / p).exists()],
                "token": os.environ.get("GH_TOKEN") == {TOKEN!r}, "path": sys.path,
            }}) + "\\n")
    """  # fmt: skip
    fake(site, "aws/power.py", probe)
    fake(site, "admin/provision_repos.py", probe)
    with _stdin_a_pipe():  # the review of #353: an inherited fd 0 must show
        assert (await request(server, "!stop alice")).state == PENDING
        assert (await request(server, "!provision repos alice", mid=5001)).state == PENDING
    rows = [json.loads(line) for line in report.read_text().splitlines()]
    base = ["HOME", "LANG", "PATH", "TMPDIR"]
    assert [r["keys"] for r in rows] == [base, sorted([*base, "GH_TOKEN"])]
    assert [r["token"] for r in rows] == [False, True]
    for r in rows:
        assert r["home"] == site.paths.state and r["found"] == []
        assert r["tmp"] == r["cwd"] and Path(r["tmp"]).parent == Path(site.paths.tmp)
        assert r["flags"] == ["-E", "-s", "-B", "-u"] and r["stdin_null"] and r["leader"]
        assert "/decoy" not in r["path"]


async def test_an_unreadable_token_refuses_the_plan(site, server):
    fake(site, "admin/provision_repos.py", 'print("would invite alice")')
    _remove(site.paths.github_token)
    plan = await request(server, "!provision repos alice")
    assert plan.state == REFUSED and plan.note == "the GitHub token can't be read"
    assert calls(site) == []


async def test_the_timeout_kills_the_group_and_drops_a_cut_line(site, server, monkeypatch):
    """§2.4's timeout (shortened from 15 minutes): SIGTERM to the group, then SIGKILL after
    `GRACE`, said in the output; a script and its child that ignore SIGTERM die all the same.
    The line the kill cut short, a token's head, is never shown."""
    monkeypatch.setattr(runner_mod, "TIMEOUT", 1.0)
    pidfile = site.tmp / "pids"
    fake(site, "admin/status.py", STUBBORN.format(pids=str(pidfile)))
    await status(server, "!status alice")
    await until(lambda: len(pids(pidfile)) == 2 and all(gone(pid) for pid in pids(pidfile)))
    text = posts(site)[-1]
    assert "working\n[its last line, cut short by the kill, is not shown]\n" in text
    assert "── admin/status.py timed out: SIGTERM, then SIGKILL; exit -9" in text
    assert "QQQQ" not in everything(site)


async def test_a_timeout_is_never_a_success(site, server, monkeypatch):
    """Codex round 2 on #354: a script that exits 0 on the timeout's SIGTERM still timed out. Its
    dry run refuses the plan, and `!status` says so."""
    monkeypatch.setattr(runner_mod, "TIMEOUT", 1.0)
    graceful = """\
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        print("partial", flush=True)
        for _ in range(6000):  # 60 s, never in one sleep (why: above `CHILD`)
            time.sleep(0.01)
    """
    fake(site, "aws/power.py", graceful)
    plan = await request(server, "!stop alice")
    assert plan.state == REFUSED
    assert plan.note == "the dry run failed: aws/power.py timed out: SIGTERM; exit 0"
    fake(site, "admin/status.py", graceful)
    await status(server, "!status alice")
    assert "partial\n── admin/status.py timed out: SIGTERM; exit 0" in posts(site)[-1]


async def test_a_stop_during_a_dry_run_leaves_nothing_running(site):
    """The bot stopping cancels the plan's task: the group is stopped within `STOP_GRACE`, and
    the next start marks the plan INTERRUPTED."""
    pidfile = site.tmp / "pids"
    fake(site, "aws/power.py", STUBBORN.format(pids=str(pidfile)))
    srv = await start(site)
    try:
        await bot_of(srv, HARVARD).on_message(incoming(OLGA, "!stop alice"))
        await until(lambda: len(pids(pidfile)) == 2)
    finally:
        await srv.close()
    await until(lambda: all(gone(pid) for pid in pids(pidfile)))  # SIGKILL lands soon after
    srv = await start(site)
    try:
        assert srv.store.get(1).state == INTERRUPTED
    finally:
        await srv.close()
    assert "QQQQ" not in everything(site) and verify(site.audit).ok


async def test_sigterm_during_a_dry_run(site):
    """systemd's stop: `serve` cancels the bots, and the runner stops its process group."""
    pidfile = site.tmp / "pids"
    fake(site, "aws/power.py", STUBBORN.format(pids=str(pidfile)))
    srv = site.server()
    srv.planner = Planning(Runner(srv, vms=vms))
    task = asyncio.create_task(serve(srv))
    await until(lambda: len({q.owner for q in site.realm.queues.values()}) == 3)
    site.realm.say(OLGA, HARVARD_CH, "!stop alice")
    await until(lambda: len(pids(pidfile)) == 2)
    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(task, 10)
    await until(lambda: all(gone(pid) for pid in pids(pidfile)))  # SIGKILL lands soon after
    assert verify(site.audit).ok


async def test_a_process_that_leaves_its_group_cant_hold_the_run(site, server):
    """A process in a session of its own that holds the output pipe is waited for `DRAIN`
    seconds, no more; the line it was writing, a token's head, is dropped, since the run's end
    cut it. (No script of the release does this.)"""
    pidfile, ready = site.tmp / "pids", str(site.tmp / "ready")
    fake(site, "admin/status.py", f"""\
        print("done", flush=True)
        argv = [sys.executable, "-c", "import sys, time; sys.stdout.write('ghp_' + 'Q' * 20); "
                "sys.stdout.flush(); open(sys.argv[1], 'w').close(); time.sleep(30)", {ready!r}]
        away = subprocess.Popen(argv, start_new_session=True)
        pathlib.Path({str(pidfile)!r}).write_text(str(away.pid))
        while not os.path.exists({ready!r}):
            time.sleep(0.01)
    """)  # fmt: skip
    try:
        await asyncio.wait_for(status(server, "!status alice"), 10)
        assert "done\n[its last line, cut short by the kill, is not shown]" in posts(site)[-1]
        assert "QQQQ" not in everything(site)
    finally:
        os.kill(pids(pidfile)[0], signal.SIGKILL)


LEFT_BEHIND = """\
    print("working", flush=True)
    child = [sys.executable, "-c", "import sys, time; sys.stdout.write('ghp_' + 'Q' * 20); "
             "sys.stdout.flush(); open(sys.argv[1], 'w').close(); time.sleep(60)", {ready!r}]
    pathlib.Path({pids!r}).write_text(str(subprocess.Popen(child).pid))
    while not os.path.exists({ready!r}):
        time.sleep(0.01)
"""


async def test_what_the_leader_leaves_is_killed_and_its_cut_line_dropped(site, server):
    """The review of #353: the leader exits 0 and leaves a child of its group mid-line. The
    child is killed at once, and the line it was writing, a token's head, never shows."""
    pidfile, ready = site.tmp / "pids", str(site.tmp / "ready")
    fake(site, "admin/status.py", LEFT_BEHIND.format(pids=str(pidfile), ready=ready))
    await status(server, "!status alice")
    await until(lambda: pids(pidfile) and gone(pids(pidfile)[0]), seconds=2)
    assert "working\n[its last line, cut short by the kill, is not shown]" in posts(site)[-1]
    assert "QQQQ" not in everything(site)


async def test_invalid_utf8_never_splits_a_secret(site, server):
    """The review of #353: a byte that isn't UTF-8 inside a registered token is deleted like any
    invisible character, so the token is redacted; a hashed dry run with such bytes still
    plans, and hashes alike twice."""
    raw = TOKEN[:20].encode() + b"\xff" + TOKEN[20:].encode() + b"\n"
    fake(site, "admin/status.py", f"sys.stdout.buffer.write({raw!r}); sys.stdout.flush()")
    await status(server, "!status alice")
    assert "[redacted secret]" in posts(site)[-1] and "Z" * 12 not in everything(site)
    fake(site, "admin/push_config.py", 'sys.stdout.buffer.write(b"alice: \\xff\\xfe ok\\n")')
    one = await request(server, "!idle-stop 30 alice")
    two = await request(server, "!idle-stop 30 alice", mid=5001)
    assert one.state == two.state == PENDING and one.hash == two.hash


#: `CHILD` and `WAITS_FOR_ITS_CHILD` wait in 10 ms sleeps, never one long one (the review of
#: #640): a Python handler runs only at a check of the eval loop, and `time.sleep` makes none
#: before its syscall. A SIGTERM that lands just before a `time.sleep(60)` enters it waits out
#: the whole sleep, past the grace, whose SIGKILL leaves no mark; a short sleep's next wake
#: runs the handler.
CHILD = """\
import pathlib, signal, sys, time
mark = pathlib.Path(sys.argv[1])
def term(*_):
    mark.write_text("SIGTERM")
    sys.exit(0)
signal.signal(signal.SIGTERM, term)
pathlib.Path(sys.argv[1] + ".ready").touch()
for _ in range(6000):  # 60 s, never in one sleep
    time.sleep(0.01)
"""
#: A leader that, on SIGTERM, waits for its child, which records its own SIGTERM: the mark
#: exists only if the runner signalled the whole group, and before any SIGKILL. The pids are
#: written once both have their handler: the child touches `.ready` after installing its own.
WAITS_FOR_ITS_CHILD = """\
    child = subprocess.Popen([sys.executable, {child!r}, {mark!r}])
    def term(*_):
        child.wait()
        sys.exit(0)
    signal.signal(signal.SIGTERM, term)
    while not os.path.exists({mark!r} + ".ready"):
        time.sleep(0.01)
    pathlib.Path({pids!r}).write_text(f"{{os.getpid()}} {{child.pid}}")
    print("waiting", flush=True)
    for _ in range(6000):  # 60 s, never in one sleep
        time.sleep(0.01)
"""


def _term_script(site):
    child = site.tmp / "child.py"
    child.write_text(CHILD)
    mark, pidfile = site.tmp / "mark", site.tmp / "pids"
    body = WAITS_FOR_ITS_CHILD.format(child=str(child), mark=str(mark), pids=str(pidfile))
    return body, mark, pidfile


def _deadline_on_cue(monkeypatch):
    """The run's deadline passes when the test calls what this returns, and not after a delay:
    `runner_mod`'s `asyncio` alone gets a `timeout` that keeps each one the runner enters, the
    first being the run's deadline (`_stop`'s grace comes after it)."""
    entered = []

    def timeout(delay):
        cm = asyncio.timeout(delay)
        entered.append(cm)
        return cm

    monkeypatch.setattr(runner_mod, "TIMEOUT", 3600.0)
    monkeypatch.setattr(
        runner_mod, "asyncio", SimpleNamespace(**vars(asyncio) | {"timeout": timeout})
    )

    def passes():
        assert len(entered) == 1, entered  # the run's deadline, and no grace yet
        entered[0].reschedule(asyncio.get_running_loop().time())

    return passes


async def test_the_timeout_sends_sigterm_to_the_whole_group(site, server, monkeypatch):
    """#631: the deadline passes once both scripts have their SIGTERM handler, and the grace
    only bounds a failure. A 1 s deadline raced the two interpreters' start, and a 0.3 s grace
    their exit, on a loaded runner: its SIGKILL cut the child inside `write_text`, after the
    open that made the mark and before the write (`'' == 'SIGTERM'`, main bab07012)."""
    passes = _deadline_on_cue(monkeypatch)
    monkeypatch.setattr(runner_mod, "GRACE", 30.0)
    body, mark, pidfile = _term_script(site)
    fake(site, "admin/status.py", body)
    task = asyncio.create_task(status(server, "!status alice"))
    await until(lambda: len(pids(pidfile)) == 2 or task.done())
    if not task.done():  # else the run ended on its own, which the asserts below report
        passes()
    await task
    assert mark.read_text() == "SIGTERM"  # the child got it, before any SIGKILL
    assert "── admin/status.py timed out: SIGTERM; exit 0" in posts(site)[-1]
    await until(lambda: all(gone(pid) for pid in pids(pidfile)))


async def test_a_stop_sends_sigterm_to_the_whole_group_first(site, monkeypatch):
    """The stop comes once both scripts have their handler, and its grace only bounds a
    failure, as in the timeout's test above (#631)."""
    monkeypatch.setattr(runner_mod, "STOP_GRACE", 30.0)
    body, mark, pidfile = _term_script(site)
    fake(site, "aws/power.py", body)
    srv = await start(site)
    try:
        await bot_of(srv, HARVARD).on_message(incoming(OLGA, "!stop alice"))
        await until(lambda: len(pids(pidfile)) == 2)
    finally:
        await srv.close()
    assert mark.read_text() == "SIGTERM"
    await until(lambda: all(gone(pid) for pid in pids(pidfile)))


# -------------------------------------------------------------------------- every cut


async def test_a_private_key_across_the_kept_part(site, server, monkeypatch):
    """The cut at `KEEP`: what is kept is scrubbed whole, so a key block cut there is redacted
    to its end, the note on the cut stays outside it, and nothing after the cut is kept."""
    monkeypatch.setattr(runner_mod, "KEEP", 3000)
    body = "\n".join(f"BODY{n}" + "A" * 60 for n in range(20))
    fake(site, "aws/power.py", f"""\
        print(("x" * 99 + "\\n") * 28, end="")
        print({BEGIN!r}); print({body!r}); print({END!r}); print("after the key")
    """)  # fmt: skip
    plan = await request(server, "!stop alice")
    saved = (Path(site.paths.layout.runs) / plan.name / "dry-run.txt").read_text()
    assert "[redacted private key][output cut here: " in saved
    dry, read = saved.split("── admin/status.py, the status read\n")  # saved after the dry run
    assert dry.endswith("more characters, neither kept nor shown]\n") and "BODY" not in read
    # the review of #353: the approver is told too, in the plan, and in `!status`
    assert "[redacted private key][output cut here: " in posts(site)[-2]
    fake(site, "admin/status.py", f"""\
        print(("x" * 99 + "\\n") * 28, end="")
        print({BEGIN!r}); print({body!r}); print({END!r}); print("after the key")
    """)  # fmt: skip
    await status(server, "!status alice")
    assert "[redacted private key][output cut here: " in posts(site)[-1]
    assert "after the key" not in everything(site) and "BODY" not in everything(site)


async def test_a_key_and_a_token_across_the_cuts_of_a_post(site, server):
    """The plan's 40 lines and `!status`'s 4000 characters are cut after the whole output is
    scrubbed: a key block across the 40th line, and a registered token across the 4000th
    character, never show."""
    key = BEGIN + "\n" + "\n".join(f"BODY{n}" + "A" * 60 for n in range(20)) + "\n" + END
    fake(site, "aws/power.py", f'print("dry\\n" * 30 + {key!r})')
    fake(site, "admin/status.py", f'print("a" * 3950 + {TOKEN!r} + "b" * 100)')
    plan = await request(server, "!stop alice")
    assert plan.state == PENDING and "[redacted private key]" in posts(site)[-2]
    await status(server, "!status alice")
    assert "a" * 100 + "[redacted secret]" in posts(site)[-1]
    shown = everything(site)
    assert "BODY" not in shown and "Z" * 12 not in shown


def test_capture_keeps_whole_lines_and_counts_the_rest():
    capture = Capture(limit=20)
    capture.feed("one\ntwo\nthree-and-more\nfour\n")
    assert capture.kept() == "one\ntwo\n"
    assert capture.note() == "[output cut here: 20 more characters, neither kept nor shown]\n"
    capture.feed("five\n")
    assert "25 more characters" in capture.text()
    partial = Capture()
    partial.feed("done\nghp_head")
    assert partial.kept(whole_lines=True) == "done\n"
    partial.end_line(killed=True)
    assert partial.text() == "done\n[its last line, cut short by the kill, is not shown]\n"
    whole = Capture()
    whole.feed("no newline at the end")
    whole.end_line(killed=False)
    assert whole.text() == "no newline at the end\n"


def _remove(path):
    Path(path).unlink()


def _listed(path):
    return list(Path(path).iterdir())


@contextlib.contextmanager
def _stdin_a_pipe():
    """This process's fd 0, which a child would inherit, as a pipe at its end of file."""
    read, write = os.pipe()
    os.close(write)
    saved = os.dup(0)
    os.dup2(read, 0)
    try:
        yield
    finally:
        os.dup2(saved, 0)
        os.close(saved)
        os.close(read)


# ---------------------------------------------------------------- stage 3 (#357, #367)

ADMIN_SCRIPTS = Path(__file__).resolve().parents[2] / "workshop" / "admin"
#: The real final_sync.py, run as the release's script, its SSH answered as a synced VM would.
REAL_FINAL_SYNC = f"""\
sys.path.insert(0, {str(ADMIN_SCRIPTS)!r})
import admin_common, final_sync
report = json.dumps({{"worktrees": [], "branches": [], "index": "unchanged"}})
final_sync.subprocess_runner = lambda argv, **kw: admin_common.RunResult(0, report + "\\n", "")
sys.exit(final_sync.main(sys.argv[1:]))
"""


def _rewrite(path: str, text: str) -> None:
    Path(path).write_text(text)


async def test_a_strict_final_sync_through_the_runner_marks_only_rows_it_synced(site, server):
    """#367: the runner names its targets in `apply_start` and `apply_end`, so a successful
    `!final-sync` after `!uninstall` marks its rows, and `!teardown` takes them without
    `unsynced`; a row with a VM that `final_sync.py --strict` would skip, bob, never counts."""
    _rewrite(
        server.config.workshop("harvard-2026").roster,
        "slug,first_name,last_name,email,github,ip,region,repo_status\n"
        "alice,Alice,A,alice@example.com,alice-gh,198.51.100.1,us-west-2,ok\n"
        "bob,Bob,B,bob@example.com,bob-gh,198.51.100.2,us-west-2,\n"  # has a VM, not ok
        "carol,Carol,C,carol@example.com,carol-gh,,,\n",
    )
    fake(site, "admin/push_config.py", 'print("would uninstall" if DRY else "uninstalled")')
    fake(site, "admin/final_sync.py", REAL_FINAL_SYNC)
    fake(site, "aws/teardown.py", 'print("terminate i-1 ws-alice (running, participant alice)")')
    down = await request(server, "!uninstall all")
    await approve(server, down, phrase="uninstall 2", mid=LATE)
    await settle(server)
    assert server.store.get(down.id).state == DONE
    every = await request(server, "!final-sync all", mid=5001)
    assert every.state == REFUSED  # bob's VM can't be synced: the dry run fails, --strict
    assert "has a VM but no repo_status: not synced" in posts(site)[-1]
    one = await request(server, "!final-sync alice", mid=5002)
    await approve(server, one, mid=LATE + 1)
    await settle(server)
    assert server.store.get(one.id).state == DONE
    start, end = records(site, "apply_start")[-1], records(site, "apply_end")[-1]
    assert start["targets"] == end["targets"] == ["alice"] and end["exit"] == 0
    ending = server.ledger.workshop("harvard-2026").ending()
    assert ending["alice"].counts and not ending["bob"].counts
    refused = await request(server, "!teardown all", mid=5003)
    assert refused.state == REFUSED
    assert "no !final-sync after its !uninstall on record for bob, carol:" in posts(site)[-1]
    alone = await request(server, "!teardown alice", mid=5004)
    assert alone.state == PENDING
    assert "- alice: synced P-" in posts(site)[-2] and ", after its uninstall P-" in posts(site)[-2]


async def _ended(srv, verb, *slugs, exit=0):
    """A plan of `verb` on `slugs` as the runner records it: its start and its end."""
    fields = {"workshop": "harvard-2026", "plan": "P-90", "verb": verb, "args": list(slugs)}
    await srv.audit("apply_start", **fields, targets=list(slugs))
    await srv.audit("apply_end", **fields, exit=exit, targets=list(slugs))


@pytest.mark.parametrize("unsynced", [True, False])
@pytest.mark.parametrize("verb", ["rollout", "start", "launch"])
async def test_a_plan_naming_a_row_before_the_teardown_applies_makes_it_stale(
    site, server, verb, unsynced
):
    """The review of #367: a `!rollout vX alice` between a teardown's plan and its second
    approval brings her bridge back. The teardown must not apply: STALE, with `unsynced` (its
    hash covers each row's state) and without (its rebuild refuses the row). Either way the
    post names the row and what changed (#376; §2.4 step 5)."""
    fake(site, "aws/teardown.py", 'print("terminate i-1 ws-alice (running, participant alice)")')
    await _ended(server, "uninstall", "alice", "bob")
    await _ended(server, "final-sync", "alice", "bob")
    plan = await request(server, "!teardown alice bob" + " unsynced" * unsynced)
    assert plan.state == PENDING
    phrase = "teardown 2" + " unsynced" * unsynced
    await approve(server, plan, phrase=phrase, mid=LATE)
    await _ended(server, verb, "alice")  # applied meanwhile, on its own plan
    await approve(server, plan, sender=OSCAR, phrase=phrase, mid=LATE + 1)
    await settle(server)
    assert server.store.get(plan.id).state == STALE
    assert [c for c in calls(site, "aws/teardown.py") if not c["dry"]] == []  # nothing ran
    if not unsynced:
        refused = "it no longer builds: no !final-sync after its !uninstall on record for alice:"
        assert refused in posts(site)[-1]
        return
    now = "uninstalled, not synced since" if verb == "start" else "not uninstalled, not synced"
    change = f"alice: synced after its uninstall → {now}"
    assert records(site)[-1]["changes"] == [change]  # bob's state is the same: not named
    assert f"; since the plan: {change}): nothing was done." in posts(site)[-1]


@pytest.mark.parametrize("rows", [19, 20])
async def test_what_changed_keeps_20_and_says_how_many_more(site, server, monkeypatch, rows):
    """The review of #502: a teardown of many rows can differ in more than 20 places (a
    `!rollout … all` between its plan and its approval). The list keeps 20 and says how many
    it left out, never dropping them silently. Through the apply (#510): the STALE post and
    the record's `changes` both carry the list, at the cap's boundary: 20 changes are all
    named, and 21 keep 20 and say `and 1 more`."""
    plan = await request(server, "!stop alice")
    (kept,) = server.store.db.execute(
        "SELECT material FROM plan_material WHERE plan = ?", (plan.id,)
    ).fetchone()
    then = json.loads(kept)  # as the plan kept it, plus rows it named that are gone now
    then["ends"] = {f"p{n:02}": [True, True, True] for n in range(rows)}
    with server.store.db:
        server.store.db.execute(
            "UPDATE plan_material SET material = ? WHERE plan = ?", (json.dumps(then), plan.id)
        )
    monkeypatch.setitem(VMS, "alice", "i-9")  # the hash's own change: one more
    await approve(server, plan)
    await settle(server)
    assert server.store.get(plan.id).state == STALE
    changes = records(site)[-1]["changes"]
    assert records(site)[-1]["event"] == "stale" and changes[0] == "alice: i-1 → i-9"
    assert [c.split(":")[0] for c in changes[1:20]] == [f"p{n:02}" for n in range(19)]
    assert changes[20:] == ([] if rows == 19 else ["and 1 more"]), changes
    assert f"; since the plan: {'; '.join(changes)}): nothing was done." in posts(site)[-1]


def test_the_rows_ends_in_what_changed():
    """#376: a row the plan didn't name reads `not in the plan`; a material kept before #376,
    with no `ends`, names no row, and still says the rest."""
    base = {"release": "v1", "steps": "s", "inputs": {}, "vms": {}}
    then = {**base, "ends": {"alice": [True, True, True]}}
    now = {**base, "ends": {"alice": [True, True, True], "dave": [False, False, False]}}
    assert Runner._diff(then, now) == ["dave: not in the plan → not uninstalled, not synced"]
    assert Runner._diff(now, then) == ["dave: not uninstalled, not synced → not in the plan"]
    assert Runner._diff(base, {**now, "release": "v2"}) == ["the server's release, v1 → v2"]
