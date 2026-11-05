from unittest.mock import AsyncMock

import pytest

from zab.core.links import WorkspaceLinks
from zab.core.types import RenderContext
from zab.render.markdown import MarkdownRenderer
from zab.workspace.local import LocalWorkspace
from zab.workspace.manager import LocalWorkspaceManager

pytestmark = pytest.mark.integration


async def test_workspace_renderer_wiring(tmp_path):
    ws = LocalWorkspace(str(tmp_path))
    await ws.write_bytes("tracked.py", b"print('ok')\n")
    for args in (
        ["git", "init", "-q"],
        ["git", "add", "tracked.py"],
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "initial",
        ],
        ["git", "remote", "add", "origin", "https://github.com/owner/repo.git"],
        ["git", "update-ref", "refs/remotes/origin/main", "HEAD"],
        ["git", "config", "branch.master.remote", "origin"],
        ["git", "config", "branch.master.merge", "refs/heads/main"],
    ):
        assert (await ws.run(args)).exit_code == 0
    # Explicit branch selection avoids depending on the machine's init.defaultBranch.
    branch = (await ws.run(["git", "branch", "--show-current"])).stdout.strip()
    await ws.run(["git", "config", f"branch.{branch}.remote", "origin"])
    await ws.run(["git", "config", f"branch.{branch}.merge", "refs/heads/main"])
    await ws.write_bytes("local.py", b"local\n")
    await ws.write_bytes("plot.png", b"decoy image bytes")
    upload = AsyncMock(return_value="/user_uploads/plot.png")
    renderer = MarkdownRenderer(WorkspaceLinks(LocalWorkspaceManager()))
    parts = await renderer.render_agent_text(
        "tracked.py:1 `local.py` `plot.png`",
        RenderContext(
            workspace=ws, max_message_length=10000, max_upload_bytes=10000, upload=upload
        ),
    )
    sha = (await ws.run(["git", "rev-parse", "HEAD"])).stdout.strip()
    assert f"https://github.com/owner/repo/blob/{sha}/tracked.py#L1" in parts[0]
    assert "`local.py` *(local)*" in parts[0]
    assert "/user_uploads/plot.png" in parts[0]
    upload.assert_awaited_once()
    await ws.write_bytes("tracked.py", b"changed\n")
    parts = await renderer.render_agent_text(
        "tracked.py:1",
        RenderContext(workspace=ws, max_message_length=10000, max_upload_bytes=10000, upload=None),
    )
    assert "*(local)*" in parts[0]
