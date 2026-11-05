"""Issue #36 (Drew, Sun 18:40): on a real workspace, a final answer's local text files are
uploaded as `<name>.<ext>.txt` through the manager's checks (SPEC §11.3, §11.4)."""

from __future__ import annotations

import os
from pathlib import Path

from zab.core.links import WorkspaceLinks
from zab.core.types import RenderContext, Upload
from zab.render.links import rewrite_links
from zab.render.markdown import protect_code, restore_code
from zab.workspace.local import LocalWorkspace
from zab.workspace.manager import LocalWorkspaceManager


async def test_local_text_files_are_uploaded_through_the_manager(repo: Path):
    (repo / "fermat").mkdir()
    (repo / "fermat" / "Solution.lean").write_text("theorem flt : True := trivial\n")
    (repo / "fermat" / "secret.lean").write_text("not for upload\n")
    os.link(repo / "fermat" / "secret.lean", repo / "fermat" / "alias.lean")  # 2 hard links
    sent: list[Upload] = []

    async def upload(item: Upload) -> str:
        sent.append(item)
        return f"/user_uploads/1/x/{item.name}"

    ws = LocalWorkspace(str(repo), str(repo / "fermat"))
    ctx = RenderContext(
        workspace=ws,
        max_message_length=10_000,
        max_upload_bytes=1 << 20,
        upload=upload,
        text_upload_ext=frozenset({"lean"}),
        final_answer=True,
    )
    text = "See `Solution.lean:1` and `alias.lean`."
    protected, table = protect_code(text)
    out, table = await rewrite_links(protected, table, ctx, WorkspaceLinks(LocalWorkspaceManager()))
    out = restore_code(out, table)
    assert [u.name for u in sent] == ["Solution.lean.txt"]
    assert "[fermat/Solution.lean:1](/user_uploads/1/x/Solution.lean.txt)" in out
    assert "`fermat/alias.lean` *(local)*" in out  # the hard-link refusal still applies
