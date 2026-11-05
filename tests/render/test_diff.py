"""Tests for zab.render.diff (SPEC §11.8)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from zab.core.types import DiffFile, DiffResult, RenderContext, Upload
from zab.render.diff import code_span, fence_for, human_size, render_diff, summary_line
from zab.render.markdown import MarkdownRenderer, protect_code

ACTIVE = re.compile(r"@(?:\*\*[^*\n]+\*\*|\*[^*\n]+\*)")  # a literal Zulip mention form


@dataclass
class FakeWorkspace:
    root: str = "/repo"
    cwd: str = "/repo"
    writes: dict[str, bytes] = field(default_factory=dict)

    async def write_bytes(self, path: str, data: bytes) -> None:
        self.writes[path] = data


@dataclass
class FakeUploader:
    uploads: list[Upload] = field(default_factory=list)

    async def __call__(self, upload: Upload) -> str:
        self.uploads.append(upload)
        return f"/user_uploads/1/abc/{upload.name}"


def _ctx(
    ws: FakeWorkspace,
    upload: FakeUploader | None,
    limit: int = 10_000,
    upload_limit: int = 10 << 20,
) -> RenderContext:
    return RenderContext(
        workspace=ws,  # type: ignore[arg-type]
        max_message_length=limit,
        max_upload_bytes=upload_limit,
        upload=upload,
        turn_number=3,
    )


PATCH = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n"
STAT = " x.py | 2 +-\n 1 file changed, 1 insertion(+), 1 deletion(-)\n"


def _diff(patch: str = PATCH, untracked: tuple[str, ...] = (), files: int = 1) -> DiffResult:
    return DiffResult(
        files_changed=files,
        insertions=40,
        deletions=12,
        stat=STAT,
        patch=patch,
        new_untracked=untracked,
    )


def test_summary_line() -> None:
    assert summary_line(_diff(files=3)) == "Changes: 3 files (+40 -12)"
    assert summary_line(_diff()) == "Changes: 1 file (+40 -12)"
    # without per-file data, new untracked files count as files and as new (§11.8, rc6)
    assert summary_line(_diff(untracked=("a", "b"))) == "Changes: 3 files (+40 -12), 2 new"
    seven = DiffResult(files_changed=2, insertions=0, deletions=0, stat="", patch="", files=SEVEN)
    assert summary_line(seven) == "Changes: 7 files (+1210 -3), 5 new"


async def test_inline_spoiler() -> None:
    ws, up = FakeWorkspace(), FakeUploader()
    out = await render_diff(_diff(files=3, untracked=("a.txt", "b/c.py")), _ctx(ws, up))
    assert out == (
        "Changes: 5 files (+40 -12), 2 new\n````spoiler Details\n"
        "- `x.py`\n- `a.txt` new\n- `b/c.py` new\n```diff\n" + PATCH.rstrip("\n") + "\n```\n````"
    )
    assert not up.uploads and not ws.writes


async def test_line_count_triggers_upload() -> None:
    ws, up = FakeWorkspace(), FakeUploader()
    patch = "\n".join(f"+line {i}" for i in range(301)) + "\n"
    out = await render_diff(_diff(patch), _ctx(ws, up))
    assert out == (
        "Changes: 1 file (+40 -12): [turn3.diff](/user_uploads/1/abc/turn3.diff)\n"
        "```spoiler Details\n- `x.py`\n```"
    )
    assert [u.name for u in up.uploads] == ["turn3.diff"]
    assert re.fullmatch(r"/repo/\.zab/outbox/turn3-[0-9a-f]{8}\.diff", up.uploads[0].path)
    assert ws.writes[up.uploads[0].path] == patch.encode()


async def test_line_limit_is_configurable() -> None:
    ws, up = FakeWorkspace(), FakeUploader()
    patch = "\n".join(f"+line {i}" for i in range(300)) + "\n"
    assert "```diff\n" in await render_diff(_diff(patch), _ctx(ws, up))
    out = await render_diff(_diff(patch), _ctx(ws, up), inline_max_lines=100)
    assert out.startswith("Changes: 1 file (+40 -12): [turn3.diff]") and "```diff" not in out
    renderer = MarkdownRenderer(diff_inline_max_lines=100)
    out = await renderer.render_diff(_diff(patch), _ctx(ws, up))
    assert out.startswith("Changes: 1 file (+40 -12): [turn3.diff]")


async def test_message_size_triggers_upload() -> None:
    ws, up = FakeWorkspace(), FakeUploader()
    patch = "\n".join("+" + "x" * 200 for _ in range(100)) + "\n"  # 100 lines, ~20 000 chars
    out = await render_diff(_diff(patch), _ctx(ws, up))
    assert out.startswith("Changes: 1 file (+40 -12): [turn3.diff](/user_uploads/1/abc/turn3.diff)")
    assert any(k.startswith("/repo/.zab/outbox/turn3-") for k in ws.writes)


async def test_too_large_without_uploads() -> None:
    ws = FakeWorkspace()
    patch = "\n".join(f"+line {i}" for i in range(400)) + "\n"
    out = await render_diff(_diff(patch, untracked=("n.txt",)), _ctx(ws, None))
    assert out == (
        "Changes: 2 files (+40 -12), 1 new (diff too large to inline)\n"
        "```spoiler Details\n- `x.py`\n- `n.txt` new\n```"
    )
    assert not ws.writes


async def test_untracked_only() -> None:
    diff = DiffResult(
        files_changed=0, insertions=0, deletions=0, stat="", patch="", new_untracked=("new.txt",)
    )
    assert not diff.empty
    out = await render_diff(diff, _ctx(FakeWorkspace(), FakeUploader()))
    assert out == "Changes: 1 file (+0 -0), 1 new\n```spoiler Details\n- `new.txt` new\n```"


KEY = "sk-ant-" + "A" * 25


async def test_secrets_are_redacted_inline() -> None:
    """Codex round 1 on #23: a key in the patch or a file name never reaches Zulip."""
    ws, up = FakeWorkspace(), FakeUploader()
    diff = DiffResult(
        files_changed=1,
        insertions=1,
        deletions=0,
        stat=f" {KEY}.txt | 1 +\n",
        patch=f"--- a/k\n+++ b/k\n@@ -0,0 +1 @@\n+{KEY}\n",
        new_untracked=(f"notes/{KEY}.md",),
    )
    out = await render_diff(diff, _ctx(ws, up))
    assert KEY not in out and "[redacted anthropic key]" in out
    assert out.startswith("Changes: 2 files (+1 -0), 1 new\n````spoiler Details\n")
    assert "- `notes/[redacted anthropic key].md` new\n```diff\n" in out


async def test_secrets_are_redacted_before_upload() -> None:
    ws, up = FakeWorkspace(), FakeUploader()
    patch = "\n".join(f"+line {i}" for i in range(300)) + f"\n+{KEY}\n"
    diff = DiffResult(
        files_changed=1, insertions=301, deletions=0, stat=f" {KEY} | 1\n", patch=patch
    )
    out = await render_diff(diff, _ctx(ws, up))
    assert KEY not in out and "[redacted anthropic key]" in out
    written = ws.writes[up.uploads[-1].path].decode()
    assert KEY not in written and "+[redacted anthropic key]\n" in written
    out = await render_diff(diff, _ctx(ws, None))
    assert KEY not in out and "(diff too large to inline)" in out


async def test_deleted_key_lines_are_redacted() -> None:
    """Codex round 2 on PR #48: the slug guard must not treat a unified diff's deletion marker
    as part of a hyphenated token; removing a key is as sensitive as adding one."""
    ws, up = FakeWorkspace(), FakeUploader()
    openai = "sk-proj-" + "B" * 25
    patch = f"--- a/k\n+++ b/k\n@@ -1,2 +0,0 @@\n-{KEY}\n-OPENAI_API_KEY={openai}\n"
    diff = DiffResult(files_changed=1, insertions=0, deletions=2, stat=" k | 2 --\n", patch=patch)
    out = await render_diff(diff, _ctx(ws, up))
    assert KEY not in out and openai not in out
    assert "\n-[redacted anthropic key]\n-OPENAI_API_KEY=[redacted openai key]\n" in out
    long_patch = "\n".join(f"-line {i}" for i in range(300)) + f"\n-{KEY}\n-{openai}\n"
    diff = DiffResult(
        files_changed=1, insertions=0, deletions=302, stat=" k | 302\n", patch=long_patch
    )
    out = await render_diff(diff, _ctx(ws, up))
    written = ws.writes[up.uploads[-1].path].decode()
    assert KEY not in written and openai not in written and KEY not in out
    assert "-[redacted anthropic key]\n-[redacted openai key]\n" in written


async def test_zulip_key_lines_are_redacted_behind_diff_prefixes() -> None:
    """Codex round 2 on #23: `+key=`, `-key=` and ` key=` lines are still whole-line keys."""
    k1, k2, k3 = "a" * 32, "b" * 32, "c" * 32
    patch = f"--- a/z\n+++ b/z\n@@ -1,2 +1,2 @@\n key={k1}\n-key={k2}\n+key={k3}\n"
    diff = DiffResult(files_changed=1, insertions=1, deletions=1, stat=" z | 2 +-\n", patch=patch)
    out = await render_diff(diff, _ctx(FakeWorkspace(), None))
    assert k1 not in out and k2 not in out and k3 not in out
    assert " [redacted zulip api key]\n-[redacted zulip api key]\n+[redacted zulip api key]" in out


async def test_fallback_message_is_bounded() -> None:
    """Codex round 1 on #23: the file list is clipped to the message limit."""
    ws, up = FakeWorkspace(), FakeUploader()
    stat = "\n".join(f" file{i:04d}.py | 1 +" for i in range(800)) + "\n 800 files changed\n"
    untracked = tuple(f"new/file{i:04d}.txt" for i in range(500))
    patch = "\n".join(f"+line {i}" for i in range(400)) + "\n"
    diff = DiffResult(
        files_changed=800,
        insertions=800,
        deletions=0,
        stat=stat,
        patch=patch,
        new_untracked=untracked,
    )
    for upload in (up, None):
        out = await render_diff(diff, _ctx(ws, upload))
        assert len(out) <= 9_800
        assert re.search(r"\n- \.\.\. \(\d+ more\)\n```$", out)
        assert out.startswith("Changes: 1300 files (+800 -0), 500 new")


async def test_untracked_only_is_bounded() -> None:
    untracked = tuple(f"new/file{i:04d}.txt" for i in range(500))
    diff = DiffResult(
        files_changed=0, insertions=0, deletions=0, stat="", patch="", new_untracked=untracked
    )
    out = await render_diff(diff, _ctx(FakeWorkspace(), FakeUploader()))
    assert len(out) <= 9_800
    assert out.startswith(
        "Changes: 500 files (+0 -0), 500 new\n```spoiler Details\n- `new/file0000.txt` new"
    )
    assert out.endswith("more)\n```")


async def test_inline_with_a_long_file_list_clips_the_list() -> None:
    untracked = tuple(f"new/file{i:04d}.txt" for i in range(500))
    out = await render_diff(_diff(untracked=untracked), _ctx(FakeWorkspace(), None))
    assert len(out) <= 9_800
    assert out.startswith("Changes: 501 files (+40 -12), 500 new\n````spoiler Details\n")
    assert re.search(r"\n- \.\.\. \(\d+ more\)\n```diff\n", out)  # the patch stays
    assert out.endswith("\n```\n````")


async def test_mentions_in_file_names_and_stat_are_disarmed() -> None:
    """Codex round 3 on #23: a file called @**all**.txt must notify nobody."""
    diff = DiffResult(
        files_changed=1,
        insertions=1,
        deletions=0,
        stat=" @**all**.txt | 1 +\n",
        patch="--- a/@**all**.txt\n+++ b/@**all**.txt\n@@ -0,0 +1 @@\n+@*ops* @**Bob**\n",
        new_untracked=("@**everyone**.md", "@*admins*/x.py", "@_**Bob**.txt", "x` @**all** `y.txt"),
    )
    for upload in (FakeUploader(), None):
        out = await render_diff(diff, _ctx(FakeWorkspace(), upload))
        assert not ACTIVE.search(out), out
        assert "@\u200b**everyone**.md" in out and "@\u200b*admins*/x.py" in out
        assert "- ``x` @\u200b**all** `y.txt``" in out  # backticks in the name cannot expose it
        assert "@_**Bob**.txt" in out  # a silent mention stays as it is
    big = DiffResult(
        files_changed=1, insertions=1, deletions=0, stat=" @**all**.txt | 1 +\n", patch="+x\n" * 400
    )
    out = await render_diff(big, _ctx(FakeWorkspace(), None))
    assert not ACTIVE.search(out) and "@\u200b**all**.txt" in out


async def test_upload_size_limit_is_respected() -> None:
    """Codex round 4 on #23: a patch over ctx.max_upload_bytes is not uploaded (§11.4)."""
    patch = "\n".join(f"+line {i}" for i in range(400)) + "\n"
    size = len(patch.encode())
    assert human_size(size) == "3.8 kB" and human_size(1024) == "1.0 kB"
    ws, up = FakeWorkspace(), FakeUploader()
    out = await render_diff(_diff(patch, untracked=("n.txt",)), _ctx(ws, up, upload_limit=1024))
    assert out == (
        "Changes: 2 files (+40 -12), 1 new (diff too large to upload: 3.8 kB, limit 1.0 kB)\n"
        "```spoiler Details\n- `x.py`\n- `n.txt` new\n```"
    )
    assert not up.uploads and not ws.writes
    out = await render_diff(_diff(patch), _ctx(ws, up, upload_limit=size))  # exactly at the limit
    assert out.startswith("Changes: 1 file (+40 -12): [turn3.diff]")
    assert [u.name for u in up.uploads] == ["turn3.diff"]


async def test_concurrent_diff_renders_upload_their_own_patch() -> None:
    """Codex round 7 follow-up: sessions sharing a workspace and turn number keep their files."""
    import asyncio

    ws = FakeWorkspace()

    class SlowUploader(FakeUploader):
        async def __call__(self, upload: Upload) -> str:
            await asyncio.sleep(0)  # let the other render write its file first
            return await super().__call__(upload)

    up = SlowUploader()
    a = "\n".join(f"+a {i}" for i in range(400)) + "\n"
    b = "\n".join(f"+b {i}" for i in range(400)) + "\n"
    ctx = _ctx(ws, up)
    await asyncio.gather(render_diff(_diff(a), ctx), render_diff(_diff(b), ctx))
    assert len(up.uploads) == 2 and len({u.path for u in up.uploads}) == 2
    assert {u.name for u in up.uploads} == {"turn3.diff"}
    assert {ws.writes[u.path] for u in up.uploads} == {a.encode(), b.encode()}


async def test_zulip_key_lines_are_redacted_with_crlf() -> None:
    """Codex round 7: CRLF lines keep their `\r` and lose the key, prefixes included."""
    k = "k" * 32
    patch = f"--- a/z\r\n+++ b/z\r\n@@ -1 +1 @@\r\n-key={k}\r\n+key={k}\r\n"
    diff = DiffResult(files_changed=1, insertions=1, deletions=1, stat=" z | 2 +-\n", patch=patch)
    out = await render_diff(diff, _ctx(FakeWorkspace(), None))
    assert k not in out
    assert "-[redacted zulip api key]\r\n+[redacted zulip api key]" in out


#: A 7-file turn as the workspace reports it (§11.8, rc6): committed and uncommitted, new,
#: deleted, binary, hidden, and new untracked files.
SEVEN = (
    DiffFile(path="genus2/curves.py", status="new", insertions=800, deletions=0),
    DiffFile(path="genus2/README.md", status="new", insertions=40, deletions=0),
    DiffFile(path="old/notes.txt", status="deleted", insertions=0, deletions=3),
    DiffFile(path="plots/fig1.png", status="new", binary=True),
    DiffFile(path=".env", status="modified", hidden=True),
    DiffFile(path="report/index.html", status="new", insertions=350, deletions=0),
    DiffFile(path="scratch.sage", status="new", insertions=20, deletions=0),
)

HIDDEN_STAT = (
    STAT + " .env | changed (contents hidden)\n sub/id_ed25519 | changed (contents hidden)\n"
)


async def test_hidden_changes_are_listed_under_an_inline_patch() -> None:
    """Codex #50 round 2: the owner must learn that a denylisted file changed (§11.8)."""
    diff = DiffResult(files_changed=3, insertions=1, deletions=1, stat=HIDDEN_STAT, patch=PATCH)
    out = await render_diff(diff, _ctx(FakeWorkspace(), FakeUploader()))
    assert out == (
        "Changes: 3 files (+1 -1)\n````spoiler Details\n- `x.py`\n"
        "- `.env` changed (contents hidden)\n- `sub/id_ed25519` changed (contents hidden)\n"
        "```diff\n" + PATCH.rstrip("\n") + "\n```\n````"
    )
    diff = DiffResult(
        files_changed=3,
        insertions=1,
        deletions=1,
        stat=HIDDEN_STAT,
        patch=PATCH,
        new_untracked=("new.txt",),
    )
    out = await render_diff(diff, _ctx(FakeWorkspace(), None))
    assert "- `sub/id_ed25519` changed (contents hidden)\n- `new.txt` new\n```diff" in out


async def test_hidden_list_is_clipped_within_the_limit() -> None:
    hidden = "".join(f" secret{i}.pem | changed (contents hidden)\n" for i in range(40))
    diff = DiffResult(
        files_changed=41,
        insertions=1,
        deletions=1,
        stat=STAT + hidden,
        patch=PATCH,
        new_untracked=("a.txt", "b.txt"),
    )
    limit = len(PATCH) + 60 + 200 + 120  # room for the line, the spoiler and a short list
    out = await render_diff(diff, _ctx(FakeWorkspace(), FakeUploader(), limit=limit))
    assert len(out) <= limit - 200
    assert "````spoiler Details" in out and "changed (contents hidden)" in out
    assert re.search(r"- \.\.\. \(\d+ more\)\n```diff\n", out)  # clipped, patch kept


async def test_hidden_changes_appear_in_the_uploaded_form() -> None:
    ws, up = FakeWorkspace(), FakeUploader()
    patch = "\n".join(f"+line {i}" for i in range(301)) + "\n"
    diff = DiffResult(files_changed=2, insertions=301, deletions=0, stat=HIDDEN_STAT, patch=patch)
    out = await render_diff(diff, _ctx(ws, up))
    assert out.startswith("Changes: 2 files (+301 -0): [turn3.diff]")
    assert "- `.env` changed (contents hidden)" in out and "sub/id_ed25519" in out


async def test_hidden_changes_show_their_names_without_a_patch() -> None:
    """Issue #36: the workspace hides denylisted files; the list still names them."""
    stat = " .env | changed (contents hidden)\n sub/id_ed25519 | changed (contents hidden)\n"
    diff = DiffResult(files_changed=2, insertions=0, deletions=0, stat=stat, patch="")
    assert not diff.empty
    out = await render_diff(diff, _ctx(FakeWorkspace(), FakeUploader()))
    assert out == (
        "Changes: 2 files (+0 -0)\n```spoiler Details\n- `.env` changed (contents hidden)\n"
        "- `sub/id_ed25519` changed (contents hidden)\n```"
    )
    diff = DiffResult(
        files_changed=1, insertions=0, deletions=0, stat=stat, patch="", new_untracked=("x",)
    )
    out = await render_diff(diff, _ctx(FakeWorkspace(), None))
    assert out.endswith("- `x` new\n```") and "contents hidden" in out


def test_fence_for_and_code_span() -> None:
    assert fence_for("no ticks") == "```"
    assert fence_for("a ``` b") == "````"
    assert fence_for("`````") == "``````"
    assert code_span("plain.txt") == "`plain.txt`"
    assert code_span("x` y") == "``x` y``"
    assert code_span("`tick.txt") == "`` `tick.txt ``"


async def test_fences_outgrow_backtick_runs_in_the_patch() -> None:
    """Issue #36: a context line of three backticks must not close the diff block, and a file
    name with backticks must not close the spoiler."""
    patch = "--- a/README.md\n+++ b/README.md\n@@ -1,3 +1,3 @@\n ```\n-old\n+new\n ```\n"
    out = await render_diff(_diff(patch, untracked=("x` y.txt",)), _ctx(FakeWorkspace(), None))
    assert out.startswith("Changes: 2 files (+40 -12), 1 new\n`````spoiler Details\n")
    assert "- ``x` y.txt`` new\n````diff\n" in out and out.endswith("\n ```\n````\n`````")
    # Under Zulip's exact-closer rule (protect_code), the message is one spoiler holding the
    # list and one diff block, the ``` context lines included.
    _protected, table = protect_code(out)
    fences = [e.text for e in table if e.kind == "fence"]
    assert fences[0] == "`````spoiler Details" and fences[-1] == "`````"
    assert any(f.startswith("````diff\n") and f.endswith("\n ```\n````") for f in fences)
    diff = DiffResult(
        files_changed=1, insertions=1, deletions=0, stat=" a```b | 1 +\n", patch="+x\n" * 400
    )
    out = await render_diff(diff, _ctx(FakeWorkspace(), None))
    # the name's code span uses four backticks, so the spoiler fence takes five
    assert out.endswith("\n`````spoiler Details\n- ````a```b````\n`````")


async def test_empty_diff_gives_nothing() -> None:
    diff = DiffResult(files_changed=0, insertions=0, deletions=0, stat="", patch="")
    assert diff.empty
    assert await render_diff(diff, _ctx(FakeWorkspace(), FakeUploader())) == ""
    assert await MarkdownRenderer().render_diff(diff, _ctx(FakeWorkspace(), None)) == ""


async def test_uploaded_patch_preserves_mentions_but_inline_disarms() -> None:
    ws, up = FakeWorkspace(), FakeUploader()
    patch = PATCH.replace("-old", "-@**all**").replace("+new", "+@**everyone**")
    inline = await render_diff(_diff(patch), _ctx(ws, up))
    assert not ACTIVE.search(inline)
    await render_diff(_diff(patch), _ctx(ws, up), inline_max_lines=1)
    assert next(iter(ws.writes.values())) == patch.encode()


async def test_nearly_full_inline_patch_keeps_untracked_notice() -> None:
    ws = FakeWorkspace()
    text = await render_diff(_diff("+" + "x" * 9739, untracked=("new.txt",)), _ctx(ws, None))
    assert "new.txt" in text
    assert len(text) <= 9800


async def test_a_near_limit_inline_patch_is_never_dropped() -> None:
    """codex on #164: with a short file list, the inline check passed but the spoiler wanted
    room for the "(n more)" notice, and the message came out as the bare summary line, with
    neither the patch nor an upload. Every size around the limit now gives the patch, inline
    or uploaded."""
    for size in range(9700, 9760):
        diff = DiffResult(
            files_changed=1,
            insertions=1,
            deletions=0,
            stat="",
            patch="+" + "x" * size,
            files=(DiffFile(path="a", status="modified", insertions=1, deletions=0),),
        )
        ws, up = FakeWorkspace(), FakeUploader()
        text = await render_diff(diff, _ctx(ws, up))
        assert ("x" * 100 in text) or ("turn3.diff" in text and up.uploads), size
        assert len(text) <= 9800


async def test_new_files_link_into_the_turn_snapshot():
    """rc6 (SPEC §10.7): new files present in the snapshot link to the file server."""
    from zab.render.diff import render_diff

    diff = DiffResult(
        files_changed=0,
        insertions=0,
        deletions=0,
        stat="",
        patch="",
        new_untracked=("report/index.html", "big.dat"),
    )
    ctx = RenderContext(
        workspace=None,
        max_message_length=10_000,
        max_upload_bytes=1 << 20,
        upload=None,  # type: ignore[arg-type]
    )
    out = await render_diff(
        diff, ctx, links={"report/index.html": "https://h/s/r/c/report/index.html"}
    )
    assert "- [`report/index.html`](https://h/s/r/c/report/index.html)" in out
    assert "- `big.dat`" in out


async def test_the_file_list_from_the_workspace_with_links_counts_and_states() -> None:
    """rc6 (Drew's rehearsal, #36 Mon 18:25): one visible line, then one spoiler with every
    file the turn touched: links to the snapshot, new/deleted, +a -d, binary, hidden."""
    diff = DiffResult(
        files_changed=5,
        insertions=1190,
        deletions=3,
        stat="(not shown any more)",
        patch=PATCH,
        changed=("genus2/curves.py", "genus2/README.md", "old/notes.txt", "plots/fig1.png", ".env"),
        new_untracked=("report/index.html", "scratch.sage"),
        files=SEVEN,
    )
    links = {
        "genus2/curves.py": "https://f/r/s/genus2/curves.py",
        "report/index.html": "https://f/r/s/report/index.html",
        ".env": "https://f/r/s/.env",  # never linked: hidden
    }
    out = await render_diff(diff, _ctx(FakeWorkspace(), None), links=links)
    assert out == (
        "Changes: 7 files (+1210 -3), 5 new\n````spoiler Details\n"
        "- [`genus2/curves.py`](https://f/r/s/genus2/curves.py) new, +800 -0\n"
        "- `genus2/README.md` new, +40 -0\n"
        "- `old/notes.txt` deleted, +0 -3\n"
        "- `plots/fig1.png` new, binary\n"
        "- `.env` changed (contents hidden)\n"
        "- [`report/index.html`](https://f/r/s/report/index.html) new, +350 -0\n"
        "- `scratch.sage` new, +20 -0\n"
        "```diff\n" + PATCH.rstrip("\n") + "\n```\n````"
    )
    assert "(not shown any more)" not in out  # no --stat block


async def test_hidden_new_files_and_uncounted_files() -> None:
    files = (
        DiffFile(path="id_ed25519", status="new", hidden=True),
        DiffFile(path="huge.csv", status="new"),
        DiffFile(path="x.py", insertions=2, deletions=1),
    )
    diff = DiffResult(files_changed=1, insertions=2, deletions=1, stat="", patch=PATCH, files=files)
    out = await render_diff(diff, _ctx(FakeWorkspace(), None))
    assert out.startswith("Changes: 3 files (+2 -1), 2 new\n")
    assert "- `id_ed25519` new, contents hidden\n- `huge.csv` new\n- `x.py` +2 -1\n" in out


class _SnapshotResolver:
    """Only `snapshot_paths`: every asked path is in the snapshot, at its relative path."""

    def __init__(self) -> None:
        self.asked: list[list[str]] = []

    async def snapshot_paths(self, ws, sha, paths):
        self.asked.append(sorted(paths))
        return {p: p.removeprefix("/repo/") for p in paths}


class _LinkWorkspace(FakeWorkspace):
    async def realpath(self, path: str) -> str:
        return path


async def test_every_listed_file_links_into_the_snapshot_except_hidden_and_deleted() -> None:
    """rc6: the single file list links every file with a snapshot copy, committed ones
    included (not just new untracked files); the provider is awaited only when there is a
    file to link (SPEC §10.7)."""
    from zab.core.types import PublishedSnapshot

    calls = []

    async def provide():
        calls.append(1)
        return PublishedSnapshot(sha="c" * 40, url="https://files.example/r/" + "c" * 40)

    resolver = _SnapshotResolver()
    renderer = MarkdownRenderer(resolver)
    ctx = _ctx(_LinkWorkspace(), None)
    ctx.snapshot = provide
    diff = DiffResult(files_changed=5, insertions=0, deletions=0, stat="", patch=PATCH, files=SEVEN)
    out = await renderer.render_diff(diff, ctx)
    base = "https://files.example/r/" + "c" * 40
    assert f"- [`genus2/curves.py`]({base}/genus2/curves.py) new, +800 -0" in out
    assert f"- [`plots/fig1.png`]({base}/plots/fig1.png) new, binary" in out
    assert "- `old/notes.txt` deleted" in out and "- `.env` changed (contents hidden)" in out
    assert "/repo/.env" not in resolver.asked[0] and "/repo/old/notes.txt" not in resolver.asked[0]
    assert calls == [1]
    only_hidden = DiffResult(
        files_changed=2,
        insertions=0,
        deletions=0,
        stat="",
        patch="",
        files=(SEVEN[2], SEVEN[4]),  # a deletion and a hidden file: nothing to link
    )
    await renderer.render_diff(only_hidden, ctx)
    assert calls == [1]  # the provider was not asked
