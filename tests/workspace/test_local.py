"""LocalWorkspace: the Workspace protocol on the local filesystem (SPEC §10, §10.6)."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest

from zab.core.types import Workspace
from zab.workspace.local import LocalWorkspace

from .conftest import real


def test_is_a_workspace_and_defaults_cwd_to_root(tmp_path: Path):
    ws = LocalWorkspace(str(tmp_path))
    assert isinstance(ws, Workspace)
    assert ws.root == str(tmp_path) == ws.cwd


def test_cwd_and_tilde_are_normalized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    ws = LocalWorkspace("~/proj", "~/proj/sub")
    assert ws.root == str(tmp_path / "proj")
    assert ws.cwd == str(tmp_path / "proj" / "sub")


async def test_run_captures_output_exit_code_and_cwd(tmp_path: Path):
    sub = tmp_path / "sub"
    sub.mkdir()
    ws = LocalWorkspace(str(tmp_path), str(sub))
    res = await ws.run(["pwd"])
    assert res.exit_code == 0
    assert real(res.stdout.strip()) == real(sub)
    res = await ws.run(["pwd"], cwd=str(tmp_path))
    assert real(res.stdout.strip()) == real(tmp_path)
    res = await ws.run(["sh", "-c", "echo out; echo err >&2; exit 3"])
    assert (res.exit_code, res.stdout, res.stderr) == (3, "out\n", "err\n")


async def test_run_sets_git_environment(tmp_path: Path):
    ws = LocalWorkspace(str(tmp_path))
    assert (await ws.run(["printenv", "GIT_TERMINAL_PROMPT"])).stdout.strip() == "0"
    assert (await ws.run(["printenv", "GIT_OPTIONAL_LOCKS"])).stdout.strip() == "0"


async def test_run_drops_inherited_git_variables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Issue #36: an inherited GIT_DIR or GIT_INDEX_FILE must not reach the bridge's git;
    the identity variables (codex #50 round 3) do."""
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_COUNT",
        "GIT_CEILING_DIRECTORIES",
        "GIT_EXEC_PATH",
        "GIT_SSH_COMMAND",
        "GIT_PROXY_COMMAND",
    ):
        monkeypatch.setenv(name, "/nonexistent")
    monkeypatch.setenv("GIT_OPTIONAL_LOCKS", "1")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Op")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "op@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Op")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "op@example.com")
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2026-10-02T00:00:00Z")
    monkeypatch.setenv("GITHUB_TOKEN_LIKE", "kept")  # not a GIT_ variable
    ws = LocalWorkspace(str(tmp_path))
    # Only the GIT_ prefix is compared: GitHub Actions exports GITHUB_* variables (the CI
    # failure on PR #50), so the kept non-GIT_ variable is checked on its own.
    res = await ws.run(["sh", "-c", "env | grep '^GIT_' | LC_ALL=C sort"])  # byte order
    assert (await ws.run(["printenv", "GITHUB_TOKEN_LIKE"])).stdout == "kept\n"
    assert res.stdout == (
        "GIT_AUTHOR_EMAIL=op@example.com\n"
        "GIT_AUTHOR_NAME=Op\n"
        "GIT_COMMITTER_DATE=2026-10-02T00:00:00Z\n"
        "GIT_COMMITTER_EMAIL=op@example.com\n"
        "GIT_COMMITTER_NAME=Op\n"
        "GIT_OPTIONAL_LOCKS=0\n"
        "GIT_TERMINAL_PROMPT=0\n"
    )


async def test_run_times_out(tmp_path: Path):
    ws = LocalWorkspace(str(tmp_path))
    with pytest.raises(TimeoutError):
        await ws.run(["sleep", "5"], timeout_s=0.2)


async def test_run_timeout_kills_the_process_group(tmp_path: Path):
    """Codex round 1: a grandchild holding the pipes must not extend the timeout."""
    ws = LocalWorkspace(str(tmp_path))
    pidfile = tmp_path / "grandchild.pid"
    t0 = time.monotonic()
    with pytest.raises(TimeoutError):
        # 1 s, not less: a slow CI runner must get as far as writing the pid (offline-tests on
        # #146 read an empty file at 0.1 s in the cancellation test below)
        await ws.run(["sh", "-c", f"sleep 30 & echo $! > {pidfile}; sleep 30"], timeout_s=1.0)
    assert time.monotonic() - t0 < 5
    await _assert_gone(await _read_pid(pidfile), "grandchild of a timed-out run()")


async def _read_pid(pidfile: Path, wait_s: float = 5.0) -> int:
    """The pid a test's shell writes, once it is there (a slow runner may need a moment)."""
    for _ in range(int(wait_s / 0.05)):
        text = await asyncio.to_thread(_read_text, pidfile)
        if text.strip():
            return int(text)
        await asyncio.sleep(0.05)
    pytest.fail(f"{pidfile.name} was never written")


def _read_text(path: Path) -> str:
    try:
        return path.read_text()
    except FileNotFoundError:
        return ""


async def _assert_gone(pid: int, what: str) -> None:
    for _ in range(50):  # the reaper may take a moment after SIGKILL
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        await asyncio.sleep(0.05)
    os.kill(pid, 9)
    pytest.fail(f"{what} survived")


async def test_run_cancellation_kills_the_process_group(tmp_path: Path):
    """Codex round 3: cancelling the awaiting task must not orphan the child."""
    ws = LocalWorkspace(str(tmp_path))
    pidfile = tmp_path / "child.pid"
    task = asyncio.create_task(
        ws.run(["sh", "-c", f"echo $$ > {pidfile}; exec sleep 30"], timeout_s=30)
    )
    pid = await _read_pid(pidfile)  # the child is running: now cancel (not after a fixed 0.1 s)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _assert_gone(pid, "child of a cancelled run()")


async def test_read_write_stat_realpath(tmp_path: Path):
    ws = LocalWorkspace(str(tmp_path))
    await ws.write_bytes("a/b/c.txt", b"0123456789")
    assert (tmp_path / "a" / "b" / "c.txt").read_bytes() == b"0123456789"
    assert await ws.read_bytes("a/b/c.txt", 4) == b"0123"
    assert await ws.read_bytes(str(tmp_path / "a/b/c.txt"), 100) == b"0123456789"
    st = await ws.stat("a/b/c.txt")
    assert st is not None and st.size == 10 and not st.is_dir
    d = await ws.stat("a/b")
    assert d is not None and d.is_dir
    assert await ws.stat("missing") is None
    assert await ws.stat("a/b/c.txt/notadir") is None
    os.symlink(tmp_path / "a" / "b" / "c.txt", tmp_path / "link.txt")
    assert await ws.realpath("link.txt") == real(tmp_path / "a" / "b" / "c.txt")
    assert await ws.realpath("nope/../x") == real(tmp_path) + "/x"


async def test_stat_and_realpath_degrade_on_unusable_paths(tmp_path: Path):
    """Issue #36: agent text yields names the OS rejects; the renderer must not raise."""
    ws = LocalWorkspace(str(tmp_path))
    assert await ws.stat("a" * 300) is None  # ENAMETOOLONG
    assert await ws.stat("data:image/png;base64,iVBORw0KGgo=") is None
    assert await ws.stat("nul\0byte.png") is None  # ValueError from os.stat
    assert await ws.stat("/proc/1/root/secret") is None  # EACCES or ENOENT, never raised
    (tmp_path / "loop").symlink_to(tmp_path / "loop")
    assert await ws.stat("loop/x") is None  # ELOOP
    assert (await ws.realpath("nul\0byte.png")).startswith(str(tmp_path))
    assert (await ws.realpath("a" * 300)).endswith("a" * 300)


async def test_dotdot_is_resolved_after_symlinks(tmp_path: Path):
    """Codex round 1: link -> pkg/nested, so link/../file is pkg/file, not ./file."""
    (tmp_path / "pkg" / "nested").mkdir(parents=True)
    (tmp_path / "pkg" / "file").write_text("pkg/file\n")
    (tmp_path / "file").write_text("top-level file\n")
    os.symlink(tmp_path / "pkg" / "nested", tmp_path / "link")
    ws = LocalWorkspace(str(tmp_path))
    assert await ws.realpath("link/../file") == real(tmp_path / "pkg" / "file")
    assert await ws.read_bytes("link/../file", 100) == b"pkg/file\n"
    st = await ws.stat("link/../file")
    assert st is not None and st.size == len(b"pkg/file\n")
    await ws.write_bytes("link/../written", b"w")
    assert (tmp_path / "pkg" / "written").read_bytes() == b"w"
    assert not (tmp_path / "written").exists()
