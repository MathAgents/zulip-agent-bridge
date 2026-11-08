"""Agent markdown -> Zulip markdown (SPEC §11): redaction, code, math, mentions, splitting.

Every step is a pure function on text, exported so it can be table-tested on its own. The
`MarkdownRenderer` class chains them in the order of SPEC §11 and implements the `Renderer`
protocol from `zab.core.types`. Link rewriting (§11.3, §11.4) and `render_diff` (§11.8) arrive
with M1a in `zab.render.links`.
"""

from __future__ import annotations

import bisect
import logging
import posixpath
import re
import secrets
from collections import deque
from urllib.parse import quote

from zab.core.types import DiffResult, RenderContext, Upload
from zab.render.code import (
    _BACKTICK_RUN,
    CodeSpan,
    _fence_closes,
    _fence_open,
    _markdown_body,
    protect_code,
    restore_code,
)
from zab.render.diff import file_entries
from zab.render.diff import render_diff as _render_diff
from zab.render.links import LinkResolver, rewrite_links
from zab.render.mentions import ZWSP, disarm_mentions
from zab.render.redact import redact_secrets

log = logging.getLogger(__name__)


def normalize(text: str) -> str:
    """Step 0 of every entry point: drop NUL characters (SPEC §11.6 safety).

    Zulip refuses NUL in a message, and a NUL inside a secret would let it slip past
    `redact_secrets` only to be reassembled when the placeholder step drops the character
    later, so it goes first. `protect_code` drops NUL as well, as a safety net that keeps its
    placeholder space private whoever calls it.
    """
    return text.replace("\x00", "")


# §11 step 2, code protection, is `zab.render.code`: the waker reads mentions with it (#683).

# ------------------------------------------------------------------------------------ §11.2 math

_ENV_NAMES = r"align\*?|equation\*?|gather\*?|multline\*?"
# Display math and \( \) are found in one left-to-right walk over these tokens; the closer of
# an opener is looked up by bisection, so an unmatched opener costs O(log n), not a rescan.
_MATH_TOKEN = re.compile(
    r"\\\[|\\\]|\\\(|\\\)|\$\$|\n[ \t]*\n"
    r"|\\begin\{(?P<bname>" + _ENV_NAMES + r")\}|\\end\{(?P<ename>" + _ENV_NAMES + r")\}"
)
_CLOSER_OF = {"\\[": "\\]", "\\(": "\\)", "$$": "$$"}
_PARAGRAPH_END = re.compile(r"[ \t]*(?=\n\n|\n?\Z)")
# pandoc tex_math_dollars: `$` followed by non-space ... non-space `$` not followed by a digit;
# neither `$` escaped or adjacent to another `$`; no blank line and no bare `$` inside (an
# escaped `\$` is fine), so `$100 and $200; compute $x^2$` converts only the formula. The
# inner run is possessive: it can't pass a bare `$`, so it ends where a lazy one could, and a
# lazy or greedy repeat of a group keeps a frame of `re` per character, 147 MB on 1 MB (#833).
_DOLLAR_MATH = re.compile(
    r"(?<![\\$])\$(?!\$)(?=\S)(?P<inner>(?:(?!\n[ \t]*\n)(?:\\.|[^$\\]))++)(?<=\S)\$(?![\d$])",
    re.DOTALL,
)


def _math_block(text: str, start: int, end: int, inner: str) -> str:
    """A ```math block for the display math at `text[start:end]`, on lines of its own."""
    lead = "" if start == 0 or text[start - 1] == "\n" else "\n"
    trail = "" if end >= len(text) or text[end] == "\n" else "\n"
    return f"{lead}```math\n{inner.strip()}\n```{trail}"


def _token_kind(t: re.Match[str]) -> str:
    if t.group("bname") is not None:
        return "begin:" + t.group("bname")
    if t.group("ename") is not None:
        return "end:" + t.group("ename")
    return "blank" if t.group().startswith("\n") else t.group()


def convert_math(text: str) -> str:
    """Convert LaTeX math delimiters to Zulip's (SPEC §11.2); call with code protected.

    Display forms become ```` ```math ```` blocks: a paragraph consisting only of `$$ ... $$`,
    `\\[ ... \\]`, and a bare `align`, `equation`, `gather` or `multline` environment (kept
    verbatim inside the block). Inline forms become `$$...$$`: `\\( ... \\)`, and `$...$`
    under pandoc's `tex_math_dollars` rules, so that `$100 and $200` and `costs $5.` are
    left alone. Existing `$$...$$` is left alone. The `$` pass never runs inside a block
    produced here.

    The delimited forms are found in a single walk over the delimiter tokens of the text,
    leftmost opener first, so a nested form is consumed once by the outermost delimiter; an
    opener whose closer is missing, or lies beyond a blank line (environments excepted), is
    literal text, and the walk goes on from the next token without rescanning. No step copies
    or scans a prefix or suffix of the text per candidate, so the pass is linear.
    """
    tokens = list(_MATH_TOKEN.finditer(text))
    kinds = [_token_kind(t) for t in tokens]
    by_kind: dict[str, list[int]] = {}
    for i, kind in enumerate(kinds):
        by_kind.setdefault(kind, []).append(i)
    blanks = by_kind.get("blank", [])

    def next_of(kind: str, after: int) -> int | None:
        indices = by_kind.get(kind, [])
        j = bisect.bisect_right(indices, after)
        return indices[j] if j < len(indices) else None

    def blank_between(a: int, b: int) -> bool:
        j = bisect.bisect_right(blanks, a)
        return j < len(blanks) and blanks[j] < b

    out: list[str] = []
    pos = 0
    i = 0
    while i < len(tokens):
        t, kind = tokens[i], kinds[i]
        is_env = kind.startswith("begin:")
        close_kind = "end:" + kind[6:] if is_env else _CLOSER_OF.get(kind)
        j = next_of(close_kind, i) if close_kind else None
        if j is None or (not is_env and blank_between(i, j)):
            i += 1  # a closer, a blank line, or an opener that never closes: literal
            continue
        c = tokens[j]
        inner = text[t.end() : c.start()]
        start, end = t.start(), c.end()
        if kind == "$$":  # only a paragraph consisting of `$$ ... $$` becomes a block
            k = start  # back over the blanks before `$$`: the paragraph must begin there
            while k > 0 and text[k - 1] in " \t":
                k -= 1
            after = _PARAGRAPH_END.match(text, end)
            if not inner or after is None or not (k == 0 or text[k - 2 : k] == "\n\n"):
                i += 1
                continue
            start, end = max(k, pos), after.end()
            math = _math_block(text, start, end, inner)
        elif kind == "\\(":
            if not inner:
                i += 1
                continue
            math = f"$${inner.strip()}$$"
        else:  # `\[ ... \]`, or an environment kept verbatim
            math = _math_block(text, start, end, text[start:end] if is_env else inner)
        out.append(_DOLLAR_MATH.sub(lambda m: f"$${m.group('inner')}$$", text[pos:start]))
        out.append(math)
        pos = end
        i = j + 1
    out.append(_DOLLAR_MATH.sub(lambda m: f"$${m.group('inner')}$$", text[pos:]))
    return "".join(out)


# -------------------------------------------------------------------------------- §11.5 mentions

_WILDCARD_MENTION = re.compile(r"@\*\*(?:all|everyone|channel|stream|topic)\*\*")
_GROUP_MENTION = re.compile(r"@\*[^*\n]+\*(?!\*)")
_USER_MENTION = re.compile(r"@\*\*(?!(?:all|everyone|channel|stream|topic)\*\*)([^*\n]+)\*\*")


def neutralize_mentions(text: str) -> str:
    """Make agent text unable to notify anyone (SPEC §11.5); call with code protected.

    Wildcard mentions (`@**all**`, `@**everyone**`, `@**channel**`, `@**stream**`,
    `@**topic**`) and group mentions (`@*group*`) become inline code, with a zero-width space
    after the `@` (see `disarm_mentions`, which makes that the guarantee); every other
    `@**Name**` (including `@**Name|id**`) becomes the silent form `@_**Name**`, and existing
    silent mentions stay as they are.
    """
    lengths = {len(r.group()) for r in _BACKTICK_RUN.finditer(text)}
    tick = "`" * next(k for k in range(1, len(lengths) + 2) if k not in lengths)
    text = _WILDCARD_MENTION.sub(lambda m: _as_code(m, tick), text)
    text = _GROUP_MENTION.sub(lambda m: _as_code(m, tick), text)
    return _USER_MENTION.sub(r"@_**\1**", text)


def _as_code(m: re.Match[str], tick: str) -> str:
    """Wrap the match in a code span that no stray backtick in the text can pair with.

    Real code spans are placeholders at this point, so every remaining backtick run is a
    stray one (unmatched or escaped). `tick` is the shortest run that occurs nowhere in the
    text, computed once per text by `neutralize_mentions` (the wrappers it inserts are
    matched pairs, so the same run serves the wildcard and the group pass); a space keeps
    it from merging with an adjacent stray run, with the backticks of an adjacent protected
    span or fence (a placeholder ends in `\\x00`), or from being escaped by a preceding
    backslash. Restored spans pair with their own backticks before any later run, so they
    can never capture the wrapper.
    """
    lead = " " if m.start() > 0 and m.string[m.start() - 1] in "`\\\x00" else ""
    trail = " " if m.end() < len(m.string) and m.string[m.end()] in "`\x00" else ""
    return f"{lead}{tick}@{ZWSP}{m.group(0)[1:]}{tick}{trail}"


# ------------------------------------------------------------------------------- §11.7 splitting

_LABEL_RESERVE = 12  # room for "\n\n(i/n)" with up to three-digit counts


def _split_for_labels(text: str, limit: int) -> tuple[list[str], int]:
    """Unlabeled parts whose labels fit `limit`, and the budget they were cut to.

    The label `(i/n)` grows with the number of parts, which is only known after splitting, so
    the reserve starts at `_LABEL_RESERVE` and the text is split again with a larger reserve
    whenever `n` turns out to need more digits (issue #36). The reserve only grows, so this
    ends after a few rounds.
    """
    reserve = _LABEL_RESERVE
    while True:
        parts = _split_unlabeled(text, limit - reserve)
        needed = len(f"\n\n({len(parts)}/{len(parts)})")
        if needed <= reserve:
            return parts, limit - reserve
        reserve = needed


def _chunk_long_lines(lines: list[str], max_len: int) -> list[str]:
    out: list[str] = []
    for line in lines:
        if len(line) <= max_len:
            out.append(line)
        else:
            out.extend(line[k : k + max_len] for k in range(0, len(line), max_len))
    return out


def _trim_blank(lines: list[str]) -> list[str]:
    """Drop leading and trailing blank lines."""
    start, end = 0, len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return lines[start:end]


def _split_unlabeled(text: str, budget: int) -> list[str]:
    """Split `text` into parts of at most `budget` characters (see `split_message`).

    Open fenced blocks are tracked on a stack (a spoiler or quote may hold nested blocks); a
    cut inside them closes every open block, innermost first, and reopens all of them, with
    their info strings, at the top of the next part.
    """
    budget = max(budget, 1)
    if len(text) <= budget:
        return [text]
    queue = deque(_chunk_long_lines(text.split("\n"), budget))  # lines still to place
    parts: list[str] = []
    cur: list[str] = []
    cur_len = 0
    fences: list[tuple[str, str]] = []  # open blocks, outermost first: (fence, info)
    fence_start = 0  # index in `cur` of the outermost open block's opening line

    def emit(part_lines: list[str]) -> None:
        part_lines = _trim_blank(part_lines)
        if part_lines:
            parts.append("\n".join(part_lines))

    def reset(new_cur: list[str], new_fence_start: int) -> None:
        nonlocal cur, cur_len, fence_start
        cur = new_cur
        cur_len = len("\n".join(cur))
        fence_start = new_fence_start

    def closers() -> list[str]:
        return [fence for fence, _ in reversed(fences)]

    def reopeners() -> list[str]:
        return [fence + info for fence, info in fences]

    def fits_as_block(line: str, opened: tuple[str, str]) -> bool:
        """Whether this block, reopened with a content line, can still be closed in a part.

        A block that cannot is treated as plain text, so that every cut below makes progress.
        """
        reopen = sum(len(f + i) + 1 for f, i in fences) + len(line) + 1
        close = sum(len(f) + 1 for f, _ in fences) + len(opened[0]) + 1
        return reopen + 1 + close <= budget

    while queue:
        line = queue[0]
        add = len(line) + (1 if cur else 0)
        # Would this line open a block (at the top level, or nested in a spoiler or quote)?
        opened: tuple[str, str] | None = None
        if not (fences and _fence_closes(line, fences[-1][0])) and (
            not fences or _markdown_body(fences[-1][1])
        ):
            candidate = _fence_open(line)
            if candidate is not None and fits_as_block(line, candidate):
                opened = candidate
        # Keep room to close every open block, and the block this line would open.
        room = budget - sum(len(f) + 1 for f, _ in fences) - (len(opened[0]) + 1 if opened else 0)
        if cur and cur_len + add > room:
            if fences:
                # Lines after the outermost opener that are not themselves open-block openers.
                content_lines = len(cur) - fence_start - len(fences)
                if content_lines >= 2 or (content_lines == 1 and fence_start == 0):
                    # Close the blocks here and reopen them at the top of the next part.
                    emit([*cur, *closers()])
                    reset(reopeners(), 0)
                    continue
                if fence_start > 0:  # move the whole block to the next part
                    emit(cur[:fence_start])
                    reset(cur[fence_start:], 0)
                    continue
                # Only opening lines so far and the line cannot fit intact: cut it.
                queue.popleft()
                queue.extendleft(reversed(_chunk_long_lines([line], max(room - cur_len - 1, 1))))
                continue
            blank = next((k for k in range(len(cur) - 1, 0, -1) if not cur[k].strip()), None)
            if blank is not None:  # cut at the last blank line
                emit(cur[:blank])
                reset(cur[blank + 1 :], 0)
            else:
                emit(cur)
                reset([], 0)
            continue
        cur.append(line)
        cur_len += add
        if fences and _fence_closes(line, fences[-1][0]):
            fences.pop()
        elif opened is not None:
            if not fences:
                fence_start = len(cur) - 1
            fences.append(opened)
        queue.popleft()
    emit(cur)
    return parts or [""]


def split_message(text: str, limit: int) -> list[str]:
    """Split a rendered message into parts of at most `limit` characters (SPEC §11.7).

    A text that fits is returned as a single unlabeled part. Otherwise the text is cut at
    blank lines outside code fences; a fenced block that straddles a cut is closed at the end
    of the part and reopened, with the same fence and info string, at the start of the next.
    A paragraph longer than a part is cut at line boundaries; a line is cut only when it
    cannot fit in a part of its own. Every part ends with a label on a line of its own,
    `(i/n)`, preceded by a blank line; the label counts toward `limit`.
    """
    if len(text) <= limit:
        return [text]
    parts, _ = _split_for_labels(text, limit)
    n = len(parts)
    return [f"{part}\n\n({i}/{n})" for i, part in enumerate(parts, 1)]


# ------------------------------------------------------------------------------- the Renderer


class MarkdownRenderer:
    """`Renderer` implementation (SPEC §11): agent markdown to Zulip message parts.

    `resolver` supplies the workspace side of link rewriting (§11.3, §11.4); without one,
    step 4 is skipped. `diff_inline_max_lines` is `render.diff_inline_max_lines` (§11.8).
    """

    def __init__(
        self, resolver: LinkResolver | None = None, *, diff_inline_max_lines: int = 300
    ) -> None:
        self._resolver = resolver
        self._diff_inline_max_lines = diff_inline_max_lines

    async def render_agent_text(self, text: str, ctx: RenderContext) -> list[str]:
        """Steps 1-7 of SPEC §11 after `normalize`; the message limit is `max_message_length - 200`.

        If the text would need more than three parts, or holds a line that cannot fit in a
        part intact, and uploads are available, the full rendered text is written to
        `<root>/.zab/outbox/answer-turn<N>-<8 hex>.md` (a path unique to this render, so
        that sessions sharing a workspace and turn number never overwrite each other's file
        before it is uploaded), uploaded under the name `answer-turn<N>.md`, and a single
        part is posted: the beginning of the answer plus a link to the upload. An answer
        larger than `ctx.max_upload_bytes` (§11.4) is not uploaded: all its parts are posted
        instead, so nothing is lost. Before splitting, every mention
        form left in the restored text, including those inside code, is disarmed with a
        zero-width space (`disarm_mentions`), so no cut can expose an active mention and the
        parts are sized on their final text.
        """
        limit = ctx.max_message_length - 200
        text = redact_secrets(normalize(text))
        text, table = protect_code(text)
        text = convert_math(text)
        text, table = await self._rewrite_links(text, table, ctx)
        text = neutralize_mentions(text)
        text = disarm_mentions(restore_code(text, table))
        parts, budget = _split_for_labels(text, limit)
        n = len(parts)
        if n <= 1:
            return parts
        overlong = any(len(line) > budget for line in text.split("\n"))
        data = text.encode("utf-8")
        if (n > 3 or overlong) and ctx.upload is not None and len(data) <= ctx.max_upload_bytes:
            name = f"answer-turn{ctx.turn_number or 0}.md"
            stored = f"answer-turn{ctx.turn_number or 0}-{secrets.token_hex(4)}.md"
            path = posixpath.join(ctx.workspace.root, ".zab", "outbox", stored)
            await ctx.workspace.write_bytes(path, data)
            url = await ctx.upload(Upload(path=path, name=name))
            suffix = f"\n\n(1/{n}) Full answer: [{name}]({url})"
            head = _split_unlabeled(text, limit - len(suffix))[0]
            return [head + suffix]
        return [f"{part}\n\n({i}/{n})" for i, part in enumerate(parts, 1)]

    async def _rewrite_links(
        self, text: str, table: list[CodeSpan], ctx: RenderContext
    ) -> tuple[str, list[CodeSpan]]:
        """Step 4 (SPEC §11.3, §11.4), through `zab.render.links` when a resolver is set."""
        if self._resolver is None:
            return text, table
        return await rewrite_links(text, table, ctx, self._resolver)

    def render_inline(self, text: str) -> str:
        """One line of prose: redaction, math and mentions; no links, uploads or splitting."""
        text = redact_secrets(normalize(text))
        text, table = protect_code(text)
        text = convert_math(text)
        text = neutralize_mentions(text)
        return disarm_mentions(restore_code(text, table))

    async def render_diff(self, diff: DiffResult, ctx: RenderContext) -> str:
        """The diff message (SPEC §11.8): a visible summary line, then one spoiler with the
        file list and, when it fits, the patch (else the patch is uploaded)."""
        links = await self._snapshot_links(diff, ctx)
        return await _render_diff(
            diff, ctx, inline_max_lines=self._diff_inline_max_lines, links=links
        )

    async def _snapshot_links(self, diff: DiffResult, ctx: RenderContext) -> dict[str, str]:
        """Listed files present in the turn's snapshot → their file-server URLs (SPEC §10.7,
        §11.8): every file the diff message lists except hidden and deleted ones. The snapshot
        is asked for only when there is such a file to link; the core shares it with the
        answer's links (one snapshot, one push per turn)."""
        linkable = [f.path for f in file_entries(diff) if not f.hidden and f.status != "deleted"]
        if ctx.snapshot is None or self._resolver is None or not linkable:
            return {}
        try:
            snapshot = await ctx.snapshot()
            if snapshot is None:
                return {}
            # the paths are relative to the top level, which is ws.root in Phase 1 (git.py runs
            # the diff from ws.root); no HEAD needed, so a first snapshot links too
            top = await ctx.workspace.realpath(ctx.workspace.root)
            by_abs = {posixpath.join(top, p): p for p in linkable}
            found = await self._resolver.snapshot_paths(ctx.workspace, snapshot.sha, list(by_abs))
        except Exception as exc:  # no links, never no diff
            log.warning("snapshot lookup failed: %s", type(exc).__name__)
            return {}
        return {
            rel: f"{snapshot.url}/{quote(found[path], safe='/')}"
            for path, rel in by_abs.items()
            if path in found
        }


__all__: list[str] = [
    "CodeSpan",
    "MarkdownRenderer",
    "convert_math",
    "disarm_mentions",
    "neutralize_mentions",
    "normalize",
    "protect_code",
    "redact_secrets",
    "restore_code",
    "split_message",
]
