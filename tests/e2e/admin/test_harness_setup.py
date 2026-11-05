"""The setup end to end, offline with fakes (issue #356): the dry run's plan, then a first run
(bootstrap) and a second (upgrade) in the order §7 needs, and the runner's teardown."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from e2e.admin import run as admin_run
from e2e.admin.fakes import FakeEc2, FakeIam, FakeZulip
from e2e.admin.harness import rig as rigs
from e2e.admin.harness.aws_rig import AwsRig, fingerprint
from e2e.admin.harness.gate import Aws, Gate, NotTestInfrastructure
from e2e.admin.harness.release import Deployed, Releases, local
from e2e.admin.harness.setup import Setup
from e2e.admin.harness.zulip_rig import stage_secrets
from e2e.admin.test_harness_aws import KEY
from e2e.harness import report
from e2e.harness.config import CHECKOUT
from e2e.harness.secret_files import SecretFileError
from e2e.harness.vm import Result

SHA = "0123456789abcdef0123456789abcdef01234567"


class FakeReleases(Releases):
    """git answers from this checkout; deploy.sh and the tags are recorded, not run."""

    def __init__(self, rig, events):
        super().__init__(rig, run=self.answer)
        self.events = events

    def answer(self, argv, **kw):
        if argv[0] == "git" and "rev-parse" in argv:
            return Result(0, SHA + "\n", "")
        if argv[0] == "git" and argv[3] == "show":
            path = argv[4].split(":", 1)[1]
            text = (
                'version = "0.2.3"\n' if path == "pyproject.toml" else (CHECKOUT / path).read_text()
            )
            return Result(0, text, "")
        return local(argv, **kw)  # ssh-keygen, render_policy.py

    def ensure_repo(self):
        self.events.append("repo")

    def has_tag(self, version):
        return False

    def release(self, base, version, signer="signing"):
        self.events.append(f"tag {version}")
        return "c" * 40

    def deploy(self, script, version, *flags, out, log=None, confirm=None):
        assert script.read_text().startswith("#!/usr/bin/env bash")  # the commit's own deploy.sh
        self.events.append(" ".join(["deploy", *flags, version]))
        return Deployed(Result(0, "", ""), "d" * 64)


class FakeServer:
    def __init__(self, events, running=None):
        self.events, self.now, self.installed = events, running, {}
        self.etc = {"admin.toml": "old", "allowed_signers": "", "backup_recipients": ""}

    def __call__(self, ip, rig):
        self.events.append(f"ssh {ip}")
        self.rig = rig
        return self

    def reachable(self):
        return True

    def running(self):
        return self.now

    def bot(self, action):
        self.events.append(f"bot {action}")

    def reset_workshops(self):
        self.events.append("reset")

    def read(self, path):
        return self.etc[Path(path).name]

    def install_etc(self, name, text):
        self.events.append(f"install {name}")

    def secret_sha256(self, name):
        """What the bootstrap installed: the rig's staged secret then, unless set since."""
        staged = hashlib.sha256((self.rig.secrets / name).read_bytes()).hexdigest()
        return self.installed.get(name, staged)

    def install_secret(self, name, text):
        self.events.append(f"install secret {name}")


def inputs(tmp_path) -> rigs.AdminRig:
    rig = rigs.load({"HOME": str(tmp_path)})
    rig.ssh_key.parent.mkdir(parents=True)
    rig.ssh_key.write_text("private\n")
    rig.ssh_key.with_name("id_ed25519.pub").write_text("ssh-ed25519 AAAA deployer\n")
    rig.tester_zuliprc.parent.mkdir(parents=True)
    rig.tester_zuliprc.write_text("[api]\nemail=t@zulip.example\nkey=k\nsite=https://z.example\n")
    rig.tester2_zuliprc.write_text("[api]\nemail=u@zulip.example\nkey=k\nsite=https://z.example\n")
    return rig


def make_setup(tmp_path, rig, *, dry, ec2, iam, zulip, server, events):
    lines: list[str] = []
    gate = Gate(dry_run=dry, out=lines.append)
    ec2.console = f"-----BEGIN SSH HOST KEY FINGERPRINTS-----\n256 {fingerprint(KEY)} r (ED25519)\n"
    ec2.console += "-----END SSH HOST KEY FINGERPRINTS-----\n"
    scan = lambda ip: f"{ip} {KEY}\n"  # noqa: E731 - never the network, in a test
    aws = AwsRig(Aws(gate, ec2, iam), rig, scan=scan, sleep=lambda s: None)
    out = tmp_path / ("dry" if dry else f"run{len(events)}")
    out.mkdir(exist_ok=True)
    setup = Setup(rig, gate, aws, FakeReleases(rig, events),
                  zulip_clients=lambda: (zulip, FakeZulip(12)), server=server, out=out)  # fmt: skip
    return setup, gate, lines, out


def test_the_dry_run_reads_and_plans_but_changes_nothing(tmp_path):
    rig = inputs(tmp_path)
    ec2, iam, events = FakeEc2(), FakeIam(), []
    zulip = FakeZulip(11, users=[{"user_id": 11, "email": "t@zulip.example"}])
    setup, gate, lines, out = make_setup(tmp_path, rig, dry=True, ec2=ec2, iam=iam, zulip=zulip,
                                         server=FakeServer(events), events=events)  # fmt: skip
    setup.run(SHA)
    assert ec2.writes() == [] and iam.writes() == [] and zulip.posts() == []
    assert not rig.dir.exists() and events == []  # no key, secret, clone, deploy or ssh
    plan = "\n".join(gate.writes)
    assert "zulip POST bots" in plan and "ec2 run_instances" in plan and "put_role_policy" in plan
    assert "local deploy.sh --bootstrap" in plan
    policy = json.loads((out / "policy" / "zab-admin-1.json").read_text())
    assert any(st["Effect"] == "Deny" for st in policy["Statement"])
    assert any("would write" in line and "admin.toml" in line for line in lines)


def test_a_first_run_bootstraps_and_a_second_upgrades(tmp_path):
    rig = inputs(tmp_path)
    rig.dir.mkdir(parents=True)
    rig.host_key.write_text("ssh-ed25519 AAAA old-server\n")  # a server since replaced
    ec2, iam, events = FakeEc2(), FakeIam(), []
    zulip = FakeZulip(11, users=[{"user_id": 11, "email": "t@zulip.example"}])
    server = FakeServer(events)
    setup, *_ = make_setup(tmp_path, rig, dry=False, ec2=ec2, iam=iam, zulip=zulip,
                           server=server, events=events)  # fmt: skip
    first = setup.run(SHA)
    assert events == [
        "ssh 198.51.100.10", "repo", "tag v0.2.4rc1", "deploy --bootstrap v0.2.4rc1",
        "bot stop", "reset", "bot start", "deploy --wheel v0.2.4rc1",
    ]  # fmt: skip
    assert first.release == "v0.2.4rc1" and first.wheel.endswith("0.2.4rc1-py3-none-any.whl")
    assert (rig.etc / "admin.toml").exists() and (rig.etc / "break-glass" / "deployer.pub").exists()
    assert (rig.secrets / "provisioner.zuliprc").exists()
    assert rig.known_hosts.read_text() == f"198.51.100.10 {KEY}\n"  # from the new console
    events.clear()
    server.now = "v0.2.4rc1"
    for name in ("github-token", "provisioner.zuliprc"):  # the placeholders
        server.installed[name] = server.secret_sha256(name)
    rig.github_token.write_text("github_pat_from_drew\n")  # Q9 answered, for the token only
    second = setup.run(SHA)
    assert events == [
        "ssh 198.51.100.10", "repo", "tag v0.2.4rc1", "tag v0.2.4rc2", "install admin.toml",
        "install allowed_signers", "install backup_recipients", "deploy v0.2.4rc2",
        "install secret github-token", "bot stop", "reset", "bot start",
        "deploy --wheel v0.2.4rc2",
    ]  # fmt: skip
    assert second.github_token is True and first.github_token is False
    assert second.provisioner is False and first.provisioner is False  # no yes from Drew yet
    assert second.server == first.server
    assert [op for op, _ in ec2.calls].count("run_instances") == 1


# ------------------------------------------------------------------------------ the runner


class FakeAws:
    def __init__(self, *, left=(), dry=False, eips=(), stuck=False, enis=()):
        self.calls, self.left, self.dry = [], list(left), dry
        self.eips, self.stuck, self.enis = list(eips), stuck, list(enis)

    def terminate_participants(self):
        self.calls.append("terminate")

    def drop_probe_interfaces(self):
        self.calls.append("eni")

    def drop_throwaway_key_pairs(self):  # A16's (#418)
        self.calls.append("key-pairs")

    def probe_interfaces(self):
        return [{"NetworkInterfaceId": e} for e in self.enis]

    def fleet(self):
        return [{"InstanceId": i} for i in self.left]

    def volumes(self):
        return []

    def volumes_settled(self, *, gone=False):
        return []

    def until(self, read, wanted, **_):  # this EC2 shows every change at once
        return read()

    def addresses(self):
        return [{"AllocationId": a} for a in self.eips]

    def server(self):
        stopped = "stop" in self.calls and not self.stuck
        return {"InstanceId": "i-s", "State": {"Name": "stopped" if stopped else "running"}}

    def stop(self, inst):
        self.calls.append("stop")


class FakeSetup:
    def __init__(self, fail=False):
        self.fail, self.releases = fail, self

    def commit_of(self, rev):
        return SHA

    def run(self, sha):
        if self.fail:
            raise RuntimeError("deploy.sh v0.2.4rc1: refused")
        from e2e.admin.harness.setup import Rigged

        return Rigged(sha, "v0.2.4rc1", "c" * 40, "i-s", "198.51.100.10", wheel="z.whl")


@pytest.fixture
def env(tmp_path, monkeypatch):
    inputs(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ZAB_LIVE", "admin-e2e")
    for var in ("ZAB_E2E_TESTER_ZULIPRC", "ZAB_E2E_TESTER2_ZULIPRC", "ZAB_ADMIN_E2E_DIR"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def run_main(argv, *, aws, setup, outcome="passed", rc=0, logged="1 passed\n"):
    def pytest_cmd(argv, *, cwd, env, timeout, grace, log):
        log.write_text(logged)  # `command` writes pytest's whole output there (#517)
        rows = [report.ScenarioResult(10, "Deploy gate", outcome, label="A10")]
        report.Report(results=rows).write(Path(env["ZAB_ADMIN_E2E_REPORT_JSON"]))
        assert Path(env["ZAB_ADMIN_E2E_STATE"]).is_file() and env["ZAB_LIVE"] == "admin-e2e"
        return Result(rc, "", "")

    def make(rig, gate, out):
        return admin_run.Parts(gate, aws, setup, pytest=pytest_cmd)

    return admin_run.main(argv, make=make)


def reports(tmp_path):
    return sorted((tmp_path / ".local" / "state" / "zab-admin-e2e").glob("*/report.json"))


def test_a_passing_run_tears_down_and_reports(env):
    aws = FakeAws()
    assert run_main(["--scenarios", "A10"], aws=aws, setup=FakeSetup()) == 0
    assert aws.calls == ["terminate", "eni", "key-pairs", "stop"]
    (path,) = reports(env)
    assert path.parent.name.endswith(SHA[:8])
    done = report.Report.read(path)
    assert done.verdict == "PASS" and done.title == "Admin end-to-end"
    assert done.vm == {"server": "i-s", "ip": "198.51.100.10", "release": "v0.2.4rc1"}
    assert (path.parent / "summary.md").read_text().startswith("## Admin end-to-end: PASS")
    assert (path.parent / "pytest.log").read_text() == "1 passed\n"


def test_a_rig_secret_in_pytests_output_is_redacted_and_a_run_problem(env):
    """Reviewer on #518: a traceback prints its frames' arguments; A12 scans the run's files
    from inside pytest, before pytest.log exists, so the runner sweeps it afterwards."""
    secrets = rigs.load({"HOME": str(env)}).secrets
    secrets.mkdir(parents=True)
    (secrets / "github-token").write_text("github_pat_DECOY123\n")
    logged = "E   input = 'token=github_pat_DECOY123'\n"
    assert run_main(["--scenarios", "A10"], aws=FakeAws(), setup=FakeSetup(), logged=logged) == 1
    (path,) = reports(env)
    assert report.Report.read(path).problems == [
        "pytest.log held rig secrets, now redacted: github-token"
    ]
    assert (path.parent / "pytest.log").read_text() == "E   input = 'token=<github-token>'\n"


class StagingSetup(FakeSetup):
    """A setup that reads the rig's inputs as `Setup.run` does after Zulip: `stage_secrets`."""

    def run(self, sha):
        stage_secrets(Gate(dry_run=False), rigs.load(), "https://z.example",
                      frozenset({"t@zulip.example"}))  # fmt: skip
        return super().run(sha)


DECOY = "DECOY-SECRET-519"  # a decoy, never a real key


@pytest.mark.parametrize("dry", [False, True])
def test_a_malformed_secrets_file_is_named_never_quoted(env, capsys, dry):
    """#519: configparser quotes the offending line in its message, and the run problems went
    to summary.md, which is posted on PRs. Now they name the file (its path in the rig's
    directory) and the exception's type: setup's parse of the placed provisioner zuliprc, and
    the pytest.log sweep's of the rig's secrets."""
    rig = rigs.load({"HOME": str(env)})
    rig.dir.mkdir(parents=True)
    rig.provisioner_zuliprc.write_text(f"key={DECOY}\n")  # no [api] header
    (rig.secrets / "bots").mkdir(parents=True)
    (rig.secrets / "bots" / "zab-admin-e2e.zuliprc").write_text(f"[api]\n{DECOY}\n")  # no `=`
    argv = ["--scenarios", "A10", *(["--dry-run"] if dry else [])]
    assert run_main(argv, aws=FakeAws(), setup=StagingSetup()) == 1
    problems = [
        "aborted: SecretFileError: provisioner.zuliprc: MissingSectionHeaderError",
        "pytest.log not swept for secrets: SecretFileError: "
        "secrets/bots/zab-admin-e2e.zuliprc: ParsingError",
    ]
    printed = capsys.readouterr()
    assert DECOY not in printed.out + printed.err
    if dry:  # printed, not reported
        assert [f"admin-e2e: {p}" for p in problems] == printed.out.splitlines()[-2:]
        return
    (path,) = reports(env)
    assert report.Report.read(path).problems == problems
    summary = (path.parent / "summary.md").read_text()
    assert all(f"**Run problem:** {p}" in summary for p in problems)
    assert DECOY not in path.read_text() + summary


def test_a_staged_secret_that_cant_be_decoded_is_named_never_quoted(tmp_path):
    """Reviewer on #522: `_sync_secrets` reads the staged secrets through `read_secret` too."""
    rig = inputs(tmp_path)
    rig.secrets.mkdir(parents=True)
    (rig.secrets / "github-token").write_bytes(b"\xff" + DECOY.encode())  # not UTF-8
    (rig.secrets / "provisioner.zuliprc").write_text("[api]\n")
    gate = Gate(dry_run=False, out=lambda line: None)
    setup = Setup(rig, gate, None, None, zulip_clients=None, out=tmp_path)
    events: list[str] = []
    server = FakeServer(events)("198.51.100.10", rig)
    server.installed["github-token"] = "0" * 64  # the server's differs: it is installed
    with pytest.raises(SecretFileError) as caught:
        setup._sync_secrets(server)
    assert str(caught.value) == "secrets/github-token: UnicodeDecodeError"
    assert events == ["ssh 198.51.100.10"]  # nothing installed


def test_a_failed_setup_or_scenario_still_tears_down(env):
    aws = FakeAws()
    assert run_main(["--scenarios", "A10"], aws=aws, setup=FakeSetup(fail=True)) == 1
    assert aws.calls == ["terminate", "eni", "key-pairs", "stop"]
    assert any("aborted: RuntimeError" in p for p in report.Report.read(reports(env)[0]).problems)
    aws = FakeAws()
    assert run_main(["--scenarios", "A10"], aws=aws, setup=FakeSetup(), outcome="failed",
                    rc=1) == 1  # fmt: skip
    assert aws.calls == ["terminate", "eni", "key-pairs", "stop"]


def test_the_teardown_check_fails_a_run_that_left_a_participant(env):
    aws = FakeAws(left=["i-left"])
    assert run_main(["--scenarios", "A10"], aws=aws, setup=FakeSetup()) == 1
    problems = report.Report.read(reports(env)[0]).problems
    assert problems == ["teardown check: instances not terminated: i-left"]


def test_the_teardown_check_fails_a_run_that_left_a13s_probe_interface(env):
    aws = FakeAws(enis=["eni-left"])
    assert run_main(["--scenarios", "A10"], aws=aws, setup=FakeSetup()) == 1
    problems = report.Report.read(reports(env)[0]).problems
    assert problems == ["teardown check: A13's probe interface not deleted: eni-left"]


def test_the_teardown_check_fails_a_kept_address_or_a_running_server(env):
    aws = FakeAws(eips=["eipalloc-1"], stuck=True)
    assert run_main(["--scenarios", "A10"], aws=aws, setup=FakeSetup()) == 1
    problems = report.Report.read(reports(env)[0]).problems
    assert problems == [
        "teardown check: Elastic IPs not released: eipalloc-1",
        "teardown check: the server is running, not stopped",
    ]
    aws = FakeAws(stuck=True)
    assert run_main(["--scenarios", "A10", "--keep-server"], aws=aws, setup=FakeSetup()) == 0
    assert aws.calls == ["terminate", "eni", "key-pairs"]  # asked to keep it running: not a problem


def test_a_scenario_without_a_row_fails(env):
    assert run_main(["--scenarios", "A10,A12"], aws=FakeAws(), setup=FakeSetup()) == 1
    rows = report.Report.read(reports(env)[0]).results
    assert [(r.shown, r.outcome, r.reason) for r in rows] == [
        ("A10", "passed", ""), ("A12", "failed", "no result")
    ]  # fmt: skip


def test_the_dry_run_and_the_guards(env, capsys):
    aws = FakeAws(dry=True)
    assert run_main(["--dry-run"], aws=aws, setup=FakeSetup()) == 0
    assert aws.calls == ["terminate", "eni", "key-pairs", "stop"]
    assert reports(env) == []  # printed, not made
    assert "dry run" in capsys.readouterr().out
    assert run_main(["--scenarios", "A99"], aws=aws, setup=FakeSetup()) == 2
    import os

    os.environ.pop("ZAB_LIVE")
    assert run_main([], aws=aws, setup=FakeSetup()) == 2


def test_a_renderer_that_grants_everything_is_refused_before_any_iam_write(tmp_path, monkeypatch):
    """Review of #363, item 1: whatever `--commit` renders, the document put is checked."""
    rig = inputs(tmp_path)
    ec2, iam, events = FakeEc2(), FakeIam(), []
    zulip = FakeZulip(11, users=[{"user_id": 11, "email": "t@zulip.example"}])
    setup, *_ = make_setup(tmp_path, rig, dry=False, ec2=ec2, iam=iam, zulip=zulip,
                           server=FakeServer(events), events=events)  # fmt: skip
    everything = {"Version": "2012-10-17",
                  "Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}]}  # fmt: skip
    monkeypatch.setattr(Setup, "_policies", lambda self, commit, toml: [everything])
    with pytest.raises(NotTestInfrastructure):
        setup.run(SHA)
    assert iam.writes() == [] and "run_instances" not in ec2.writes() and events == []


def test_a_half_made_server_is_resumed(tmp_path):
    """The first live --setup-only died right after the launch: the next run finds the server
    running, never bootstrapped, its host key unpinned. It pins the key from the first boot's
    console, launches nothing, and bootstraps it."""
    rig = inputs(tmp_path)
    ec2, iam, events = FakeEc2(), FakeIam(), []
    ec2.add_instance("i-half", dict(rigs.SERVER_TAGS))  # running, from the aborted run
    zulip = FakeZulip(11, users=[{"user_id": 11, "email": "t@zulip.example"}])
    setup, *_ = make_setup(tmp_path, rig, dry=False, ec2=ec2, iam=iam, zulip=zulip,
                           server=FakeServer(events), events=events)  # fmt: skip
    rigged = setup.run(SHA)
    assert rigged.server == "i-half" and "run_instances" not in ec2.writes()
    assert "deploy --bootstrap v0.2.4rc1" in events
    assert rig.known_hosts.read_text() == f"198.51.100.9 {KEY}\n"  # from its console
