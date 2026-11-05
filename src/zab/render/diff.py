"""The diff message that follows a final answer (SPEC §11.8, §4.9)."""

from __future__ import annotations

import posixpath
import re
import secrets
from collections.abc import Callable, Mapping

from zab.core.types import DiffFile, DiffResult, RenderContext, Upload
from zab.render.mentions import disarm_mentions
from zab.render.redact import redact_secrets


def file_entries(diff: DiffResult) -> list[DiffFile]:
    """Every file the turn touched (§11.8): `diff.files`, or for a result from an older
    workspace without it, the paths of the stat (hidden ones marked) and the new untracked
    files, without counts."""
    if diff.files:
        return list(diff.files)
    out: list[DiffFile] = []
    for line in diff.stat.split("\n"):
        m = _HIDDEN_STAT_LINE.match(line)
        if m:
            out.append(DiffFile(path=m.group("path"), hidden=True))
            continue
        m = _STAT_LINE.match(line)
        if m:
            out.append(DiffFile(path=m.group("path")))
    out += [DiffFile(path=path, status="new") for path in diff.new_untracked]
    return out


def summary_line(diff: DiffResult) -> str:
    """`Changes: 7 files (+1210 -0), 5 new`: the visible line of the diff message (§11.8)."""
    if diff.files:
        entries = list(diff.files)
        count = len(entries)
        ins = sum(f.insertions or 0 for f in entries if not f.hidden)
        dels = sum(f.deletions or 0 for f in entries if not f.hidden)
        new = sum(1 for f in entries if f.status == "new")
    else:
        count = diff.files_changed + len(diff.new_untracked)
        ins, dels, new = diff.insertions, diff.deletions, len(diff.new_untracked)
    files = "file" if count == 1 else "files"
    return f"Changes: {count} {files} (+{ins} -{dels})" + (f", {new} new" if new else "")


_BACKTICK_RUN = re.compile(r"`+")


def fence_for(text: str, minimum: int = 3) -> str:
    """A backtick fence longer than any backtick run in `text`, at least `minimum` long.

    Zulip closes a fenced block only with a line equal to its opening fence, so a context
    line such as ``" ```"`` inside a three-backtick diff block would end it; a longer fence
    keeps the whole patch inside (issue #36).
    """
    longest = max((len(m.group()) for m in _BACKTICK_RUN.finditer(text)), default=0)
    return "`" * max(minimum, longest + 1)


def code_span(text: str) -> str:
    """`text` in an inline code span whose backticks cannot pair with its own."""
    tick = fence_for(text, 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{tick}{pad}{text}{pad}{tick}"


def human_size(n: int) -> str:
    """`3.1 kB` or `12.0 MB`, for size notices."""
    return f"{n / (1 << 20):.1f} MB" if n >= 1 << 20 else f"{n / 1024:.1f} kB"


def _clip(lines: list[str], budget: int, more: Callable[[int], str]) -> str:
    """Join `lines` within `budget` characters; when lines are dropped, end with `more(n)`."""
    joined = "\n".join(lines)
    if len(joined) <= budget:
        return joined
    kept: list[str] = []
    length = -1  # the joining newline before the first kept line does not exist
    for i, line in enumerate(lines):
        notice = more(len(lines) - i - 1)
        if length + 1 + len(line) + (1 + len(notice) if notice else 0) > budget:
            break
        kept.append(line)
        length += 1 + len(line)
    tail = more(len(lines) - len(kept))
    return "\n".join([*kept, tail]) if kept else tail[:budget]


HIDDEN = "changed (contents hidden)"
_HIDDEN_STAT_LINE = re.compile(r"^\s*(?P<path>.*\S)\s+\|\s+changed \(contents hidden\)\s*$")
_STAT_LINE = re.compile(r"^\s*(?P<path>\S.*?)\s+\|\s+(?:\d+|Bin)\b")
SPOILER_TITLE = "Details"


def _file_line(entry: DiffFile, name: str, link: str | None) -> str:
    """`- [`path`](link) new, +12 -0`; `- `path` changed (contents hidden)` (§11.8)."""
    shown = f"[{code_span(name)}]({link})" if link and not entry.hidden else code_span(name)
    if entry.hidden:
        tags = "new, contents hidden" if entry.status == "new" else HIDDEN
    else:
        parts = [entry.status] if entry.status in ("new", "deleted") else []
        if entry.binary:
            parts.append("binary")
        elif entry.insertions is not None and entry.deletions is not None:
            parts.append(f"+{entry.insertions} -{entry.deletions}")
        tags = ", ".join(parts)
    return f"- {shown} {tags}" if tags else f"- {shown}"


def _more(n: int) -> str:
    return f"- ... ({n} more)" if n else ""


async def render_diff(
    diff: DiffResult,
    ctx: RenderContext,
    *,
    inline_max_lines: int = 300,
    links: Mapping[str, str] | None = None,
) -> str:
    """Build the diff message (SPEC §11.8); an empty diff gives "" and the core posts nothing.

    One visible line, `Changes: 7 files (+1210 -0), 5 new` (`summary_line`), followed by
    `: [turn<N>.diff](…)` when the patch was uploaded; everything else sits in one collapsed
    ```` ````spoiler Details ```` below it (rc6, Drew's rehearsal on issue #36). The spoiler
    holds a single list of every file the turn touched, committed or not (`file_entries`):
    each path linked to its copy in the turn's snapshot when `links` has one (SPEC §10.7),
    then `new`/`deleted` and `+a -d` (`binary` instead of counts); hidden (denylisted) files
    as `changed (contents hidden)` without link or counts. Below the list, the patch in a
    ```` ```diff ```` block when it has at most `inline_max_lines` lines and the message fits
    in `max_message_length - 200`; both fences grow past the longest backtick run in what
    they hold, so no patch line or file name can close them (`fence_for`). Otherwise the
    patch is written to `<root>/.zab/outbox/turn<N>-<8 hex>.diff` (unique per render, so
    sessions sharing a workspace and turn number never overwrite each other's file before
    it is uploaded) and uploaded through `ctx.upload` as `turn<N>.diff`; without an upload
    callback the visible line says the diff was too large to inline, and when the encoded
    patch exceeds `ctx.max_upload_bytes` (§11.4) nothing is uploaded and the line says so,
    with the sizes. The list is clipped with an explicit "(n more)" notice to keep every
    message within the limit. The patch and the file names are redacted first (§11.6), and
    their inline copies go through `disarm_mentions` (§11.5) before formatting and sizing: a
    file called `@**all**.txt` notifies nobody, whatever backticks its name holds.
    """
    if diff.empty:
        return ""
    limit = ctx.max_message_length - 200
    title = summary_line(diff)
    upload_patch = redact_secrets(diff.patch).strip("\n")
    patch = disarm_mentions(upload_patch)
    # links: repo-relative path -> its URL in the turn's snapshot (SPEC §10.7)
    links = {disarm_mentions(redact_secrets(path)): url for path, url in (links or {}).items()}
    lines = []
    for entry in file_entries(diff):
        name = disarm_mentions(redact_secrets(entry.path))
        lines.append(_file_line(entry, name, links.get(name)))
    listing = "\n".join(lines)
    # room the list needs: all of it, or at least its notice (one rule for the inline check
    # and for `spoiler`, so that a patch judged to fit is never dropped; codex on #164)
    reserve = min(len(listing), len(_more(len(lines))))

    def spoiler(head: str, tail: str, outer: str) -> str:
        opening = f"{head}\n{outer}spoiler {SPOILER_TITLE}\n"
        closing = f"{tail}\n{outer}"
        budget = limit - len(opening) - len(closing)
        if budget < reserve:
            return head  # no room for even the notice: the visible line alone
        return opening + _clip(lines, budget, _more) + closing

    if patch and patch.count("\n") + 1 <= inline_max_lines:
        inner = fence_for(patch)  # longer than any run in the patch; the spoiler longer still
        block = f"\n{inner}diff\n{patch}\n{inner}"
        outer = fence_for(listing + block)
        frame = len(f"{title}\n{outer}spoiler {SPOILER_TITLE}\n") + len(f"{block}\n{outer}")
        if frame + reserve <= limit:
            return spoiler(title, block, outer)
    if not patch:
        # no visible patch: new files only, or changes the workspace hid from the diff
        # (`changed (contents hidden)`, SPEC §11.4, issue #36)
        return spoiler(title, "", fence_for(listing))
    data = (upload_patch + "\n").encode("utf-8")
    if ctx.upload is None:
        head = f"{title} (diff too large to inline)"
    elif len(data) > ctx.max_upload_bytes:
        sizes = f"{human_size(len(data))}, limit {human_size(ctx.max_upload_bytes)}"
        head = f"{title} (diff too large to upload: {sizes})"
    else:
        name = f"turn{ctx.turn_number or 0}.diff"
        stored = f"turn{ctx.turn_number or 0}-{secrets.token_hex(4)}.diff"
        path = posixpath.join(ctx.workspace.root, ".zab", "outbox", stored)
        await ctx.workspace.write_bytes(path, data)
        url = await ctx.upload(Upload(path=path, name=name))
        head = f"{title}: [{name}]({url})"
    return spoiler(head, "", fence_for(listing))


__all__ = ["code_span", "fence_for", "file_entries", "human_size", "render_diff", "summary_line"]
