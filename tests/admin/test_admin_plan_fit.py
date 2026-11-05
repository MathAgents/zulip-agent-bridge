"""Plans that fit (#361): the plan messages of a one-VM `!stop`, a three-VM `!rollout` and a
120-VM `!stop all`, from the scripts' own output, through the bot's own planner.

The dry runs are the scripts' `main` on the plan's own argument lists: `power.py` with a fake
EC2, `push_config.py --dry-run` (which never connects). The status read is `status.py`'s own
`fleet_json`, as `--json` prints it, over rows such as SSH would gather. The fake runner frames
them as `Runner._step` does and keeps the status read apart, as `Runner.prepare` does.

The first two fit inline, with no attachment; the third is cut, keeps its essentials, and
attaches the whole plan. What the hash covers is what the approver sees (§2.4): the hashed dry
run, or each target's VM, is in the message or its attachment; the status read is not hashed.
"""

import asyncio
import contextlib
import importlib
import io
import json
import sys
import zipfile
from pathlib import Path
from typing import ClassVar

import pytest

from zab.admin import planning
from zab.admin.actions import DRY_RUN, TARGETS, Live
from zab.admin.planning import Planning, Prepared
from zab.admin.plans import PENDING
from zab.admin.runner import Ran, script
from zab.admin.scrub import Scrubber

from .botkit import HARVARD, HARVARD_CH, OLGA, TESS, TEST, TEST_CH, incoming, make_site

ROOT = Path(__file__).resolve().parents[2]
SRC_CONFIG = ROOT / "src" / "zab" / "config.py"
NOW = 1_800_000_000.0


def scripts():
    """`power`, `push_config` and `status`, imported from `workshop/` by path, as their own
    tests do (`tests/workshop/conftest.py`)."""
    for sub in ("aws", "admin"):
        path = str(ROOT / "workshop" / sub)
        if path not in sys.path:
            sys.path.insert(0, path)
    names = ("power", "push_config", "status", "grow_disk")
    return tuple(importlib.import_module(name) for name in names)


POWER, PUSH, STATUS, GROW = scripts()


def run(main, argv):
    """`main` on the arguments after the script, its stdout and stderr merged, as the runner
    reads them: (output, exit code)."""
    args = list(argv[next(i for i, w in enumerate(argv) if w.endswith(".py")) + 1 :])
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = main(args)
    return out.getvalue(), code


class Ec2:
    """power.py's EC2, faked: the workshop's instances, each tagged with its participant."""

    instances: ClassVar[list[dict]] = []

    def __init__(self, region, **config):  # power.py's EC2_CONFIG
        self.region = region

    def find_instances(self, tags, states):
        def tagged(inst):
            have = {t["Key"]: t["Value"] for t in inst["Tags"]}
            return all(have.get(k) == v for k, v in tags.items())

        return [inst for inst in self.instances if tagged(inst)]

    def stop(self, ids):
        raise AssertionError("a dry run stops nothing")

    start = stop


def vm_rows(slugs, turns):
    """What `status.py` gathers over SSH from healthy VMs; `turns` running on the first."""
    rows = []
    for n, slug in enumerate(slugs):
        vm = STATUS.VmStatus(slug=slug, host=f"198.51.100.{n % 250 + 1}")
        vm.codex_login = vm.claude_login = vm.git_push = vm.math_tools = "ok"
        vm.bridge, vm.zab_version = "active, sign-in pending", "0.2.3"
        vm.active_sessions, vm.last_activity = 1, NOW - 600 - 97 * n
        vm.running_turns = [
            {"bot": "claude", "backend": "claude", "started_at": "2027-01-15T08:00:00Z"}
        ] * (turns if n == 0 else 0)
        vm.idle_stop = {"enabled": True, "active": True, "minutes": 15, "last_verdict": None}
        vm.metrics = {"load1": 0.12, "cpus": 8, "mem_total_kib": 32_594_412,
                      "mem_available_kib": 29_870_204, "disk_percent": 23}  # fmt: skip
        rows.append(vm)
    return rows


class ScriptRunner:
    """`Runner.prepare`'s shape with the scripts' own output; `apply` is never reached."""

    def __init__(self, status_turns=0):
        self.turns, self.hashed, self.status = status_turns, {}, {}

    async def prepare(self, plan, action, fleet):
        shown = hashed = ""
        for step in action.steps:
            if step.dry_run is None:
                continue
            name = script(step.dry_run)
            mains = {"aws/power.py": POWER.main, "admin/push_config.py": PUSH.main}
            main = (mains | {"aws/grow_disk.py": GROW.main})[name]
            text, code = run(main, step.dry_run)
            shown += f"── {name}\n{text}── {Ran(name, code)}\n"
            hashed += text
        only = [w for i, w in enumerate(action.status or ()) if action.status[i - 1] == "--only"]
        answer = json.dumps(STATUS.fleet_json(vm_rows(only, self.turns)), indent=2) + "\n"
        tmp = f"/var/lib/zab-admin/tmp/{plan.name}"
        ids = {i["Tags"][0]["Value"]: i["InstanceId"] for i in Ec2.instances}
        live = Live(
            vms={t: ids.get(t) for t in action.targets} if action.hashed == TARGETS else {},
            dry_run=hashed if action.hashed == DRY_RUN else "",
            tmpdir=tmp,
        )
        self.hashed[plan.name] = (action.hashed, hashed, live.vms)
        self.status[plan.name] = answer
        return Prepared(True, shown, live, status=answer if action.status else "")

    async def read(self, action, fleet):
        raise AssertionError("no !status here")

    def busy(self, workshop):
        return None

    async def apply(self, job):
        raise AssertionError("nothing is approved here")


def fleet_files(site, ws, slugs, region):
    """A roster with a VM per slug, each with its bot keys in `out/`."""
    base = site.tmp / "ws" / ws
    lines = ["slug,first_name,last_name,email,github,ip,region"]
    lines += [f"{s},P,{s.upper()},{s}@example.com,{s}-gh,198.51.100.{n % 250 + 1},{region}"
              for n, s in enumerate(slugs)]  # fmt: skip
    (base / "roster.csv").write_text("\n".join(lines) + "\n")
    (base / "out" / "known_hosts").write_text("")
    for slug in slugs:
        (base / "out" / slug).mkdir()
        for kind in ("codex", "claude"):
            rc = f"[api]\nemail={kind}-{slug}-bot@example.com\nkey={'K' * 32}\nsite=https://x\n"
            (base / "out" / slug / f"{kind}.zuliprc").write_text(rc)
    Ec2.instances = [
        {"InstanceId": f"i-0123456789abc{n + 1:04d}", "State": {"Name": "running"},
         "PublicIpAddress": f"198.51.100.{n % 250 + 1}",
         "Tags": [{"Key": "participant", "Value": s}, {"Key": "workshop", "Value": ws},
                  {"Key": "zab-scope", "Value": "mathagents"}]}  # the fleet's (#422)
        for n, s in enumerate(slugs)
    ]  # fmt: skip


@pytest.fixture
def site(tmp_path, monkeypatch):
    site = make_site(tmp_path)
    wheels = Path(site.paths.layout.wheels)
    wheels.mkdir()
    with zipfile.ZipFile(wheels / "zulip_agent_bridge-0.2.4-py3-none-any.whl", "w") as whl:
        whl.write(SRC_CONFIG, "zab/config.py")  # all a dry run reads of the release
    monkeypatch.setattr(POWER, "Ec2", Ec2)
    return site


async def plan_of(site, runner, text, *, sender, bot, stream):
    server = site.server(planner=Planning(runner))
    await server.start()
    try:
        await next(b for b in server.bots if b.user_id == bot).on_message(
            incoming(sender, text, stream=stream)
        )
        while server.planner.tasks:
            await asyncio.gather(*list(server.planner.tasks), return_exceptions=True)
        plan = server.store.get(max(r[0] for r in server.store.db.execute("SELECT id FROM plans")))
    finally:
        await server.close()
    assert plan.state == PENDING, plan.note
    message = site.realm.posts(bot)[-2]["content"]
    attached = [v.decode() for k, v in site.realm.uploads.items() if k.endswith("-plan.txt")]
    return plan, message, attached


def fenced_after(text, title):
    """The lines of the fenced block right after the line that starts with `title`."""
    lines = text.split(title, 1)[1].split("\n")[1:]
    assert lines[0].startswith("```")
    return lines[1 : lines.index(lines[0][: len(lines[0]) - len("text")])]


def essentials(plan, text):
    """The hash, approve and reject commands, the chain head, every fence closed."""
    assert f"**Plan** `{plan.hash[:12]}`: approve " in text
    assert f"`!approve {plan.name} {plan.hash[:6]}" in text and f"`!reject {plan.name}`" in text
    assert text.splitlines()[-1].startswith("`audit #") and " · chain " in text.splitlines()[-1]
    assert text.count("```") % 2 == 0
    assert len(text) <= 10000 - planning.MARGIN


#: A GitHub token, as a VM could print it into status.py's JSON: split by U+200B, which
#: `json.dumps` writes as `\u200b`, out of sight of a scan of the text (the review of #380).
TOKEN = "ghp_" + "Z" * 36
SPLIT = TOKEN[:10] + "\u200b" + TOKEN[10:]


JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJl"


@pytest.mark.parametrize("hidden", ["split", "after a tab"])
@pytest.mark.parametrize("field", ["last_verdict", "doctor_failures", "last_error"])
def test_a_token_a_vm_hid_is_redacted_in_the_status_read(field, hidden):
    """The three fields where status.py passes a VM's own text through, a token hidden two
    ways (the reviews of #380): split by U+200B, which `json.dumps` escapes; or whole right
    after a tab, whose kept escape `\\t` ends in a letter glued to it. The status read the
    attachment carries is scrubbed, and stays JSON."""
    value, tail, escape = (
        (SPLIT, "Z" * 30, "\\u200b") if hidden == "split" else ("\t" + JWT, JWT[-8:], "\\t")
    )
    (vm,) = vm_rows(["alice"], 0)
    if field == "last_verdict":
        vm.idle_stop = {**(vm.idle_stop or {}), "last_verdict": f"stopped? {value}"}
    elif field == "doctor_failures":
        vm.doctor_failures = [f"codex:connection: {value}"]
    else:
        vm.last_error = f"status: {value}"
    answer = json.dumps(STATUS.fleet_json([vm]), indent=2) + "\n"
    scrubber = Scrubber()
    assert escape in answer and tail in scrubber(answer)  # a scan of the text misses it
    read = planning.StatusRead.of(answer, scrubber)
    assert tail[-6:] not in read.text and "[redacted" in read.text
    assert tail[-6:] not in "\n".join(read.lines)
    assert json.loads(read.text)["vms"][0]["slug"] == "alice"  # still status.py's answer


async def test_a_one_vm_stop_fits_inline(site):
    fleet_files(site, "zab-test", ["zab-test-a", "zab-test-b"], "us-east-1")
    runner = ScriptRunner(status_turns=1)
    plan, text, attached = await plan_of(
        site, runner, "!stop zab-test-b", sender=TESS, bot=TEST, stream=TEST_CH
    )
    essentials(plan, text)
    assert attached == [] and "Cut to fit" not in text and "The whole plan" not in text
    dry = fenced_after(text, "**Dry run**")
    assert dry[:3] == [
        "── aws/power.py",
        "scope: workshop=zab-test, zab-scope=mathagents; region us-east-1",
        "zab-test-b i-0123456789abc0002 (running): would stop",
    ]
    assert fenced_after(text, "**Status**") == [
        "VM          state                    zab    idle stop  running turns",
        "zab-test-b  active, sign-in pending  0.2.3  15 min     1 claude",
    ]
    # the hash covers the target's VM, which the dry run names; never the status read
    kind, _, vms = runner.hashed[plan.name]
    assert kind == TARGETS and vms == {"zab-test-b": "i-0123456789abc0002"}
    assert all(f"{t} {i} (running)" in text for t, i in vms.items())
    assert '"running_turns"' not in text and "schema" not in text  # the JSON stays out


async def test_a_three_vm_rollout_fits_inline(site):
    slugs = ["zab-test-a", "zab-test-b", "zab-test-c"]
    fleet_files(site, "zab-test", slugs, "us-east-1")
    runner = ScriptRunner()
    plan, text, attached = await plan_of(
        site, runner, "!rollout v0.2.4 all", sender=TESS, bot=TEST, stream=TEST_CH
    )
    essentials(plan, text)
    assert f"`!approve {plan.name} {plan.hash[:6]} rollout 3`" in text  # above half: the phrase
    assert attached == [] and "Cut to fit" not in text and "The whole plan" not in text
    dry = fenced_after(text, "**Dry run**")
    vms = [line for line in dry if line.startswith("dry-run ")]
    assert [line.split(":")[0] for line in vms] == [
        f"dry-run {s} (198.51.100.{n})" for n, s in enumerate(slugs, 1)
    ]
    assert all(f"scp config.toml {s}/codex.zuliprc {s}/claude.zuliprc " in line
               for s, line in zip(slugs, vms, strict=True))  # fmt: skip
    assert all("zulip_agent_bridge-0.2.4-py3-none-any.whl; " in line for line in vms)
    # 453 characters a VM since the tools list's two files and step (#475), 488 since the sage
    # wrapper's (#482), 545 since the shared Mathlib's default, which the dry run shows as on a
    # new VM (#704), 562 since every row's idle-stop probe (review of #705); no row has the
    # waker here, so none has the idle-stop default's install
    assert all("scp lean-project; lean-project + shared Mathlib; lean check; " in x for x in vms)
    assert all(line.endswith("; idle-stop probe; staging removed; hold released") for line in vms)
    assert max(len(line) for line in dry) < 580 and len(dry) < 20
    assert fenced_after(text, "**Status**")[1:] == [
        f"{s}  active, sign-in pending  0.2.3  15 min     0" for s in slugs
    ]
    # the hash covers the dry run's output, all of it shown in the message
    kind, hashed, _ = runner.hashed[plan.name]
    assert kind == DRY_RUN and hashed.count("\n") > 5
    assert all(line in dry for line in hashed.removesuffix("\n").split("\n"))
    assert "schema" not in text


async def test_a_120_vm_stop_all_is_cut_and_attached_whole(site):
    slugs = [f"ex-{n:03d}" for n in range(1, 121)]
    fleet_files(site, "harvard-2026", slugs, "us-west-2")
    runner = ScriptRunner(status_turns=2)
    plan, text, attached = await plan_of(
        site, runner, "!stop all", sender=OLGA, bot=HARVARD, stream=HARVARD_CH
    )
    essentials(plan, text)
    assert f"`!approve {plan.name} {plan.hash[:6]} stop 120`" in text
    assert "*Cut to fit: " in text and "lines of the dry run" in text
    assert "71 lines of the status left out" in text  # 121 lines, 50 shown (#764)
    assert f"The whole plan: [{plan.name}-plan.txt](/user_uploads/" in text
    # each block is cut by its own cap, not by the room: 40 lines and 50 (#764)
    assert len(fenced_after(text, "**Dry run**")) == planning.DRY_LINES == 40
    shown = fenced_after(text, "**Status**")
    assert shown[:2] == [
        "VM      state                    zab    idle stop  running turns",
        "ex-001  active, sign-in pending  0.2.3  15 min     2 claude",
    ]
    assert len(shown) == planning.STATUS_LINES == 50
    (whole,) = attached
    # everything: every target's VM (what the hash covers), every status line, the JSON
    kind, _, vms = runner.hashed[plan.name]
    assert kind == TARGETS and len(vms) == 120
    assert all(f"{t} {i} (running): would stop" in whole for t, i in vms.items())
    assert all(f"\n{s}  active, sign-in pending  0.2.3  15 min" in whole for s in slugs)
    assert whole.count('"slug": "ex-') == 120 and "Targets (120): ex-001, " in whole
    assert whole.rstrip().endswith(f"or reject with `!reject {plan.name}`.")


class GrowEc2(Ec2):
    """grow_disk.py's EC2, faked over the same instances: each one's root volume, 200 GB of
    gp3 in the scope, never modified."""

    def volumes(self, ids):
        found = [i for i in self.instances if f"vol-{i['InstanceId'][2:]}" in ids]
        return [{"VolumeId": f"vol-{i['InstanceId'][2:]}", "Size": 200, "VolumeType": "gp3",
                 "Attachments": [{"InstanceId": i["InstanceId"]}], "Tags": i["Tags"]}
                for i in found]  # fmt: skip

    def volume_modifications(self, vid):
        return []

    def modify_volume(self, vid, size):
        raise AssertionError("a dry run modifies nothing")


DF_LINE = "/dev/root ext4 203056560 195000000 8039944 97% /"


def df_over_ssh(argv, **kwargs):
    assert argv[0] == "ssh" and argv[-1] == GROW.DF, argv
    return importlib.import_module("admin_common").RunResult(0, DF_LINE + "\n", "")


async def test_a_grow_disk_plan_shows_the_volume_its_cost_and_the_disk(site, monkeypatch):
    """#553: one row's plan, a change for one approver with no phrase, hashed on the row's VM;
    its dry run is grow_disk.py's own, on the plan's argument list."""
    fleet_files(site, "zab-test", ["zab-test-a", "zab-test-b"], "us-east-1")
    for inst in Ec2.instances:
        root = {"DeviceName": "/dev/sda1", "Ebs": {"VolumeId": f"vol-{inst['InstanceId'][2:]}"}}
        inst |= {"RootDeviceName": "/dev/sda1", "BlockDeviceMappings": [root]}
    monkeypatch.setattr(GROW, "Ec2", GrowEc2)
    monkeypatch.setattr(GROW, "run_keeping", df_over_ssh)
    runner = ScriptRunner()
    plan, text, attached = await plan_of(
        site, runner, "!grow-disk zab-test-b 300GB", sender=TESS, bot=TEST, stream=TEST_CH
    )
    essentials(plan, text)
    assert attached == [] and "**Status**" not in text  # no status read: the dry run says it
    assert f"`!approve {plan.name} {plan.hash[:6]}`" in text  # one approver, no phrase
    assert fenced_after(text, "**Dry run**") == [
        "── aws/grow_disk.py",
        "zab-test-b i-0123456789abc0002 (running): volume vol-0123456789abc0002 gp3 200 GB → "
        "300 GB, +100 GB: about $8.00 a month more, at $0.08 per GB-month",
        "zab-test-b: root file system now: 194G, 186G used (97%), 8G free (ext4, /dev/root) "
        "[df -PkT /: /dev/root ext4 203056560 195000000 8039944 97% /]",
        "zab-test-b: on apply: ModifyVolume, then wait for EC2's optimizing state; then growpart "
        "and resize2fs over SSH, online",
        "(dry run: nothing changed)",
        "── aws/grow_disk.py exited 0",
    ]
    kind, _, vms = runner.hashed[plan.name]
    assert kind == TARGETS and vms == {"zab-test-b": "i-0123456789abc0002"}
