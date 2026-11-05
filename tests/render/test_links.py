"""Tests for zab.render.links (SPEC §11.3, §11.4; AT-12) with fake workspace and resolver."""

from __future__ import annotations

import posixpath
import time
from collections.abc import Sequence
from dataclasses import dataclass, field

import pytest

from zab.core.types import FileStat, PublishedSnapshot, RenderContext, Upload, UploadRefused
from zab.render.links import LinkTarget, RepoInfo, find_candidates, rewrite_links
from zab.render.markdown import MarkdownRenderer, protect_code, restore_code

SHA = "4e1d2aa0f3c9b8d7e6a5b4c3d2e1f0a9b8c7d6e5"
WEB = "https://github.com/o/r"
Z = "\u200b"  # the zero-width space that disarms a mention


@dataclass
class FakeWorkspace:
    root: str = "/repo"
    cwd: str = "/repo/sub"
    files: dict[str, bool] = field(default_factory=dict)  # canonical path -> is_dir
    symlinks: dict[str, str] = field(default_factory=dict)  # path prefix -> target prefix
    writes: dict[str, bytes] = field(default_factory=dict)
    sizes: dict[str, int] = field(default_factory=dict)  # canonical path -> size (default 10)
    contents: dict[str, bytes] = field(default_factory=dict)  # read_bytes (default: text)

    async def stat(self, path: str) -> FileStat | None:
        if path not in self.files:
            return None
        return FileStat(size=self.sizes.get(path, 10), is_dir=self.files[path], mtime=0.0)

    async def realpath(self, path: str) -> str:
        path = posixpath.normpath(path)
        for src, dst in self.symlinks.items():
            if path == src or path.startswith(src + "/"):
                path = dst + path[len(src) :]
        return path

    async def write_bytes(self, path: str, data: bytes) -> None:
        self.writes[path] = data

    async def read_bytes(self, path: str, max_bytes: int) -> bytes:
        return self.contents.get(path, b"theorem t : True := trivial\n")[:max_bytes]


@dataclass
class FakeResolver:
    repo: RepoInfo | None
    tracked: set[str] = field(default_factory=set)
    modified: set[str] = field(default_factory=set)
    refused: set[str] = field(default_factory=set)
    classify_calls: int = 0
    checked: list[str] = field(default_factory=list)
    snapshot: set[str] = field(default_factory=set)  # canonical paths in the turn snapshot

    async def repo_info(self, ws: FakeWorkspace) -> RepoInfo | None:
        return self.repo

    async def classify(self, ws: FakeWorkspace, paths: Sequence[str]) -> dict[str, LinkTarget]:
        self.classify_calls += 1
        return {
            p: LinkTarget(
                exists=p in ws.files,
                is_dir=ws.files.get(p, False),
                tracked=p in self.tracked,
                modified=p in self.modified,
            )
            for p in paths
        }

    async def snapshot_paths(
        self, ws: FakeWorkspace, sha: str, paths: Sequence[str]
    ) -> dict[str, str]:
        return {p: posixpath.relpath(p, ws.root) for p in paths if p in self.snapshot}

    async def check_upload(self, ws: FakeWorkspace, path: str, *, explicit: bool) -> Upload:
        self.checked.append(path)
        if path in self.refused:
            raise UploadRefused("refused by the denylist")
        return Upload(path=path, name=posixpath.basename(path))


@dataclass
class FakeUploader:
    uploads: list[Upload] = field(default_factory=list)

    async def __call__(self, upload: Upload) -> str:
        self.uploads.append(upload)
        return f"/user_uploads/1/abc/{upload.name}"


def _ws() -> FakeWorkspace:
    return FakeWorkspace(
        files={
            "/repo/src/foo.py": False,
            "/repo/notes.md": False,
            "/repo/new.py": False,
            "/repo/plots/plot.png": False,
            "/repo/plots/fig].png": False,
            "/repo/secret.png": False,
            "/repo/docs": True,
            "/repo/sub/local.py": False,
            "/repo/src/a#b.py": False,
            "/repo/my%file.md": False,
            "/outside/x.py": False,
        },
        symlinks={"/repo/link": "/outside"},
    )


def _resolver(pushed: bool = True, web_url: str | None = WEB) -> FakeResolver:
    return FakeResolver(
        repo=RepoInfo(head_sha=SHA, pushed=pushed, web_url=web_url, root="/repo"),
        tracked={
            "/repo/src/foo.py",
            "/repo/notes.md",
            "/repo/plots/plot.png",
            "/repo/secret.png",
            "/repo/docs",
            "/repo/sub/local.py",
            "/repo/src/a#b.py",
            "/repo/my%file.md",
        },
        modified={"/repo/notes.md"},
        refused={"/repo/secret.png"},
    )


def _ctx(ws: FakeWorkspace, upload: FakeUploader | None) -> RenderContext:
    return RenderContext(
        workspace=ws,  # type: ignore[arg-type]
        max_message_length=10_000,
        max_upload_bytes=10 << 20,
        upload=upload,
        turn_number=3,
    )


async def _rewrite(text: str, resolver: FakeResolver, upload: FakeUploader | None = None) -> str:
    protected, table = protect_code(text)
    out, table = await rewrite_links(protected, table, _ctx(_ws(), upload), resolver)
    return restore_code(out, table)


# ------------------------------------------------------------------------------ candidates

CANDIDATE_TABLE = [
    ("see src/foo.py:10-20 here", [("bare", "src/foo.py:10-20", "src/foo.py", (10, 20))]),
    ("at foo.py:7.", [("bare", "foo.py:7", "foo.py", (7, None))]),
    ("time 12:30 and v1:2", []),
    ("[label](src/foo.py:12)", [("link", "src/foo.py:12", "src/foo.py", (12, None))]),
    ("[l](src/foo.py#L3-L4)", [("link", "src/foo.py#L3-L4", "src/foo.py", (3, 4))]),
    ("[l](src/foo.py#L3C2-L4C9)", [("link", "src/foo.py#L3C2-L4C9", "src/foo.py", (3, 4))]),
    ("[l](src/foo.py:12:5)", [("link", "src/foo.py:12:5", "src/foo.py", (12, None))]),
    ("at src/foo.py:12:5.", [("bare", "src/foo.py:12:5", "src/foo.py", (12, None))]),
    ("at src/foo.py:12:5-13:1", [("bare", "src/foo.py:12:5-13:1", "src/foo.py", (12, 13))]),
    ("[u](https://x.y/z.py) [a](#top) [m](mailto:a@b.c) [f](/user_uploads/1/x.png)", []),
    ("[d](data:text/plain,hi) [j](javascript:alert(1)) [f](file:///etc/passwd)", []),
    ("[c](c:src/foo.py)", []),  # a scheme-looking prefix is never a path
    ("![plot](plots/plot.png)", [("link", "plots/plot.png", "plots/plot.png", None)]),
    ("http://h/a/b.py:12", []),
    (
        "see /repo/src/foo.py:12 here",
        [("bare", "/repo/src/foo.py:12", "/repo/src/foo.py", (12, None))],
    ),
    ("/outside/x.py:1-3", [("bare", "/outside/x.py:1-3", "/outside/x.py", (1, 3))]),
    ("a//b.py:1", [("bare", "a//b.py:1", "a//b.py", (1, None))]),  # a relative path
]


@pytest.mark.parametrize(("text", "expected"), CANDIDATE_TABLE)
def test_find_candidates_in_text(text: str, expected: list) -> None:
    protected, table = protect_code(text)
    got = [(c.kind, c.written, c.path, c.lines) for c in find_candidates(protected, table)]
    assert got == expected


SPAN_TABLE = [
    ("`src/foo.py:120`", [("span", "src/foo.py:120", "src/foo.py", (120, None))]),
    ("`src/foo.py:120:7`", [("span", "src/foo.py:120:7", "src/foo.py", (120, None))]),
    ("`README.md`", [("span", "README.md", "README.md", None)]),
    ("`docs/`", [("span", "docs/", "docs/", None)]),
    ("`pytest -q`", []),
    ("`data:text/plain,hi` and `file:///x/y.py`", []),
    ("`main`", []),
    ("`--bound 1e6`", []),
    ("```\nsrc/foo.py:10\n```", []),  # fences are never candidates
]


@pytest.mark.parametrize(("text", "expected"), SPAN_TABLE)
def test_find_candidates_in_spans(text: str, expected: list) -> None:
    protected, table = protect_code(text)
    got = [(c.kind, c.written, c.path, c.lines) for c in find_candidates(protected, table)]
    assert got == expected


# ------------------------------------------------------------------------------- rendering


async def test_permalink_forms() -> None:
    """AT-12: a tracked, pushed file with a line number gets a permalink at the SHA."""
    r = _resolver()
    assert await _rewrite("see src/foo.py:10-20 here", r) == (
        f"see [src/foo.py:10-20]({WEB}/blob/{SHA}/src/foo.py#L10-L20) here"
    )
    assert await _rewrite("in `src/foo.py:120`.", r) == (
        f"in [src/foo.py:120]({WEB}/blob/{SHA}/src/foo.py#L120)."
    )
    assert (
        await _rewrite("[the parser](src/foo.py)", r)
        == f"[the parser]({WEB}/blob/{SHA}/src/foo.py)"
    )
    assert await _rewrite("[l](src/foo.py#L3-L4)", r) == f"[l]({WEB}/blob/{SHA}/src/foo.py#L3-L4)"
    assert (
        await _rewrite("`src/foo.py:5-5`", r) == f"[src/foo.py:5-5]({WEB}/blob/{SHA}/src/foo.py#L5)"
    )


async def test_column_suffixes_render_line_anchors() -> None:
    """Issue #36: `path:line:col` and `#LxCy` parse; anchors and labels keep the line only."""
    r = _resolver()
    assert await _rewrite("`src/foo.py:12:5`", r) == (
        f"[src/foo.py:12:5]({WEB}/blob/{SHA}/src/foo.py#L12)"
    )
    assert await _rewrite("[l](src/foo.py#L3C2-L4C9)", r) == (
        f"[l]({WEB}/blob/{SHA}/src/foo.py#L3-L4)"
    )
    assert await _rewrite("see src/foo.py:12:5-13:1.", r) == (
        f"see [src/foo.py:12:5-13:1]({WEB}/blob/{SHA}/src/foo.py#L12-L13)."
    )


async def test_local_forms() -> None:
    """AT-12: modified or untracked files, and unpushed repos, get a code span and *(local)*."""
    r = _resolver()
    assert await _rewrite("edit `notes.md:4`", r) == "edit `notes.md:4` *(local)*"
    assert await _rewrite("new.py:3 is new", r) == "`new.py:3` *(local)* is new"
    assert await _rewrite("[notes](notes.md)", r) == "notes (`notes.md` *(local)*)"
    assert await _rewrite("[notes.md](notes.md)", r) == "`notes.md` *(local)*"
    assert await _rewrite("`src/foo.py:1`", _resolver(pushed=False)) == "`src/foo.py:1` *(local)*"
    assert await _rewrite("`src/foo.py:1`", _resolver(web_url=None)) == "`src/foo.py:1` *(local)*"
    assert await _rewrite("`src/foo.py:1`", FakeResolver(repo=None)) == "`src/foo.py:1` *(local)*"


async def test_uploads() -> None:
    """AT-12: an image is uploaded and linked even when it is committed."""
    r, up = _resolver(), FakeUploader()
    assert (
        await _rewrite("plot: `plots/plot.png`", r, up)
        == "plot: [plot.png](/user_uploads/1/abc/plot.png)"
    )
    assert up.uploads == [Upload(path="/repo/plots/plot.png", name="plot.png")]
    assert (
        await _rewrite("![my plot](plots/plot.png)", r, up)
        == "[my plot](/user_uploads/1/abc/plot.png)"
    )
    assert r.checked[-1] == "/repo/plots/plot.png"


async def test_auto_upload_links_escape_the_name_and_quote_the_url() -> None:
    """Same escaping as `!file` (#70): a bracket in the file name cannot end the link label, and
    the URL is quoted."""
    r, up = _resolver(), FakeUploader()
    assert (
        await _rewrite("`plots/fig].png`", r, up) == r"[fig\].png](/user_uploads/1/abc/fig%5D.png)"
    )


async def test_failing_uploader_degrades_to_the_next_row() -> None:
    """Issue #36: an upload error must not lose the answer; the file gets the next row."""

    class Boom(FakeUploader):
        async def __call__(self, upload: Upload) -> str:
            raise ConnectionError("network down")

    r = _resolver()
    assert await _rewrite("`plots/plot.png`", r, Boom()) == (
        f"[plots/plot.png]({WEB}/blob/{SHA}/plots/plot.png)"
    )
    assert await _rewrite("`new.png`", r, Boom()) == "`new.png`"  # does not exist: unchanged


async def test_oversized_files_are_not_uploaded() -> None:
    """Issue #36: files over ctx.max_upload_bytes never reach check_upload or the uploader."""
    ws, r, up = _ws(), _resolver(), FakeUploader()
    ws.sizes["/repo/plots/plot.png"] = 20 << 20
    protected, table = protect_code("`plots/plot.png`")
    out, table = await rewrite_links(protected, table, _ctx(ws, up), r)
    assert restore_code(out, table) == f"[plots/plot.png]({WEB}/blob/{SHA}/plots/plot.png)"
    assert not up.uploads and r.checked == []


async def test_upload_dedup_within_a_message() -> None:
    r, up = _resolver(), FakeUploader()
    await _rewrite("`plots/plot.png` and again `plots/plot.png`", r, up)
    assert len(up.uploads) == 1


async def test_refused_upload_falls_through() -> None:
    r, up = _resolver(), FakeUploader()
    assert await _rewrite("`secret.png`", r, up) == f"[secret.png]({WEB}/blob/{SHA}/secret.png)"
    assert not up.uploads


async def test_no_upload_callback_falls_through() -> None:
    r = _resolver()
    assert await _rewrite("`plots/plot.png`", r, None) == (
        f"[plots/plot.png]({WEB}/blob/{SHA}/plots/plot.png)"
    )
    assert r.checked == []


async def test_outside_root_and_missing_paths_are_left_alone() -> None:
    r = _resolver()
    for text in (
        "`../outside/x.py`",
        "`link/x.py:1`",
        "[x](/outside/x.py)",
        "`/etc/passwd`",
        "`src/nope.py:1`",
        "[gone](does/not/exist.py)",
    ):
        assert await _rewrite(text, r) == text, text


async def test_absolute_bare_tokens() -> None:
    """Codex round 5 on #23: an absolute `path:line` token is a candidate (§11.3)."""
    r = _resolver()
    assert await _rewrite("at /repo/src/foo.py:12.", r) == (
        f"at [src/foo.py:12]({WEB}/blob/{SHA}/src/foo.py#L12)."
    )
    assert (
        await _rewrite("/repo/src/foo.py:3-4", r)
        == f"[src/foo.py:3-4]({WEB}/blob/{SHA}/src/foo.py#L3-L4)"
    )
    assert await _rewrite("/repo/new.py:1", r) == "`new.py:1` *(local)*"
    assert (
        await _rewrite("/outside/x.py:1 and /repo/link/x.py:2", r)
        == "/outside/x.py:1 and /repo/link/x.py:2"
    )


async def test_symlinked_workspace_root() -> None:
    """Issue #36 / AT-12: a root reached through a symlink must still get its links."""
    ws = _ws()
    ws.root, ws.cwd = "/link", "/link/sub"
    ws.symlinks["/link"] = "/repo"  # the root is reached through a symlink
    for repo_root in ("/repo", "/link"):  # git may report either spelling
        r = _resolver()
        r.repo = RepoInfo(head_sha=SHA, pushed=True, web_url=WEB, root=repo_root)
        protected, table = protect_code("`src/foo.py:120` and `local.py:2`")
        out, table = await rewrite_links(protected, table, _ctx(ws, None), r)
        assert restore_code(out, table) == (
            f"[src/foo.py:120]({WEB}/blob/{SHA}/src/foo.py#L120) and "
            f"[sub/local.py:2]({WEB}/blob/{SHA}/sub/local.py#L2)"
        )


async def test_cwd_then_root_resolution() -> None:
    r = _resolver()
    # Spans and tokens are shown root-relative, however the agent wrote them (issue #36).
    assert (
        await _rewrite("`local.py:2`", r) == f"[sub/local.py:2]({WEB}/blob/{SHA}/sub/local.py#L2)"
    )
    assert await _rewrite("`src/foo.py`", r) == f"[src/foo.py]({WEB}/blob/{SHA}/src/foo.py)"
    assert await _rewrite("`/repo/src/foo.py`", r) == f"[src/foo.py]({WEB}/blob/{SHA}/src/foo.py)"
    assert await _rewrite("`/repo/new.py:3:4`", r) == "`new.py:3:4` *(local)*"


async def test_permalink_paths_are_percent_encoded() -> None:
    """Codex round 1 on #23: `#`, spaces and the like must not end the URL path."""
    r = _resolver()
    assert (
        await _rewrite("`src/a#b.py:1`", r) == f"[src/a#b.py:1]({WEB}/blob/{SHA}/src/a%23b.py#L1)"
    )
    assert await _rewrite("[f](my%file.md)", r) == f"[f]({WEB}/blob/{SHA}/my%25file.md)"


async def test_span_inside_a_link_label_is_kept_as_the_label() -> None:
    """Codex round 2 on #23: no nested links when the label is itself a code span."""
    r = _resolver()
    assert (
        await _rewrite("[`src/foo.py`](src/foo.py)", r)
        == f"[`src/foo.py`]({WEB}/blob/{SHA}/src/foo.py)"
    )
    assert await _rewrite("[`x`](src/foo.py:3)", r) == f"[`x`]({WEB}/blob/{SHA}/src/foo.py#L3)"
    assert (
        await _rewrite("[`src/foo.py`](https://example.org)", r)
        == "[`src/foo.py`](https://example.org)"
    )
    assert await _rewrite("[`notes.md`](notes.md)", r) == "`notes.md` (`notes.md` *(local)*)"


async def test_directories_use_tree() -> None:
    r = _resolver()
    assert await _rewrite("[docs](docs)", r) == f"[docs]({WEB}/tree/{SHA}/docs)"
    assert await _rewrite("`docs/`", r) == f"[docs]({WEB}/tree/{SHA}/docs)"


async def test_one_classify_call_per_message() -> None:
    r = _resolver()
    await _rewrite("`src/foo.py:1`, notes.md:2, [d](docs) and `local.py`", r)
    assert r.classify_calls == 1


async def test_fenced_code_is_never_rewritten() -> None:
    r = _resolver()
    text = "```\nsrc/foo.py:10\n```\nand src/foo.py:10"
    assert await _rewrite(text, r) == (
        f"```\nsrc/foo.py:10\n```\nand [src/foo.py:10]({WEB}/blob/{SHA}/src/foo.py#L10)"
    )


async def test_renderer_wiring() -> None:
    r, up = _resolver(), FakeUploader()
    text = "See `src/foo.py:120` and $x$; plot `plots/plot.png`; ping @**all**."
    parts = await MarkdownRenderer(r).render_agent_text(text, _ctx(_ws(), up))
    assert parts == [
        f"See [src/foo.py:120]({WEB}/blob/{SHA}/src/foo.py#L120) and $$x$$; "
        f"plot [plot.png](/user_uploads/1/abc/plot.png); ping `@{Z}**all**`."
    ]
    unchanged = await MarkdownRenderer().render_agent_text("See `src/foo.py:120`", _ctx(_ws(), up))
    assert unchanged == ["See `src/foo.py:120`"]


# ----------------------------------------------------------------------------- Zulip times

TIMES = [
    "<time:2026-10-27T01:08:38Z>",
    "<time:2026-10-27T01:08:38-04:00>",
    "<time:2026-10-27 01:08:38 UTC>",
    "<time:2026-10-27 notes.md:4>",  # Zulip takes all of it, to the `>`, as the time
]


@pytest.mark.parametrize("stamp", TIMES)
def test_nothing_inside_a_zulip_time_is_a_candidate(stamp: str) -> None:
    """Issue #673: `<time:…>` is Zulip's timestamp, which each reader sees in their own time
    zone. Nothing inside it is a file reference: not `time:2026-10` (lines 2026-10 of a file
    `time`), nor a path after a space. A reference beside it still is."""
    protected, table = protect_code(f"started {stamp}, see src/foo.py:10")
    assert [(c.kind, c.written) for c in find_candidates(protected, table)] == [
        ("bare", "src/foo.py:10")
    ]


def test_unclosed_zulip_times_are_scanned_in_linear_time() -> None:
    """Issue #673: a time holds at most 100 characters, so a line of `<time:` that never closes
    is not rescanned from each one (on CPU time, as in test_markdown)."""
    text = "<time:" * 30_000 + " src/foo.py:10"
    start = time.process_time()
    found = find_candidates(*protect_code(text))
    assert time.process_time() - start < 1.0  # unbounded, it took about 12 s
    assert [c.written for c in found] == ["src/foo.py:10"]


async def test_a_zulip_time_is_never_rewritten_even_with_a_file_named_time() -> None:
    """Issue #673: with a file `time` in the cwd and in the root, every stamp stays as written,
    and so does a link label holding one, while the references beside them are rewritten."""
    ws = _ws()
    ws.files.update({"/repo/sub/time": False, "/repo/time": False})
    r = _resolver()
    r.tracked |= {"/repo/sub/time", "/repo/time"}
    stamps = " ".join(TIMES)
    text = f"{stamps} and src/foo.py:10; [run of {TIMES[0]}](src/foo.py)"
    protected, table = protect_code(text)
    out, table = await rewrite_links(protected, table, _ctx(ws, None), r)
    assert restore_code(out, table) == (
        f"{stamps} and [src/foo.py:10]({WEB}/blob/{SHA}/src/foo.py#L10); "
        f"[run of {TIMES[0]}]({WEB}/blob/{SHA}/src/foo.py)"
    )


async def test_a_zulip_time_survives_the_final_answer() -> None:
    """Issue #673: through redaction, math, link rewriting, mention disarming and a split into
    parts, each `<time:…>` of an answer reaches Zulip whole."""
    stamp = TIMES[0]
    para = f"Started {stamp} with $x^2$, ping @**all**, see src/foo.py:10. " + "y" * 60
    shown = (
        f"Started {stamp} with $$x^2$$, ping `@{Z}**all**`, "
        f"see [src/foo.py:10]({WEB}/blob/{SHA}/src/foo.py#L10). " + "y" * 60
    )
    parts = await MarkdownRenderer(_resolver()).render_agent_text(
        "\n\n".join([para] * 60), _ctx(_ws(), None)
    )
    assert len(parts) == 2 and all(len(p) <= 9_800 for p in parts)
    bodies = [p.rsplit("\n\n", 1)[0] for p in parts]
    assert "\n\n".join(bodies) == "\n\n".join([shown] * 60)


async def test_upload_file_posts_the_checked_upload_and_needs_a_callback(tmp_path):
    """`!file` helper (SPEC §11.4): sends the already-checked Upload through ctx.upload and
    returns the markdown link; without an upload callback it refuses like any other refusal."""
    from zab.core.types import Upload, UploadRefused
    from zab.render.links import upload_file
    from zab.workspace.local import LocalWorkspace

    sent: list[str] = []

    async def fake_upload(upload: Upload) -> str:
        sent.append(upload.path)
        return "/user_uploads/1/abc/notes.md"

    ws = LocalWorkspace(str(tmp_path))
    ctx = RenderContext(
        workspace=ws, max_message_length=10000, max_upload_bytes=1000, upload=fake_upload
    )
    up = Upload(path=str(tmp_path / "notes.md"), name="notes.md")
    assert await upload_file(ctx, up) == "[notes.md](/user_uploads/1/abc/notes.md)"
    assert sent == [up.path]
    ctx_off = RenderContext(
        workspace=ws, max_message_length=10000, max_upload_bytes=1000, upload=None
    )
    with pytest.raises(UploadRefused, match="not enabled"):
        await upload_file(ctx_off, up)


async def test_upload_link_escapes_label_and_url(tmp_path):
    from zab.core.types import Upload
    from zab.render.links import upload_file
    from zab.workspace.local import LocalWorkspace

    async def upload(_):
        return "/user_uploads/1/plot (1).png"

    ctx = RenderContext(
        workspace=LocalWorkspace(str(tmp_path)),
        max_message_length=10000,
        max_upload_bytes=1000,
        upload=upload,
    )
    result = await upload_file(ctx, Upload(path="plot [1].png", name="plot [1].png"))
    assert result == r"[plot \[1\].png](/user_uploads/1/plot%20%281%29.png)"


# ------------------------------------------------------- files the turn touched (issue #36)


def _turn_ws() -> FakeWorkspace:
    """Drew's case: a tracked, pushed root README.md, and a directory the turn just created."""
    return FakeWorkspace(
        cwd="/repo",
        files={
            "/repo/README.md": False,
            "/repo/fermat/README.md": False,
            "/repo/other/README.md": False,
            "/repo/fermat/Solution.lean": False,
            "/repo/a/notes.md": False,
            "/repo/b/notes.md": False,
            "/repo/src/foo.py": False,
        },
    )


async def _rewrite_turn(text: str, turn_files: frozenset[str] | None) -> str:
    resolver = FakeResolver(
        repo=RepoInfo(head_sha=SHA, pushed=True, web_url=WEB, root="/repo"),
        tracked={"/repo/README.md", "/repo/src/foo.py"},
    )
    ctx = _ctx(_turn_ws(), None)
    ctx.turn_files = turn_files
    protected, table = protect_code(text)
    out, table = await rewrite_links(protected, table, ctx, resolver)
    return restore_code(out, table)


TURN = frozenset({"fermat/README.md", "fermat/Solution.lean", "a/notes.md", "b/notes.md"})


async def test_a_file_the_turn_created_beats_an_untouched_one_at_the_same_written_path():
    # Without turn information: today's resolution, the root's README (the bug Drew hit)
    assert (
        await _rewrite_turn("`README.md:1`", None)
        == f"[README.md:1]({WEB}/blob/{SHA}/README.md#L1)"
    )
    # The turn created fermat/README.md: that one, shown root-relative and local
    assert await _rewrite_turn("`README.md:1`", TURN) == "`fermat/README.md:1` *(local)*"
    assert (
        await _rewrite_turn("see README.md:1 now", TURN) == "see `fermat/README.md:1` *(local)* now"
    )
    # A name that resolved to nothing now finds the one touched file ending with it
    assert await _rewrite_turn("`Solution.lean:7`", TURN) == "`fermat/Solution.lean:7` *(local)*"


async def test_several_touched_matches_are_ambiguous_and_linked_nowhere():
    files = TURN | {"src/notes.md"}
    # notes.md resolves to nothing at the root: left as written, as before
    assert await _rewrite_turn("see notes.md:2", files) == "see notes.md:2"
    # README.md resolves to the untouched root file, but two touched files end with it
    two = TURN | {"other/README.md"}
    assert await _rewrite_turn("`README.md:1`", two) == "`README.md:1`"
    assert await _rewrite_turn("see README.md:1", two) == "see `README.md:1`"
    assert await _rewrite_turn("[the readme](README.md)", two) == "the readme (`README.md`)"


async def test_written_paths_that_already_name_a_touched_or_unrelated_file_are_unchanged():
    assert await _rewrite_turn("`fermat/README.md`", TURN) == "`fermat/README.md` *(local)*"
    # an untouched file no touched file ends with keeps its permalink
    assert (
        await _rewrite_turn("`src/foo.py:3`", TURN)
        == f"[src/foo.py:3]({WEB}/blob/{SHA}/src/foo.py#L3)"
    )
    # absolute paths are unambiguous
    assert (
        await _rewrite_turn("`/repo/README.md:1`", TURN)
        == f"[README.md:1]({WEB}/blob/{SHA}/README.md#L1)"
    )
    # a turn that touched nothing relevant changes nothing
    assert (
        await _rewrite_turn("`README.md:1`", frozenset())
        == f"[README.md:1]({WEB}/blob/{SHA}/README.md#L1)"
    )


# -------------------------------------------- text uploads (issue #36, Drew Sun 18:40)

TEXT_EXT = frozenset({"lean", "txt", "md", "json", "csv"})


def _text_ws(**contents: bytes) -> FakeWorkspace:
    files = {f"/repo/proof/{name}.lean": False for name in "abcdefghijkl"}
    files |= {
        "/repo/proof/Solution.lean": False,
        "/repo/proof/notes.txt": False,
        "/repo/proof/data.json": False,
        "/repo/proof/blob.lean": False,
        "/repo/proof/latin1.md": False,
        "/repo/proof/big.lean": False,
        "/repo/proof/page.html": False,
        "/repo/proof/.env.json": False,
        "/repo/src/foo.py": False,
    }
    ws = FakeWorkspace(cwd="/repo/proof", files=files)
    ws.contents = {
        "/repo/proof/blob.lean": b"\x00\x01binary",
        "/repo/proof/latin1.md": "caf\xe9".encode("latin-1"),
        **{k: v for k, v in contents.items()},
    }
    ws.sizes = {"/repo/proof/big.lean": 600 * 1024}
    return ws


async def _rewrite_text(text: str, *, final: bool = True, limit: int = 10, ws=None):
    resolver = FakeResolver(
        repo=RepoInfo(head_sha=SHA, pushed=True, web_url=WEB, root="/repo"),
        tracked={"/repo/src/foo.py"},
        refused={"/repo/proof/.env.json"},
    )
    uploader = FakeUploader()
    ctx = _ctx(ws or _text_ws(), uploader)
    ctx.text_upload_ext, ctx.final_answer, ctx.max_text_uploads = TEXT_EXT, final, limit
    protected, table = protect_code(text)
    out, table = await rewrite_links(protected, table, ctx, resolver)
    return restore_code(out, table), uploader.uploads


async def test_a_local_lean_file_is_uploaded_as_txt():
    out, ups = await _rewrite_text("Created `Solution.lean:7` and [the notes](notes.txt).")
    assert all(u.data == b"theorem t : True := trivial\n" for u in ups)  # the checked bytes
    by_name = {u.name: u.path for u in ups}  # .txt keeps its name
    assert by_name == {
        "Solution.lean.txt": "/repo/proof/Solution.lean",
        "notes.txt": "/repo/proof/notes.txt",
    }
    assert out == (
        "Created [proof/Solution.lean:7](/user_uploads/1/abc/Solution.lean.txt) and "
        "[the notes](/user_uploads/1/abc/notes.txt)."
    )


async def test_text_uploads_skip_what_must_not_or_cannot_be_read():
    out, ups = await _rewrite_text(
        "`blob.lean`, `latin1.md`, `big.lean`, `page.html`, `.env.json`, `src/foo.py:3`"
    )
    assert ups == []
    assert "`proof/blob.lean` *(local)*" in out  # binary
    assert "`proof/latin1.md` *(local)*" in out  # not UTF-8
    assert "`proof/big.lean` *(local)*" in out  # over the per-file cap
    assert "`proof/page.html` *(local)*" in out  # HTML is not a text upload
    assert "`proof/.env.json` *(local)*" in out  # the denylist (check_upload)
    assert f"[src/foo.py:3]({WEB}/blob/{SHA}/src/foo.py#L3)" in out  # committed: permalink


async def test_at_most_ten_distinct_text_uploads_per_answer():
    refs = " ".join(f"`{name}.lean`" for name in "abcdefghijkl") + " `a.lean:3` `a.lean:9`"
    out, ups = await _rewrite_text(refs)
    assert len(ups) == 10 and len({u.path for u in ups}) == 10  # a.lean once, though cited 3x
    assert out.count("*(local)*") == 2 and "`proof/k.lean` *(local)*" in out
    assert out.count("/user_uploads/1/abc/a.lean.txt") == 3


async def test_text_uploads_only_in_final_answers_and_when_enabled():
    out, ups = await _rewrite_text("`Solution.lean:7`", final=False)  # commentary
    assert ups == [] and out == "`proof/Solution.lean:7` *(local)*"
    out, ups = await _rewrite_text("`Solution.lean:7`", limit=0)
    assert ups == [] and out == "`proof/Solution.lean:7` *(local)*"


async def test_text_uploads_redact_secrets_like_the_answer():
    """Codex review on #124: a referenced log's contents reach Zulip, so they are redacted."""
    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0" * 2
    ws = _text_ws()
    ws.files["/repo/proof/debug.log"] = False
    ws.contents["/repo/proof/debug.log"] = f"step 1 ok\nGITHUB_TOKEN={token}\n".encode()
    resolver = FakeResolver(repo=RepoInfo(head_sha=SHA, pushed=True, web_url=WEB, root="/repo"))
    uploader = FakeUploader()
    ctx = _ctx(ws, uploader)
    ctx.text_upload_ext, ctx.final_answer = frozenset({"log"}), True
    protected, table = protect_code("See `debug.log`.")
    await rewrite_links(protected, table, ctx, resolver)
    [up] = uploader.uploads
    assert up.name == "debug.log.txt" and up.data is not None
    assert token.encode() not in up.data and b"[redacted" in up.data and b"step 1 ok" in up.data


# --------------------------------- the turn's snapshot on the central file server (rc6, §10.7)

SNAP = PublishedSnapshot(sha="c" * 40, url="https://files.mathagents.org/alice/" + "c" * 40)


def provider(snapshot):
    """The core's lazy provider (SPEC §10.7), counting how often it is awaited."""

    async def provide():
        provide.calls += 1
        return snapshot

    provide.calls = 0
    return provide


def _snap_ws() -> FakeWorkspace:
    return FakeWorkspace(
        cwd="/repo",
        files={
            "/repo/proof/Solution.lean": False,
            "/repo/report/index.html": False,
            "/repo/report": True,
            "/repo/docs/guide.html": False,
            "/repo/src/foo.py": False,
            "/repo/plots/plot.png": False,
            "/repo/.env.json": False,
            "/repo/notes.md": False,
            "/repo/scripts/check.gp": False,
            "/repo/gap/Groups.gi": False,
            "/repo/scripts/..py": False,
            "/repo/data/table.csv": False,
        },
    )


async def _rewrite_snap(text: str, *, snapshot=SNAP, uploader=None, provide=None):
    resolver = FakeResolver(
        repo=RepoInfo(head_sha=SHA, pushed=True, web_url=WEB, root="/repo"),
        tracked={"/repo/src/foo.py", "/repo/docs/guide.html", "/repo/plots/plot.png"},
        snapshot={
            "/repo/proof/Solution.lean",
            "/repo/report/index.html",
            "/repo/report",
            "/repo/docs/guide.html",
            "/repo/src/foo.py",
            "/repo/plots/plot.png",
            "/repo/notes.md",
            "/repo/scripts/check.gp",
            "/repo/gap/Groups.gi",
            "/repo/scripts/..py",
            "/repo/data/table.csv",
        },
    )
    ctx = _ctx(_snap_ws(), uploader)
    if provide is None and snapshot is not None:
        provide = provider(snapshot)
    ctx.snapshot, ctx.final_answer, ctx.text_upload_ext = provide, True, frozenset({"md", "lean"})
    protected, table = protect_code(text)
    out, table = await rewrite_links(protected, table, ctx, resolver)
    return restore_code(out, table)


async def test_files_in_the_snapshot_link_to_refserve():
    base = SNAP.url
    assert await _rewrite_snap("`proof/Solution.lean:7`") == (
        f"[proof/Solution.lean:7]({base}/proof/Solution.lean#L7)"
    )
    assert await _rewrite_snap("[the report](report/index.html)") == (
        f"[the report]({base}/report/index.html)"
    )
    assert await _rewrite_snap("`report/`") == f"[report]({base}/report/)"  # a directory listing
    # HTML always goes to refserve, even tracked and pushed: GitHub would show its source
    assert await _rewrite_snap("`docs/guide.html`") == f"[docs/guide.html]({base}/docs/guide.html)"


async def test_rendered_sources_on_the_file_server_keep_the_line():
    """Issue #448: a source file the file server renders with line numbers keeps the line as
    `#L<n>`, the only anchor its pages have (rows `id="L<n>"`, workshop/files/render.py)."""
    base = SNAP.url
    assert await _rewrite_snap("see scripts/check.gp:42.") == (
        f"see [scripts/check.gp:42]({base}/scripts/check.gp#L42)."
    )
    assert await _rewrite_snap("[the bound](scripts/check.gp#L7)") == (
        f"[the bound]({base}/scripts/check.gp#L7)"
    )
    assert await _rewrite_snap("`src/foo.py:12:5`") == f"[src/foo.py:12:5]({base}/src/foo.py#L12)"
    assert await _rewrite_snap("`src/foo.py`") == f"[src/foo.py]({base}/src/foo.py)"  # no line
    assert (
        await _rewrite_snap("`gap/Groups.gi:12`") == f"[gap/Groups.gi:12]({base}/gap/Groups.gi#L12)"
    )


async def test_a_range_on_the_file_server_opens_at_its_first_line():
    """The server's pages have no range anchor: `#L10-L20` would match no row and open at the
    top, so a range links to its first line and the label keeps it as written (issue #448)."""
    base = SNAP.url
    assert await _rewrite_snap("`proof/Solution.lean:10-20`") == (
        f"[proof/Solution.lean:10-20]({base}/proof/Solution.lean#L10)"
    )
    assert await _rewrite_snap("[l](src/foo.py#L3C2-L4C9)") == f"[l]({base}/src/foo.py#L3)"
    assert await _rewrite_snap("src/foo.py:5-5") == f"[src/foo.py:5-5]({base}/src/foo.py#L5)"


async def test_files_without_line_numbers_on_the_file_server_get_no_anchor():
    """Markdown renders without line anchors, HTML is the agent's own page, and other files
    (`.csv`, directories) are not rendered: no `#L` for them (issue #448)."""
    base = SNAP.url
    assert await _rewrite_snap("`notes.md:3`") == f"[notes.md:3]({base}/notes.md)"
    assert await _rewrite_snap("[guide](docs/guide.html#L4)") == f"[guide]({base}/docs/guide.html)"
    assert await _rewrite_snap("data/table.csv:5-9") == (
        f"[data/table.csv:5-9]({base}/data/table.csv)"
    )
    assert await _rewrite_snap("[r](report#L2)") == f"[r]({base}/report/)"  # a directory
    # the server's extension: `posixpath.splitext` skips leading dots, so `..py` has none
    assert await _rewrite_snap("`scripts/..py:3`") == f"[scripts/..py:3]({base}/scripts/..py)"


async def test_the_snapshot_comes_before_permalinks_and_images_keep_their_upload():
    """PM on issue #36 (Mon 09:30): with a snapshot, a committed, unchanged file links to the
    file server too, which needs no GitHub access; GitHub is the fallback without one."""
    up = FakeUploader()
    out = await _rewrite_snap("`src/foo.py:3`, `plots/plot.png`, `.env.json`", uploader=up)
    assert f"[src/foo.py:3]({SNAP.url}/src/foo.py#L3)" in out  # pushed, but the snapshot wins
    assert "github.com" not in out
    assert "[plot.png](/user_uploads/1/abc/plot.png)" in out  # images keep their inline upload
    assert "`.env.json` *(local)*" in out  # not in the snapshot (denylisted)
    # text uploads only when refserve is off: notes.md is served, not uploaded
    out = await _rewrite_snap("`notes.md`", uploader=up)
    assert out == f"[notes.md]({SNAP.url}/notes.md)"
    assert [u.name for u in up.uploads] == ["plot.png"]
    out = await _rewrite_snap("`notes.md`", snapshot=None, uploader=FakeUploader())
    assert out == "[notes.md](/user_uploads/1/abc/notes.md.txt)"  # refserve off: the fallback
    # no snapshot (the server is off, or the push failed): a pushed file gets its permalink,
    # with the line anchor
    for provide in (None, provider(None)):
        out = await _rewrite_snap("`src/foo.py:3`", snapshot=None, provide=provide)
        assert out == f"[src/foo.py:3]({WEB}/blob/{SHA}/src/foo.py#L3)"


async def test_the_snapshot_is_asked_for_only_when_a_file_would_be_linked():
    """Drew on #36 (Mon 04:01): "what is 2+2" → "4" makes no snapshot and pushes nothing; a
    message is asked about once, and only once it has a file to link."""
    nothing = provider(SNAP)
    assert await _rewrite_snap("4, and `x + 1`, and `missing.lean`", provide=nothing) == (
        "4, and `x + 1`, and `missing.lean`"
    )
    assert nothing.calls == 0
    some = provider(SNAP)
    out = await _rewrite_snap("`notes.md` and `proof/Solution.lean`", provide=some)
    assert f"[notes.md]({SNAP.url}/notes.md)" in out and some.calls == 1
    # a provider without a snapshot (a failed push, no GitHub origin): the usual fallbacks
    none = provider(None)
    out = await _rewrite_snap("`notes.md`", provide=none, uploader=FakeUploader())
    assert out == "[notes.md](/user_uploads/1/abc/notes.md.txt)" and none.calls == 1


async def test_a_provider_that_raises_degrades_to_no_snapshot_links():
    async def broken():
        raise RuntimeError("push hung")

    out = await _rewrite_snap("`notes.md`", provide=broken, uploader=FakeUploader())
    assert out == "[notes.md](/user_uploads/1/abc/notes.md.txt)"
