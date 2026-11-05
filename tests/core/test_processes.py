import asyncio
import os
import signal
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from zab.core import processes

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "native,executable,group",
    [
        ("session", "claude", 12345),
        ("other", "claude", 12345),
        ("session", "other", 12345),
        ("session", "claude", 111),
    ],
)
def test_orphan_leader_requires_matching_executable_session_and_group(
    tmp_path, monkeypatch, native, executable, group
):
    binary = tmp_path / "claude"
    binary.touch()
    (tmp_path / "other").touch()
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "cmdline").write_bytes(
        f"{tmp_path / executable}\0-p\0--session-id\0{native}\0".encode()
    )
    (proc / "stat").write_text("12345 (claude) " + " ".join(["0"] * 19 + ["123"]))
    (proc / "exe").symlink_to(tmp_path / executable)
    original_path = processes.Path
    monkeypatch.setattr(
        processes,
        "Path",
        lambda path: (
            proc
            if path in ("/proc", "/proc/12345")
            else proc / "stat"
            if path == "/proc/12345/stat"
            else original_path(path)
        ),
    )
    monkeypatch.setattr(os, "getpgid", lambda _: group)
    monkeypatch.setattr(os, "getsid", lambda _: 12345)
    expected = "123" if (native, executable, group) == ("session", "claude", 12345) else None
    assert processes.leader_identity(12345, "session", str(binary)) == expected


async def test_unverified_group_is_never_signalled(monkeypatch):
    monkeypatch.setattr(processes, "leader_identity", lambda *args: None)
    kill = Mock()
    monkeypatch.setattr(os, "killpg", kill)
    await processes.reap_claude_turn(12345, "session")
    kill.assert_not_called()


async def test_verified_orphan_receives_term_then_kill(monkeypatch):
    monkeypatch.setattr(processes, "leader_identity", lambda *args: "start")
    kill = Mock()
    monkeypatch.setattr(os, "killpg", kill)

    async def no_wait(_):
        return

    monkeypatch.setattr(processes.asyncio, "sleep", no_wait)
    await processes.reap_claude_turn(12345, "session")
    signals = [call.args[1] for call in kill.call_args_list if call.args[1]]
    assert signals == [signal.SIGTERM, signal.SIGKILL]


@pytest.mark.parametrize("node", [False, True])
def test_macos_leader_check_uses_ps_without_shell(tmp_path, monkeypatch, node):
    from types import SimpleNamespace

    binary = tmp_path / "claude"
    binary.touch()
    original_path = processes.Path
    monkeypatch.setattr(
        processes,
        "Path",
        lambda path: (
            SimpleNamespace(is_dir=lambda: False) if path == "/proc" else original_path(path)
        ),
    )
    monkeypatch.setattr(os, "getpgid", lambda _: 12345)
    monkeypatch.setattr(os, "getsid", lambda _: 12345)
    from zab.backends.claude import ClaudeConfig, build_argv
    from zab.core.creation import CONTEXT
    from zab.core.types import SessionSettings

    argv = build_argv(
        ClaudeConfig(binary=str(binary)),
        SessionSettings(),
        session_id="session",
        resume=True,
        context_prompt=CONTEXT,
    )
    if node:
        argv.insert(0, "/usr/local/bin/node")
    result = SimpleNamespace(stdout=f"{os.getuid()} Thu Oct 1 10:00:00 2026 " + " ".join(argv))
    run = Mock(
        side_effect=lambda args, **kwargs: (
            SimpleNamespace(stdout="1") if args[-1] == "ppid=" else result
        )
    )
    monkeypatch.setattr(processes.subprocess, "run", run)
    assert processes.leader_identity(12345, "session", str(binary)) == "Thu Oct 1 10:00:00 2026"
    assert run.call_args.args[0][:4] == ["/bin/ps", "-ww", "-p", "12345"]
    assert "shell" not in run.call_args.kwargs


async def test_reused_group_with_unverified_leader_is_not_killed(monkeypatch):
    monkeypatch.setattr(processes, "leader_identity", Mock(side_effect=["original", None]))
    kill = Mock()
    monkeypatch.setattr(os, "killpg", kill)
    monkeypatch.setattr(os, "kill", Mock())

    async def no_wait(_):
        return

    monkeypatch.setattr(processes.asyncio, "sleep", no_wait)
    await processes.reap_claude_turn(12345, "session")
    assert [call.args[1] for call in kill.call_args_list if call.args[1]] == [signal.SIGTERM]


def exec_done(pid: int) -> None:
    """Until /proc shows `pid`'s argv: a `Popen` returns partway through the child's exec, while
    its cmdline is still empty, so the launcher's print can come before it. Without /proc (macOS,
    where `processes` asks `ps`) there is nothing to read, so nothing to wait for here."""
    if not Path("/proc").is_dir():
        return
    deadline = time.monotonic() + 10
    while not Path(f"/proc/{pid}/cmdline").read_bytes() and time.monotonic() < deadline:
        time.sleep(0.01)


@pytest.mark.parametrize("bare", [False, True])
async def test_real_orphan_binary_is_reaped(tmp_path, monkeypatch, bare):
    import subprocess
    import sys

    binary = tmp_path / "claude"
    binary.symlink_to(sys.executable)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    executable = "claude" if bare else str(binary)
    launcher = (
        "import subprocess,sys; p=subprocess.Popen(sys.argv[1:], "
        "start_new_session=True, stdout=subprocess.DEVNULL, "
        "stderr=subprocess.DEVNULL); print(p.pid)"
    )
    result = await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            "-c",
            launcher,
            executable,
            "-c",
            "import time; time.sleep(60)",
            "-p",
            "--session-id",
            "decoy-session",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    )
    pid = int(result.stdout)
    try:
        await asyncio.to_thread(exec_done, pid)
        assert not processes.is_bridge_child(pid)
        assert processes.leader_identity(pid, "decoy-session", executable)
        await processes.reap_claude_turn(pid, "decoy-session", executable)
        # Exited leaders may remain zombies until the container's init reaps them.
        assert processes.leader_identity(pid, "decoy-session", executable) is None
    finally:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


async def test_real_live_child_is_not_reaped(tmp_path):
    import subprocess
    import sys

    child = await asyncio.to_thread(
        subprocess.Popen,
        [
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            "-p",
            "--session-id",
            "decoy-session",
        ],
        start_new_session=True,
    )
    try:
        assert processes.is_bridge_child(child.pid)
        await processes.reap_claude_turn(child.pid, "decoy-session", sys.executable)
        assert child.poll() is None
    finally:
        child.kill()
        await asyncio.to_thread(child.wait, timeout=5)


async def test_permission_denied_during_reap_is_nonfatal(monkeypatch):
    monkeypatch.setattr(processes, "leader_identity", lambda *args: "start")
    monkeypatch.setattr(os, "killpg", Mock(side_effect=PermissionError))
    await processes.reap_claude_turn(12345, "session")


@pytest.mark.parametrize(
    "stamp,expected", [("original", [signal.SIGTERM, signal.SIGKILL]), ("reused", [signal.SIGTERM])]
)
async def test_zombie_leader_retains_escalation_only_for_original_identity(
    monkeypatch, stamp, expected
):
    identities = iter(["original"])
    monkeypatch.setattr(processes, "leader_identity", lambda *args: next(identities, None))
    monkeypatch.setattr(processes, "zombie_identity", lambda pgid: stamp)
    kill = Mock()
    monkeypatch.setattr(os, "killpg", kill)  # surviving descendants keep group alive
    monkeypatch.setattr(os, "kill", Mock())  # original leader is still a zombie

    async def no_wait(_):
        return

    monkeypatch.setattr(processes.asyncio, "sleep", no_wait)
    await processes.reap_claude_turn(12345, "session")
    assert [call.args[1] for call in kill.call_args_list if call.args[1]] == expected
