"""The e2e runner and the scenario recorder, offline with fakes (issue #263)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from e2e import run
from e2e.harness import config, report, startup
from e2e.harness.vm import CONNECT_TIMEOUT_S, Result


class FakeEc2:
    def __init__(self, state: str = "stopped") -> None:
        self.state_now, self.calls = state, []

    def find(self, tags):
        self.calls.append(("find", tags))
        return {"InstanceId": "i-test"}

    def state(self, iid):
        return self.state_now

    def start(self, iid):
        self.calls.append(("start", iid))
        self.state_now = "running"

    def stop(self, iid):
        self.calls.append(("stop", iid))

    def wait_state(self, iid, target, timeout):
        assert self.state_now == target

    def public_ip(self, iid):
        return "198.51.100.4"


class Commands:
    """Plays git, uv, push_config and pytest; pytest writes a one-scenario report."""

    def __init__(
        self,
        deploy_rc: int = 0,
        outcome: str = "passed",
        pytest_rc: int = 0,
        problems: tuple[str, ...] = (),
        idle_fails: int = 0,
        deploy_fails: tuple[str, ...] = (),
    ) -> None:
        self.argvs: list[list[str]] = []
        self.envs: list[dict] = []
        self.deploy_rc, self.outcome, self.pytest_rc = deploy_rc, outcome, pytest_rc
        self.problems, self.idle_fails = list(problems), idle_fails
        self.deploy_fails = list(deploy_fails)  # push_config's failures cells, in turn (#690)

    def __call__(self, argv, *, cwd=None, env=None, timeout=0, grace=0, log=None):
        self.argvs.append(argv)
        self.envs.append(dict(env or {}))
        if argv[:1] == ["git"] and "rev-parse" in argv:
            return Result(0, SHA + "\n", "")
        if argv[:2] == ["uv", "build"]:
            out = Path(argv[argv.index("--out-dir") + 1])
            out.mkdir(parents=True)
            (out / "zulip_agent_bridge-0.2.2-py3-none-any.whl").write_bytes(b"w")
        if "push_config.py" in " ".join(argv):
            if argv[-2:] == ["--idle-stop", "0"] and self.idle_fails:
                self.idle_fails -= 1
                return Result(1, IDLE_FAILED, "\n1 VM(s) failed\n")
            if "--wheel" in argv and self.deploy_fails:  # the deploy, not `--idle-stop`
                return Result(1, table(self.deploy_fails.pop(0)), "\n1 VM(s) failed\n")
            return Result(self.deploy_rc, "1 VM(s) ok\n", "")
        if "pytest" in argv:
            rows = [report.ScenarioResult(1, "Basics, both agents", outcome=self.outcome)]
            ran = report.Report(results=rows, problems=self.problems)
            ran.write(Path(env["ZAB_E2E_REPORT_JSON"]))
            if log:  # `command` writes the whole output there
                log.parent.mkdir(parents=True, exist_ok=True)
                log.write_text("…\n1 passed in 9.00s\n")
            return Result(self.pytest_rc, "", "")
        return Result(0, "", "")


#: push_config's table for a failed `--idle-stop 0` (`render`, steps only).
IDLE_FAILED = """participant  host          steps  doctor  failures
-----------  ------------  -----  ------  ---------------------------------------
zab-test     198.51.100.4  FAIL   -       idle stop off: ssh: Connection refused
"""


SHA = "0123456789abcdef" * 2 + "01234567"  # what the fake git answers
IP = "198.51.100.4"  # the test VM's, in the roster and EC2
#: ssh's line for #690's failed connect, as push_config's table keeps it (`first_line`)
TIMED_OUT = f"ssh: connect to host {IP} port 22: Connection timed out"


def table(failures: str) -> str:
    """push_config's table for a failed install (`render`) whose row's failures are these."""
    return (
        "participant  host          install  doctor  failures\n"
        "-----------  ------------  -------  ------  --------\n"
        f"zab-test     {IP}  FAIL     ?       {failures}\n"
    )


class FakeVm:
    """The VM's build record (`/var/lib/zab-e2e/build`), the clean-up steps, a bridge PID."""

    def __init__(self, build: str = "", probes: tuple[Result, ...] = ()) -> None:
        self.build, self.calls, self.cleaned = build, [], []
        self.probes = list(probes)  # what the SSH probes get, in turn; then answers

    def probe(self):
        return self.probes.pop(0) if self.probes else Result(0, "", "")

    def run(self, argv, check=True, timeout=None):
        self.calls.append(argv)
        if argv[:2] == ["sudo", "rm"]:
            self.build = ""
        if argv[0] == "cat":
            return Result(0 if self.build else 1, self.build, "")
        if argv[0] == "cloud-init":
            return Result(0, "...\nstatus: done\n", "")
        return Result(0, "", "")

    def script(self, literal, *args, sudo=False):
        if literal == run.UNBLOCK:
            self.cleaned.append("unblock")
        else:
            self.build = f"{args[0]} {args[1]}\n"
        return Result(0, "", "")

    def unit(self):
        return {"MainPID": "4242"}

    def signal(self, pid, name):
        self.cleaned.append(f"{name} {pid}")


def make(ec2, cmds, vm=None, slept=None):
    vm, slept = vm or FakeVm(), [] if slept is None else slept
    return lambda cfg: run.Run(cfg, ec2, run=cmds, vm_factory=lambda ip: vm, sleep=slept.append)


#: The pauses between the steady probes of a VM that answers each (#690)
STEADY = [startup.STEADY_GAP_S] * (startup.STEADY_PROBES - 1)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    home = tmp_path / "home"
    for name in ("test-roster.csv", "tester.zuliprc", "tester2.zuliprc", "test-known_hosts"):
        (home / ".config" / "zab").mkdir(parents=True, exist_ok=True)
        (home / ".config" / "zab" / name).write_text("x")
    (home / ".config" / "zab" / "test-roster.csv").write_text("slug,ip\nzab-test,198.51.100.4\n")
    (home / ".ssh").mkdir()
    (home / ".ssh" / "id_ed25519").write_text("x")
    for var in [v for v in __import__("os").environ if v.startswith("ZAB_E2E_")]:
        monkeypatch.delenv(var)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ZAB_E2E_OUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("ZAB_E2E_REPORT_DIR", str(tmp_path / "reports"))
    return config.load()


def runs(rig) -> list[Path]:
    """The report directories (not the per-instance lock file)."""
    return [p for p in rig.report_dir.iterdir() if p.is_dir()]


def summary_of(rig) -> str:
    (out,) = runs(rig)
    return (out / "summary.md").read_text()


def test_a_run_starts_builds_deploys_tests_reports_and_stops(rig):
    ec2, cmds, vm = FakeEc2(), Commands(), FakeVm(f"{'f' * 40} old.whl")
    assert run.main(["--scenarios", "1"], make=make(ec2, cmds, vm)) == 0
    assert ec2.calls == [
        ("find", {"participant": "zab-test"}),
        ("start", "i-test"),
        ("stop", "i-test"),  # stopped when the run ends
    ]
    kinds = [a[1] if a[0] != "git" else a[3] for a in cmds.argvs]
    assert kinds == ["rev-parse", "worktree", "build", cmds.argvs[3][1], "-m", "worktree"]
    push = cmds.argvs[3]
    tree = Path(push[1]).parents[2]
    assert push[1].endswith("workshop/admin/push_config.py") and tree.name == "tree"
    assert push[push.index("--only") + 1] == "zab-test" and "--claude-steer" in push
    assert push[push.index("--known-hosts") + 1] == str(rig.known_hosts)
    assert push[push.index("--roster") + 1] == str(rig.roster)
    assert cmds.argvs[5][3:5] == ["worktree", "remove"]  # the tree goes, after the scenarios
    env = cmds.envs[4]
    assert env["ZAB_LIVE"] == "e2e" and env["ZAB_E2E_SCENARIOS"] == "1"
    # the scenarios' push_config (idle stop) is the tested build's too (codex on #285)
    assert env["ZAB_E2E_ADMIN_DIR"] == str(tree / "workshop" / "admin")
    assert cmds.envs[3]["ZAB_SSH_IDENTITY"] == str(rig.ssh_key)  # push_config uses the rig key
    assert vm.build == f"{SHA} zulip_agent_bridge-0.2.2-py3-none-any.whl\n"  # the new record
    (out,) = runs(rig)
    assert out.name.endswith("-01234567")
    assert summary_of(rig).startswith("## End-to-end: PASS")
    assert report.Report.read(out / "report.json").vm["instance"] == "i-test"
    # pytest's whole output, beside the report (#517), and nothing left outside it
    assert (out / "pytest.log").read_text() == "…\n1 passed in 9.00s\n"
    assert sorted(p.name for p in rig.report_dir.iterdir()) == [out.name]


def test_a_failed_deploy_aborts_stops_the_vm_and_forgets_the_build(rig):
    ec2, cmds, vm = FakeEc2("running"), Commands(deploy_rc=1), FakeVm(f"{'f' * 40} old.whl")
    assert run.main([], make=make(ec2, cmds, vm)) == 1
    assert ec2.calls[-1] == ("stop", "i-test") and not any("pytest" in a for a in cmds.argvs)
    assert "aborted: RuntimeError: push_config failed" in summary_of(rig)
    assert vm.build == ""  # what the VM has is unknown now


def test_no_deploy_reports_the_installed_build_and_merges_the_same(rig, tmp_path):
    """Codex on #283: `--no-deploy` credits the build the VM has, never the one asked for."""
    built = f"{SHA} zab.whl\n"
    earlier = report.Report(commit=SHA, wheel="zab.whl", results=[report.ScenarioResult(3, "W")])
    earlier.write(tmp_path / "earlier.json")
    ec2, cmds = FakeEc2("running"), Commands(outcome="failed")
    argv = ["--no-deploy", "--keep-running", "--scenarios", "1"]
    argv += ["--merge-into", str(tmp_path / "earlier.json")]
    assert run.main(argv, make=make(ec2, cmds, FakeVm(built))) == 1
    assert ("stop", "i-test") not in ec2.calls
    kinds = [a[1] if a[0] != "git" else a[3] for a in cmds.argvs]
    assert kinds == ["rev-parse", "worktree", "-m", "worktree"]  # the installed build's tree
    assert cmds.argvs[1][-1] == SHA and "ZAB_E2E_ADMIN_DIR" in cmds.envs[2]
    (out,) = runs(rig)
    assert [r.number for r in report.Report.read(out / "report.json").results] == [1, 3]
    assert run.main(["--scenarios", "99"], make=lambda cfg: None) == 2


def test_no_deploy_refuses_another_or_an_unknown_build(rig, tmp_path, capsys):
    ec2, cmds = FakeEc2("running"), Commands()
    other = FakeVm(f"{'f' * 40} other.whl\n")
    argv = ["--no-deploy", "--scenarios", "1", "--commit", "HEAD"]
    assert run.main(argv, make=make(ec2, cmds, other)) == 1
    assert "the VM has ffffffffffff, not 0123456789ab" in summary_of(rig)
    for path in runs(rig):
        (path / "summary.md").unlink()
        (path / "report.json").unlink()
        path.rmdir()
    earlier = report.Report(commit=SHA, wheel="zab.whl", results=[report.ScenarioResult(3, "W")])
    earlier.write(tmp_path / "earlier.json")
    argv = ["--no-deploy", "--scenarios", "1", "--merge-into", str(tmp_path / "earlier.json")]
    assert run.main(argv, make=make(ec2, cmds, FakeVm())) == 1  # unknown: a run problem
    assert "the installed build is unknown" in summary_of(rig)
    assert "not merged" in capsys.readouterr().err


def test_configured_relative_paths_become_absolute(tmp_path, monkeypatch):
    """Codex on #283: the deploy runs in a temporary worktree and pytest in the checkout, so a
    relative ZAB_E2E_* path is resolved against the directory run.py was started in."""
    monkeypatch.chdir(tmp_path)
    env = {"HOME": str(tmp_path), "ZAB_E2E_ROSTER": "r.csv", "ZAB_E2E_OUT_DIR": "out"}
    cfg = config.load({**env, "ZAB_E2E_KNOWN_HOSTS": "kh", "ZAB_E2E_SSH_KEY": "k.pem"})
    for path in (cfg.roster, cfg.out_dir, cfg.known_hosts, cfg.ssh_key):
        assert path.is_absolute() and path.parent == tmp_path


def test_pytest_gets_the_runs_absolute_paths(rig, tmp_path, monkeypatch):
    """Codex on #283: started outside the checkout with a relative roster, the scenarios'
    pytest (which runs in the checkout and loads the rig again) gets the absolute one."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "r.csv").write_text("slug,ip\nzab-test,198.51.100.4\n")
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("ZAB_E2E_ROSTER", "r.csv")
    ec2, cmds = FakeEc2("running"), Commands()
    assert run.main(["--scenarios", "1"], make=make(ec2, cmds)) == 0
    pytest_env = next(e for a, e in zip(cmds.argvs, cmds.envs, strict=True) if "pytest" in a)
    assert pytest_env["ZAB_E2E_ROSTER"] == str(elsewhere / "r.csv")
    assert pytest_env["ZAB_E2E_KNOWN_HOSTS"] == str(rig.known_hosts)
    assert all(Path(pytest_env[v]).is_absolute() for v in pytest_env if v.endswith("_ZULIPRC"))


@pytest.mark.parametrize(("require", "rc"), [("rc", "1"), ("1", None), ("", None)])
def test_a_release_candidates_run_tells_the_scenarios(rig, monkeypatch, require, rc):
    """#922 (N1, the maintainer's decision): `--require rc` gives pytest `ZAB_E2E_RC=1`, which
    makes part 7's gap a target; any other run leaves the bound hard."""
    monkeypatch.setenv("ZAB_E2E_RC", "1")  # left in the shell: no run but an rc's passes it on
    ec2, cmds = FakeEc2("running"), Commands()
    argv = ["--scenarios", "1", *(["--require", require] if require else [])]
    run.main(argv, make=make(ec2, cmds))
    pytest_env = next(e for a, e in zip(cmds.argvs, cmds.envs, strict=True) if "pytest" in a)
    assert pytest_env.get("ZAB_E2E_RC") == rc


def test_a_skipped_scenario_is_no_result_and_exits_3(rig):
    """Reviewer on #286: the exit code is a merge gate; a requested scenario that skipped (its
    feature not installed) vouches for nothing: NO-RESULT, exit 3, the reason in the summary."""
    ec2, cmds = FakeEc2("running"), Commands(outcome="skipped")
    argv = ["--no-deploy", "--scenarios", "1"]
    assert run.main(argv, make=make(ec2, cmds, FakeVm(f"{SHA} zab.whl"))) == 3
    assert summary_of(rig).startswith("## End-to-end: NO-RESULT (0 passed, 0 failed, 1 skipped)")


@pytest.mark.parametrize("spec,pytest_rc", [("1", 1), ("1,2", 0)])
def test_pytest_failures_and_missing_scenarios_fail_the_run(rig, spec, pytest_rc):
    """Codex on #283: pytest's exit status counts even when the rows passed (a teardown error,
    an interrupt), and a selected scenario without a recorded outcome failed."""
    ec2, cmds = FakeEc2("running"), Commands(pytest_rc=pytest_rc)
    argv = ["--no-deploy", "--scenarios", spec]
    assert run.main(argv, make=make(ec2, cmds, FakeVm(f"{SHA} zab.whl"))) == 1
    text = summary_of(rig)
    if pytest_rc:
        assert "**Run problem:** pytest exited 1" in text
    else:
        assert "| 2 | Catch-up | ❌ FAIL |" in text and "no result" in text


class Interrupting(Commands):
    """pytest is interrupted (SIGTERM reaches the runner while it waits for pytest)."""

    def __call__(self, argv, **kw):
        if "pytest" in argv:
            self.argvs.append(argv)
            self.envs.append(dict(kw.get("env") or {}))
            raise run.Interrupted("SIGTERM")
        return super().__call__(argv, **kw)


def test_an_interrupt_still_cleans_up_and_stops_the_vm(rig):
    """Reviewer on #283: SIGTERM, SIGHUP or Ctrl-C still undo the scenarios' leftovers (the
    Zulip block, a frozen bridge, idle stop) and stop the VM; the report says why."""
    ec2, cmds, vm = FakeEc2("running"), Interrupting(), FakeVm()
    assert run.main(["--scenarios", "11"], make=make(ec2, cmds, vm)) == 1
    assert vm.cleaned == ["unblock", "CONT 4242"]
    pushes = [a for a in cmds.argvs if "push_config.py" in " ".join(a)]
    assert pushes[-1][-2:] == ["--idle-stop", "0"]  # scenario 11 was selected
    assert ec2.calls[-1] == ("stop", "i-test")
    assert "aborted: Interrupted: SIGTERM" in summary_of(rig)


#: How a child that must answer SIGINT waits 30 s: in sleeps of 10 ms, never in one. A Python
#: handler runs only when the eval loop checks for signals, and `time.sleep` makes no check
#: before its syscall: a SIGINT that lands just before it only sets the handler's flag, and the
#: child sleeps on to the end, past the grace (#890).
POLITE_WAIT = "for _ in range(3000):\n    time.sleep(0.01)\n"


def test_wind_down_lets_pytest_finish_then_kills_it(tmp_path):
    """On an interrupt, pytest gets SIGINT and time to run its finally blocks."""
    import subprocess
    import sys
    import time

    polite = "import signal, sys, time\n"
    polite += "signal.signal(signal.SIGINT, lambda *a: (print('cleaned'), sys.exit(0)))\n"
    polite += "print('ready', flush=True)\n" + POLITE_WAIT
    proc = subprocess.Popen([sys.executable, "-c", polite], stdout=subprocess.PIPE,  # noqa: S603
                            stderr=subprocess.PIPE, text=True)  # fmt: skip
    proc.stdout.readline()
    out, err = run._wind_down(proc, 10)
    # -9: the grace ran out before the handler ran; 1: the handler failed (in `err`)
    assert "cleaned" in out and proc.returncode == 0, (proc.returncode, out, err)
    stubborn = "import signal, time\nsignal.signal(signal.SIGINT, signal.SIG_IGN)\n"
    stubborn += "print('ready', flush=True); time.sleep(30)"
    proc = subprocess.Popen([sys.executable, "-c", stubborn], stdout=subprocess.PIPE,  # noqa: S603
                            stderr=subprocess.PIPE, text=True)  # fmt: skip
    proc.stdout.readline()
    run._wind_down(proc, 0.5)
    assert proc.returncode == -9
    # codex on #283: a descendant holding the pipes dies with the group, and nothing hangs
    parent = "import subprocess, time\nsubprocess.Popen(['sleep', '30'])\n"
    parent += "print('ready', flush=True); time.sleep(30)"
    proc = subprocess.Popen([sys.executable, "-c", parent], stdout=subprocess.PIPE,  # noqa: S603
                            stderr=subprocess.PIPE, text=True, start_new_session=True)  # fmt: skip
    proc.stdout.readline()
    start = time.monotonic()
    run._wind_down(proc, 0)
    assert time.monotonic() - start < 5 and proc.returncode == -9


def test_one_run_per_vm_and_a_bad_merge_path(rig, tmp_path):
    import fcntl

    # the lock's place is fixed (~/.local/state/zab-e2e/locks), whatever the report directory
    assert run.lock_dir() == Path.home() / ".local" / "state" / "zab-e2e" / "locks"
    run.lock_dir().mkdir(parents=True)
    with open(run.lock_dir() / "i-test.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert run.main(["--scenarios", "1"], make=make(FakeEc2("running"), Commands())) == 2
    missing = ["--merge-into", str(tmp_path / "nope.json")]
    assert run.main(missing, make=lambda cfg: None) == 2


def test_a_second_interrupt_still_kills_the_program(tmp_path):
    """Reviewer on #283: a second interrupt during the grace window skipped the SIGKILL and
    left the program (pytest) orphaned. Interrupts are ignored while it winds down."""
    import os
    import signal
    import sys
    import threading
    import time

    pid_file = tmp_path / "pid"
    stubborn = "import os, signal, sys, time\nsignal.signal(signal.SIGINT, signal.SIG_IGN)\n"
    stubborn += f"open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(60)"
    previous = signal.signal(signal.SIGTERM, run._interrupt)
    me = os.getpid()

    def interrupt_twice():
        while not pid_file.exists():
            time.sleep(0.05)
        os.kill(me, signal.SIGTERM)  # the first: the runner winds the program down
        time.sleep(0.5)
        os.kill(me, signal.SIGTERM)  # the second, during the grace: ignored

    threading.Thread(target=interrupt_twice, daemon=True).start()
    try:
        with pytest.raises(run.Interrupted):
            run.command([sys.executable, "-c", stubborn], timeout=60, grace=2)
    finally:
        signal.signal(signal.SIGTERM, previous)
    child = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)  # killed and reaped, not orphaned


class StuckEc2(FakeEc2):
    def stop(self, iid):
        raise RuntimeError("UnauthorizedOperation")


def test_a_failed_stop_fails_the_report(rig):
    """Reviewer on #283: a VM left running must show in what gets posted."""
    assert run.main(["--scenarios", "1"], make=make(StuckEc2("running"), Commands())) == 1
    assert "**Run problem:** the VM was not stopped: UnauthorizedOperation" in summary_of(rig)


def test_the_roster_must_point_at_the_tagged_vm(rig):
    """push_config deploys to the roster's address: it must be the tagged test VM's."""
    rig.roster.write_text("slug,ip\nzab-test,203.0.113.99\n")
    cmds, vm = Commands(), FakeVm()
    assert run.main(["--scenarios", "all"], make=make(FakeEc2("running"), cmds, vm)) == 1
    assert "the roster says 203.0.113.99, the test VM is 198.51.100.4" in summary_of(rig)
    # nothing deployed, and no clean-up (idle stop off, scenario 11 being selected) sent to the
    # address the guard refused (codex on #283)
    assert not any("push_config.py" in " ".join(a) for a in cmds.argvs) and not vm.cleaned


def test_no_deploy_names_the_report_after_the_installed_build(rig):
    installed = FakeVm(f"{'a' * 40} zab.whl\n")
    run.main(
        ["--no-deploy", "--scenarios", "1"], make=make(FakeEc2("running"), Commands(), installed)
    )
    (out,) = runs(rig)
    assert out.name.endswith("-aaaaaaaa")


def test_a_run_stopped_by_a_scenarios_leftover_says_so(rig):
    """#517: the scenarios' report carries the leftover as a run problem; the runner keeps it."""
    stopped = "the run stopped after scenario 11: idle stop is still installed on the VM (…)"
    ec2, cmds = FakeEc2("running"), Commands(pytest_rc=2, problems=(stopped,))
    argv = ["--no-deploy", "--scenarios", "1,11"]
    assert run.main(argv, make=make(ec2, cmds, FakeVm(f"{SHA} zab.whl"))) == 1
    text = summary_of(rig)
    assert f"**Run problem:** {stopped}" in text and "**Run problem:** pytest exited 2" in text


@pytest.mark.parametrize("fails", [2, 3])
def test_the_runs_idle_stop_off_is_tried_three_times_and_says_why(rig, fails):
    """#517: the run-level clean-up retries push_config as scenario 11's does, and a failure
    says why (the row's failures), not only the exit status."""
    ec2, cmds, slept = FakeEc2("running"), Commands(idle_fails=fails), []
    argv = ["--no-deploy", "--scenarios", "11"]
    run.main(argv, make=make(ec2, cmds, FakeVm(f"{SHA} zab.whl"), slept))
    offs = [a for a in cmds.argvs if a[-2:] == ["--idle-stop", "0"]]
    assert len(offs) == 3 and slept == [*STEADY, 15.0, 15.0]
    problem = (
        "**Run problem:** cleanup: idle stop off: RuntimeError: push_config --idle-stop 0: "
        "exit 1: idle stop off: ssh: Connection refused"
    )
    assert (problem in summary_of(rig)) == (fails == 3)


def test_command_keeps_the_whole_output_even_when_it_winds_the_program_down(tmp_path, capsys):
    """#517: the runner printed only the last 4000 characters; `log` gets all of it, also
    when the program is cut short (here the timeout; an interrupt winds it down the same)."""
    import sys

    log = tmp_path / "pytest.log"
    long = "import sys\nprint('x' * 9000)\nprint('oops', file=sys.stderr)"
    assert run.command([sys.executable, "-c", long], log=log).ok
    assert log.read_text() == "x" * 9000 + "\n\n--- stderr ---\noops\n"
    assert capsys.readouterr().out.count("x") < 4100  # the tail printed, as before
    assert log.stat().st_mode & 0o777 == 0o600  # owner only (reviewer on #518)
    log.chmod(0o644)  # an existing log is made 0600 too
    slow = "import signal, sys, time\n"
    slow += "signal.signal(signal.SIGINT, lambda *a: (print('cleaned'), sys.exit(0)))\n"
    slow += "print('started', flush=True)\n" + POLITE_WAIT
    done = run.command([sys.executable, "-c", slow], timeout=2, grace=10, log=log)
    assert done.returncode == 124
    assert (
        log.read_text().startswith("started\ncleaned\n")
        and "timed out after 2 s" in log.read_text()
    )
    assert log.stat().st_mode & 0o777 == 0o600


def test_command_closes_a_log_whose_mode_it_cant_set(tmp_path, monkeypatch):
    """Reviewer on #518: `fchmod` fails on an existing log owned by another user, and the
    descriptor was left open. It is closed now, and nothing is written there (#519)."""
    import os

    log, opened, real_open = tmp_path / "pytest.log", [], os.open

    def spy(path, *args, **kwargs):
        fd = real_open(path, *args, **kwargs)
        opened.append((path, fd))
        return fd

    def refused(fd, mode):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(run.os, "open", spy)
    monkeypatch.setattr(run.os, "fchmod", refused)
    run._keep(log, "a traceback's arguments", "")  # no error: a log never ends the run
    monkeypatch.undo()
    (fd,) = [fd for path, fd in opened if path == log]
    with pytest.raises(OSError):
        os.fstat(fd)  # closed
    assert log.read_text() == ""


SCOPE = {"zab-role": "test", "workshop": "zab-test"}  # the rig's default scope


class Timeline:
    """EC2 for the test VM, whose state follows `phases`, `(state, until)` on a clock that the
    harness's waits advance; the last phase lasts. A start, sent only to a `stopped` VM (EC2
    refuses one `stopping`), makes it `pending` for 15 s, then `running`."""

    def __init__(self, *phases: tuple[str, float]) -> None:
        self.now, self.phases, self.starts = 0.0, list(phases), []

    def state(self) -> str:
        return next((s for s, until in self.phases if self.now < until), self.phases[-1][0])

    def describe_instances(self, InstanceIds):
        state = self.state()
        tags = [{"Key": k, "Value": v} for k, v in {**SCOPE, "participant": "zab-test"}.items()]
        inst = {"InstanceId": "i-test", "State": {"Name": state}, "Tags": tags,
                "PublicIpAddress": "198.51.100.4" if state == "running" else None}  # fmt: skip
        return {"Reservations": [{"Instances": [inst]}]}

    def start_instances(self, InstanceIds):
        assert self.state() == "stopped", self.state()
        self.starts.append(self.now)
        self.phases = [("pending", self.now + 15), ("running", float("inf"))]

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.mark.parametrize(
    ("phases", "starts", "polls"),
    [
        # the run before stopped the VM, and the test waker woke it for that run's last
        # messages: `stopped` lasts 3 s, between two polls 10 s apart (#305, Oct 14, 23:18Z)
        ([("stopping", 25), ("stopped", 28), ("pending", 45), ("running", 0)], [], 50),
        ([("stopping", 25), ("stopped", 0)], [30], 50),  # no waker: started once stopped
        ([("pending", 12), ("running", 0)], [], 20),  # the waker's start under way
        ([("running", 0)], [], 0),
    ],
)
def test_the_runner_takes_a_vm_the_waker_started_again_as_it_is(rig, phases, starts, polls):
    """#305: at the start of a run, a VM `pending` or `running` is accepted as is and waited
    for; the runner starts only a VM it reads `stopped`, and waits for `stopping` to end in any
    state, not for `stopped` alone, which coarse polls can miss."""
    from e2e.harness.aws import TestEc2

    timeline = Timeline(*[(s, until or float("inf")) for s, until in phases])
    ec2 = TestEc2(rig.region, SCOPE, client=timeline, sleep=timeline.sleep,
                  clock=lambda: timeline.now)  # fmt: skip
    slept: list[float] = []
    runner = run.Run(rig, ec2, run=Commands(), vm_factory=lambda ip: FakeVm(), sleep=slept.append)
    assert runner.ensure_running("i-test") == "198.51.100.4"
    assert timeline.starts == starts and timeline.now == polls
    assert slept == STEADY  # then SSH must answer steadily (#690)


# ------------------------------------------------------------ SSH after the start (#690)


class Booting(FakeVm):
    """The test VM as #690 saw it, on a fake clock from `start_instances` at 0: EC2 reads it
    `running` at 10.5 s, and SSH answers from then (#517's probes: the first banner at 10.7 to
    11.8 s), in 0.5 s; but a connection made while `lost(t)` gets no answer: every SYN is lost,
    and ssh gives up after its ConnectTimeout, as the deploy's first connection did, about
    3 s after the probe that passed. Each other command takes 1 s."""

    def __init__(self, lost=lambda t: 12.0 <= t < 30.0) -> None:
        super().__init__()
        self.now, self.lost, self.answers, self.timeouts = 0.0, lost, [], []

    def probe(self):
        at, lost = self.now, self.lost(self.now)
        self.now += CONNECT_TIMEOUT_S if lost else 0.5
        self.answers.append((at, not lost))
        return Result(255, "", TIMED_OUT + "\n") if lost else Result(0, "", "")

    def run(self, argv, check=True, timeout=None):
        self.now += 1.0
        self.timeouts.append(timeout)
        return super().run(argv, check=check, timeout=timeout)

    def sleep(self, seconds):
        self.now += seconds


class Starting(FakeEc2):
    """Stopped; `running` 10.5 s after the start, on the VM's clock."""

    def __init__(self, vm: Booting) -> None:
        super().__init__("stopped")
        self.vm = vm

    def wait_state(self, iid, target, timeout):
        self.vm.now = max(self.vm.now, 10.5)
        assert self.state_now == target


#: 13:54:56Z, Oct 28: #690's third start (`20261028T135454Z-57cb094b`)
LAUNCHED = dt.datetime(2026, 10, 28, 13, 54, 56, tzinfo=dt.UTC).timestamp()


def booting_run(rig, vm: Booting) -> run.Run:
    clocks = {"clock": lambda: vm.now, "wall": lambda: LAUNCHED + vm.now}
    return run.Run(rig, Starting(vm), run=Commands(), vm_factory=lambda ip: vm, sleep=vm.sleep,
                   log=lambda line: None, **clocks)  # fmt: skip


def test_ssh_must_answer_steadily_after_a_start(rig):
    """#690: `ensure_running` took the first answer, at 10.5 s, for SSH being up; the deploy's
    first connection, at about 13 s, then timed out, and the run ended before any scenario.
    Now SSH must answer STEADY_PROBES connections in a row, 10 s apart, with cloud-init's end
    waited for after the first: the lost spell costs one probe, and the three that count all
    come after it. The timeline says when each step began (to set beside the VM's journal)."""
    vm = Booting()
    runner = booting_run(rig, vm)
    assert runner.ensure_running("i-test") == IP
    assert vm.answers == [(10.5, True), (22.0, False), (42.0, True), (52.5, True), (63.0, True)]
    assert [a for a in vm.calls if a[0] == "cloud-init"] == [["cloud-init", "status", "--wait"]]
    assert runner.timeline == [
        "13:54:56.0Z +0.0 s: EC2 stopped",
        "13:54:56.0Z +0.0 s: EC2 start sent",
        "13:55:06.5Z +10.5 s: EC2 running",
        "13:55:06.5Z +10.5 s: SSH answered in 0.5 s (1 of 3)",
        "13:55:07.0Z +11.0 s: cloud-init status: done (exit 0), after 1.0 s",
        f"13:55:18.0Z +22.0 s: SSH failed after 15.0 s: {TIMED_OUT}",
        "13:55:38.0Z +42.0 s: SSH answered in 0.5 s (1 of 3)",
        "13:55:48.5Z +52.5 s: SSH answered in 0.5 s (2 of 3)",
        "13:55:59.0Z +63.0 s: SSH answered in 0.5 s (3 of 3)",
    ]


@pytest.mark.parametrize(
    "lost",
    [lambda t: True, lambda t: (t // 20) % 2 == 1],  # never answers; keeps dropping out
    ids=["never", "flapping"],
)
def test_ssh_that_never_steadies_ends_the_wait(rig, lost):
    vm = Booting(lost)
    with pytest.raises(TimeoutError, match=f"{IP}: SSH not steady 600 s after the start"):
        booting_run(rig, vm).ensure_running("i-test")
    assert vm.now <= 10.5 + startup.SSH_WAIT_S + CONNECT_TIMEOUT_S + 1  # bounded: one probe past
    assert not any(all(ok for _, ok in vm.answers[i : i + 3]) for i in range(len(vm.answers) - 2))


def deploys(cmds: Commands) -> list[list[str]]:
    return [a for a in cmds.argvs if "push_config.py" in " ".join(a) and "--wheel" in a]


def test_the_cloud_init_wait_keeps_to_what_is_left_of_the_wait(rig):
    """Review of #800: `cloud-init status --wait` gets at most what is left of SSH_WAIT_S, not
    its own 300 s past it. SSH first answers at 410.5 s, 400 s into the wait."""
    vm = Booting(lambda t: t < 400.0)
    booting_run(rig, vm).ensure_running("i-test")
    assert vm.timeouts == [10.5 + startup.SSH_WAIT_S - 411.0]  # 199.5 s, not BOOT_WAIT_S


class Lossy(Commands):
    """push_config's deploy, on the VM's clock: its first connection (the owner check) times
    out, after ssh's 15 s, when it is made while the VM's `lost(t)`; else it takes 5 s."""

    def __init__(self, vm: Booting) -> None:
        super().__init__()
        self.vm, self.deploys = vm, []

    def __call__(self, argv, **kw):
        if "push_config.py" not in " ".join(argv) or "--wheel" not in argv:
            return super().__call__(argv, **kw)
        self.argvs.append(argv)
        self.envs.append(dict(kw.get("env") or {}))
        self.deploys.append(self.vm.now)
        if self.vm.lost(self.vm.now):
            self.vm.now += CONNECT_TIMEOUT_S
            return Result(1, table(f"ssh: {TIMED_OUT}"), "\n1 VM(s) failed\n")
        self.vm.now += 5.0
        return Result(0, "1 VM(s) ok\n", "")


@pytest.mark.parametrize(
    ("lost", "deploys", "code"),
    [
        (lambda t: 12.0 <= t < 30.0, [64.5], 0),  # #690's: the steady probes outlast it
        (lambda t: 34.0 <= t < 55.0, [34.0, 64.0], 0),  # after the probes: the rerun covers it
        (lambda t: t >= 34.0, [34.0, 64.0, 109.0], 1),  # for good: no tries left
    ],
    ids=["during the probes", "after the probes", "for good"],
)
def test_690s_whole_run_replayed(rig, lost, deploys, code):
    """Review of #800: #690's whole run through `main`, on one clock: the start, the probes,
    the deploy, the scenarios or the abort, and the stop."""
    vm = Booting(lost)
    ec2, cmds = Starting(vm), Lossy(vm)
    clocks = {"clock": lambda: vm.now, "wall": lambda: LAUNCHED + vm.now}
    made = run.Run(rig, ec2, run=cmds, vm_factory=lambda ip: vm, sleep=vm.sleep,
                   log=lambda line: None, **clocks)  # fmt: skip
    assert run.main(["--scenarios", "1"], make=lambda cfg: made) == code
    assert cmds.deploys == deploys and ec2.calls[-1] == ("stop", "i-test")
    text = summary_of(rig)
    if code == 0:
        assert text.startswith("## End-to-end: PASS") and any("pytest" in a for a in cmds.argvs)
        return
    assert f"ssh: {TIMED_OUT}; no tries left" in text
    assert f"aborted: RuntimeError: push_config failed: exit 1: ssh: {TIMED_OUT}" in text
    assert not any("pytest" in a for a in cmds.argvs)


@pytest.mark.parametrize("fails", [1, 2, 3])
def test_a_deploy_that_could_not_connect_runs_again(rig, fails):
    """#690: push_config never retries a connect, and a rollout is idempotent, so the runner
    runs it again, 15 then 30 s later, 3 tries in all; the report's Start-up says so."""
    ec2, slept = FakeEc2(), []
    cmds = Commands(deploy_fails=(f"ssh: {TIMED_OUT}",) * fails)
    code = run.main(["--scenarios", "1"], make=make(ec2, cmds, FakeVm(), slept))
    assert len(deploys(cmds)) == min(fails + 1, 3)
    assert slept == [*STEADY, *(15.0, 30.0)[:fails]]
    text = summary_of(rig)
    assert "\n### Start-up\n" in text and f": ssh: {TIMED_OUT}; push_config again in 15 s" in text
    if fails < 3:
        assert code == 0 and text.startswith("## End-to-end: PASS")
        return
    assert code == 1 and f"ssh: {TIMED_OUT}; no tries left" in text
    assert f"aborted: RuntimeError: push_config failed: exit 1: ssh: {TIMED_OUT}" in text
    assert ec2.calls[-1] == ("stop", "i-test") and not any("pytest" in a for a in cmds.argvs)


@pytest.mark.parametrize(
    "failures",
    [
        f"ssh: ubuntu@{IP}: Permission denied (publickey).",  # it connected
        "ssh: ssh: connect to host 203.0.113.9 port 22: Connection timed out",  # not the VM
        f"service install: exit 1; ssh: {TIMED_OUT}",  # a step failed besides
        f"ssh: {TIMED_OUT}; agent instructions: exit 1",  # in push_config's order
    ],
    ids=["auth", "another host", "a failure, then the connect", "the connect, then a failure"],
)
def test_any_other_deploy_failure_ends_the_run_at_once(rig, failures):
    cmds = Commands(deploy_fails=(failures,))
    assert run.main(["--scenarios", "1"], make=make(FakeEc2(), cmds)) == 1
    assert len(deploys(cmds)) == 1
    assert f"aborted: RuntimeError: push_config failed: exit 1: {failures}" in summary_of(rig)


def test_the_start_up_is_in_the_report_and_a_merge_keeps_both(tmp_path):
    first = report.Report(commit="c", wheel="w", boot=["13:54:56.0Z +0.0 s: EC2 stopped"])
    first.merge(report.Report(commit="c", wheel="w", boot=["14:10:02.5Z +0.0 s: EC2 running"]))
    first.write(tmp_path / "report.json")
    again = report.Report.read(tmp_path / "report.json")
    assert again.boot == ["13:54:56.0Z +0.0 s: EC2 stopped", "14:10:02.5Z +0.0 s: EC2 running"]
    assert again.markdown().endswith(
        "\n### Start-up\n\n- 13:54:56.0Z +0.0 s: EC2 stopped\n- 14:10:02.5Z +0.0 s: EC2 running\n"
    )
    assert "Start-up" not in report.Report(commit="c").markdown()
