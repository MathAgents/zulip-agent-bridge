"""The admin rig's names, inputs and test admin.toml, offline (issue #356)."""

from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path

import pytest

from e2e.admin.fakes import test_toml
from e2e.admin.harness import admin_toml
from e2e.admin.harness import rig as rigs
from e2e.harness import report
from e2e.harness.config import CHECKOUT
from e2e.harness.recorder import Recorder
from zab.admin.config import parse_admin_config


def rendered() -> str:
    return test_toml()


def test_select_labels_ranges_and_the_go_criteria():
    assert rigs.select("go") == [1, 2, 3, 4, 5, 6, 7, 10, 12, 13, 14, 15]
    assert rigs.select("A10,a12-A13") == [10, 12, 13]
    assert rigs.select("A10b") == [110]
    assert 110 in rigs.select("all") and 17 in rigs.select("all") and 18 in rigs.select("all")
    assert rigs.select("A18") == [18] and 18 not in rigs.select("go")  # §8.4's, not §8.2's
    assert rigs.select("A20") == [20] and 20 in rigs.select("all") and 20 not in rigs.select("go")
    assert rigs.select("A18-A20") == [18, 19, 20]
    assert rigs.select("A21") == [21] and 21 in rigs.select("all") and 21 not in rigs.select("go")
    assert rigs.select("A22") == [22] and 22 in rigs.select("all") and 22 not in rigs.select("go")
    assert rigs.select("A23") == [23] and 23 in rigs.select("all") and 23 not in rigs.select("go")
    assert rigs.select("A18-A23") == [18, 19, 20, 21, 22, 23]
    for bad in ("", "A24", "10", "A1-A99"):
        with pytest.raises(ValueError):
            rigs.select(bad)


def test_load_defaults_and_refusals(tmp_path):
    rig = rigs.load({"HOME": str(tmp_path)})
    assert rig.dir == tmp_path / ".config" / "zab" / "admin-e2e"
    assert (rig.region, rig.key_pair, rig.ssh_group) == ("us-east-1", "zab-test", "zab-test-ssh")
    assert rig.tester_zuliprc == tmp_path / ".config" / "zab" / "tester.zuliprc"
    assert len(rig.missing()) == 4  # nothing is there
    for var, value in (
        ("ZAB_ADMIN_E2E_REGION", "us east"),
        ("ZAB_ADMIN_E2E_AMI", "ami-xyz"),
        ("ZAB_ADMIN_E2E_KEY_PAIR", "--key"),
    ):
        with pytest.raises(ValueError, match=var):
            rigs.load({"HOME": str(tmp_path), var: value})


def test_names_are_test_infrastructure():
    assert rigs.SERVER.startswith("zab-test-")  # IAM: zab-test-* only (test infrastructure)
    assert "workshop" not in rigs.SERVER_TAGS and rigs.SERVER_TAGS["zab-role"] == "test"
    for ws in rigs.WORKSHOPS:
        assert ws.channel.startswith("zab-dev") and ws.tags["zab-role"] == "test"
        assert ws.alerts.startswith("zab-dev") and ws.alerts != ws.channel
        assert ws.participants.startswith("zab-dev") or not ws.participants
    assert rigs.ADMINS_CHANNEL.startswith("zab-dev")


def test_the_test_admin_toml_loads_with_the_strict_loader():
    config = parse_admin_config(rendered())
    assert config.release == "rc" and config.destructive_approvals == 1
    assert config.plan_ttl_minutes == 2  # A3's expiry
    assert config.admins == config.deployers == {11}
    e2e, e2e2 = config.workshops
    assert e2e.organizers == frozenset() and e2e2.organizers == {12}  # tester2: e2e2 only
    assert dict(e2e.tags) == rigs.E2E.tags  # the rig creates and finds by the loader's scope
    assert dict(e2e.tags) == {"workshop": "zab-admin-e2e", "zab-scope": "admin-e2e",
                              "zab-role": "test"}  # fmt: skip
    assert config.regions == ("us-east-1",) and config.instance_types == ("t3.medium",)
    assert e2e.bot.channels == {31} and e2e2.bot.channels == {32}
    assert config.global_bot is not None and config.global_bot.channels == {33}
    assert (e2e.alerts_channel, e2e2.alerts_channel) == (34, 35)  # `!waker on` needs it
    assert (e2e.channel, e2e2.channel) == (None, 36)  # A22's `!roster sync` (#627)
    assert (
        e2e.aws.instance_types == ("t3.medium",) and e2e.aws.key_name == "zab-admin-zab-admin-e2e"
    )
    assert config.console_url == "https://admin-e2e.localhost"  # A11, through Caddy too


def test_the_role_policy_has_zab_role_test_in_every_scoped_condition():
    """Every scoped Allow names `zab-role=test` and the rig's scope (#422), never a workshop's
    value; what it creates or acts on must carry some `workshop` tag (`Null`), but for the
    image, group and key pair it launches from."""
    path = CHECKOUT / "workshop" / "adminserver" / "render_policy.py"
    spec = importlib.util.spec_from_file_location("ae2e_render_policy", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    policy = module.render(tomllib.loads(rendered()))
    scoped = [
        st
        for st in policy["Statement"]
        if st["Effect"] == "Allow" and st["Sid"] not in ("Read", "LaunchNetwork")
    ]
    assert scoped
    for st in scoped:
        conditions = st["Condition"]["StringEquals"]
        tags = {k: v for k, v in conditions.items() if k.endswith("/zab-role")}
        assert tags and set(tags.values()) == {"test"}, st["Sid"]
        scope = {k: v for k, v in conditions.items() if k.endswith("/zab-scope")}
        assert scope and set(scope.values()) == {rigs.SCOPE[1]}, st["Sid"]
        assert not [k for k in conditions if k.endswith("/workshop")], st["Sid"]
        named = st["Condition"].get("Null", {})
        if not st["Sid"].endswith("LaunchFrom"):
            assert list(named.values()) == ["false"] and str(named).count("/workshop") == 1
    assert "zab-admin" in str([st for st in policy["Statement"] if st["Effect"] == "Deny"])


def test_allowed_signers_line():
    line = admin_toml.allowed_signers("ssh-ed25519 AAAAC3Nza test\n")
    assert line == 'zab-admin-e2e@example.org namespaces="git" ssh-ed25519 AAAAC3Nza test\n'


def test_report_rows_carry_the_admin_labels(tmp_path):
    rec = Recorder(
        titles={10: "Deploy gate", 110: "!upgrade-server"}, labels={10: "A10", 110: "A10b"}
    )
    rec.row(10).outcome = "passed"
    rec.row(110).reason = "stage 6 is not built yet"
    out = rec.report()
    out.title, out.required = "Admin end-to-end", [10, 110]
    out.write(tmp_path / "r.json")
    back = report.Report.read(tmp_path / "r.json")
    text = back.markdown()
    assert text.startswith("## Admin end-to-end: FAIL")
    assert "| A10 | Deploy gate | ✅ pass |" in text and "**not passed: A10b**" in text
    assert "**A10b. !upgrade-server**" in text
    with pytest.raises(ValueError):
        rec.row(3)


def test_old_reports_still_read(tmp_path):
    """A #263 report written before the label and title existed reads as before."""
    path = tmp_path / "old.json"
    path.write_text('{"commit": "abc", "results": [{"number": 1, "name": "Basics"}]}')
    old = report.Report.read(path)
    assert old.title == "End-to-end" and old.results[0].shown == "1"
    assert Path(path).exists()
