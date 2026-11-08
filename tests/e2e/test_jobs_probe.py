"""#655's probe of background jobs (`probes/jobs_systemd.py`), offline: its stand-ins for
`zab job start`, `_exec` and `_ended`, its reading of a turn's stream, and its helpers, so that
its code runs before the test VM's time is spent on it. Nothing here needs systemd."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from e2e.probes import jobs_systemd as js


def made(tmp_path, systemd_run: str = "systemd-run") -> js.Probe:
    host = js.Host(os.getuid(), dict(os.environ), systemd_run)
    probe = js.Probe(host, js.Report(), "abc123", tmp_path / "work")
    probe.setup()
    return probe


def run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    kwargs.setdefault("check", False)
    return subprocess.run(argv, **kwargs)  # noqa: S603 - this interpreter, the probe's scripts


def wait_for(condition, seconds: float = 10.0) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


def test_the_ended_stand_in_records_what_systemd_tells_it(tmp_path):
    probe = made(tmp_path)
    out = tmp_path / "ended.json"
    env = {"SERVICE_RESULT": "exit-code", "EXIT_CODE": "exited", "EXIT_STATUS": "1"}
    script = str(probe.workdir / "ended.py")
    run([sys.executable, "-I", script, str(out)], env=env, check=True)
    data = json.loads(out.read_text())
    assert {k: data[k] for k in env} == env and data["at"] > 0


@pytest.mark.parametrize(("case", "code", "text"), [("exit0", 0, "ran"), ("exit1", 1, "failed")])
def test_the_exec_stand_in_ends_with_its_childs_status(tmp_path, case, code, text):
    probe = made(tmp_path)
    log = tmp_path / "log.txt"
    argv = [sys.executable, "-I", str(probe.workdir / "exec.py"), case, "m-" + case, str(log)]
    assert run(argv).returncode == code
    assert text in log.read_text()


def test_a_stop_ends_the_child_and_the_wrapper_ends_by_its_signal(tmp_path):
    """systemd's stop sends SIGTERM to every process of the unit: the wrapper waits for its
    child, then dies of the same signal, which systemd reports (`EXIT_CODE=killed`)."""
    probe = made(tmp_path)
    marker = "zab-probe655-test-stop"
    argv = [sys.executable, "-I", str(probe.workdir / "exec.py"), "sleep", marker, "/dev/null"]
    wrapper = subprocess.Popen(argv, start_new_session=True)  # noqa: S603 - as above
    try:
        wait_for(lambda: len(js.marked(marker)) == 2)
        time.sleep(0.3)  # the child's interpreter running
        os.killpg(wrapper.pid, signal.SIGTERM)
        assert wrapper.wait(10) == -signal.SIGTERM
        wait_for(lambda: not js.marked(marker))
    finally:
        js.end(js.marked(marker))
        wrapper.kill()


def test_the_setsid_case_leaves_a_grandchild_in_a_session_of_its_own(tmp_path):
    """Without systemd nothing stops it: the case tests what the unit's cgroup must catch."""
    probe = made(tmp_path)
    marker = "zab-probe655-test-setsid"
    argv = [
        sys.executable,
        "-I",
        str(probe.workdir / "exec.py"),
        "setsid-exit",
        marker,
        "/dev/null",
    ]
    try:
        assert run(argv).returncode == 0
        wait_for(lambda: len(js.marked(marker)) == 1)
        (pid,) = js.marked(marker)
        assert js.proc_facts(pid)["sid"] == pid
    finally:
        js.end(js.marked(marker))


def fake_systemd_run(tmp_path):
    """A `systemd-run` that fails as the real one does without `XDG_RUNTIME_DIR`, and records
    its arguments otherwise."""
    record = tmp_path / "argv.json"
    fake = tmp_path / "fake-systemd-run"
    fake.write_text(
        f"#!{sys.executable} -I\nimport json, os, sys\n"
        "if 'XDG_RUNTIME_DIR' not in os.environ:\n"
        "    sys.exit('Failed to connect to bus: No medium found')\n"
        f"open({str(record)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
    )
    fake.chmod(0o700)
    return fake, record


def test_start_job_records_where_it_ran_and_tries_again_with_xdg(tmp_path):
    fake, record = fake_systemd_run(tmp_path)
    probe = made(tmp_path, str(fake))
    env = {k: v for k, v in os.environ.items() if k not in js.USER_ENV}
    env["ZAB_BOT"] = "a bot"
    start = probe.workdir / "start-job"
    done = run([str(start), "claude"], env=env, capture_output=True, text=True)
    assert done.returncode == 0 and done.stdout.strip() == "started zab-probe655-abc123-claude"
    rec = json.loads((probe.workdir / "caller-claude.json").read_text())
    assert rec["first"]["rc"] == 1 and "No medium found" in rec["first"]["error"]
    assert rec["with_xdg"]["rc"] == 0
    assert rec["env"] == {"XDG_RUNTIME_DIR": False, "DBUS_SESSION_BUS_ADDRESS": False,
                          "ZAB_BOT": True}  # fmt: skip
    assert rec["pgid"] > 0 and rec["sid"] > 0
    assert rec["cgroup"] == js.proc_cgroup("self")  # a child stays in its parent's cgroup
    argv = json.loads(record.read_text())
    expected = js.unit_argv(str(fake), "zab-probe655-abc123-claude", ["x"], js.PROPERTIES)[1:-1]
    assert argv[: len(expected)] == expected
    assert argv[len(expected)].endswith("sleep") and argv[len(expected) + 1 :] == ["900"]
    assert "a bot" not in json.dumps(rec)  # names only, never values


def test_start_job_refuses_another_caller(tmp_path):
    fake, record = fake_systemd_run(tmp_path)
    probe = made(tmp_path, str(fake))
    done = run([str(probe.workdir / "start-job"), "bash"], capture_output=True)
    assert done.returncode != 0 and not record.exists()


def test_the_stream_summary_keeps_names_and_times_but_no_text():
    stream = js.Stream()
    hidden = "do-not-print-this"
    lines = [
        {"type": "system", "subtype": "init", "cwd": hidden},
        {
            "type": "assistant",
            "message": {"content": [
                {"type": "text", "text": hidden},
                {"type": "tool_use", "id": "t1", "name": "Bash",
                 "input": {"command": hidden, "run_in_background": False}},
            ]},
        },
        {"type": "system", "subtype": "background_tasks_changed",
         "tasks": [{"task_id": "x", "task_type": "local_bash", "description": hidden}]},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": hidden}]}},
        {"type": "result", "subtype": "success", "is_error": False, "result": hidden},
    ]  # fmt: skip
    for t, obj in enumerate(lines):
        stream.feed("stdout", json.dumps(obj), float(t))
    stream.feed("stderr", hidden, 5.0)
    stream.feed("stdout", "not json " + hidden, 6.0)
    summary = stream.summary()
    assert summary["tools"] == [
        {"tool": "Bash", "background": False, "t": 1.0},
        {"result": "Bash", "seconds": 2.0, "error": False, "t": 3.0},
    ]
    assert summary["tasks"] == [
        {"t": 2.0, "tasks": [{"task_type": "local_bash", "status": None, "ambient": None}]}
    ]
    assert summary["results"] == [{"t": 4.0, "subtype": "success", "error": False}]
    assert summary["events"]["system/background_tasks_changed"] == 1
    assert summary["stderr_lines"] == 1
    assert hidden not in json.dumps(summary)


def test_the_turn_environment_is_the_managers_and_zab_services(tmp_path, monkeypatch):
    units = tmp_path / "config" / "systemd" / "user"
    units.mkdir(parents=True)
    (units / "zab.service").write_text(
        "[Service]\nEnvironment=PATH=/opt/bin:/usr/bin\n"
        'Environment="CODEX_HOME=/home/agent/.codex" ZAB_X=50%%\n'
        "UnsetEnvironment=OPENAI_API_KEY CODEX_ACCESS_TOKEN\n"
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    probe = made(tmp_path)
    probe.manager_env = {"XDG_RUNTIME_DIR": "/run/user/1", "PATH": "/usr/bin", "LANG": "C",
                         "OPENAI_API_KEY": "k"}  # fmt: skip
    env = probe.turn_base_env()
    assert "OPENAI_API_KEY" not in env  # zab.service's UnsetEnvironment= (#925's review, N6)
    assert env["PATH"] == "/opt/bin:/usr/bin" and env["CODEX_HOME"] == "/home/agent/.codex"
    assert env["ZAB_X"] == "50%" and env["XDG_RUNTIME_DIR"] == "/run/user/1"
    assert env["HOME"] and env["USER"]


def test_the_helpers():
    assert js.under("/a/b/c.service", "/a/b") and js.under("/a/b", "/a/b/")
    assert not js.under("/a/bc", "/a/b") and not js.under("/a/b", "") and not js.under("", "/a")
    assert js.pairs("A=1\nB=x=y\nC=$'a\\nb'\nnoise\n") == {"A": "1", "B": "x=y"}
    changed = js.with_properties("RuntimeMaxSec=5", "TimeoutStopSec=5")
    assert "RuntimeMaxSec=12h" not in changed and changed[-2:] == (
        "RuntimeMaxSec=5",
        "TimeoutStopSec=5",
    )
    assert len(changed) == len(js.PROPERTIES)
    argv = js.unit_argv("/usr/bin/systemd-run", "u", ["/bin/true", "a b"], ("Nice=10",))
    assert argv == [
        "/usr/bin/systemd-run", "--user", "--unit=u", f"--slice={js.SLICE}", "--collect",
        "--quiet", "--property=Nice=10", "--", "/bin/true", "a b",
    ]  # fmt: skip
    facts = js.proc_facts(os.getpid())
    assert facts["pgid"] == os.getpgid(0) and facts["sid"] == os.getsid(0)


def test_the_command_line(tmp_path):
    probe = js.probe(["--skip", "claude,restarts", "--unload", "5"], workdir=tmp_path)
    assert probe.skip == {"claude", "restarts"} and probe.unload_s == 5
    assert probe.host.env["XDG_RUNTIME_DIR"] and probe.workdir.parent == tmp_path
    with pytest.raises(SystemExit):
        js.probe(["--skip", "everything"], workdir=tmp_path)


FAKE_CLAUDE = Path(__file__).resolve().parents[2] / "src" / "zab" / "testing" / "fake_claude.py"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "claude" / "2.1.283"


async def test_a_claude_turn_runs_as_the_bridge_runs_one(tmp_path, monkeypatch):
    """`claude_turn` against the scripted CLI (a recorded turn with a `Bash` call): the bridge's
    argv and stdin, and what the probe reports of it."""
    from zab.backends.claude import ClaudeConfig
    from zab.core.types import SessionSettings

    monkeypatch.setattr(js, "SETTLE_S", 0.0)
    account = tmp_path / "account"
    claude = ClaudeConfig(binary=str(FAKE_CLAUDE), accounts={"main": str(account)}, steer=True)
    log = tmp_path / "calls.jsonl"
    base = {**os.environ, "ZAB_FAKE_CLAUDE_FIXTURE": str(FIXTURES / "tool_use.jsonl"),
            "ZAB_FAKE_CLAUDE_LOG": str(log)}  # fmt: skip
    setup = (claude, SessionSettings(), str(account), "the bot")
    result = await js.claude_turn(setup, base, "a prompt", str(tmp_path), "zab-probe655-x")
    assert result["code"] == 0 and not result["timed_out"]
    assert not result["note"] and not result["group_killed"] and result["left_in_group"] == 0
    assert result["final"]["status"] == "completed"
    tools = [t["tool"] for t in result["stream"]["tools"] if "tool" in t]
    assert "Bash" in tools
    assert result["stream"]["results"] and result["after_last_result_s"] is not None
    (call,) = [json.loads(line) for line in log.read_text().splitlines()]
    assert "--input-format" in call["argv"] and call["env"]["CLAUDE_CONFIG_DIR"] == str(account)


async def test_a_claude_turns_parser_is_the_bridges(tmp_path, monkeypatch):
    """#925's review, N1: with steering, `ClaudeBackend._run` joins a turn's several results
    (`multi_result`, as in the live Monitor turn's two) and knows its steers; the probe's parser
    is built the same way."""
    from zab.backends import claude_stream
    from zab.backends.claude import ClaudeConfig
    from zab.core.types import SessionSettings

    built = []
    parser = claude_stream.StreamParser

    def recording(*args, **kwargs):
        built.append(kwargs)
        return parser(*args, **kwargs)

    monkeypatch.setattr(claude_stream, "StreamParser", recording)
    monkeypatch.setattr(js, "SETTLE_S", 0.0)
    account = tmp_path / "account"
    claude = ClaudeConfig(binary=str(FAKE_CLAUDE), accounts={"main": str(account)}, steer=True)
    base = {**os.environ, "ZAB_FAKE_CLAUDE_FIXTURE": str(FIXTURES / "tool_use.jsonl")}
    setup = (claude, SessionSettings(effort="high"), str(account), "the bot")
    result = await js.claude_turn(setup, base, "a prompt", str(tmp_path), "zab-probe655-x")
    (kwargs,) = built
    assert kwargs["multi_result"] is True and kwargs["effort"] == "high"
    assert kwargs["steers"] is not None
    assert result["code"] == 0 and result["final"]["status"] == "completed"


async def test_a_codex_turn_starts_a_thread_as_the_bridge_does(tmp_path):
    """`codex_turn` against the scripted app-server: the bridge's thread parameters, `ZAB_BOT`
    among the shell's variables, and the turn's end."""
    import asyncio

    from zab.testing.fake_codex import FakeAppServer

    fake = FakeAppServer(str(tmp_path))
    fake.hold = True
    socket = str(tmp_path / "s.sock")
    params = {
        "home": str(tmp_path), "socket": socket, "bot": "the bot", "cwd": str(tmp_path),
        "approval": "never", "sandbox": "danger-full-access", "model": "", "effort": "",
    }  # fmt: skip
    async with fake.serve(socket):
        running = asyncio.create_task(js.codex_turn(params, "a prompt"))
        async with asyncio.timeout(10):
            while not fake.threads or not any(  # noqa: ASYNC110 - the probe's own connection
                t["status"] == "inProgress" for th in fake.threads.values() for t in th["turns"]
            ):
                await asyncio.sleep(0.01)
        (thread,) = fake.threads
        await fake.finish(thread, "STARTED")
        result = await asyncio.wait_for(running, 10)
    assert result["status"] == "completed" and result["thread"] == thread
    (start,) = fake.requests("thread/start")
    assert start["sandbox"] == "danger-full-access" and start["approvalPolicy"] == "never"
    assert start["config"]["shell_environment_policy.set"]["ZAB_BOT"] == "the bot"
    # the probe's thread is no session's: an inherited ZAB_SESSION is removed (#931's review)
    assert "ZAB_SESSION" not in start["config"]["shell_environment_policy.set"]
    assert "ZAB_SESSION" in start["config"]["shell_environment_policy.exclude"]
    assert "STARTED" not in json.dumps(result)


async def test_a_codex_turn_that_times_out_is_interrupted(tmp_path, monkeypatch):
    """#925's review, N4: as the Claude path interrupts its turn."""
    import asyncio

    from zab.testing.fake_codex import FakeAppServer

    monkeypatch.setattr(js, "TURN_S", 0.3)
    fake = FakeAppServer(str(tmp_path))
    fake.hold = True
    socket = str(tmp_path / "s.sock")
    params = {
        "home": str(tmp_path), "socket": socket, "bot": "b", "cwd": str(tmp_path),
        "approval": "never", "sandbox": "danger-full-access", "model": "", "effort": "",
    }  # fmt: skip
    async with fake.serve(socket):
        result = await asyncio.wait_for(js.codex_turn(params, "a prompt"), 10)
        (interrupt,) = fake.requests("turn/interrupt")
        state = await js.thread_status(params, result["thread"])
    assert result["status"] == "timeout" and interrupt["threadId"] == result["thread"]
    assert isinstance(state, str) and state  # what `thread/read` says, on a new connection


class FakeHost(js.Host):
    """`systemctl` answered from a table: every unit active, `restart` exiting with `restart`."""

    def __init__(self, restart: int) -> None:
        super().__init__(os.getuid(), {})
        self.restart, self.calls = restart, []

    def run(self, argv, *, env=None, timeout=60.0):
        self.calls.append(argv)
        if argv[2:3] == ["restart"]:
            return subprocess.CompletedProcess(argv, self.restart, "", "Access denied")
        return subprocess.CompletedProcess(argv, 0, "ActiveState=active\n", "")


@pytest.mark.parametrize("code", [0, 1])
def test_a_refused_restart_fails_its_check(tmp_path, code):
    """Codex round 1 on #925: a restart that systemctl refused leaves the units as they were,
    so their survival proves nothing; the restart's own check fails."""
    probe = js.Probe(FakeHost(code), js.Report(), "abc123", tmp_path / "work")
    probe.units = {"zab.service": {"ActiveState": "active"}}
    probe.turn_units = ["zab-probe655-abc123-claude"]
    probe.restarts_phase()
    assert ["systemctl", "--user", "restart", "zab.service"] in probe.host.calls
    survived = "restarts: zab-probe655-abc123-claude runs after restarting zab.service"
    assert survived in probe.report.passed
    restarted = "restarts: zab.service restarted"
    assert (restarted in probe.report.failed) == (code != 0)
    assert (restarted in probe.report.passed) == (code == 0)


class SystemdHost(FakeHost):
    """A host whose units are as `active` says, and which records every call."""

    def __init__(self, active: bool = True) -> None:
        super().__init__(0)
        self.is_active = active

    def run(self, argv, *, env=None, timeout=60.0):
        self.calls.append(argv)
        state = "active" if self.is_active else "inactive"
        return subprocess.CompletedProcess(argv, 0, f"ActiveState={state}\n", "")


def test_the_clean_up_runs_when_a_phase_fails(tmp_path, monkeypatch, capsys):
    """#925's review, N5: the slice stopped, its runtime drop-ins reverted, its file and the
    probe's directory removed, whatever stopped the run."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    probe = js.Probe(SystemdHost(), js.Report(), "abc123", tmp_path / "work")

    def broken() -> None:
        raise RuntimeError("a phase failed")

    monkeypatch.setattr(probe, "direct_phase", broken)
    try:
        assert js.main(probe) == 1
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.signal(signal.SIGHUP, signal.SIG_DFL)
    slice_file = tmp_path / "config" / "systemd" / "user" / js.SLICE
    assert probe.slice_written and not slice_file.exists() and not probe.workdir.exists()
    calls = probe.host.calls
    assert ["systemctl", "--user", "stop", js.SLICE] in calls
    assert ["systemctl", "--user", "revert", js.SLICE] in calls
    assert calls[-2:] == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "list-units", "--all", "--plain", "--no-legend",
         f"{probe.marker}-*"],
    ]  # fmt: skip
    assert "aborted: RuntimeError" in probe.report.failed


def test_sigterm_reaches_the_clean_up(tmp_path, monkeypatch):
    """#925's review, N4: a `kill` or a `timeout` wrapper ends the probe through its clean-up."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    probe = js.Probe(SystemdHost(), js.Report(), "abc123", tmp_path / "work")

    def killed() -> None:
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(5)  # the handler raises first

    monkeypatch.setattr(probe, "direct_phase", killed)
    try:
        assert js.main(probe) == 128 + signal.SIGTERM
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.signal(signal.SIGHUP, signal.SIG_DFL)
    assert "interrupted: SIGTERM" in probe.report.failed and not probe.workdir.exists()
    assert ["systemctl", "--user", "stop", js.SLICE] in probe.host.calls


def test_a_unit_inside_the_callers_cgroup_fails_its_check(tmp_path):
    """#925's review, N5: the check that matters when systemd-run did not fork the unit."""
    probe = js.Probe(SystemdHost(), js.Report(), "abc123", tmp_path / "work")
    probe.workdir.mkdir()
    caller = "/user.slice/user-1001.slice/session-3.scope"
    record = {"first": {"rc": 0, "seconds": 0.01}, "cgroup": caller, "pgid": 10, "sid": 10,
              "env": {"ZAB_BOT": True}}  # fmt: skip
    (probe.workdir / "caller-claude.json").write_text(json.dumps(record))

    def show(unit, *props):
        return {"MainPID": "0", "ControlGroup": caller + "/inner", "ActiveState": "active"}

    probe.host.show = show
    probe.check_turn_unit("V1 claude", "claude", caller)
    outside = "V1 claude: the unit is outside the turn's cgroup, zab.service's and the daemon's"
    assert outside in probe.report.failed


def test_the_shutdown_case_arms_then_reads(tmp_path, monkeypatch):
    """B3 (#923's review): armed before a VM stop, read after it, from the earlier boot."""
    host = SystemdHost()
    probe = js.Probe(host, js.Report(), "abc123", tmp_path / "work", shutdown="arm")
    probe.arm_shutdown()
    where = tmp_path / js.SHUTDOWN_DIR
    armed = json.loads((where / "armed.json").read_text())
    assert "B3: armed" in probe.report.passed and (where / "ended.py").exists()
    post = next(a for a in host.calls[0] if a.startswith("--property=ExecStopPost="))
    assert post.endswith(f"ended.py {where / 'ended.json'}")
    # read before the stop: the unit still runs
    probe = js.Probe(host, js.Report(), "abc123", tmp_path / "work", shutdown="read")
    probe.read_shutdown()
    assert probe.report.failed == ["B3: the VM was stopped since the probe was armed"]
    # after it: the record of the earlier boot, the manager stopping
    (where / "ended.json").write_text(json.dumps({
        "SERVICE_RESULT": "success", "EXIT_CODE": "killed", "EXIT_STATUS": "TERM",
        "boot_id": armed["boot_id"], "manager_state": "stopping", "system_state": "stopping",
    }))  # fmt: skip
    monkeypatch.setattr(js, "boot_id", lambda: "a later boot")
    probe = js.Probe(SystemdHost(active=False), js.Report(), "abc123", tmp_path / "work",
                     shutdown="read")  # fmt: skip
    probe.read_shutdown()
    assert probe.report.failed == [] and len(probe.report.passed) == 4
    assert not where.exists()


def test_the_shutdown_stand_in_records_to_disk(tmp_path):
    """B3's `ExecStopPost`, run as it will be: the ending, the boot, both states."""
    script = tmp_path / "ended.py"
    script.write_text(js.SHUTDOWN_PY)
    out = tmp_path / "ended.json"
    env = {**os.environ, "SERVICE_RESULT": "success", "EXIT_CODE": "killed", "EXIT_STATUS": "TERM"}
    run([sys.executable, "-I", str(script), str(out)], env=env, check=True)
    data = json.loads(out.read_text())
    assert data["EXIT_STATUS"] == "TERM" and data["boot_id"] == js.boot_id()
    assert {"manager_state", "system_state", "at"} <= data.keys()
