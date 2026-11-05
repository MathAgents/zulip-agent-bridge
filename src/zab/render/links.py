"""Link rewriting and upload selection for agent text (SPEC §11.3, §11.4); step 4 of §11.

The workspace side (git queries, the upload denylist) is injected through the `LinkResolver`
protocol, so this module depends only on `zab.core.types` and the `CodeSpan` table produced
by `zab.render.markdown.protect_code`.
"""

from __future__ import annotations

import logging
import posixpath
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Protocol
from urllib.parse import quote

from zab.core.types import RenderContext, Upload, UploadRefused, Workspace
from zab.render.redact import redact_secrets

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from zab.render.markdown import CodeSpan

# --------------------------------------------------------------------------- the resolver side


@dataclass(frozen=True, slots=True, kw_only=True)
class LinkTarget:
    """What the workspace side knows about one canonical path (§11.3, batched)."""

    exists: bool
    is_dir: bool
    tracked: bool
    modified: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class RepoInfo:
    """The parts of `GitContext` (§10.4) that permalinks need."""

    head_sha: str
    pushed: bool
    web_url: str | None
    root: str


class LinkResolver(Protocol):
    async def repo_info(self, ws: Workspace) -> RepoInfo | None:
        """None when the workspace is not inside a git repository."""
        ...

    async def classify(self, ws: Workspace, paths: Sequence[str]) -> dict[str, LinkTarget]:
        """One batched answer for all candidate paths of a message (§11.3)."""
        ...

    async def check_upload(self, ws: Workspace, path: str, *, explicit: bool) -> Upload:
        """Root check, denylist and size limit (§11.4); raises UploadRefused."""
        ...

    async def snapshot_paths(self, ws: Workspace, sha: str, paths: Sequence[str]) -> dict[str, str]:
        """The canonical `paths` present, as files or directories, in commit `sha` (§10.7),
        mapped to their paths in the snapshot (relative to the repository's top level)."""
        ...


# ------------------------------------------------------------------------------- candidates


@dataclass(frozen=True, slots=True, kw_only=True)
class Candidate:
    """One path reference found in a message, before resolution."""

    kind: str  # "link" (markdown link), "span" (inline code span), "bare" (path:line token)
    written: str  # the path exactly as the agent wrote it, line suffix included
    path: str  # the path without its line suffix
    lines: tuple[int, int | None] | None  # (first, last) from :L, :L-M or #Lx-Ly
    label: str = ""  # markdown links: the label; "!" prefix is dropped
    index: int = -1  # spans: index in the code table


#: Zulip's timestamp, `<time:2026-10-27T01:08:38Z>`: each reader's client shows it in their own
#: time zone (issue #673). Zulip takes everything up to the next `>` as the time. Here a token
#: stays on one line, so a reference after a stray `<time:` still links, and holds at most 100
#: characters, which keeps a line of unclosed `<time:` linear to scan (no written time is longer).
ZULIP_TIME = re.compile(r"<time:[^>\n]{0,100}>")

_LINK_OR_BARE = re.compile(
    r"(?P<time>" + ZULIP_TIME.pattern + r")"
    r"|!?\[(?P<label>[^\]\n]*)\]\((?P<target>[^()\s]+)\)"
    r"|(?<![\w/.\-\\@:])(?P<bare>/?[\w.\-][\w.\-/]*)"
    r":(?P<l1>\d+)(?::\d+)?(?:-(?P<l2>\d+)(?::\d+)?)?(?![\w/])"
)
# A Zulip time is matched first and skipped whole: nothing inside it is a file reference, not
# `time:2026-10` (lines 2026-10 of a file `time`) nor a path after a space (issue #673). A bare
# token may be absolute (`/work/proj/src/foo.py:12`, as Codex writes them, §11.3); the
# lookbehind keeps a token from starting inside a path or after the `://` of a URL.
_LINE_SUFFIX = re.compile(
    # `:L`, `:L-M`, `#Lx-Ly`, each line optionally followed by a column (`:L:C`, `#LxCy`),
    # which is dropped: GitHub anchors and the text shown use lines only.
    r"^(?P<path>.*?)(?::(?P<a>\d+)(?::\d+)?(?:-(?P<b>\d+)(?::\d+)?)?"
    r"|#L(?P<c>\d+)(?:C\d+)?(?:-L(?P<d>\d+)(?:C\d+)?)?)$"
)
_EXTENSION = re.compile(r"\.[A-Za-z0-9]{1,10}$")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")  # `data:`, `javascript:`, `file:`, ...
_PLACEHOLDER_INDEX = re.compile(r"\x00zabcode(\d+)\x00")  # the placeholders of protect_code


def _split_suffix(target: str) -> tuple[str, tuple[int, int | None] | None]:
    m = _LINE_SUFFIX.match(target)
    if m is None or (m.group("a") is None and m.group("c") is None):
        return target, None
    first = m.group("a") or m.group("c")
    last = m.group("b") or m.group("d")
    return m.group("path"), (int(first), int(last) if last else None)


def _pathlike(path: str) -> bool:
    """Contains a slash or has an extension, holds no whitespace, and is not a flag."""
    if not path or len(path) > 300 or path.startswith("-") or re.search(r"\s", path):
        return False
    return "/" in path or _EXTENSION.search(posixpath.basename(path)) is not None


def _link_target_is_path(target: str) -> bool:
    """False for URLs, anchors, Zulip uploads, mentions, and anything with a URI scheme.

    The scheme test runs on the target without its line suffix, so `foo.py:12` stays a path
    while `data:...`, `javascript:...` or `file:...` never do (issue #36).
    """
    if "://" in target or target.startswith(("#", "mailto:", "/user_uploads/", "@")):
        return False
    return _SCHEME.match(_split_suffix(target)[0]) is None


def _span_inner(span: str) -> str | None:
    """The content of an inline code span, or None if it is not a plausible path."""
    m = re.match(r"^(`+)(.*)\1$", span, re.DOTALL)
    if m is None:
        return None
    inner = m.group(2)
    if inner.startswith(" ") and inner.endswith(" ") and inner.strip():
        inner = inner[1:-1]
    path = _split_suffix(inner)[0]
    return inner if _pathlike(path) and _SCHEME.match(path) is None else None


def find_candidates(text: str, table: list[CodeSpan]) -> list[Candidate]:
    """Collect the link candidates of §11.3 from the protected text and its code table.

    Markdown links whose target is not a URL, an anchor, a `mailto:` or a Zulip upload;
    inline code spans that look like a path with an optional line suffix; and bare
    `path:line` or `path:line-line` tokens whose path contains a slash or has an extension.
    Fenced blocks are never candidates, nor is a span that sits inside a markdown link's
    label: the link is rewritten as a whole and keeps the span as its label. Nor is anything
    inside a Zulip time, `<time:…>` (issue #673).
    """
    found: list[Candidate] = []
    in_labels: set[int] = set()  # spans inside a link label stay as they are (the label)
    for m in _LINK_OR_BARE.finditer(text):
        if m.group("time") is not None:
            continue
        if m.group("bare") is not None:
            path = m.group("bare")
            if _pathlike(path):
                last = int(m.group("l2")) if m.group("l2") else None
                written = m.group(0)
                found.append(
                    Candidate(
                        kind="bare", written=written, path=path, lines=(int(m.group("l1")), last)
                    )
                )
            continue
        target = m.group("target")
        in_labels.update(int(k) for k in _PLACEHOLDER_INDEX.findall(m.group("label")))
        if not _link_target_is_path(target):
            continue
        path, lines = _split_suffix(target)
        if path:  # any existing target counts for a markdown link (§11.3)
            found.append(
                Candidate(
                    kind="link", written=target, path=path, lines=lines, label=m.group("label")
                )
            )
    for i, entry in enumerate(table):
        if entry.kind != "span" or i in in_labels:
            continue
        inner = _span_inner(entry.text)
        if inner is None:
            continue
        path, lines = _split_suffix(inner)
        found.append(Candidate(kind="span", written=inner, path=path, lines=lines, index=i))
    return found


# -------------------------------------------------------------------------------- resolution


def _inside(root: str, path: str) -> bool:
    root = root.rstrip("/") or "/"
    return path == root or path.startswith(root + "/")


def _anchor(lines: tuple[int, int | None] | None) -> str:
    if lines is None:
        return ""
    first, last = lines
    return f"#L{first}" if last is None or last == first else f"#L{first}-L{last}"


#: The extensions the central file server renders as source, one table row per line with
#: `id="L<n>"` (issue #256: `workshop/files/render.py`, its `LANGUAGES` but Markdown, and
#: `source_html`; a test keeps the two equal). A link there keeps its line (issue #448).
SERVED_LINE_EXT = frozenset(
    "lean py sage c h jl rs gp m mag magma g gd gi gap m2 cpp cc cxx hpp hh sh bash tex sty cls "
    "bib toml yaml yml json js mjs css".split()
)


def _extension(path: str) -> str:
    base = posixpath.basename(path)
    return base.rsplit(".", 1)[1].lower() if "." in base[1:] else ""


def _served_anchor(real: str, target: LinkTarget, lines: tuple[int, int | None] | None) -> str:
    """The line anchor of a file-server link (issue #448): `#L<first>` for a source file the
    server renders with line numbers, else nothing. Its pages have no range anchor (the row
    ids are `L<n>` alone), so a range opens at its first line; Markdown, HTML and other files
    have no line anchors at all. A file over the server's caps is plain text, where a browser
    ignores the anchor. The extension is the server's (`posixpath.splitext`, which skips
    leading dots: `..py` has none), not `_extension`'s."""
    ext = posixpath.splitext(posixpath.basename(real))[1][1:].lower()
    if lines is None or target.is_dir or ext not in SERVED_LINE_EXT:
        return ""
    return f"#L{lines[0]}"


async def _options(ws: Workspace, path: str, root: str) -> list[str]:
    """Canonical paths to try for `path`, in order: cwd-relative, then root-relative.

    `root` is the canonical workspace root: a root reached through a symlink would otherwise
    never contain the canonical candidates, and nothing would be rewritten (issue #36).
    """
    bases = [""] if posixpath.isabs(path) else [ws.cwd, ws.root]
    options: list[str] = []
    for base in bases:
        real = await ws.realpath(posixpath.join(base, path) if base else path)
        if real not in options and _inside(root, real):
            options.append(real)
    return options


async def _touched_matches(
    ws: Workspace,
    candidates: Sequence[Candidate],
    turn_files: frozenset[str] | None,
    repo: RepoInfo | None,
    root: str,
) -> dict[Candidate, list[str]]:
    """For each relative candidate: the canonical paths of the files the turn touched whose
    repo-relative path is the written path or ends with `/` + it (§11.3, issue #36). Empty
    without turn information; absolute candidates are unambiguous and get no entry."""
    if turn_files is None or repo is None:
        return {}
    out: dict[Candidate, list[str]] = {}
    for cand in candidates:
        if posixpath.isabs(cand.path):
            continue
        norm = posixpath.normpath(cand.path)
        if norm in (".", "..") or norm.startswith("../"):
            continue
        hits = sorted(rel for rel in turn_files if rel == norm or rel.endswith("/" + norm))
        reals = [await ws.realpath(posixpath.join(repo.root, rel)) for rel in hits]
        out[cand] = [r for r in dict.fromkeys(reals) if _inside(root, r)]
    return out


def _plain(cand: Candidate) -> str:
    """An ambiguous reference: shown as code, linked nowhere (§11.3)."""
    if cand.kind == "link" and cand.label and cand.label != cand.written:
        return f"{cand.label} (`{cand.written}`)"
    return f"`{cand.written}`"


async def rewrite_links(
    text: str, table: list[CodeSpan], ctx: RenderContext, resolver: LinkResolver
) -> tuple[str, list[CodeSpan]]:
    """Step 4 of SPEC §11: turn file references into uploads, permalinks or local markers.

    Every candidate (see `find_candidates`) is resolved against `ctx.workspace.cwd`, then
    `ctx.workspace.root`, canonicalized with `Workspace.realpath`, and dropped if it lies
    outside the root or does not exist; git questions are asked once for the whole message.
    With `ctx.turn_files` (the final answer of a turn), a reference that would resolve to a
    file the turn did not touch, or to nothing, takes instead the one touched file whose path
    ends with the written path; with several such files it is ambiguous and shown as plain
    code, linked nowhere. The first matching row of §11.3 wins: an auto-upload extension accepted by
    `check_upload` is uploaded (when `ctx.upload` is set) and linked; a tracked, unmodified
    file of a pushed commit with a known GitHub URL becomes a permalink at the head SHA with
    a line anchor (directories use `tree/`); anything else that exists becomes a code span
    followed by ` *(local)*`. Markdown links and bare tokens are rewritten in the text; a
    code span is rewritten in its table entry, since a link cannot live inside code.
    """
    ws = ctx.workspace
    candidates = find_candidates(text, table)
    if not candidates:
        return text, table
    root = await ws.realpath(ws.root)  # once per message; the boundary for every check
    repo = await resolver.repo_info(ws)
    if repo is not None:
        repo = replace(repo, root=await ws.realpath(repo.root))
    options = {c: await _options(ws, c.path, root) for c in candidates}
    touched = await _touched_matches(ws, candidates, ctx.turn_files, repo, root)
    wanted = sorted(
        {p for opts in options.values() for p in opts} | {p for ps in touched.values() for p in ps}
    )
    targets = await resolver.classify(ws, wanted) if wanted else {}
    uploads: dict[str, str | None] = {}
    text_left = [ctx.max_text_uploads if ctx.final_answer and ctx.text_upload_ext else 0]
    rendered: dict[Candidate, str] = {}
    chosen: dict[Candidate, str] = {}
    turn_files = ctx.turn_files or frozenset()
    for cand in candidates:
        real = next((p for p in options[cand] if targets.get(p, None) and targets[p].exists), None)
        if cand in touched and (
            real is None or repo is None or posixpath.relpath(real, repo.root) not in turn_files
        ):
            # A file this turn touched beats an untouched one at the same written path: an
            # answer listing files it just created writes them relative to where it created
            # them, and the root may have a tracked file of the same name (issue #36).
            hits = [p for p in touched[cand] if targets.get(p, None) and targets[p].exists]
            if len(hits) == 1:
                real = hits[0]
            elif len(hits) > 1 and real is not None:
                rendered[cand] = _plain(cand)
                continue
        if real is None:
            continue
        chosen[cand] = real
    in_snapshot: dict[str, str] = {}
    snapshot = None
    if ctx.snapshot is not None and chosen:
        # Only now, with a file to link, is the turn's snapshot made and pushed (SPEC §10.7);
        # an answer that links nothing leaves no trace on GitHub. No HEAD needed (a first
        # snapshot links too). One query per message; a failure only means no snapshot links.
        try:
            snapshot = await ctx.snapshot()
            if snapshot is not None:
                in_snapshot = await resolver.snapshot_paths(
                    ws, snapshot.sha, sorted(set(chosen.values()))
                )
        except Exception as exc:
            log.warning("snapshot lookup failed: %s", type(exc).__name__)
    for cand, real in chosen.items():
        served = None
        if real in in_snapshot and snapshot is not None:
            rel = quote(in_snapshot[real], safe="/")
            served = f"{snapshot.url}/{rel}" + ("/" if targets[real].is_dir else "")
        rendered[cand] = await _render(
            cand, real, targets[real], repo, ctx, resolver, uploads, root, text_left, served
        )
    if not rendered:
        return text, table
    new_table = list(table)
    for cand, out in rendered.items():
        if cand.kind == "span":
            new_table[cand.index] = replace(table[cand.index], text=out)
    by_written = {
        (c.kind, c.written, c.label): out for c, out in rendered.items() if c.kind != "span"
    }

    def sub(m: re.Match[str]) -> str:
        if m.group("time") is not None:
            return m.group(0)
        if m.group("bare") is not None:
            return by_written.get(("bare", m.group(0), ""), m.group(0))
        return by_written.get(("link", m.group("target"), m.group("label")), m.group(0))

    return _LINK_OR_BARE.sub(sub, text), new_table


async def upload_file(ctx: RenderContext, upload: Upload) -> str:
    """Upload one checked file and return the markdown link to post (`!file`, SPEC §11.4).

    `upload` comes from `WorkspaceManager.check_upload(ws, path, explicit=True)`, which has
    already applied the root check, the denylist, the extension rule and the size limit; this
    helper only sends it through `ctx.upload` and formats `[name](url)`. Without an upload
    callback it raises `UploadRefused`, so the command can reply with the reason like any
    other refusal; a failing upload propagates to the caller, which decides what to post.
    """
    if ctx.upload is None:
        raise UploadRefused("uploads are not enabled for this bot")
    return upload_link(upload.name, await ctx.upload(upload))


def upload_link(name: str, url: str) -> str:
    """`[name](url)` for an uploaded file: `\\`, `[` and `]` escaped in the label, the URL quoted
    so that spaces and parentheses cannot end it (existing `%` escapes are kept)."""
    label = re.sub(r"([\\\[\]])", r"\\\1", name).replace("\n", " ")
    return f"[{label}]({quote(url, safe='/:%?=&')})"


async def _try_upload(real: str, ctx: RenderContext, resolver: LinkResolver) -> str | None:
    """Upload `real` if the workspace allows it; None, never an exception, otherwise.

    The size is checked against `ctx.max_upload_bytes` before anything is sent (§11.4), the
    denylist through `check_upload`, and the upload callback itself may fail (network, a
    server refusal): rendering then degrades to the next row of §11.3 instead of losing the
    answer. Only exception type names are logged (issue #36).
    """
    try:
        info = await ctx.workspace.stat(real)
    except Exception as exc:  # the workspace is the agent's: never trust it
        log.warning("stat failed for an upload candidate: %s", type(exc).__name__)
        return None
    if info is None or info.size > ctx.max_upload_bytes:
        return None
    try:
        upload = await resolver.check_upload(ctx.workspace, real, explicit=False)
    except UploadRefused:
        return None
    except Exception as exc:
        log.warning("check_upload failed: %s", type(exc).__name__)
        return None
    try:
        return await ctx.upload(upload)
    except Exception as exc:
        log.warning("upload failed: %s", type(exc).__name__)
        return None


async def _try_text_upload(real: str, ctx: RenderContext, resolver: LinkResolver) -> str | None:
    """Upload a referenced text file as `<name>.<ext>.txt` (issue #36); None, never an
    exception, when it may not or cannot be uploaded.

    Zulip shows `text/plain` uploads in the browser but offers `.lean`, `.md`, `.csv` or
    `.json` as downloads, hence the `.txt` name. `check_upload` applies the root boundary,
    the denylist and the hard-link refusal (the extension rule is this function's, so
    `explicit=True`); only valid UTF-8 without NUL bytes, at most `ctx.max_text_upload_bytes`,
    is sent, with secrets redacted as in the answer (§11.6), and exactly the bytes that were
    checked (`Upload.data`). The upload is a snapshot taken when the answer is posted, like
    images.
    """
    try:
        info = await ctx.workspace.stat(real)
        if info is None or info.is_dir or info.size > ctx.max_text_upload_bytes:
            return None
        upload = await resolver.check_upload(ctx.workspace, real, explicit=True)
        data = await ctx.workspace.read_bytes(real, ctx.max_text_upload_bytes + 1)
        if len(data) > ctx.max_text_upload_bytes or b"\x00" in data:
            return None
        text = data.decode("utf-8")  # strict: a binary or another encoding is not uploaded
    except UploadRefused:
        return None
    except UnicodeDecodeError:
        return None
    except Exception as exc:  # the workspace is the agent's: never trust it
        log.warning("text upload check failed: %s", type(exc).__name__)
        return None
    # The file's contents reach Zulip: the same secret redaction as the answer itself
    # (SPEC §11.6; codex review on #124). The denylist only sees file names.
    data = redact_secrets(text).encode("utf-8")
    name = upload.name if upload.name.lower().endswith(".txt") else upload.name + ".txt"
    try:
        # the validated bytes themselves: a file that grows or changes after the check is
        # not re-read (codex review on #124)
        return await ctx.upload(replace(upload, name=name, data=data))  # type: ignore[misc]
    except Exception as exc:
        log.warning("text upload failed: %s", type(exc).__name__)
        return None


async def _render(
    cand: Candidate,
    real: str,
    target: LinkTarget,
    repo: RepoInfo | None,
    ctx: RenderContext,
    resolver: LinkResolver,
    uploads: dict[str, str | None],
    root: str,
    text_left: list[int] | None = None,
    served: str | None = None,
) -> str:
    # Spans and bare tokens are shown root-relative, with the line suffix as written: the
    # bridge context asks agents for repository-relative paths, and that is what readers
    # should see whether the agent wrote them absolute or cwd-relative (issue #36).
    shown = posixpath.relpath(real, root) + cand.written[len(cand.path) :]
    label = cand.label if cand.kind == "link" and cand.label else shown
    if not target.is_dir and ctx.upload is not None and _extension(real) in ctx.auto_upload_ext:
        url = uploads.get(real)
        if url is None:
            url = uploads[real] = await _try_upload(real, ctx, resolver)
        if url is not None:
            if cand.kind == "link":  # the agent's own label, already markdown
                return f"[{label}]({quote(url, safe='/:%?=&')})"
            return upload_link(posixpath.basename(real), url)
    if served is not None:
        # In the turn's snapshot on the central file server (rc6, §10.7): a browser shows it as
        # it stood when the answer was posted, HTML with its CSS, scripts and images, and anyone
        # with the link can open it. It comes before GitHub for every file type, committed and
        # unchanged ones too: a GitHub link needs the reader's account to have been invited,
        # which may be missing or pending (PM on issue #36, Mon 09:30). A rendered source file
        # keeps the line as `#L<first>`; the label keeps the line as written (issue #448).
        return f"[{label}]({served}{_served_anchor(real, target, cand.lines)})"
    if (
        repo is not None
        and repo.web_url
        and repo.pushed
        and target.tracked
        and not target.modified
        and _inside(repo.root, real)
    ):
        # no snapshot (the file server is off, or the push failed): GitHub's permalink
        rel = quote(posixpath.relpath(real, repo.root), safe="/")  # `#`, `?`, spaces, `%`
        kind = "tree" if target.is_dir else "blob"
        anchor = "" if target.is_dir else _anchor(cand.lines)
        return f"[{label}]({repo.web_url}/{kind}/{repo.head_sha}/{rel}{anchor})"
    if (
        not target.is_dir
        and ctx.upload is not None
        and _extension(real) in ctx.text_upload_ext
        and text_left is not None
    ):
        # A text file that would show as *(local)*: upload it so the reader can see it,
        # at most `ctx.max_text_uploads` distinct files per answer (issue #36).
        if real in uploads:
            url = uploads[real]
        elif text_left[0] > 0:
            url = uploads[real] = await _try_text_upload(real, ctx, resolver)
            if url is not None:
                text_left[0] -= 1
        else:
            url = None
        if url is not None:
            if cand.kind == "link":
                return f"[{label}]({quote(url, safe='/:%?=&')})"
            return upload_link(shown, url)
    local = f"`{shown}` *(local)*"
    if cand.kind == "link" and cand.label and cand.label not in (cand.written, shown):
        return f"{cand.label} ({local})"
    return local


__all__ = [
    "ZULIP_TIME",
    "Candidate",
    "LinkResolver",
    "LinkTarget",
    "RepoInfo",
    "find_candidates",
    "rewrite_links",
]
