"""The e2e harness's SSH and report, offline with fakes (issue #263)."""

from __future__ import annotations

import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest

from e2e.conftest import noted
from e2e.harness import report
from e2e.harness.recorder import Recorder as Rows
from e2e.harness.vm import (
    CONNECT_TIMEOUT_S,
    MIN_TRY_S,
    RemoteError,
    Result,
    Vm,
    connect_failure,
    span_seconds,
)

# ----------------------------------------------------------------------------------- SSH


class Recorder:
    def __init__(self, answers: dict[str, Result] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.answers = answers or {}

    def __call__(self, argv, timeout):
        self.calls.append(argv)
        for needle, result in self.answers.items():
            if needle in argv[-1]:
                return result
        return Result(0, "", "")


def vm(answers=None) -> tuple[Vm, Recorder]:
    rec = Recorder(answers)
    return Vm("198.51.100.4", key=Path("/k/key.pem"), known_hosts=Path("/k/kh"), runner=rec), rec


def test_ssh_takes_argument_lists_only_and_quotes_them():
    machine, rec = vm()
    hostile = "x; rm -rf / $(id) `id`"
    machine.run(["echo", hostile])
    argv = rec.calls[0]
    assert argv[0] == "ssh" and argv[-3:-1] == ["ubuntu@198.51.100.4", "--"]
    assert shlex.split(argv[-1]) == ["echo", hostile]  # the VM's shell sees two words
    assert "StrictHostKeyChecking=yes" in argv and 'UserKnownHostsFile="/k/kh"' in argv
    assert "IdentitiesOnly=yes" in argv and "ForwardAgent=no" in argv
    for bad in ("echo hi", []):
        with pytest.raises(TypeError):
            machine.run(bad)
    with pytest.raises(ValueError):
        Vm("ws-zab-test", key=Path("k"), known_hosts=Path("kh"))  # an address, not a name


def test_script_passes_inputs_as_positional_arguments():
    machine, rec = vm()
    machine.script('echo "$1"', "a b; c", sudo=True)
    assert shlex.split(rec.calls[0][-1]) == ["sudo", "bash", "-c", 'echo "$1"', "bash", "a b; c"]


def test_unit_reads_the_agent_user_manager_and_signals_are_checked():
    unit = "MainPID=4242\nNRestarts=3\nActiveState=active\nSubState=running\n"
    machine, rec = vm({"id -u agent": Result(0, "1001\n", ""), "show": Result(0, unit, "")})
    assert machine.unit() == {
        "MainPID": "4242",
        "NRestarts": "3",
        "ActiveState": "active",
        "SubState": "running",
    }
    words = shlex.split(rec.calls[-1][-1])
    assert words[:8] == ["sudo", "-u", "agent", "-H", "env", "-C", "/home/agent",
                         "XDG_RUNTIME_DIR=/run/user/1001"]  # fmt: skip
    assert words[9:12] == ["systemctl", "--user", "show"]
    machine.signal(4242, "STOP")
    assert shlex.split(rec.calls[-1][-1]) == ["sudo", "kill", "-STOP", "4242"]
    for pid, sig in ((1, "STOP"), (4242, "HUP"), ("4242", "STOP")):
        with pytest.raises(ValueError):
            machine.signal(pid, sig)
    with pytest.raises(ValueError):
        machine.unit_action("enable")


def test_the_watchdog_bound_is_read_from_the_installed_unit():
    """#305: scenario 3's bound was 360 s, and a restart took 361.3 s. It is now the unit's
    WatchdogSec, abort timeout and RestartSec, as systemd prints them, and 60 s to start."""
    from e2e import test_scenarios_slow as slow

    shown = "WatchdogUSec=5min\nTimeoutAbortUSec=30s\nRestartUSecNext=5s\n"  # the shipped unit
    machine, rec = vm({"id -u agent": Result(0, "1001\n", ""), "show": Result(0, shown, "")})
    assert slow.restart_limits(machine) == (395, 300)
    words = shlex.split(rec.calls[-1][-1])
    assert words[9:] == ["systemctl", "--user", "show", "zab.service", "--property=WatchdogUSec",
                         "--property=TimeoutAbortUSec", "--property=RestartUSecNext"]  # fmt: skip
    for text, seconds in (("1min 30s", 90), ("100ms", 0.1), ("0", 0), ("2h", 7200)):
        assert span_seconds(text) == seconds
    assert span_seconds("infinity") == float("inf")
    for bad in ("", "5 min", "30", "1x", "-5s"):
        with pytest.raises(ValueError):
            span_seconds(bad)
    for unit, why in (
        ("WatchdogUSec=0\n", "no watchdog"),
        ("RestartUSecNext=infinity\n", "unbounded"),
    ):
        machine, _ = vm({"id -u agent": Result(0, "1001\n", ""),
                         "show": Result(0, shown + unit, "")})  # fmt: skip
        with pytest.raises(AssertionError, match=why):
            slow.restart_limits(machine)
    machine, _ = vm({"id -u agent": Result(0, "1001\n", ""), "show": Result(0, "x=1\n", "")})
    with pytest.raises(RuntimeError, match="no WatchdogUSec, TimeoutAbortUSec, RestartUSec"):
        machine.unit_spans("WatchdogUSec", "TimeoutAbortUSec", "RestartUSec")


# ------------------------------------------------------------ connect failures (#517 item 4)

HOST = "198.51.100.4"
#: The first four are what OpenSSH 9.6p1 printed here, against TEST-NET-1, a closed port, a
#: listener that resets and one that never speaks (ConnectTimeout); the next two, the same
#: `connect to host` line with connect(2)'s other errors (one with CRLFs); the last, an older
#: client's closed banner.
RETRIED = {
    "timed out": f"ssh: connect to host {HOST} port 22: Connection timed out\n",
    "refused": f"ssh: connect to host {HOST} port 22: Connection refused\n",
    "banner reset": "kex_exchange_identification: read: Connection reset by peer\n"
    f"Connection reset by {HOST} port 22\n",
    "banner timed out": "Connection timed out during banner exchange\n"
    f"Connection to {HOST} port 22 timed out\n",
    "no route": f"ssh: connect to host {HOST} port 22: No route to host\r\n",
    "unreachable": f"ssh: connect to host {HOST} port 22: Network is unreachable\n",
    "banner closed (older)": "kex_exchange_identification: Connection closed by remote host\n"
    f"Connection closed by {HOST} port 22\n",
}
CONNECT = RETRIED["timed out"]
#: Exit 255 that may come after the command started, or isn't a connect failure at all.
NOT_RETRIED = {
    "session closed": f"Connection to {HOST} closed by remote host.\n",
    "session read": f"Read from remote host {HOST}: Connection reset by peer\n",
    "broken pipe": "client_loop: send disconnect: Broken pipe\n",
    "bare closed": f"Connection closed by {HOST} port 22\n",  # 9.6's closed banner, or later
    "bare reset": f"Connection reset by {HOST} port 22\n",
    "auth": f"ubuntu@{HOST}: Permission denied (publickey).\n",
    "host key": "Host key verification failed.\n",
    "the command's own 255": "",
    "another host": "ssh: connect to host 198.51.100.9 port 22: Connection timed out\n",
    "and a line more": CONNECT + "bash: line 1: oops\n",
    "a bad banner": "kex_exchange_identification: banner line contains invalid characters\n",
    # a banner line names no host: alone, or beside another host's summary, it could be a
    # remote command's own stderr (reviewer on #535)
    "lone banner timed out": "Connection timed out during banner exchange\n",
    "lone banner reset": "kex_exchange_identification: read: Connection reset by peer\n",
    "another host's banner": "kex_exchange_identification: read: Connection reset by peer\n"
    "Connection reset by 198.51.100.9 port 22\n",
}


@pytest.mark.parametrize("stderr", RETRIED.values(), ids=RETRIED.keys())
def test_a_connect_phase_failure_is_recognized(stderr):
    assert connect_failure(Result(255, "", stderr), HOST) == stderr.splitlines()[0].strip()


@pytest.mark.parametrize("stderr", NOT_RETRIED.values(), ids=NOT_RETRIED.keys())
def test_a_failure_that_may_follow_the_command_is_never_retried(stderr):
    assert connect_failure(Result(255, "", stderr), HOST) == ""


def test_only_ssh_s_own_exit_with_nothing_on_stdout_is_retried():
    assert connect_failure(Result(255, "", CONNECT), HOST)
    assert not connect_failure(Result(255, "started\n", CONNECT), HOST)  # the command ran
    for code in (0, 1, 124):  # the remote command's exit, or the local timeout
        assert not connect_failure(Result(code, "", CONNECT), HOST)


class Flaky:
    """A runner whose answers come in turn: each failed connect takes `connect_s` on the fake
    clock, as a ConnectTimeout does; the budget it was given is kept."""

    def __init__(self, *answers: Result, connect_s: float = 15.0) -> None:
        self.answers, self.connect_s = list(answers), connect_s
        self.now, self.budgets, self.slept, self.logged = 0.0, [], [], []

    def __call__(self, argv, timeout, input=None):
        self.budgets.append(timeout)
        result = self.answers.pop(0)
        self.now += self.connect_s if result.returncode == 255 else 1.0
        return result

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def vm(self) -> Vm:
        return Vm(HOST, key=Path("/k/key.pem"), known_hosts=Path("/k/kh"), runner=self,
                  log=self.logged.append, sleep=self.sleep, clock=lambda: self.now)  # fmt: skip


FAILED, DONE = Result(255, "", CONNECT), Result(0, "ok\n", "")


def test_a_connect_failure_is_tried_again_and_logged():
    flaky = Flaky(FAILED, FAILED, DONE)
    assert flaky.vm().script("echo $1", "x", sudo=True).stdout == "ok\n"
    assert flaky.slept == [5.0, 5.0] and flaky.budgets == [120, 100, 80]
    first, second = flaky.logged
    assert first == (
        "SSH connect failure (sudo bash -c 'echo $1' bash x), try 1 of 3: "
        f"ssh: connect to host {HOST} port 22: Connection timed out; again in 5 s"
    )
    assert second.endswith("try 2 of 3: " + CONNECT.strip() + "; again in 5 s")


def test_three_failures_are_the_caller_s_failure():
    flaky = Flaky(FAILED, FAILED, FAILED)
    with pytest.raises(RemoteError, match="Connection timed out"):
        flaky.vm().run(["true"])
    assert len(flaky.budgets) == 3 and flaky.slept == [5.0, 5.0]
    assert flaky.logged[-1].endswith("try 3 of 3: " + CONNECT.strip() + "; no tries left")


def test_any_other_failure_is_returned_at_once():
    for stderr in NOT_RETRIED.values():
        flaky = Flaky(Result(255, "", stderr), DONE)
        assert flaky.vm().run(["true"], check=False).returncode == 255
        assert len(flaky.budgets) == 1 and not flaky.slept and not flaky.logged


def test_retries_stay_within_the_caller_s_timeout():
    """The timeout bounds the whole call: a retry gets what is left, and none is made with
    less than MIN_TRY_S left after the pause. Never three times a long timeout."""
    flaky = Flaky(FAILED, FAILED, DONE)
    assert flaky.vm().run(["lake", "env", "lean"], timeout=1800).ok
    assert flaky.budgets == [1800, 1780, 1760]
    flaky = Flaky(FAILED, FAILED, DONE)
    assert flaky.vm().run(["true"], timeout=40, check=False) == FAILED
    assert flaky.budgets == [40, 20] and flaky.now == 35  # 5 s: too few for a pause and a try
    assert flaky.logged[-1].endswith("; not tried again: 5 s of its 40 s left")
    flaky = Flaky(FAILED, DONE, connect_s=0.0)  # refused at once: the full budget, nearly
    assert flaky.vm().run(["true"], timeout=60).ok and flaky.budgets == [60, 55]
    # a retry always gets more than ssh's ConnectTimeout: its failure is ssh's to report
    assert MIN_TRY_S == CONNECT_TIMEOUT_S + 1 == 16
    flaky = Flaky(FAILED, DONE, connect_s=0.0)
    assert flaky.vm().run(["true"], timeout=21).ok and flaky.budgets == [21, 16]
    flaky = Flaky(FAILED, DONE, connect_s=0.0)
    assert flaky.vm().run(["true"], timeout=20.9, check=False) == FAILED
    assert flaky.budgets == [20.9]  # rounded down, never past the timeout (reviewer on #535)
    assert flaky.logged[-1].endswith("; not tried again: 20 s of its 20.9 s left")


def test_reachable_is_one_probe():
    flaky = Flaky(FAILED, DONE)
    assert not flaky.vm().reachable()
    assert flaky.budgets == [30] and not flaky.slept and not flaky.logged


def test_the_admin_server_retries_with_the_same_input():
    from e2e.admin.harness.server import Server

    flaky, sent = Flaky(FAILED, DONE), []

    def runner(argv, timeout, input=None):
        sent.append(input)
        return flaky(argv, timeout)

    rig = SimpleNamespace(ssh_key=Path("/k/key.pem"), known_hosts=Path("/k/kh"))
    server = Server(HOST, rig, runner=runner, log=flaky.logged.append)
    server.vm.sleep, server.vm.clock = flaky.sleep, lambda: flaky.now
    server.install_etc("admin.toml", "[workshop]\n")
    assert sent == ["[workshop]\n", "[workshop]\n"] and len(flaky.logged) == 1
    assert not Server(HOST, rig, runner=Flaky(FAILED, DONE)).reachable()


def test_a_connect_failure_is_a_note_of_the_scenario(capsys):
    rec = Rows()
    rec.finish(11, "test_idle_stop", "passed", 700.0, "")
    noted(rec, 11)("SSH connect failure (true), try 1 of 3: x; again in 5 s")
    noted(rec, None)("SSH connect failure (true), try 1 of 3: y; again in 5 s")
    assert rec.row(11).notes == ["SSH connect failure (true), try 1 of 3: x; again in 5 s"]
    assert capsys.readouterr().err.count("e2e: SSH connect failure (true)") == 2
    text = rec.report().markdown()
    assert "PASS" in text and "(passed): SSH connect failure (true), try 1 of 3: x" in text


# -------------------------------------------------------------------------------- report


def test_select_scenarios():
    assert report.select("1,2,5-7") == [1, 2, 5, 6, 7]
    assert report.select("slow") == [3, 4, 10, 11, 15]
    assert report.select("fast") == [
        1,
        2,
        5,
        6,
        7,
        8,
        9,
        12,
        13,
        14,
        16,
        17,
    ]  # 14 fast (#305), 16 (#709), 17 (#660)
    assert report.select("all") == list(range(1, 18))
    for bad in ("", "18", "0-2", "x"):
        with pytest.raises(ValueError):
            report.select(bad)


def test_report_round_trip_merge_and_markdown(tmp_path):
    first = report.Report(commit="abc123def4567", wheel="w.whl", vm={"instance": "i-test"})
    r1 = report.ScenarioResult(1, "Basics, both agents", seconds=80)
    r1.timing("claude DM reply", 12.3, high=60)
    r3 = report.ScenarioResult(3, "Watchdog", outcome="failed", reason="no restart in 360 s")
    r3.timing("restart", 400, high=360)
    first.results = [r1, r3]
    path = tmp_path / "report.json"
    first.write(path)
    again = report.Report.read(path)
    assert again == first
    later = report.Report(results=[report.ScenarioResult(3, "Watchdog", seconds=300)])
    same = {"commit": "abc123def4567", "wheel": "w.whl"}
    for other in (  # codex on #276 and #283: another build, or an unknown one
        {**same, "commit": "fedcba9876543"},
        {**same, "wheel": "other.whl"},
        {"wheel": "w.whl"},
        {"commit": "abc123def4567"},
    ):
        with pytest.raises(ValueError, match="different or unknown builds"):
            again.merge(report.Report(**other, results=later.results))
    again.merge(report.Report(**same, results=later.results))  # the same build
    # scenario 3 in both: the failed half and its timing stay (codex on #286)
    row = again.results[1]
    assert row.outcome == "failed" and row.seconds == 300 and not again.passed
    assert [t.name for t in row.timings] == ["restart"] and row.reason == "no restart in 360 s"
    text = first.markdown()
    assert text.startswith("## End-to-end: FAIL (1 passed, 1 failed, 0 skipped)")
    assert "claude DM reply 12.3 s (≤ 60 s) ✅" in text and "restart 400.0 s (≤ 360 s) ❌" in text
    assert "no restart in 360 s" in text
    soft = report.Timing("wake", 50, high=45, target=True)
    assert not soft.ok and "⚠️" in soft.text()


def test_merging_both_halves_of_a_scenario_keeps_each():
    """Codex on #286: scenario 8's fast checks (a compile over 30 s) and its slow cold import
    (passed) merge into one row that still fails, with both timings."""
    build = {"commit": "abc", "wheel": "w.whl"}
    fast = report.ScenarioResult(8, "Lean (#205)", seconds=200)
    fast.timing("claude: compile check", 41, high=30)
    cold = report.ScenarioResult(8, "Lean (#205)", seconds=300)
    cold.timing("cold import Mathlib", 95)
    merged = report.Report(**build, results=[fast])
    merged.merge(report.Report(**build, results=[cold]))
    (row,) = merged.results
    assert [t.name for t in row.timings] == ["claude: compile check", "cold import Mathlib"]
    assert row.effective == "failed" and not merged.passed and row.seconds == 500
    skipped = report.Report(**build, results=[report.ScenarioResult(9, "File server", "skipped")])
    skipped.merge(report.Report(**build, results=[report.ScenarioResult(9, "File server")]))
    assert skipped.results[0].outcome == "passed"


def test_a_missed_threshold_fails_the_scenario_and_a_missed_target_does_not():
    """Codex on #276: a scenario whose checks passed but whose restart took 400 s of 360 s
    failed, in the verdict, the counts, the row and the details."""
    slow = report.ScenarioResult(3, "Watchdog")
    slow.timing("restart", 400, high=360)
    run = report.Report(results=[slow])
    assert slow.outcome == "passed" and slow.effective == "failed" and not run.passed
    text = run.markdown()
    assert text.startswith("## End-to-end: FAIL (0 passed, 1 failed, 0 skipped)")
    assert "| 3 | Watchdog | ❌ FAIL |" in text and "over its threshold: restart 400.0 s" in text
    waker = report.ScenarioResult(10, "Waker")
    waker.timing("wake to answer", 50, high=45, target=True)
    assert waker.effective == "passed" and report.Report(results=[waker]).passed


def test_no_result_is_neither_pass_nor_fail():
    """Reviewer on #286: a merge gate must not pass a run in which nothing passed, or in which
    a requested scenario was skipped; the summary lists why each was skipped."""
    skipped = report.ScenarioResult(9, "File server", "skipped", reason="not rendered (#256)")
    passed, failed = report.ScenarioResult(1, "Basics"), report.ScenarioResult(2, "C", "failed")
    assert report.Report().verdict == "NO-RESULT"
    assert report.Report(results=[skipped]).verdict == "NO-RESULT"
    run = report.Report(results=[passed, skipped])
    assert run.verdict == "NO-RESULT" and not run.passed
    text = run.markdown()
    assert text.startswith("## End-to-end: NO-RESULT (1 passed, 0 failed, 1 skipped)")
    assert "### Not completely run" in text and "- **9. File server**: not rendered (#256)" in text
    assert report.Report(results=[passed, skipped, failed]).verdict == "FAIL"
    assert report.Report(results=[passed], problems=["pytest exited 2"]).verdict == "FAIL"
    assert report.Report(results=[passed]).verdict == "PASS"


def test_local_commands_read_no_stdin(monkeypatch):
    """ssh must never wait on, or swallow, the runner's own stdin."""
    import subprocess

    from e2e.harness import vm as vm_module

    seen = {}

    def fake_run(argv, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(vm_module.subprocess, "run", fake_run)
    assert vm_module.subprocess_runner(["ssh", "x"], 5).ok
    assert seen["stdin"] is subprocess.DEVNULL
