"""Code protection (SPEC §11 step 2): fenced blocks and inline code spans, set aside so that
the later steps of the renderer (`zab.render.markdown`), and the reading of a message's mentions
(`zab.core.addressing`), never touch them. Standard library only: the waker, on the admin
server, reads mentions with it (#683)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class CodeSpan:
    """A protected piece of code: a fenced block or an inline span, fences included."""

    kind: Literal["fence", "span"]
    text: str


_PLACEHOLDER_RE = re.compile(r"\x00zabcode(\d+)\x00")
# An opening fence: up to 3 spaces, 3+ backticks or tildes, then the info string.
_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_BACKTICK_RUN = re.compile(r"`+")
_BLANK_LINE = re.compile(r"\n[ \t]*\n")  # a blank line may hold spaces or tabs


def _placeholder(index: int) -> str:
    return f"\x00zabcode{index}\x00"


def _fence_open(line: str) -> tuple[str, str] | None:
    """Return (fence, info) if `line` opens a fenced block, else None."""
    m = _FENCE_OPEN.match(line)
    if m is None:
        return None
    fence, info = m.group(1), m.group(2)
    if fence[0] == "`" and "`" in info:  # CommonMark: backtick fences take no backticks in info
        return None
    return fence, info


def _fence_closes(line: str, fence: str) -> bool:
    """True if `line` closes a block opened with `fence`: the same string, alone on its line.

    Zulip closes a fenced block only with a line equal to its opening fence
    (zerver/lib/markdown/fenced_code.py), so a longer run inside a block is content, and a
    block nested in a spoiler or quote may reuse the outer fence string. Up to three spaces
    of indentation and trailing spaces are allowed.
    """
    return line.strip() == fence and len(line) - len(line.lstrip(" ")) <= 3


def _escaped(text: str, pos: int) -> bool:
    """True if the character at `pos` is preceded by an odd number of backslashes."""
    k = 0
    while pos - k - 1 >= 0 and text[pos - k - 1] == "\\":
        k += 1
    return k % 2 == 1


def _protect_spans(text: str, table: list[CodeSpan]) -> str:
    """Replace inline code spans with placeholders, one paragraph at a time.

    A span never crosses a blank line (one that is empty or holds only spaces or tabs), so
    each paragraph is matched on its own: an unmatched backtick only ever scans the runs of
    its paragraph, which keeps the pass linear in the size of the text.
    """
    out: list[str] = []
    pos = 0
    for m in _BLANK_LINE.finditer(text):
        out.append(_protect_paragraph_spans(text[pos : m.start()], table))
        out.append(m.group())
        pos = m.end()
    out.append(_protect_paragraph_spans(text[pos:], table))
    return "".join(out)


def _protect_paragraph_spans(text: str, table: list[CodeSpan]) -> str:
    """CommonMark backtick-run matching inside one paragraph.

    A backtick run preceded by a backslash is escaped and cannot open a span (Zulip renders
    it as a literal backtick); inside a span backslashes do not escape, so any run of the
    right length closes it. The closer of every run is precomputed in one backward pass, so
    an unmatched opener costs nothing more than a matched one.
    """
    runs = list(_BACKTICK_RUN.finditer(text))
    next_same: list[int | None] = [None] * len(runs)  # index of the next run of equal length
    last: dict[int, int] = {}
    for j in range(len(runs) - 1, -1, -1):
        length = len(runs[j].group())
        next_same[j] = last.get(length)
        last[length] = j
    out: list[str] = []
    pos = 0
    i = 0
    while i < len(runs):
        opener = runs[i]
        if _escaped(text, opener.start()):
            i += 1
            continue
        closer_index = next_same[i]
        if closer_index is None:  # an unmatched backtick string is literal text
            i += 1
            continue
        closer = runs[closer_index]
        out.append(text[pos : opener.start()])
        table.append(CodeSpan("span", text[opener.start() : closer.end()]))
        out.append(_placeholder(len(table) - 1))
        pos = closer.end()
        i = closer_index + 1
    out.append(text[pos:])
    return "".join(out)


_MARKDOWN_FENCES = frozenset({"spoiler", "quote"})  # Zulip fences whose body is markdown


def _markdown_body(info: str) -> bool:
    words = info.strip().split(None, 1)
    return bool(words) and words[0].lower() in _MARKDOWN_FENCES


def _protect_lines(lines: list[str], table: list[CodeSpan]) -> list[str]:
    """The walker behind `protect_code`: one pass with an explicit stack of open blocks.

    A code block is protected whole, up to its closer or the end of the text. A block whose
    body is markdown (spoiler, quote) has only its fence lines protected; its body is walked
    like the top level, so blocks nested in it, however deep, are handled without recursion
    and without rescanning. Returns runs of lines with placeholders in place of code.
    """
    out: list[str] = []
    prose: list[str] = []
    code: list[str] = []  # lines of the innermost open code block, opener included
    stack: list[tuple[str, str]] = []  # open blocks, outermost first: (fence, info)

    def flush_prose() -> None:
        if prose:
            out.append(_protect_spans("\n".join(prose), table))
            prose.clear()

    def protect(text: str) -> None:
        table.append(CodeSpan("fence", text))
        out.append(_placeholder(len(table) - 1))

    for line in lines:
        top = stack[-1] if stack else None
        if top is not None and not _markdown_body(top[1]):  # inside a code block
            code.append(line)
            if _fence_closes(line, top[0]):
                protect("\n".join(code))
                code.clear()
                stack.pop()
            continue
        if top is not None and _fence_closes(line, top[0]):  # a spoiler or quote ends
            flush_prose()
            protect(line)
            stack.pop()
            continue
        opened = _fence_open(line)
        if opened is None:
            prose.append(line)
            continue
        flush_prose()
        stack.append(opened)
        if _markdown_body(opened[1]):
            protect(line)
        else:
            code.append(line)
    flush_prose()
    if code:  # an unterminated code block runs to the end of the text
        protect("\n".join(code))
    return out


def protect_code(text: str) -> tuple[str, list[CodeSpan]]:
    """Replace fenced blocks and inline code spans with placeholders (SPEC §11 step 2).

    Fences of three or more backticks or tildes open a block; it closes at a line holding at
    least as many of the same character, or runs to the end of the text. Inline spans follow
    CommonMark: a run of N backticks closes at the next run of exactly N backticks, and never
    across a blank line (one that is empty or holds only spaces or tabs). The
    placeholders (`\\x00zabcode<i>\\x00`) contain no dollar, backtick, backslash or `@`, so the
    math and mention steps cannot touch them. `restore_code` puts the originals back.

    Zulip renders the body of a ```spoiler and a ```quote fence as markdown, so only their
    fence lines are protected: their bodies go through the later steps like any prose, and
    fences nested inside them are protected on their own.

    NUL characters are dropped first: Zulip refuses them in a message anyway, and without
    them no input can contain the placeholder syntax, so `restore_code` only ever sees the
    placeholders this call produced.
    """
    table: list[CodeSpan] = []
    lines = text.replace("\x00", "").split("\n")
    return "\n".join(_protect_lines(lines, table)), table


def restore_code(text: str, table: list[CodeSpan]) -> str:
    """Put the code protected by `protect_code` back in place of its placeholders."""
    return _PLACEHOLDER_RE.sub(lambda m: table[int(m.group(1))].text, text)


_BLOCKQUOTE = re.compile(r" {0,3}>")


def _quote_fence(info: str) -> bool:
    words = info.strip().split(None, 1)
    return bool(words) and words[0].lower() == "quote"


def blank_quotes(text: str) -> str:
    """`text` with what Zulip shows as quoted made blank, line for line: a ```quote fence, with
    its fences and body, and a blockquote, from a line that starts with `>` to the next blank
    line (Zulip, as Python-Markdown, continues a quote's paragraph on the lines that follow it).
    Code blocks outside quotes stay as they are, for `protect_code`; so do the bodies of
    spoilers. Zulip silences the mentions in quoted text, someone's earlier words, and the
    reading of a message's mentions (`zab.core.addressing`) leaves them out as well (#713).
    A blanked line becomes an empty one, so that no code span forms across it."""
    out: list[str] = []
    stack: list[tuple[str, str]] = []  # open blocks, outermost first: (fence, info)
    quotes = 0  # the quote fences among them
    quoted = False  # in a `>` blockquote, which a blank line or a fence ends
    for line in text.split("\n"):
        top = stack[-1] if stack else None
        if top is not None and (not _markdown_body(top[1]) or _fence_closes(line, top[0])):
            blank = quotes > 0  # a code block's line, or the closer of the innermost block
            if _fence_closes(line, top[0]):
                stack.pop()
                quotes -= _quote_fence(top[1])
                quoted = False
            out.append("" if blank else line)
            continue
        if (opened := _fence_open(line)) is not None:
            stack.append(opened)
            quotes += _quote_fence(opened[1])
            quoted = False
            out.append("" if quotes else line)
            continue
        if not line.strip():
            quoted = False
        elif _BLOCKQUOTE.match(line):
            quoted = True
        out.append("" if quotes or quoted else line)
    return "\n".join(out)


__all__ = ["CodeSpan", "blank_quotes", "protect_code", "restore_code"]
