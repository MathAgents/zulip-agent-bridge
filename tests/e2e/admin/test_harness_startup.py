"""The admin rig's start-up (#804, as #690 for the participant runner): the server's SSH must
answer steadily before deploy.sh runs, and a deploy.sh that only failed to connect runs again.
Offline: the setup with fakes on one clock, and `release.connect_failed` on what the real
deploy.sh prints when its ssh can't connect (the deploy tests' rig, `tests/workshop/`)."""

from __future__ import annotations

import datetime as dt

import pytest

from e2e.admin.fakes import FakeEc2, FakeIam, FakeZulip
from e2e.admin.harness.aws_rig import AwsRig, fingerprint
from e2e.admin.harness.gate import Aws, Gate
from e2e.admin.harness.release import Deployed, connect_failed
from e2e.admin.harness.setup import Setup
from e2e.admin.test_harness_aws import KEY
from e2e.admin.test_harness_setup import SHA, FakeReleases, FakeServer, inputs
from e2e.harness import report, startup
from e2e.harness.startup import Timeline
from e2e.harness.vm import CONNECT_TIMEOUT_S, Result
from workshop.test_adminserver_deploy import FAKE_SSH, _tools_missing
from workshop.test_adminserver_deploy import bootstrapped as bootstrapped
from workshop.test_adminserver_deploy import index as index
from workshop.test_adminserver_deploy import server as server

IP = "198.51.100.10"  # the server FakeEc2 launches
TIMED_OUT = f"ssh: connect to host {IP} port 22: Connection timed out"
PROMPT = "Type v0.2.4rc1 to deploy it to zab-test-admin: "
#: 13:54:56Z, Oct 28, as #690's replay: the server's clock starts there
LAUNCHED = dt.datetime(2026, 10, 28, 13, 54, 56, tzinfo=dt.UTC).timestamp()


class Booting(FakeServer):
    """The server on a clock from its launch: a connection made while `lost(t)` gets no answer
    (ssh gives up after its ConnectTimeout); else a probe takes 0.5 s and a command 1 s."""

    def __init__(self, events, lost=lambda t: False) -> None:
        super().__init__(events)
        self.t, self.lost, self.answers = 0.0, lost, []

    def probe(self):
        at, lost = self.t, self.lost(self.t)
        self.t += CONNECT_TIMEOUT_S if lost else 0.5
        self.answers.append((at, not lost))
        return Result(255, "", TIMED_OUT + "\n") if lost else Result(0, "", "")

    def run(self, argv, check=True, timeout=None):
        self.t += 1.0
        return super().run(argv, check=check, timeout=timeout)

    def sleep(self, seconds):
        self.t += seconds


class Deploys(FakeReleases):
    """deploy.sh on the server's clock: while it's `lost`, its first `remote` call times out
    and it dies `unreachable`, as the real one does (`test_connect_failed_reads_the_real_…`);
    else it takes 5 s. `said`: the failures it prints instead, in turn, one per run."""

    def __init__(self, rig, events, booting: Booting, said=()) -> None:
        super().__init__(rig, events)
        self.booting, self.said, self.at = booting, list(said), []

    def deploy(self, script, version, *flags, out, log=None, confirm=None):
        super().deploy(script, version, *flags, out=out, log=log, confirm=confirm)
        self.at.append(self.booting.t)
        if self.said:
            self.booting.t += CONNECT_TIMEOUT_S
            code, stderr = self.said.pop(0)
            return Deployed(Result(code, "", stderr), "")
        if self.booting.lost(self.booting.t):
            self.booting.t += CONNECT_TIMEOUT_S
            return Deployed(Result(1, "", f"{TIMED_OUT}\n{DEATH}unreachable\n"), "")
        self.booting.t += 5.0
        return Deployed(Result(0, "", ""), "d" * 64)


DEATH = "deploy.sh: zab-test-admin: "


def booting_setup(tmp_path, booting: Booting, said=()):
    """A first run's setup, on the server's clock: EC2 shows each change at once."""
    rig, events = inputs(tmp_path), booting.events
    gate = Gate(dry_run=False, out=lambda line: None)
    ec2 = FakeEc2()
    ec2.console = f"-----BEGIN SSH HOST KEY FINGERPRINTS-----\n256 {fingerprint(KEY)} r (ED25519)\n"
    ec2.console += "-----END SSH HOST KEY FINGERPRINTS-----\n"
    aws = AwsRig(Aws(gate, ec2, FakeIam()), rig, scan=lambda ip: f"{ip} {KEY}\n",
                 sleep=booting.sleep, clock=lambda: booting.t)  # fmt: skip
    timeline = Timeline(lambda: booting.t, lambda: LAUNCHED + booting.t, lambda line: None)
    releases = Deploys(rig, events, booting, said)
    zulip = FakeZulip(11, users=[{"user_id": 11, "email": "t@zulip.example"}])
    setup = Setup(rig, gate, aws, releases, zulip_clients=lambda: (zulip, FakeZulip(12)),
                  server=booting, out=tmp_path, timeline=timeline)  # fmt: skip
    return setup, releases


def test_the_server_must_answer_steadily_before_deploy_sh(tmp_path):
    """#804: the setup took the first answer for SSH being up, as #690's runner did, and
    deploy.sh, which never retries a connect, came next. Now three answers in a row, 10 s
    apart, with cloud-init waited for after the first: a lost spell right after that answer
    costs probes, not the run, and the Start-up says when each step began. (The server
    launched reads `running` at once, and again CONFIRM_S later: `AwsRig.start`.)"""
    booting = Booting([], lost=lambda t: 31.0 <= t < 50.0)
    setup, releases = booting_setup(tmp_path, booting)
    setup.run(SHA)
    assert booting.answers == [(30.0, True), (41.5, False), (61.5, True), (72.0, True),
                               (82.5, True)]  # fmt: skip
    assert releases.at == [83.0, 88.0]  # the bootstrap, then --wheel, after the third answer
    assert setup.timeline.lines == [
        "13:54:56.0Z +0.0 s: EC2 running",
        f"13:55:26.0Z +30.0 s: EC2 running at {IP}",
        "13:55:26.0Z +30.0 s: SSH answered in 0.5 s (1 of 3)",
        "13:55:26.5Z +30.5 s: cloud-init status: done (exit 0), after 1.0 s",
        f"13:55:37.5Z +41.5 s: SSH failed after 15.0 s: {TIMED_OUT}",
        "13:55:57.5Z +61.5 s: SSH answered in 0.5 s (1 of 3)",
        "13:56:08.0Z +72.0 s: SSH answered in 0.5 s (2 of 3)",
        "13:56:18.5Z +82.5 s: SSH answered in 0.5 s (3 of 3)",
    ]


@pytest.mark.parametrize(
    "lost",
    [lambda t: True, lambda t: (t // 20) % 2 == 1],  # never answers; keeps dropping out
    ids=["never", "flapping"],
)
def test_a_server_that_never_settles_ends_the_setup(tmp_path, lost):
    booting = Booting([], lost)
    setup, releases = booting_setup(tmp_path, booting)
    with pytest.raises(TimeoutError, match=f"{IP}: SSH not steady 600 s after the start"):
        setup.run(SHA)
    first = booting.answers[0][0]  # the first probe, once EC2 showed the server running
    assert booting.t <= first + startup.SSH_WAIT_S + CONNECT_TIMEOUT_S + 1  # one probe past
    assert releases.at == []  # deploy.sh never ran
    answers = booting.answers
    assert not any(all(ok for _, ok in answers[i : i + 3]) for i in range(len(answers) - 2))


#: What deploy.sh prints when its ssh never connects, at each of its `remote` calls (the real
#: one's, `test_connect_failed_reads_the_real_deploy_sh`): (exit, stderr)
CONNECTS = {
    "what the server runs": (1, f"{TIMED_OUT}\n{DEATH}unreachable\n"),
    "the temporary directory": (1, f"{PROMPT}{TIMED_OUT}\n{DEATH}no temporary directory\n"),
    "the copy": (1, f"{PROMPT}{TIMED_OUT}\n{DEATH}could not copy the release\n{TIMED_OUT}\n"),
    "the server half": (255, f"{PROMPT}{TIMED_OUT}\n"),
    "the server half, then its rm": (255, f"{PROMPT}{TIMED_OUT}\n{TIMED_OUT}\n"),
    "refused": (1, f"{TIMED_OUT.replace('timed out', 'refused')}\n{DEATH}unreachable\n"),
}


@pytest.mark.parametrize("fails", [1, 2, 3])
@pytest.mark.parametrize("said", list(CONNECTS))
def test_a_deploy_sh_that_could_not_connect_runs_again(tmp_path, said, fails):
    """#804: deploy.sh's `remote()` never retries a connect; one that failed only there never
    ran its server half, so the setup runs it again, 15 then 30 s later, 3 tries in all."""
    booting = Booting([])
    setup, releases = booting_setup(tmp_path, booting, said=[CONNECTS[said]] * fails)
    if fails == 3:
        with pytest.raises(RuntimeError, match=r"deploy\.sh v0\.2\.4rc1: "):
            setup.run(SHA)
    else:
        setup.run(SHA)
    bootstraps = [e for e in booting.events if e.startswith("deploy --bootstrap")]
    assert len(bootstraps) == fails + (fails < 3)
    gaps = [b - a for a, b in zip(releases.at, releases.at[1:], strict=False)][:fails]
    assert gaps[: min(fails, 2)] == [15.0 + 15.0, 15.0 + 30.0][: min(fails, 2)]  # ssh's, then
    notes = [line for line in setup.timeline.lines if "deploy.sh --bootstrap" in line]
    cause = TIMED_OUT if said != "refused" else TIMED_OUT.replace("timed out", "refused")
    ends = ["deploy.sh again in 15 s", "deploy.sh again in 30 s", "no tries left"][:fails]
    assert [line.split(": ", 1)[1] for line in notes] == [
        f"deploy.sh --bootstrap v0.2.4rc1 failed after 15.0 s: {cause}; {end}" for end in ends
    ]


SESSION_CUT = f"Connection to {IP} closed by remote host."
OTHERS = {
    "an authentication refused": (
        1,
        f"ubuntu@{IP}: Permission denied (publickey).\n{DEATH}unreachable\n",
    ),
    "another host's connect": (1, f"{TIMED_OUT.replace(IP, '203.0.113.9')}\n{DEATH}unreachable\n"),
    "a refusal of the server half": (1, f"{PROMPT}{DEATH}the release tree differs\n"),
    "a session cut short": (255, f"{PROMPT}{SESSION_CUT}\n"),
    "the connect, then a line before the die": (
        1,
        f"{PROMPT}{TIMED_OUT}\ntar: write error\n{DEATH}could not copy the release\n",
    ),
    "another death after the connect": (1, f"{TIMED_OUT}\n{DEATH}runs no release\n"),
    "the connect, but not ssh's exit": (1, f"{PROMPT}{TIMED_OUT}\n"),
    "deploy.sh timed out": (124, f"{PROMPT}\ntimed out"),
    "a refused confirmation": (1, f"{PROMPT}{DEATH}not confirmed\n"),
    # #826's review, N1: the server half ran and was cut, then the trap's `rm` couldn't connect
    "a session cut short, then the rm's connect": (
        255,
        f"{PROMPT}installing v0.2.4rc1\n{SESSION_CUT}\n{TIMED_OUT}\n",
    ),
}


@pytest.mark.parametrize("said", list(OTHERS))
def test_any_other_deploy_sh_failure_ends_the_setup_at_once(tmp_path, said):
    booting = Booting([])
    setup, releases = booting_setup(tmp_path, booting, said=[OTHERS[said]])
    code, stderr = OTHERS[said]
    assert connect_failed(Result(code, "", stderr), IP) == ""
    with pytest.raises(RuntimeError, match=r"deploy\.sh v0\.2\.4rc1: "):
        setup.run(SHA)
    assert len(releases.at) == 1 and not any("deploy.sh --" in x for x in setup.timeline.lines)


def test_the_admin_report_has_the_start_up(tmp_path, monkeypatch):
    """The admin suite's report shows the setup's timeline under *Start-up*, as #690's does."""
    from e2e.admin.test_harness_setup import FakeAws, FakeSetup, reports, run_main

    inputs(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ZAB_LIVE", "admin-e2e")
    for var in ("ZAB_E2E_TESTER_ZULIPRC", "ZAB_E2E_TESTER2_ZULIPRC", "ZAB_ADMIN_E2E_DIR"):
        monkeypatch.delenv(var, raising=False)
    setup = FakeSetup()
    setup.timeline = Timeline(log=lambda line: None)
    setup.timeline.lines += ["13:54:56.0Z +0.0 s: EC2 stopped", "13:55:38.5Z +42.5 s: SSH …"]
    assert run_main(["--scenarios", "A10"], aws=FakeAws(), setup=setup) == 0
    (path,) = reports(tmp_path)
    summary = path.with_name("summary.md").read_text()
    assert summary.endswith(
        "\n### Start-up\n\n- 13:54:56.0Z +0.0 s: EC2 stopped\n- 13:55:38.5Z +42.5 s: SSH …\n"
    )
    assert report.Report.read(path).boot == setup.timeline.lines


# --------------------------------------------- `connect_failed` on the real deploy.sh's stderr

#: ssh for the deploy tests' rig that loses its connections: from the first call whose
#: command holds $ZAB_TEST_LOSE, that call and every later one fail as ssh does when no SYN
#: is answered (only that call, with $ZAB_TEST_ONCE); with $ZAB_TEST_SAID, that first call
#: says that line instead.
LOSING_SSH = FAKE_SSH.replace(
    'exec sh -c "$1"',
    'if [ -e "$ZAB_TEST_MARK" ]; then\n'
    f'  echo "{TIMED_OUT}" >&2\n'
    "  exit 255\nfi\n"
    'case $1 in *"$ZAB_TEST_LOSE"*)\n'
    '  [ -n "$ZAB_TEST_ONCE" ] || : > "$ZAB_TEST_MARK"\n'
    f'  echo "${{ZAB_TEST_SAID:-{TIMED_OUT}}}" >&2\n'
    "  exit 255 ;;\nesac\n"
    'exec sh -c "$1"',
)

#: (the command its ssh first loses, only that one, what ssh says instead of the timeout,
#: how deploy.sh ends: its `die`'s reason or its exit): whether deploy.sh runs again
AUTH = f"ubuntu@{IP}: Permission denied (publickey)."
REAL = {
    "the check of what it runs": ("readlink", False, "", "unreachable", True),
    "its temporary directory": ("mktemp", False, "", "no temporary directory", True),
    "its copy": ("tar --warning", False, "", "could not copy the release", True),
    "its server half": ("sudo bash", False, "", 255, True),
    "its server half alone": ("sudo bash", True, "", 255, True),
    "an authentication refused": ("readlink", False, AUTH, "unreachable", False),
    "a session cut short": ("sudo bash", True, SESSION_CUT, 255, False),
    "a session cut short, then the rm's connect": ("sudo bash", False, SESSION_CUT, 255, False),
}


@pytest.mark.skipif(bool(_tools_missing()), reason=_tools_missing() or "ok")
@pytest.mark.parametrize("case", list(REAL))
def test_connect_failed_reads_the_real_deploy_sh(server, case):
    """The real deploy.sh, upgrading the deploy tests' server, with an ssh that loses its
    connections from one of its `remote` calls on: `connect_failed` names ssh's line exactly
    when only a connect failed, whatever the server half's prompt or the exit trap's `rm`
    print around it."""
    lose, once, said, ends, again = REAL[case]
    losing = server.tmp / "bin" / "ssh-losing"
    losing.write_text(LOSING_SSH)
    losing.chmod(0o755)
    server.env |= {"ZAB_DEPLOY_SSH": str(losing), "ZAB_TEST_LOSE": lose,
                   "ZAB_TEST_MARK": str(server.tmp / "lost"), "ZAB_TEST_ONCE": "1" * once,
                   "ZAB_TEST_SAID": said}  # fmt: skip
    server.release("v0.3.1")
    done = server.deploy("v0.3.1")
    result = Result(done.returncode, done.stdout, done.stderr)
    assert not result.ok and server.current().endswith("v0.3.0"), done.stderr
    if isinstance(ends, int):  # ssh's own exit: the server half's connection
        assert done.returncode == ends and "deploy.sh: " not in done.stderr, done.stderr
    else:
        assert f"deploy.sh: admin.test: {ends}\n" in done.stderr, done.stderr
    assert connect_failed(result, IP, host="admin.test") == (TIMED_OUT if again else ""), (
        done.stderr
    )


def test_both_suites_wait_the_same_way():
    """One wait for both suites (#804): the participant runner's and the setup's are the
    shared `startup.steady_ssh`, with the same pauses before a deploy runs again."""
    from e2e import run
    from e2e.admin.harness import setup

    assert run.steady_ssh is setup.steady_ssh is startup.steady_ssh
    assert run.DEPLOY_PAUSES_S is setup.DEPLOY_PAUSES_S is startup.DEPLOY_PAUSES_S


#: a payload directory deploy.sh left on the server (its `mktemp -d` pattern)
LEFT = "/tmp/zab-deploy.AbCdEf1234"  # noqa: S108 - the server's, never opened here


def test_the_server_sweeps_what_deploy_sh_left_in_tmp():
    """#826's review, N2: a deploy.sh that copied its payload and then couldn't connect may
    leave it in the server's /tmp, a bootstrap's with the rig's secrets. The sweep removes, as
    root, `ubuntu`'s directories of deploy.sh's `mktemp` pattern at the top of /tmp only, and
    names them."""
    from pathlib import Path
    from types import SimpleNamespace

    from e2e.admin.harness.server import Server

    sent = []

    def runner(argv, timeout, input=None):
        sent.append(argv[-1])
        return Result(0, LEFT + "\n", "")

    rig = SimpleNamespace(ssh_key=Path("/k/key.pem"), known_hosts=Path("/k/kh"))
    assert Server(IP, rig, runner=runner).sweep_deploy_dirs() == [LEFT]
    assert sent == [
        "sudo find /tmp -maxdepth 1 -type d -user ubuntu -name 'zab-deploy.??????????' -print "
        "-exec rm -rf -- '{}' +"
    ]


class Littered(Booting):
    """A server where an earlier run's deploy.sh left its payload in /tmp."""

    def __init__(self, events) -> None:
        super().__init__(events)
        self.left = [LEFT]

    def sweep_deploy_dirs(self):
        super().sweep_deploy_dirs()
        gone, self.left = self.left, []
        return gone


def test_the_setup_sweeps_once_ssh_is_steady_and_after_its_deploys(tmp_path):
    """The sweep runs at the setup's first connection that succeeds, before any deploy.sh, and
    again after them, for what this run's tries that couldn't connect left; a removal is a
    line of the Start-up."""
    booting = Littered([])
    setup, _ = booting_setup(tmp_path, booting, said=[CONNECTS["the copy"]])
    setup.run(SHA)
    sweeps = [i for i, e in enumerate(booting.events) if e == "sweep"]
    deploys = [i for i, e in enumerate(booting.events) if e.startswith("deploy ")]
    assert len(sweeps) == 2 and sweeps[0] < deploys[0] and sweeps[1] > deploys[-1]
    assert [line.split(": ", 1)[1] for line in setup.timeline.lines if "removed" in line] == [
        f"removed what deploy.sh left: {LEFT}"
    ]
