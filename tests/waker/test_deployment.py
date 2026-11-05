import configparser
import fcntl
import importlib.util
import json
import os
import re
import stat
import sys
import threading
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "workshop/admin"))
spec = importlib.util.spec_from_file_location("push_waker", ROOT / "workshop/admin/push_waker.py")
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


def test_opt_in_plan_retains_unselected_and_never_reads_keys(tmp_path):
    row = dict(
        slug="alice",
        ip="192.0.2.7",
        region="us-west-2",
        email="alice@example.test",
        zulip_user_id="20",
    )
    keys = tmp_path / "alice"
    keys.mkdir()
    for kind in ("codex", "claude"):
        (keys / f"{kind}.zuliprc").write_text("DECOY-NEVER-IN-CONFIG")
    ec2 = Mock()
    ec2.describe_instances.return_value = {
        "Reservations": [
            {
                "Instances": [
                    {
                        "InstanceId": "i-0123456789abcdef0",
                        "Tags": [{"Key": "participant", "Value": "Former-Owner"}],
                    }
                ]
            }
        ]
    }
    text, copies = deploy.plan(
        [row],
        tmp_path,
        123,
        {"us-west-2": ec2},
        {"region": "us-east-2", "bots": [{"name": "existing"}]},
    )
    parsed = tomllib.loads(text)
    assert len(parsed["bots"]) == 3 and len(copies) == 2
    assert parsed["bots"][0]["region"] == "us-east-2"
    assert parsed["bots"][1]["allow_user_ids"] == [20]
    assert "DECOY" not in text
    filters = ec2.describe_instances.call_args.kwargs["Filters"]
    assert {"Name": "network-interface.association.public-ip", "Values": ["192.0.2.7"]} in filters
    assert all(f["Name"] != "tag:participant" for f in filters)
    ec2.start_instances.assert_not_called()
    ec2.describe_instances.return_value = {"Reservations": []}
    with pytest.raises(ValueError, match="exactly one"):
        deploy.plan([row], tmp_path, 123, {"us-west-2": ec2}, {})


def test_iam_only_describes_and_tag_restricted_starts():
    policy = json.loads((ROOT / "workshop/waker/iam-policy.json").read_text())
    read, start = policy["Statement"]
    assert read == {"Effect": "Allow", "Action": "ec2:DescribeInstances", "Resource": "*"}
    assert start["Action"] == "ec2:StartInstances"
    assert start["Condition"] == {"StringEquals": {"ec2:ResourceTag/workshop": "nt-agents-2026"}}


def test_the_test_wakers_policy_requires_zab_role_test():
    """Test infrastructure: a `zab-test-*` role acts only on resources tagged `zab-role=test`. The
    test waker's policy is the fleet's with both tags in one StringEquals (IAM ANDs them), and
    the README sends the operator to this file, not to a hand edit (reviewer on #272)."""
    fleet = json.loads((ROOT / "workshop/waker/iam-policy.json").read_text())
    test = json.loads((ROOT / "workshop/waker/iam-policy-test.json").read_text())
    assert test["Statement"][0] == fleet["Statement"][0]  # DescribeInstances, read-only
    start = test["Statement"][1]
    assert {k: v for k, v in start.items() if k != "Condition"} == {
        k: v for k, v in fleet["Statement"][1].items() if k != "Condition"
    }
    assert start["Condition"] == {
        "StringEquals": {"ec2:ResourceTag/workshop": "zab-test", "ec2:ResourceTag/zab-role": "test"}
    }
    assert len(test["Statement"]) == 2
    readme = (ROOT / "workshop/waker/README.md").read_text()
    assert "iam-policy-test.json" in readme and "condition's value changed" not in readme


def test_install_forces_selected_same_version_wheel(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "ROOT", tmp_path / "install")
    # install() removes the drop-in without a role: never the real one (Codex round 4, #298)
    monkeypatch.setattr(deploy, "DROPIN", tmp_path / "unit.d" / "assume-role.conf")
    monkeypatch.setattr(deploy.os, "geteuid", lambda: 0)
    monkeypatch.setattr(deploy.os, "chown", lambda *args: None)
    monkeypatch.setattr(deploy.os, "umask", lambda *args: None)
    monkeypatch.setattr(deploy.shutil, "copyfile", lambda *args: None)
    run = Mock()
    monkeypatch.setattr(deploy.subprocess, "run", run)
    deploy.install("organizers_channel=1", [], tmp_path / "reviewed.whl")
    commands = [call.args[0] for call in run.call_args_list]
    forced = next(c for c in commands if "--force-reinstall" in c)
    assert "--no-deps" in forced and forced[-1] == str(tmp_path / "reviewed.whl")
    assert commands.index(forced) < next(i for i, c in enumerate(commands) if "restart" in c)


def test_the_test_waker_uses_its_own_workshop_tag(tmp_path):
    """`--workshop zab-test` (issue #263): the lookup filters on it, the config records it, and
    a config installed for another workshop is not mixed with it."""
    row = dict(slug="zab-test", ip="192.0.2.9", region="us-east-1", zulip_user_id="21")
    (tmp_path / "zab-test").mkdir()
    for kind in ("codex", "claude"):
        (tmp_path / "zab-test" / f"{kind}.zuliprc").write_text("DECOY")
    ec2 = Mock()
    ec2.describe_instances.return_value = {
        "Reservations": [{"Instances": [{"InstanceId": "i-0123456789abcdef1"}]}]
    }
    text, _ = deploy.plan([row], tmp_path, 7, {"us-east-1": ec2}, {}, "zab-test")
    assert tomllib.loads(text)["workshop"] == "zab-test"
    filters = ec2.describe_instances.call_args.kwargs["Filters"]
    assert {"Name": "tag:workshop", "Values": ["zab-test"]} in filters
    fleet = {"bots": [{"name": "claude-alice"}]}  # installed before the key: the fleet's
    with pytest.raises(ValueError, match="nt-agents-2026"):
        deploy.plan([row], tmp_path, 7, {"us-east-1": ec2}, fleet, "zab-test")
    with pytest.raises(ValueError, match="invalid workshop"):
        deploy.plan([row], tmp_path, 7, {"us-east-1": ec2}, {}, "a b")
    default, _ = deploy.plan([row], tmp_path, 7, {"us-east-1": ec2}, {})
    assert tomllib.loads(default)["workshop"] == "nt-agents-2026"


#: AWS's documentation account ID: the repository holds no real one (#207).
ARN = "arn:aws:iam::111122223333:role/zab-waker-nt-agents-2026"
ASSUMED = "arn:aws:sts::111122223333:assumed-role/zab-waker-nt-agents-2026/zab-waker"
HOST = "arn:aws:sts::444455556666:assumed-role/zab-files-host/i-0a"  # its own role


def test_assume_role_profile_dropin_and_reruns(tmp_path, monkeypatch):
    """#207, VMs in another account: the profile botocore's AssumeRoleProvider reads, the
    drop-in pointing the service at it, malformed ARNs refused, and reruns keep the role."""
    monkeypatch.setattr(deploy, "ROOT", tmp_path)
    profiles = configparser.ConfigParser()
    profiles.read_string(deploy.aws_config(ARN))
    assert dict(profiles["profile zab-waker"]) == {
        "role_arn": ARN,
        "credential_source": "Ec2InstanceMetadata",
        "role_session_name": "zab-waker",
    }
    assert deploy.dropin().splitlines() == [
        "[Service]",
        f"Environment=AWS_CONFIG_FILE={tmp_path}/aws-config",
        "Environment=AWS_PROFILE=zab-waker",
    ]
    for bad in ("arn:aws:iam::1111:role/x", ARN.replace("role/", "user/"), ARN + "\nx = y", "r"):
        with pytest.raises(ValueError, match="malformed role ARN"):
            deploy.aws_config(bad)
    assert deploy.role_config(None, False) is None
    (tmp_path / "aws-config").write_text("installed")
    assert deploy.role_config(None, False) == "installed"
    assert deploy.role_config(None, True) is None
    assert f"role_arn = {ARN}" in deploy.role_config(ARN, False)


OPERATOR = {  # an operator's shell: exported keys, a profile, root's ~/.aws (all decoys)
    "AWS_ACCESS_KEY_ID": "AKIDOPERATOR",
    "AWS_SECRET_ACCESS_KEY": "decoy",
    "AWS_PROFILE": "operator",
}


def isolate_aws(monkeypatch, tmp_path) -> None:
    """Every AWS_* variable restored after the test, then the operator's decoys."""
    for name in [n for n in os.environ if n.startswith("AWS_")]:
        monkeypatch.delenv(name)
    for name in ("AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE", *OPERATOR):
        monkeypatch.setenv(name, OPERATOR.get(name, ""))
    root_aws = tmp_path / "root-aws"
    root_aws.write_text("[operator]\naws_access_key_id = AKIDOPERATOR\naws_secret_access_key = x\n")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(root_aws))


def test_botocore_takes_the_services_identity(tmp_path, monkeypatch):
    """With the service's environment botocore uses the instance profile, or assumes the role
    from it; never the operator's keys, profile or files (Codex rounds 1 and 2 on #298). No AWS
    call. botocore is not a zab dependency: run with
    `uv run --with 'boto3>=1.35,<2' python -m pytest tests/waker`."""
    session = pytest.importorskip("botocore.session")
    credentials = pytest.importorskip("botocore.credentials")
    isolate_aws(monkeypatch, tmp_path)
    instance = credentials.Credentials("AKIDINSTANCE", "decoy", "decoy", method="iam-role")
    monkeypatch.setattr(credentials.InstanceMetadataProvider, "load", lambda self: instance)

    def method() -> str:
        return session.get_session().get_credentials().method

    assert method() == "env"  # the operator's exported keys win by default
    monkeypatch.delenv("AWS_ACCESS_KEY_ID")
    assert method() == "shared-credentials-file"  # then the operator's profile and file
    deploy.use_profile(None, str(tmp_path))  # --check without a role, --no-assume-role
    assert method() == "iam-role" and os.environ["AWS_CONFIG_FILE"] == os.devnull
    deploy.use_profile(deploy.aws_config(ARN), str(tmp_path))
    assert method() == "assume-role"


def test_install_writes_and_removes_the_assume_role_dropin(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "ROOT", tmp_path / "etc")
    monkeypatch.setattr(deploy, "DROPIN", tmp_path / "unit.d" / "assume-role.conf")
    for name, fake in (("geteuid", lambda: 0), ("chown", lambda *a: None), ("umask", Mock())):
        monkeypatch.setattr(deploy.os, name, fake)
    monkeypatch.setattr(deploy.shutil, "copyfile", lambda *args: None)
    reloads = []
    monkeypatch.setattr(
        deploy.subprocess, "run", lambda argv, **kw: reloads.append(deploy.DROPIN.exists())
    )
    deploy.install("organizers_channel=1", [], tmp_path / "w.whl", deploy.aws_config(ARN))
    assert (tmp_path / "etc/aws-config").read_text() == deploy.aws_config(ARN)
    assert (tmp_path / "etc/aws-config").stat().st_mode & 0o777 == 0o600
    # written before daemon-reload, enable and restart
    assert deploy.DROPIN.read_text() == deploy.dropin() and reloads[-3:] == [True] * 3
    real = Path.stat  # the rerun finds ROOT, which root owns on the files host

    def stat(path, **kw):
        info = real(path, **kw)
        return SimpleNamespace(st_uid=0, st_mode=info.st_mode) if path == deploy.ROOT else info

    monkeypatch.setattr(Path, "stat", stat)
    deploy.install("organizers_channel=1", [], tmp_path / "w.whl")  # --no-assume-role
    assert not deploy.DROPIN.exists() and not (tmp_path / "etc/aws-config").exists()


def client_error(code: str) -> Exception:
    error = Exception()  # botocore's ClientError carries its code in `response`
    error.response = {"Error": {"Code": code}}
    return error


def fake_boto3(
    monkeypatch, tmp_path, arn=ASSUMED, start="DryRunOperation", seen=None, describe=None
):
    sts, ec2 = Mock(), Mock()
    sts.get_caller_identity.return_value = {"Arn": arn}
    vm = {"InstanceId": "i-0123456789abcdef0", "State": {"Name": "stopped"}}
    ec2.describe_instances.return_value = {"Reservations": [{"Instances": [vm]}]}
    ec2.describe_instances.side_effect = describe and client_error(describe)
    ec2.start_instances.side_effect = client_error(start)

    def client(service, region_name):
        env = deploy.os.environ  # the service's view only (Codex rounds 1 and 2)
        assert "AWS_ACCESS_KEY_ID" not in env and env.get("AWS_PROFILE") in (None, "zab-waker")
        assert env["AWS_SHARED_CREDENTIALS_FILE"] == os.devnull
        if seen is not None:  # the profile in effect when the client is made
            seen.append(Path(env["AWS_CONFIG_FILE"]).read_text())
        return sts if service == "sts" else ec2

    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(client=client))
    isolate_aws(monkeypatch, tmp_path)
    return ec2


def test_dry_run_discovers_through_the_role_and_shows_the_identity(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(deploy, "ROOT", tmp_path / "etc")
    roster = tmp_path / "roster.csv"
    roster.write_text("slug,email,ip,region,zulip_user_id\nalice,a@example.test,192.0.2.7,,20\n")
    (tmp_path / "keys/alice").mkdir(parents=True)
    for kind in ("codex", "claude"):
        (tmp_path / "keys/alice" / f"{kind}.zuliprc").write_text("DECOY")
    (tmp_path / "w.whl").write_text("")
    seen = []
    fake_boto3(monkeypatch, tmp_path, seen=seen)
    argv = ["--roster", str(roster), "--only", "alice", "--keys", str(tmp_path / "keys")]
    argv += ["--wheel", str(tmp_path / "w.whl"), "--organizers-channel", "7"]
    assert deploy.main([*argv, "--assume-role", ARN]) == 0
    assert len(seen) == 2 and set(seen) == {deploy.aws_config(ARN)}  # STS, then EC2
    assert deploy.os.environ["AWS_PROFILE"] == "zab-waker"
    out = capsys.readouterr().out
    assert f"AWS identity: {ASSUMED}" in out and f"role_arn = {ARN}" in out and "DECOY" not in out
    assert not (tmp_path / "etc").exists()  # a dry run installs nothing
    assert deploy.main([*argv, "--assume-role", "zab-waker-nt-agents-2026"]) == 2
    assert "malformed role ARN" in capsys.readouterr().out
    fake_boto3(monkeypatch, tmp_path, arn=HOST)
    assert deploy.main([*argv, "--assume-role", ARN]) == 2  # Codex round 1: not the role
    assert f"is not the role {ARN}" in capsys.readouterr().out


def test_check_confirms_the_assumed_role_and_dry_run_starts(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(deploy, "ROOT", tmp_path)
    (tmp_path / "aws-config").write_text(deploy.aws_config(ARN))
    (tmp_path / "config.toml").write_text(
        '[[bots]]\nname = "claude-alice"\ninstance_id = "i-0123456789abcdef0"\n'
    )
    unit = f"AWS_CONFIG_FILE={tmp_path}/aws-config AWS_PROFILE=zab-waker AWS_SESSION_TOKEN=DECOY"
    monkeypatch.setattr(deploy.subprocess, "run", Mock(return_value=Mock(stdout=unit + "\n")))
    ec2 = fake_boto3(monkeypatch, tmp_path)
    assert deploy.main(["--check"]) == 0
    out = capsys.readouterr().out
    assert f"role to assume: {ARN}" in out and "{'stopped': 1}" in out and "check: OK" in out
    assert "AWS_PROFILE=zab-waker AWS_SESSION_TOKEN\n" in out and "DECOY" not in out  # names only
    for other in (["--assume-role", ARN], ["--no-assume-role"], ["--only", "a"], ["--apply"]):
        with pytest.raises(SystemExit):  # it checks what is installed, nothing else
            deploy.main(["--check", *other])
    ec2.start_instances.assert_called_once_with(InstanceIds=["i-0123456789abcdef0"], DryRun=True)
    fake_boto3(monkeypatch, tmp_path, arn=HOST)
    assert deploy.main(["--check"]) == 2 and "MISMATCH" in capsys.readouterr().out
    fake_boto3(monkeypatch, tmp_path, start="UnauthorizedOperation")
    assert deploy.main(["--check"]) == 2
    assert "NO: UnauthorizedOperation" in capsys.readouterr().out


def test_check_reports_aws_errors_instead_of_raising(tmp_path, monkeypatch, capsys):
    """Review on #298: before `--assume-role --apply`, the instance profile has no EC2
    permission, and `--check` crashed on DescribeInstances' ClientError. It must say so."""
    monkeypatch.setattr(deploy, "ROOT", tmp_path)
    (tmp_path / "config.toml").write_text(
        '[[bots]]\nname = "claude-t"\ninstance_id = "i-0123456789abcdef0"\nregion = "us-east-1"\n'
    )
    monkeypatch.setattr(deploy.subprocess, "run", Mock(return_value=Mock(stdout="\n")))

    fake_boto3(
        monkeypatch, tmp_path, arn=HOST, describe="UnauthorizedOperation", start="Unauthorized"
    )
    assert deploy.main(["--check"]) == 2
    out = capsys.readouterr().out
    assert "us-east-1: cannot list VMs: UnauthorizedOperation" in out
    assert "NO: Unauthorized" in out and "--assume-role ARN --apply" in out
    assert out.rstrip().endswith("check: FAILED")

    class NoCredentialsError(Exception):
        pass

    sys.modules["boto3"].client(
        "sts", "us-east-1"
    ).get_caller_identity.side_effect = NoCredentialsError()
    assert deploy.main(["--check"]) == 2
    out = capsys.readouterr().out
    assert "AWS identity: none: no usable AWS identity" in out and "check: FAILED" in out


def test_assume_role_alone_on_a_deployed_waker(tmp_path, monkeypatch, capsys):
    """Adding the role to a deployed waker needs no roster, keys or wheel: only the profile
    and the drop-in change, after the identity is proved through the profile."""
    monkeypatch.setattr(deploy, "ROOT", tmp_path / "etc")
    monkeypatch.setattr(deploy, "DROPIN", tmp_path / "unit.d" / "assume-role.conf")
    assert deploy.main(["--assume-role", ARN]) == 2  # nothing deployed yet
    assert "no waker deployed" in capsys.readouterr().out
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc/config.toml").write_text('[[bots]]\nname = "c"\nregion = "us-east-1"\n')
    seen = []
    fake_boto3(monkeypatch, tmp_path, seen=seen)
    assert deploy.main(["--assume-role", ARN]) == 0 and seen == [deploy.aws_config(ARN)]
    assert f"AWS identity: {ASSUMED}" in capsys.readouterr().out
    assert not deploy.DROPIN.exists()  # a dry run changes nothing
    fake_boto3(monkeypatch, tmp_path, arn=HOST)
    assert deploy.main(["--assume-role", ARN, "--apply"]) == 2  # not the role: nothing applied
    assert "is not the role" in capsys.readouterr().out and not deploy.DROPIN.exists()
    fake_boto3(monkeypatch, tmp_path)
    real = Path.stat  # ROOT is root's on the files host

    def stat(path, **kw):
        info = real(path, **kw)
        return SimpleNamespace(st_uid=0, st_mode=info.st_mode) if path == deploy.ROOT else info

    monkeypatch.setattr(Path, "stat", stat)
    for name, fake in (("geteuid", lambda: 0), ("chown", lambda *a: None), ("umask", Mock())):
        monkeypatch.setattr(deploy.os, name, fake)
    run = Mock()
    monkeypatch.setattr(deploy.subprocess, "run", run)
    assert deploy.main(["--assume-role", ARN, "--apply"]) == 0
    assert (tmp_path / "etc/aws-config").read_text() == deploy.aws_config(ARN)
    assert deploy.DROPIN.read_text() == deploy.dropin()
    # try-restart: a waker stopped by the README's undo stays stopped (review on #298)
    restarts = [["daemon-reload"], ["try-restart", "zab-waker.service"]]
    assert [c.args[0][1:] for c in run.call_args_list] == restarts
    monkeypatch.setitem(sys.modules, "boto3", None)  # removing needs no AWS identity at all
    assert deploy.main(["--no-assume-role"]) == 0 and deploy.DROPIN.exists()  # dry run
    assert deploy.main(["--no-assume-role", "--apply"]) == 0
    assert not deploy.DROPIN.exists() and not (tmp_path / "etc/aws-config").exists()
    assert [c.args[0][1:] for c in run.call_args_list] == restarts * 2
    with pytest.raises(SystemExit):  # a partial deployment is still an error
        deploy.main(["--only", "alice", "--assume-role", ARN])


def test_cross_account_iam_templates():
    """Trust only the files host's role (not its account); the files host may only assume
    `zab-waker-*` roles; the test variant stays on `zab-test-*` roles tagged `zab-role=test` (test
    infrastructure only), and its trust names the account, so that in one account the AssumeRole
    still needs the identity policy (Codex round 2 on #298); placeholders, no real ID."""
    waker = ROOT / "workshop/waker"

    def statement(name: str) -> dict:
        (only,) = json.loads((waker / name).read_text())["Statement"]
        assert only["Effect"] == "Allow" and only["Action"] == "sts:AssumeRole"
        return only

    host_role = "arn:aws:iam::<FILES_HOST_ACCOUNT_ID>:role/zab-files-host"
    assert statement("trust-fleet-role.json")["Principal"] == {"AWS": host_role}
    test_account = "arn:aws:iam::<ACCOUNT_ID>:root"
    assert statement("trust-fleet-role-test.json")["Principal"] == {"AWS": test_account}
    host = statement("files-host-policy.json")  # no Condition: only the fleet's trust gates
    assert host["Resource"] == "arn:aws:iam::<FLEET_ACCOUNT_ID>:role/zab-waker-*" and len(host) == 3
    test = statement("files-host-policy-test.json")
    assert test["Resource"] == "arn:aws:iam::<ACCOUNT_ID>:role/zab-test-*"
    assert test["Condition"] == {"StringEquals": {"aws:ResourceTag/zab-role": "test"}}
    assert statement("trust-ec2.json")["Principal"] == {"Service": "ec2.amazonaws.com"}
    for path in waker.glob("*.json"):
        assert not re.search(r"\d{12}", path.read_text()), path


# ------------------------------------------------ the admin server's layout (PR 21)


def fleet_vm(instance="i-0123456789abcdef0", workshop="alpha", **tags):
    own = {"participant": "alice", "workshop": workshop, **tags}
    return {"Reservations": [{"Instances": [{
        "InstanceId": instance,
        "State": {"Name": "stopped"},
        "Tags": [{"Key": k, "Value": v} for k, v in own.items() if v is not None],
    }]}]}  # fmt: skip


def unlocked(home):
    """What a refused run left in the config directory, but the writers' `.lock` (#348)."""
    return [p.name for p in home.iterdir() if p.name != ".lock"]


def stage_admin(tmp_path):
    roster = tmp_path / "roster.csv"
    roster.write_text(
        "slug,email,ip,region,zulip_user_id\n"
        "alice,a@example.test,192.0.2.7,,20\nbob,b@example.test,192.0.2.8,us-east-1,21\n"
    )
    for slug in ("alice", "bob"):
        (tmp_path / "keys" / slug).mkdir(parents=True)
        for kind in ("codex", "claude"):
            (tmp_path / "keys" / slug / f"{kind}.zuliprc").write_text(f"DECOY-{slug}-{kind}")
    home = tmp_path / "waker"
    home.mkdir()
    home.chmod(0o2750)  # tmpfiles.conf: setgid, so what is made in it takes its group
    argv = ["--roster", str(roster), "--keys", str(tmp_path / "keys"), "--config-dir", str(home)]
    return home, [*argv, "--region", "us-east-1"]


def opt_in_two(monkeypatch, tmp_path, argv):
    """alice in alpha and bob in beta, each with a VM of its own (an instance in two workshops
    is refused, as the waker's loader refuses it)."""
    ec2 = fake_boto3(monkeypatch, tmp_path)
    for n, (slug, workshop, channel) in enumerate(
        (("alice", "alpha", "42"), ("bob", "beta", "43"))
    ):
        ec2.describe_instances.return_value = fleet_vm(
            f"i-0123456789abcdef{n}", workshop, participant=slug
        )
        on = ["--only", slug, "--workshop", workshop, "--organizers-channel", channel]
        assert deploy.main([*argv, *on, "--apply"]) == 0
    return ec2


def test_the_waker_unit_reads_the_config_the_bots_write():
    """#347 (the approval of #362): the admin server's waker unit reads `--config` from the
    directory the bots' `!waker` plans write (`Layout.waker`, `push_waker.py --config-dir`)."""
    from zab.admin.actions import LAYOUT

    unit = (ROOT / "workshop/adminserver/zab-waker.service").read_text()
    (start,) = [line for line in unit.splitlines() if line.startswith("ExecStart=")]
    words = start.split()
    assert words[words.index("--config") + 1] == f"{LAYOUT.waker}/config.toml"


def test_the_script_and_the_loader_take_the_same_workshop_names():
    """#347 (the approval of #362): a name push_waker.py writes is one the waker's loader
    takes, and the other way round: the two patterns are the same."""
    from zab.waker import __main__ as cli

    assert deploy._NAME_RE.pattern == cli._WORKSHOP_NAME.pattern
    assert deploy._NAME_RE.flags == cli._WORKSHOP_NAME.flags


def test_config_dir_writes_the_admin_layout_the_waker_loads(tmp_path, monkeypatch, capsys):
    """Dry run, then apply, of two workshops in turn: 0640 files and 0750 directories of the
    directory's group, the config naming them; the waker's starter-mode loader accepts it."""
    from zab.waker import __main__ as cli

    home, argv = stage_admin(tmp_path)
    ec2 = fake_boto3(monkeypatch, tmp_path)
    ec2.describe_instances.return_value = fleet_vm(**{"zab-role": "test"})
    alpha = [*argv, "--only", "alice", "--workshop", "alpha", "--organizers-channel", "42"]
    alpha += ["--tag", "workshop=alpha", "--tag", "zab-role=test"]
    assert deploy.main(alpha) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "DECOY" not in out and not any(home.iterdir())
    filters = ec2.describe_instances.call_args.kwargs["Filters"]
    assert filters[:2] == [
        {"Name": "tag:workshop", "Values": ["alpha"]},
        {"Name": "tag:zab-role", "Values": ["test"]},
    ]
    assert deploy.main([*alpha, "--apply"]) == 0
    group = home.stat().st_gid
    for path in (home / "config.toml", *(home / "alpha/alice").iterdir()):
        info = path.stat()
        assert (stat.S_IMODE(info.st_mode), info.st_gid) == (0o640, group), path
    for path in (home / "alpha", home / "alpha/alice"):
        assert (path.stat().st_mode & 0o2777, path.stat().st_gid) == (0o2750, group)
    assert (home / "alpha/alice/claude.zuliprc").read_text() == "DECOY-alice-claude"
    assert not list(home.rglob(".new-*"))
    ec2.describe_instances.return_value = fleet_vm("i-0123456789abcdef1", "beta", participant="bob")
    beta = [*argv, "--only", "bob", "--workshop", "beta", "--organizers-channel", "43", "--apply"]
    assert deploy.main(beta) == 0
    ec2.describe_instances.return_value = fleet_vm(**{"zab-role": "test"})
    assert deploy.main([*alpha, "--apply"]) == 0  # a rerun keeps beta
    monkeypatch.setattr(cli.pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=os.getuid()))
    monkeypatch.setattr(cli.grp, "getgrnam", lambda name: SimpleNamespace(gr_gid=group))
    config, _ = cli.load(home / "config.toml")
    assert config["starter"] == "/run/zab-admin-starter/start.sock"
    assert config["status_file"] == "/var/lib/zab-waker/status/waker.json"
    fleets = [(f.name, f.organizers, [(b.name, i, b.zuliprc) for b, i in f.entries])
              for f in config["fleets"]]  # fmt: skip
    assert fleets == [
        ("alpha", 42, [
            ("codex-alice", "i-0123456789abcdef0", f"{home}/alpha/alice/codex.zuliprc"),
            ("claude-alice", "i-0123456789abcdef0", f"{home}/alpha/alice/claude.zuliprc"),
        ]),
        ("beta", 43, [
            ("codex-bob", "i-0123456789abcdef1", f"{home}/beta/bob/codex.zuliprc"),
            ("claude-bob", "i-0123456789abcdef1", f"{home}/beta/bob/claude.zuliprc"),
        ]),
    ]  # fmt: skip
    assert config["fleets"][1].entries[0][0].allow_user_ids == [21]


def test_config_dir_remove_opts_out_without_aws(tmp_path, monkeypatch, capsys):
    """`!waker off`: the bots leave the config, then their zuliprcs go; the last opt-out leaves
    a config without workshops, which the waker loads and idles on."""
    from zab.waker import __main__ as cli

    home, argv = stage_admin(tmp_path)
    opt_in_two(monkeypatch, tmp_path, argv)
    monkeypatch.setitem(sys.modules, "boto3", None)  # opting out needs no AWS
    roster = argv[:2]
    off = [*roster, "--config-dir", str(home), "--remove", "--only", "alice", "--workshop", "alpha"]
    assert deploy.main(off) == 0 and (home / "alpha/alice/codex.zuliprc").exists()  # dry run
    assert deploy.main([*off, "--apply"]) == 0
    assert not (home / "alpha/alice").exists() and (home / "beta/bob/codex.zuliprc").exists()
    group = home.stat().st_gid
    monkeypatch.setattr(cli.pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=os.getuid()))
    monkeypatch.setattr(cli.grp, "getgrnam", lambda name: SimpleNamespace(gr_gid=group))
    assert [f.name for f in cli.load(home / "config.toml")[0]["fleets"]] == ["beta"]
    off = [*roster, "--config-dir", str(home), "--remove", "--only", "bob", "--workshop", "beta"]
    assert deploy.main([*off, "--apply"]) == 0
    assert cli.load(home / "config.toml")[0]["fleets"] == []
    alpha = [*roster, "--config-dir", str(home), "--remove", "--only", "bob", "--workshop", "alpha"]
    assert deploy.main(alpha) == 0  # a wrong workshop changes nothing, and says so
    assert "warning: bob has no bot in alpha's waker config" in capsys.readouterr().out
    for remove in ([], ["--remove"]):  # --workshop has no default with --config-dir
        with pytest.raises(SystemExit):
            deploy.main([*argv, *remove, "--only", "bob", "--organizers-channel", "43"])
        assert "required: --workshop" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        deploy.main([*roster, "--remove", "--only", "bob"])  # --remove needs --config-dir


def test_config_dir_prints_only_its_own_workshop(tmp_path, monkeypatch, capsys):
    """`!waker on|off` posts this output in the workshop's organizers channel (#358): another
    workshop's opt-ins (instances, allowlists, key paths) stay out of it, while the config
    written keeps them."""
    home, argv = stage_admin(tmp_path)
    opt_in_two(monkeypatch, tmp_path, argv).describe_instances.return_value = fleet_vm()
    capsys.readouterr()
    alpha = ["--only", "alice", "--workshop", "alpha"]
    roster = argv[:2]
    for run in (
        [*argv, *alpha, "--organizers-channel", "42"],
        [*argv, *alpha, "--organizers-channel", "42", "--apply"],
        [*roster, "--config-dir", str(home), "--remove", *alpha],
    ):
        assert deploy.main(run) == 0
        out = capsys.readouterr().out
        assert 'name = "alpha"' in out or "remove alice" in out
        assert "beta" not in out and "bob" not in out and "b@example.test" not in out
    assert 'name = "beta"' in (home / "config.toml").read_text()


@pytest.mark.parametrize(
    "case, loader_error",
    [
        ("slug", "bot names must be nonempty and unique"),  # bob in two workshops' rosters
        ("instance", "is in two workshops"),  # one VM opted in under two workshops
        ("channel", None),  # push_waker.py's own rule: the loader takes it
    ],
)
def test_config_dir_refuses_at_the_dry_run_what_would_stop_every_waker(
    tmp_path, monkeypatch, capsys, case, loader_error
):
    """Review of #362: the waker's loader refuses a whole config with a bot name or an instance
    in two workshops, and then no workshop's VMs are woken. The dry run and the apply refuse
    it, naming only this workshop's slug, and leave the config as it was. With the check off,
    the loader refuses what is written: the two agree."""
    from zab.waker import __main__ as cli

    home, argv = stage_admin(tmp_path)
    ec2 = fake_boto3(monkeypatch, tmp_path)
    ec2.describe_instances.return_value = fleet_vm("i-0123456789abcdef1", "beta", participant="bob")
    beta = [*argv, "--only", "bob", "--workshop", "beta", "--organizers-channel", "43", "--apply"]
    assert deploy.main(beta) == 0
    before = (home / "config.toml").read_bytes()
    capsys.readouterr()
    slug, instance, channel = {
        "slug": ("bob", "i-0123456789abcdef2", "42"),
        "instance": ("alice", "i-0123456789abcdef1", "42"),
        "channel": ("alice", "i-0123456789abcdef0", "43"),
    }[case]
    ec2.describe_instances.return_value = fleet_vm(instance, "alpha", participant=slug)
    alpha = [*argv, "--only", slug, "--workshop", "alpha", "--organizers-channel", channel]
    for run in (alpha, [*alpha, "--apply"]):
        assert deploy.main(run) == 2
        out = capsys.readouterr().out
        assert "refused" in out and "beta" not in out
        assert f"{slug}: opted in under another workshop" in out or case == "channel"
        assert (home / "config.toml").read_bytes() == before and not (home / "alpha").exists()
    monkeypatch.setattr(deploy, "check_shared", lambda workshops, workshop: None)
    assert deploy.main([*alpha, "--apply"]) == 0  # what the unchecked run would write
    group = home.stat().st_gid
    monkeypatch.setattr(cli.pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=os.getuid()))
    monkeypatch.setattr(cli.grp, "getgrnam", lambda name: SimpleNamespace(gr_gid=group))
    if loader_error is None:
        assert [f.name for f in cli.load(home / "config.toml")[0]["fleets"]] == ["beta", "alpha"]
    else:
        with pytest.raises(ValueError, match=loader_error):
            cli.load(home / "config.toml")


def test_config_dir_dry_run_prints_the_same_for_the_same_state(tmp_path, monkeypatch, capsys):
    """§2.4: a `!waker` plan is hashed on this output, so two dry runs of one state print the
    same text, and another instance at the roster's address prints another."""
    _, argv = stage_admin(tmp_path)
    ec2 = fake_boto3(monkeypatch, tmp_path)
    ec2.describe_instances.return_value = fleet_vm()
    alpha = [*argv, "--only", "alice", "--workshop", "alpha", "--organizers-channel", "42"]
    outs = []
    for _ in range(2):
        assert deploy.main(alpha) == 0
        outs.append(capsys.readouterr().out)
    assert outs[0] == outs[1] and "i-0123456789abcdef0" in outs[0]
    ec2.describe_instances.return_value = fleet_vm("i-0123456789abcdef9")
    assert deploy.main(alpha) == 0 and capsys.readouterr().out != outs[0]


@pytest.mark.parametrize("branch", ["remove", "add"])
def test_config_dir_runs_of_two_workshops_never_lose_an_update(
    tmp_path, monkeypatch, capsys, branch
):
    """Codex round 2 on #343: workshops apply concurrently (ADMIN §2.4). beta's run snapshots
    the config and holds the lock; alpha's run, in a thread, must wait and then build on what
    beta wrote. Without the lock, beta's later write would restore alice after alpha deleted
    her zuliprcs, and the waker would refuse the config for every workshop."""
    from zab.waker import __main__ as cli

    home, argv = stage_admin(tmp_path)
    opt_in_two(monkeypatch, tmp_path, argv)
    roster = argv[:2]
    alpha = ["--config-dir", str(home), "--only", "alice", "--workshop", "alpha", "--apply"]
    if branch == "remove":
        alpha = [*roster, "--remove", *alpha]
    else:  # alice's opt-in again, with her VM looked up anew
        alpha = [*argv, *alpha, "--organizers-channel", "42"]
    lock = os.open(home / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX)  # beta's run holds it
    snapshot = tomllib.loads((home / "config.toml").read_text())
    results = []
    thread = threading.Thread(target=lambda: results.append(deploy.main(alpha)))
    thread.start()
    thread.join(0.3)
    assert thread.is_alive() and (home / "alpha/alice/codex.zuliprc").exists()  # waiting
    config, stale, _ = deploy.remove_shared(snapshot, "beta", ["bob"], home)
    deploy.install_shared(config, [], home, stale)  # beta's run, from its snapshot
    os.close(lock)
    thread.join(10)
    assert results == [0]
    group = home.stat().st_gid
    monkeypatch.setattr(cli.pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=os.getuid()))
    monkeypatch.setattr(cli.grp, "getgrnam", lambda name: SimpleNamespace(gr_gid=group))
    fleets = cli.load(home / "config.toml")[0]["fleets"]  # loads: no zuliprc is missing
    assert [f.name for f in fleets] == ([] if branch == "remove" else ["alpha"])  # bob stays out
    capsys.readouterr()


def test_config_dir_lock_is_one_the_waker_cannot_hold(tmp_path, monkeypatch, capsys):
    """#348, from the final review of #343: zab-waker reads `waker/` and its files through its
    group, and an flock needs only a read-only fd. A holder of read-only locks on all of that
    doesn't delay a run; the writers' `.lock` is 0600, the writer's own. Another run's lock is
    waited for `LOCK_WAIT` seconds, then the run is refused and nothing changes."""
    home, argv = stage_admin(tmp_path)
    fake_boto3(monkeypatch, tmp_path).describe_instances.return_value = fleet_vm()
    on = [*argv, "--only", "alice", "--workshop", "alpha", "--organizers-channel", "42"]
    assert deploy.main([*on, "--apply"]) == 0
    lock = home / ".lock"
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600 and lock.stat().st_uid == os.getuid()
    held = []
    for path in (home, home / "config.toml", home / "alpha", home / "alpha/alice/codex.zuliprc"):
        fd = os.open(path, os.O_RDONLY)  # what the waker's group could open
        fcntl.flock(fd, fcntl.LOCK_EX)
        held.append(fd)
    roster = argv[:2]
    off = [*roster, "--config-dir", str(home), "--remove", "--only", "alice", "--workshop", "alpha"]
    result = []
    thread = threading.Thread(target=lambda: result.append(deploy.main([*off, "--apply"])))
    thread.start()
    thread.join(10)
    assert result == [0] and not (home / "alpha/alice").exists()  # not delayed at all
    for fd in held:
        os.close(fd)
    monkeypatch.setattr(deploy, "LOCK_WAIT", 0.3)
    other = os.open(lock, os.O_RDWR)
    fcntl.flock(other, fcntl.LOCK_EX)  # another writer's run
    before = (home / "config.toml").read_text()
    assert deploy.main([*on, "--apply"]) == 2
    assert "has held" in capsys.readouterr().out and (home / "config.toml").read_text() == before
    os.close(other)
    lock.chmod(0o640)  # not the writer's private file any more: refused, never used
    assert deploy.main([*on, "--apply"]) == 2
    assert "mode 0600" in capsys.readouterr().out
    lock.chmod(0o600)
    with monkeypatch.context() as patch:  # another user's lock file: refused
        patch.setattr(deploy.os, "geteuid", lambda: os.getuid() + 1)
        assert deploy.main([*on, "--apply"]) == 2
        assert "regular file of this user" in capsys.readouterr().out
    lock.unlink()
    os.mkfifo(lock, 0o600)  # not a regular file: refused
    assert deploy.main([*on, "--apply"]) == 2
    assert "regular file of this user" in capsys.readouterr().out
    lock.unlink()
    target = tmp_path / "elsewhere"
    lock.symlink_to(target)  # never followed
    assert deploy.main([*on, "--apply"]) == 2
    assert "Too many levels of symbolic links" in capsys.readouterr().out
    assert not target.exists() and (home / "config.toml").read_text() == before
    lock.unlink()
    monkeypatch.setattr(deploy.os, "geteuid", lambda: 0)
    assert deploy.main([*on, "--apply"]) == 2
    assert "not root" in capsys.readouterr().out


@pytest.mark.parametrize(
    "response,code",
    [({"Error": {"Code": "UnauthorizedOperation"}}, "UnauthorizedOperation"), ("odd", "Odd"),
     (None, "Odd"), ({"Error": "odd"}, "Odd"), ({"Error": {"Code": 7}}, "Odd")],
)  # fmt: skip
def test_aws_error_reads_any_response(response, code):
    """#348: `--check` reports an odd error by its class, never with a traceback."""

    class Odd(Exception):
        pass

    error = Odd()
    error.response = response
    assert deploy.aws_error(error) == code


@pytest.mark.parametrize(
    "extra,vm,error",
    [
        (["--tag", "zab-role=test"], {}, "must include workshop=alpha"),
        (["--tag", "workshop=alpha", "--tag", "a b"], {}, "KEY=VALUE"),
        (["--tag", "workshop=alpha", "--tag", "Workshop=x"], {}, "each key once"),
        (["--workshop", "Alpha"], {}, "invalid workshop name"),
        ([], {"participant": None}, "no participant tag"),
        ([], {"participant": " "}, "no participant tag"),
        ([], {"ZAB-ADMIN": "server"}, "tagged zab-admin"),
        (["--only", "bob", "--region", "us-west-2"], {}, "roster's region"),
        (["--organizers-channel", "0"], {}, "positive organizers channel"),
    ],
)
def test_config_dir_refusals(tmp_path, monkeypatch, capsys, extra, vm, error):
    home, argv = stage_admin(tmp_path)
    ec2 = fake_boto3(monkeypatch, tmp_path)
    ec2.describe_instances.return_value = fleet_vm(**vm)
    argv = [*argv, "--only", "alice", "--workshop", "alpha", "--organizers-channel", "42"]
    assert deploy.main([*argv, *extra, "--apply"]) == 2
    assert error in capsys.readouterr().out and unlocked(home) == []


def test_config_dir_takes_the_setgid_directory_and_no_files_host_option(
    tmp_path, monkeypatch, capsys
):
    home, argv = stage_admin(tmp_path)
    fake_boto3(monkeypatch, tmp_path).describe_instances.return_value = fleet_vm()
    argv = [*argv, "--only", "alice", "--workshop", "alpha", "--organizers-channel", "42"]
    home.chmod(0o750)
    assert deploy.main([*argv, "--apply"]) == 2
    assert "setgid directory" in capsys.readouterr().out and unlocked(home) == []
    for other in (["--wheel", "w.whl"], ["--check"], ["--assume-role", ARN], ["--no-assume-role"]):
        with pytest.raises(SystemExit):
            deploy.main([*argv, *other])
    with pytest.raises(SystemExit):  # --region is required
        deploy.main([a for a in argv if a not in ("--region", "us-east-1")])
    for alone in (["--region", "us-east-1"], ["--tag", "workshop=alpha"]):
        with pytest.raises(SystemExit):  # both go with --config-dir only
            deploy.main(["--check", *alone])
