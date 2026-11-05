"""A20's readers and its roster fix-up (#456), offline, against what the product itself writes:
the `!adopt-spare` chain as `actions.build` makes it and the plan shows it, the refusal of a
stopped spare as `adopt_spare.py` prints it (#455), the waker's config
as `push_waker.py` renders it, a VM's idle stop and release as `!status`'s table shows what
`status.py` read on it (#439), and a roster as
`aws_common.write_roster` writes it."""

from __future__ import annotations

import contextlib
import dataclasses
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from admin.test_admin_actions import FLEET, make
from e2e.admin import test_scenarios_3_late_joiner as a20
from e2e.admin.harness.chat import status_of
from zab.admin import planning, runner
from zab.admin.actions import Layout
from zab.admin.scrub import Scrubber

ROOT = Path(__file__).resolve().parents[3]


def script(name: str, path: str):
    """A workshop script as a module of its own name here, with its directory's imports."""
    folder = str(ROOT / Path(path).parent)
    if folder not in sys.path:
        sys.path.insert(0, folder)
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # its dataclasses look their module up
    spec.loader.exec_module(module)
    return module


def test_the_chain_is_the_products_with_the_waker_on(tmp_path):
    """`CHAIN` names the steps of `!adopt-spare` with the fleet's waker on, as the plan shows
    them (`planning._step`) and as a result names the one that failed (`runner.script`)."""
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    (wheels / "zulip_agent_bridge-0.2.2-py3-none-any.whl").write_bytes(b"wheel")
    layout = Layout(wheels=str(wheels), retired=str(tmp_path / "retired-slugs.txt"))
    fleet = dataclasses.replace(FLEET, waker=True, settings=(f"idle-stop={a20.MINUTES}",))
    action = make("!adopt-spare carol zab-spare-1", layout, fleet)
    shown = [planning._step(step.argv, step.env) for step in action.steps]
    assert [a20.script_of(line) for line in shown] == list(a20.CHAIN)
    assert [runner.script(step.argv) for step in action.steps] == list(a20.CHAIN)
    argv = [line.split() for line in shown]
    assert a20.values(argv[3], "--only") == ["carol"] and "--apply" in argv[3]
    for push in (argv[4], argv[6]):
        assert a20.values(push, "--idle-stop") == [str(a20.MINUTES)]
    assert a20.values(argv[4], "--adopt-spare") == ["carol"]
    off = make("!adopt-spare carol zab-spare-1", layout, dataclasses.replace(fleet, waker=False))
    assert "admin/push_waker.py" not in [runner.script(step.argv) for step in off.steps]


def test_the_plans_line_for_a_stopped_spare_is_adopt_spares(tmp_path, monkeypatch):
    """#715: `STARTED` is the first line `adopt_spare.py --dry-run` prints for a stopped spare,
    against a fake EC2, and the dry run passes: it never tries the spare's SSH, nor starts it."""
    ad = script("ae2e_adopt_spare", "workshop/aws/adopt_spare.py")
    scope = {"workshop": "zab-admin-e2e", "zab-scope": "mathagents"}
    own = {**scope, "participant": a20.SPARE}
    tags = [{"Key": key, "Value": value} for key, value in own.items()]

    class Ec2:
        def __init__(self, region, **config):
            pass

        def find_instances(self, wanted, states, **bound):
            spare = {"InstanceId": "i-s", "State": {"Name": "stopped"}, "Tags": tags}
            return [spare] if wanted.items() <= own.items() else []

        def find_addresses(self, wanted):
            eip = {"AllocationId": "eipalloc-s", "PublicIp": "198.51.100.9", "InstanceId": "i-s"}
            return [{**eip, "Tags": tags}] if wanted.items() <= own.items() else []

    monkeypatch.setattr(ad, "Ec2", Ec2)
    monkeypatch.setattr(ad, "wait_for_ssh", lambda ip, **kw: pytest.fail("SSH tried"))
    roster, spares = tmp_path / "roster.csv", tmp_path / "spares.json"
    roster.write_text(f"slug,email\n{a20.D},d@example.com\n")
    key = f"{ad.scope_label(scope)}/us-east-1/{a20.SPARE}"
    spares.write_text(json.dumps({key: {"instance_id": "i-s", "ip": "198.51.100.9"}}))
    argv = ["--roster", str(roster), "--slug", a20.D, "--spare", a20.SPARE]
    argv += ["--region", "us-east-1", "--spares-file", str(spares), "--dry-run"]
    for key_value in scope.items():
        argv += ["--tag", "=".join(key_value)]
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        assert ad.main(argv) == 0
    assert out.getvalue().splitlines()[0] == a20.STARTED
    assert out.getvalue().splitlines()[-1] == "(dry run: nothing changed)"


def test_waker_bots_reads_the_config_push_waker_writes():
    waker = script("ae2e_push_waker", "workshop/admin/push_waker.py")
    bot = dict(name="codex-zab-test-d", instance_id="i-0123456789abcdef0",
               zuliprc="/var/lib/zab-admin/waker/zab-admin-e2e/zab-test-d/codex.zuliprc",
               allow_user_ids=[11], allow_emails=["t@example.com"])  # fmt: skip
    other = {**bot, "name": "codex-zab-test-c", "instance_id": "i-0fedcba9876543210"}
    text = waker.render_shared(
        [
            dict(name="zab-admin-e2e", organizers_channel=34, bots=[bot]),
            dict(name="zab-admin-e2e2", organizers_channel=35, bots=[other]),
        ]
    )
    assert a20.waker_bots(text) == {"codex-zab-test-d": bot}
    assert a20.waker_bots(text, "zab-admin-e2e2") == {"codex-zab-test-c": other}
    assert a20.waker_bots(waker.render_shared([])) == {} and a20.waker_bots("") == {}


def test_idle_reads_the_status_table_the_bot_renders():
    """`!status`'s answer as the bot renders it (`planning.status_reply`, #439) from status.py's
    JSON, its idle stop read on the VM (`idle_stop_state`): A20's cells, through the harness's
    own parser (`chat.status_of`)."""
    status = script("ae2e_status", "workshop/admin/status.py")
    on = "enabled=enabled\nactive=active\nminutes=--minutes 240\nlast=stay up: uptime\n"
    off = "enabled=\nactive=inactive\nminutes=\nlast=powering off\n"
    vms = [
        {"slug": a20.D, "bridge": "active, sign-in pending", "zab_version": "0.2.5rc30",
         "idle_stop": status.idle_stop_state(on), "running_turns": []},
        {"slug": a20.A, "bridge": "active, owner+sign-in pending", "zab_version": "0.2.5rc30",
         "idle_stop": status.idle_stop_state(off), "running_turns": []},
    ]  # fmt: skip
    doc = json.dumps({"schema": 1, "vms": vms, "summary": {}}, indent=2)
    rows = status_of(planning.status_reply(doc, Scrubber(), limit=10000).text)
    assert a20.idle(rows[a20.D]) == a20.minutes_on(a20.MINUTES) == "240 min"
    assert rows[a20.D]["zab"] == "0.2.5rc30" and rows[a20.D]["state"] == "active, sign-in pending"
    assert a20.idle(rows[a20.A]) == "off"
    assert a20.idle({}) is None


def test_adopted_by_reads_spares_json():
    key = "workshop=zab-admin-e2e,zab-role=test/us-east-1/zab-spare-1"
    spares = {key: {"name": "zab-spare-1", "ip": "198.51.100.9", "adopted_by": "zab-test-d"}}
    spares["x/us-east-1/zab-spare-2"] = {"name": "zab-spare-2", "ip": "198.51.100.10"}
    assert a20.adopted_by(spares, "zab-spare-1") == "zab-test-d"
    assert a20.adopted_by(spares, "zab-spare-2") is None
    assert a20.adopted_by(spares, "zab-spare-3") is None


def test_volume_owners_are_the_attached_volumes_tags():
    def volume(instance: str, owner: str | None) -> dict:
        tags = [{"Key": "workshop", "Value": "zab-admin-e2e"}]
        tags += [{"Key": "participant", "Value": owner}] if owner else []
        return {"Attachments": [{"InstanceId": instance}], "Tags": tags}

    found = [volume("i-1", "zab-test-d"), volume("i-2", "zab-spare-1"), volume("i-1", None)]
    aws = SimpleNamespace(volumes=lambda: [*found, {"Tags": []}])
    assert a20.volume_owners(aws, "i-1") == ["zab-test-d", None]
    assert a20.volume_owners(aws, "i-3") == []


def drop(tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    """`DROP_ROWS` as the server runs it, on `tmp_path`'s workshops instead of the server's."""
    literal = a20.DROP_ROWS.replace('"/var/lib/zab-admin/ws"', repr(str(tmp_path)))
    assert literal != a20.DROP_ROWS
    return subprocess.run(  # noqa: S603 - an argument list, the literal of this repository
        [sys.executable, "-I", "-c", literal, *args], capture_output=True, text=True, check=False
    )


def test_drop_rows_leaves_the_roster_as_write_roster_writes_the_rest(tmp_path):
    aws = script("ae2e_aws_common", "workshop/aws/aws_common.py")
    ws = tmp_path / "zab-admin-e2e"
    ws.mkdir()
    roster, expected = str(ws / "roster.csv"), str(tmp_path / "expected.csv")

    def row(slug: str, **more: str) -> dict[str, str]:
        return {"slug": slug, "email": f"{slug}@example.com", "notes": "admin e2e, (#356)", **more}

    a, b = row("zab-test-a", ip="198.51.100.1"), row("zab-test-b", host_keys="SHA256:x SHA256:y")
    late = [row("zab-test-e", ip="198.51.100.5"), row("zab-test-d", zulip_user_id="11")]
    for path, rows in ((roster, [a, *late[:1], b, *late[1:]]), (expected, [a, b])):
        Path(path).write_text("slug\n", encoding="utf-8")  # write_roster keeps a file's mode
        os.chmod(path, 0o600)
        aws.write_roster(path, rows)
    before = Path(roster).read_bytes()
    for bad in (("zab-admin-e2e2", "zab-test-d"), ("zab-admin-e2e", "zab-test-a"),
                ("zab-admin-e2e",)):  # fmt: skip
        assert drop(tmp_path, *bad).returncode == 2, bad
        assert Path(roster).read_bytes() == before, f"{bad} changed the roster"
    done = drop(tmp_path, "zab-admin-e2e", "zab-test-d", "zab-test-e")
    assert (done.returncode, done.stdout.strip()) == (0, "2"), done.stderr
    assert Path(roster).read_bytes() == Path(expected).read_bytes()
    assert stat.S_IMODE(os.stat(roster).st_mode) == 0o600
    assert [r["slug"] for r in aws.read_roster(roster)] == ["zab-test-a", "zab-test-b"]
    assert sorted(p.name for p in ws.iterdir()) == ["roster.csv", "roster.csv.bak"]
    again = drop(tmp_path, "zab-admin-e2e", "zab-test-d")
    assert (again.returncode, again.stdout.strip()) == (0, "0"), again.stderr


def test_drop_rows_never_writes_through_a_link_left_at_its_temporary_file(tmp_path):
    """A leftover `roster.csv.a20`, a symlink or a hard link, is removed, then the file is made
    afresh (`O_EXCL`): neither link's target is written (the review of #457)."""
    ws = tmp_path / "zab-admin-e2e"
    ws.mkdir()
    roster = ws / "roster.csv"
    roster.write_text("slug,email\r\nzab-test-a,a@example.com\r\nzab-test-d,d@example.com\r\n")
    os.chmod(roster, 0o600)
    target = tmp_path / "elsewhere.txt"
    for link in (os.symlink, os.link):
        target.write_text("untouched\n")
        stale = ws / "roster.csv.a20"
        link(target, stale)
        done = drop(tmp_path, "zab-admin-e2e", "zab-test-d")
        assert done.returncode == 0, done.stderr
        assert target.read_text() == "untouched\n", link.__name__
        assert not stale.exists() and not stale.is_symlink()
        assert roster.read_bytes() == b"slug,email\r\nzab-test-a,a@example.com\r\n"
        roster.write_bytes(roster.read_bytes() + b"zab-test-d,d@example.com\r\n")


def test_the_spares_wait_survives_a_failed_read_and_says_why_it_ended():
    """A20's 90-minute wait (#715): a read of EC2 that fails is a state that names it, never
    the wait's end; at its end, the spare's own verdicts, or why they couldn't be read."""

    class Aws:
        def __init__(self, fail):
            self.fail = fail

        def describe(self, iid):
            if self.fail:
                raise RuntimeError("RequestLimitExceeded")
            return {"InstanceId": iid, "State": {"Name": "stopped"}}

    assert a20.described(Aws(True), "i-1")["State"]["Name"] == "unread (RuntimeError)"
    assert a20.described(Aws(False), "i-1")["State"]["Name"] == "stopped"
    compile(a20.SPARE_LOG_TAIL, "SPARE_LOG_TAIL", "exec")  # the server's script parses
    assert a20.spare_log_tail(None, None) == "(no address in spares.json)"

    class Server:
        def __init__(self, done=None, broken=False):
            self.done, self.broken, self.ran = done, broken, []

        def read(self, path):
            if self.broken:
                raise OSError("no such file")
            entry = {"name": a20.SPARE, "ip": "198.51.100.9", "host_keys": "SHA256:k"}
            return json.dumps({"x/us-east-1/zab-spare-1": entry})

        def as_admin(self, *argv, timeout):
            self.ran.append(argv)
            return self.done

    said = SimpleNamespace(stdout="2026-11-02T01:00:00Z stay up: load 3.20\n", stderr="",
                           returncode=0)  # fmt: skip
    server = Server(said)
    assert a20.spare_log_tail(server, "198.51.100.9") == said.stdout.strip()
    assert server.ran[0][-3:] == ("198.51.100.9", "SHA256:k", a20.SPARE_LOG)
    failed = SimpleNamespace(stdout="", stderr="no key of 198.51.100.9", returncode=1)
    assert a20.spare_log_tail(Server(failed), "198.51.100.9") == (
        "(not read: exit 1: no key of 198.51.100.9)"
    )
    assert a20.spare_log_tail(Server(broken=True), "198.51.100.9").startswith(
        "(not read: OSError: no such file"
    )
