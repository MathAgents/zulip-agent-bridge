"""Table tests for zab.render.markdown (SPEC §11.2, §11.5-§11.7; AT-13, AT-14, AT-15)."""

from __future__ import annotations

import re
import time
import tracemalloc
from dataclasses import dataclass, field

import pytest

from zab.core.types import RenderContext, Upload
from zab.render.markdown import (
    CodeSpan,
    MarkdownRenderer,
    convert_math,
    disarm_mentions,
    neutralize_mentions,
    normalize,
    protect_code,
    redact_secrets,
    restore_code,
    split_message,
)

Z = "\u200b"  # the zero-width space that disarms a mention (codex round 4)
_ACTIVE = re.compile(r"@(?:\*\*[^*\n]+\*\*|\*[^*\n]+\*)")  # a literal Zulip mention form


def _active(parts: list[str]) -> bool:
    return any(_ACTIVE.search(p) for p in parts)


# ------------------------------------------------------------------------------ §11.6 redaction

REDACT_TABLE = [
    ("key sk-" + "a" * 24 + " ok", "key [redacted openai key] ok"),
    ("sk-proj-" + "Ab_-" * 6, "[redacted openai key]"),
    ("sk-ant-" + "x" * 30, "[redacted anthropic key]"),
    ("sk-ant-api03-" + "x" * 30 + " tail", "[redacted anthropic key] tail"),
    ("ghp_" + "A" * 36, "[redacted github token]"),
    ("ghs_" + "0" * 40, "[redacted github token]"),
    ("github_pat_" + "a_" * 30, "[redacted github token]"),
    ("AKIA" + "Q" * 16, "[redacted aws access key]"),
    ("AKIA" + "q" * 16, "AKIA" + "q" * 16),  # lower case: not an access key
    ("sk-short", "sk-short"),
    ("key=" + "k" * 32, "[redacted zulip api key]"),
    ("  key=" + "k" * 32 + "  ", "  [redacted zulip api key]"),
    ("+key=" + "k" * 32, "+[redacted zulip api key]"),  # a diff line is still a whole line
    ("+key=" + "k" * 32 + "\r\n", "+[redacted zulip api key]\r\n"),
    ("- key=" + "k" * 32, "- [redacted zulip api key]"),  # so is a list item
    ("x key=" + "k" * 32, "x key=" + "k" * 32),
    ("prefix key=" + "k" * 32, "prefix key=" + "k" * 32),  # not a whole line
    ("key=" + "k" * 31, "key=" + "k" * 31),
    ("key=" + "k" * 32 + "\r\n", "[redacted zulip api key]\r\n"),  # CRLF: the \r is kept
    ("a\r\nkey=" + "k" * 32 + "\r\nb", "a\r\n[redacted zulip api key]\r\nb"),
    (
        "a\n-----BEGIN RSA PRIVATE KEY-----\nMIIE\nvQ==\n-----END RSA PRIVATE KEY-----\nb",
        "a\n[redacted private key]\nb",
    ),
    (
        "-----BEGIN PRIVATE KEY-----\nMIIE\n-----END PRIVATE KEY-----",
        "[redacted private key]",
    ),
    ("-----BEGIN PRIVATE KEY-----\nMIIE\ntruncated", "[redacted private key]"),
    ("nothing here", "nothing here"),
    # issue #36 additions
    ("ASIA" + "Q" * 16, "[redacted aws access key]"),
    ("t eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl end", "t [redacted jwt] end"),
    ("eyJhbGci.notajwt", "eyJhbGci.notajwt"),
    ("xeyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2ln", "xeyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2ln"),
    ("key = " + "k" * 32, "[redacted zulip api key]"),
    ("+key =" + "k" * 32 + "\r\n", "+[redacted zulip api key]\r\n"),
    ("export ZULIP_API_KEY=" + "k" * 32, "export ZULIP_API_KEY=[redacted zulip api key]"),
    ('ZULIP_API_KEY = "' + "k" * 32 + '"', "ZULIP_API_KEY = [redacted zulip api key]"),
    (
        "curl -u bot@example.com:" + "k" * 32 + " https://z/api",
        "curl -u bot@example.com:[redacted zulip api key] https://z/api",
    ),
    ("curl --user 'bot@x.y:" + "k" * 32 + "'", "curl --user 'bot@x.y:[redacted zulip api key]'"),
    (
        "a\n-----BEGIN PGP PRIVATE KEY BLOCK-----\nlQOY\n=abcd\n"
        "-----END PGP PRIVATE KEY BLOCK-----\nb",
        "a\n[redacted pgp private key]\nb",
    ),
    ("-----BEGIN PGP PRIVATE KEY BLOCK-----\nlQOY\ntruncated", "[redacted pgp private key]"),
    (
        "-----BEGIN PGP PUBLIC KEY BLOCK-----\nmQ\n-----END PGP PUBLIC KEY BLOCK-----",
        "-----BEGIN PGP PUBLIC KEY BLOCK-----\nmQ\n-----END PGP PUBLIC KEY BLOCK-----",
    ),
    # PM review of PR #48: a key prefix inside a hyphenated URL slug is not a key
    (
        "https://mathoverflow.net/questions/12345/conformal-map-from-the-unit-disk-to-the-upper-half-plane",
        "https://mathoverflow.net/questions/12345/conformal-map-from-the-unit-disk-to-the-upper-half-plane",
    ),
    (
        "see https://math.stackexchange.com/questions/678/task-scheduling-with-risk-constraints-and-mask-operations?rq=1",
        "see https://math.stackexchange.com/questions/678/task-scheduling-with-risk-constraints-and-mask-operations?rq=1",
    ),
    (
        "[disk](https://mathoverflow.net/q/1/a-disk-to-the-plane) sk-" + "a" * 24,
        "[disk](https://mathoverflow.net/q/1/a-disk-to-the-plane) [redacted openai key]",
    ),
    ("sk-proj-" + "a" * 24 + " at line start", "[redacted openai key] at line start"),
    ('"sk-ant-api03-' + "x" * 30 + '"', '"[redacted anthropic key]"'),
    ("(sk-" + "a" * 24 + ")", "([redacted openai key])"),
    ("OPENAI_API_KEY=sk-" + "a" * 24, "OPENAI_API_KEY=[redacted openai key]"),
    ("ask-" + "a" * 24, "ask-" + "a" * 24),  # a word ending in sk-: not a key
    ("foo-sk-" + "a" * 24, "foo-sk-" + "a" * 24),  # a slug word starting with sk-: not a key
    # codex round 2 on PR #48: a diff's deletion marker must not hide a key
    ("-sk-" + "a" * 24, "-[redacted openai key]"),
    ("-sk-proj-" + "a" * 24, "-[redacted openai key]"),
    ("-sk-ant-api03-" + "x" * 30, "-[redacted anthropic key]"),
    (
        "a\n-sk-ant-" + "x" * 30 + "\n+sk-" + "a" * 24,
        "a\n-[redacted anthropic key]\n+[redacted openai key]",
    ),
    ("--sk-" + "a" * 24, "--[redacted openai key]"),  # no word before the dashes: a key
    ("MALAYSIA" + "Q" * 16, "MALAYSIA" + "Q" * 16),  # upper-case prose around ASIA
    ("highp_" + "A" * 36, "highp_" + "A" * 36),
    (" ghp_" + "A" * 36, " [redacted github token]"),
]


@pytest.mark.parametrize(("text", "expected"), REDACT_TABLE)
def test_redact_secrets(text: str, expected: str) -> None:
    assert redact_secrets(text) == expected


#: Every character after which `str.splitlines` starts a new line (a 0.1 s scan of Unicode).
LINE_ENDS = [c for c in map(chr, range(0x110000)) if len(f"a{c}b".splitlines()) == 2]


@pytest.mark.parametrize("end", LINE_ENDS, ids=lambda c: f"U+{ord(c):04X}")
def test_a_zuliprc_line_ends_where_splitlines_ends_it(end: str) -> None:
    """Review of #663: a caller that takes a line out of text (stderr's last line) gets a whole
    `key=` line wherever `splitlines` splits, a lone `\\r` from progress output included; the
    rule's `^…$` alone knows only `\\n`. The prefix is kept, and part of a line is not a line."""
    k = "k" * 32
    assert redact_secrets(f"10%{end}key={k}{end}more") == (
        f"10%{end}[redacted zulip api key]{end}more"
    )
    assert redact_secrets(f"a{end} key = {k}") == f"a{end} [redacted zulip api key]"
    assert redact_secrets(f"a{end}x key={k}") == f"a{end}x key={k}"  # not a whole line
    assert redact_secrets(f"key={k[:16]}{end}{k[16:]}") == f"key={k[:16]}{end}{k[16:]}"
    lines = redact_secrets(f"progress{end}key={k}").splitlines()
    assert lines == ["progress", "[redacted zulip api key]"]


def test_line_ends_are_the_ones_splitlines_knows() -> None:
    from zab.render import redact

    assert sorted(LINE_ENDS) == sorted("\n" + redact._OTHER_LINE_ENDS)


# ------------------------------------------------------------------------- code protection

PROTECT_TABLE = [
    ("plain", "plain", []),
    ("a `x` b", "a \x00zabcode0\x00 b", [CodeSpan("span", "`x`")]),
    ("a ``x`y`` b", "a \x00zabcode0\x00 b", [CodeSpan("span", "``x`y``")]),
    (
        "`a` and `b`",
        "\x00zabcode0\x00 and \x00zabcode1\x00",
        [CodeSpan("span", "`a`"), CodeSpan("span", "`b`")],
    ),
    ("lonely ` tick", "lonely ` tick", []),
    ("``mismatch` here", "``mismatch` here", []),
    ("`no\n\nparagraph`", "`no\n\nparagraph`", []),
    ("`a\n \nb`", "`a\n \nb`", []),  # a whitespace-only line is a paragraph boundary too
    (
        "x\n```py\n$1\n```\ny",
        "x\n\x00zabcode0\x00\ny",
        [CodeSpan("fence", "```py\n$1\n```")],
    ),
    (  # Zulip closes a fence only with its exact string: a longer run is content
        "~~~\n```\nnot a close\n~~~~\nz",
        "\x00zabcode0\x00",
        [CodeSpan("fence", "~~~\n```\nnot a close\n~~~~\nz")],
    ),
    (
        "~~~\n```\nnot a close\n~~~~\n~~~\nz",
        "\x00zabcode0\x00\nz",
        [CodeSpan("fence", "~~~\n```\nnot a close\n~~~~\n~~~")],
    ),
    (  # a spoiler may nest a block with the same fence string (Zulip fenced_code stack)
        "```spoiler S\n```py\n$1\n```\nafter\n```",
        "\x00zabcode0\x00\n\x00zabcode1\x00\nafter\n\x00zabcode2\x00",
        [
            CodeSpan("fence", "```spoiler S"),
            CodeSpan("fence", "```py\n$1\n```"),
            CodeSpan("fence", "```"),
        ],
    ),
    (
        "````\n```\nstill open\n````",
        "\x00zabcode0\x00",
        [CodeSpan("fence", "````\n```\nstill open\n````")],
    ),
    ("```\nunterminated\n$x$", "\x00zabcode0\x00", [CodeSpan("fence", "```\nunterminated\n$x$")]),
    (
        "  ```js\ncode\n  ```\nafter `s`",
        "\x00zabcode0\x00\nafter \x00zabcode1\x00",
        [CodeSpan("fence", "  ```js\ncode\n  ```"), CodeSpan("span", "`s`")],
    ),
    ("``` a ` b\nnot a fence", "``` a ` b\nnot a fence", []),
    ("\\`@**all**`", "\\`@**all**`", []),  # an escaped backtick opens nothing
    ("\\\\`x`", "\\\\\x00zabcode0\x00", [CodeSpan("span", "`x`")]),  # escaped backslash
    ("`a\\` b", "\x00zabcode0\x00 b", [CodeSpan("span", "`a\\`")]),  # no escapes in a span
    (  # a spoiler body is markdown: only its fence lines and nested spans are code
        "```spoiler Details\n@**all** `x`\n```\nafter",
        "\x00zabcode0\x00\n@**all** \x00zabcode1\x00\n\x00zabcode2\x00\nafter",
        [
            CodeSpan("fence", "```spoiler Details"),
            CodeSpan("span", "`x`"),
            CodeSpan("fence", "```"),
        ],
    ),
    (  # a quote body with a nested code fence
        "````quote\n```py\n$1\n```\ntext\n````",
        "\x00zabcode0\x00\n\x00zabcode1\x00\ntext\n\x00zabcode2\x00",
        [
            CodeSpan("fence", "````quote"),
            CodeSpan("fence", "```py\n$1\n```"),
            CodeSpan("fence", "````"),
        ],
    ),
    ("```spoiler\nopen", "\x00zabcode0\x00\nopen", [CodeSpan("fence", "```spoiler")]),
    (
        "```Spoilers\n@**all**\n```",
        "\x00zabcode0\x00",
        [CodeSpan("fence", "```Spoilers\n@**all**\n```")],
    ),
]


@pytest.mark.parametrize(("text", "protected", "table"), PROTECT_TABLE)
def test_protect_code(text: str, protected: str, table: list[CodeSpan]) -> None:
    got, got_table = protect_code(text)
    assert got == protected
    assert got_table == table
    assert restore_code(got, got_table) == text


# ------------------------------------------------------------------------------------ §11.2 math

MATH_TABLE = [
    ("$100 and $200", "$100 and $200"),
    ("costs $5.", "costs $5."),
    ("$x$-adic", "$$x$$-adic"),
    ("$x^2$", "$$x^2$$"),
    (r"\(a\)", "$$a$$"),
    (r"\( a \)", "$$a$$"),
    ("$a$ and $b$", "$$a$$ and $$b$$"),
    ("$ x$", "$ x$"),
    ("$x $", "$x $"),
    (r"\$5 and \$6", r"\$5 and \$6"),
    ("$$a$$", "```math\na\n```"),  # a paragraph consisting only of $$...$$
    ("keep $$a$$ inline", "keep $$a$$ inline"),
    ("$$a$$ $$b$$", "$$a$$ $$b$$"),
    ("p\n\n$$\nx = 1\n$$\n\nq", "p\n\n```math\nx = 1\n```\n\nq"),
    ("p\n\n$$ x = 1 $$\n\nq", "p\n\n```math\nx = 1\n```\n\nq"),
    ("p\n$$x$$\nq", "p\n$$x$$\nq"),  # not a paragraph of its own
    (r"\[ x^2 \]", "```math\nx^2\n```"),
    ("see \\[x\\] here", "see \n```math\nx\n```\n here"),
    (
        "\\begin{align*}\na &= b \\\\\nc &= d\n\\end{align*}",
        "```math\n\\begin{align*}\na &= b \\\\\nc &= d\n\\end{align*}\n```",
    ),
    (
        "\\begin{equation}E=mc^2\\end{equation}",
        "```math\n\\begin{equation}E=mc^2\\end{equation}\n```",
    ),
    ("\\[\\begin{align}a\\end{align}\\]", "```math\n\\begin{align}a\\end{align}\n```"),
    (
        "$$\n\\begin{gather}\na\n\\end{gather}\n$$",
        "```math\n\\begin{gather}\na\n\\end{gather}\n```",
    ),
    ("$\\mathbb{Q}$", "$$\\mathbb{Q}$$"),
    ("\\(\\overline{\\mathbb{Q}}\\)", "$$\\overline{\\mathbb{Q}}$$"),
    ("$a\nb$", "$$a\nb$$"),
    ("$a\n\nb$", "$a\n\nb$"),
    ("$a\n \nb$", "$a\n \nb$"),
    ("$100 and $200; compute $x^2$.", "$100 and $200; compute $$x^2$$."),
    (r"$a\$b$", r"$$a\$b$$"),
    ("$a $b$ c$", "$a $$b$$ c$"),
    ("$$a$$ b $$c$$", "$$a$$ b $$c$$"),  # two inline maths, not a paragraph of their own
    ("$$\na\n$$\n\n$$\nb\n$$", "```math\na\n```\n\n```math\nb\n```"),
    ("t\n\n  $$ a $$  \nmore", "t\n\n  $$ a $$  \nmore"),  # not alone in its paragraph
    ("t\n\n  $$ a $$  \n\nmore", "t\n\n```math\na\n```\n\nmore"),
    ("$n$-th and $20,000", "$$n$$-th and $20,000"),
    ("no math", "no math"),
]


@pytest.mark.parametrize(("text", "expected"), MATH_TABLE)
def test_convert_math(text: str, expected: str) -> None:
    assert convert_math(text) == expected


# -------------------------------------------------------------------------------- §11.5 mentions

MENTION_TABLE = [
    ("@**all**", f"`@{Z}**all**`"),
    ("hi @**everyone** and @**channel**", f"hi `@{Z}**everyone**` and `@{Z}**channel**`"),
    ("@**stream** @**topic**", f"`@{Z}**stream**` `@{Z}**topic**`"),
    ("@*admins*", f"`@{Z}*admins*`"),
    ("@**Drew Sutherland**", "@_**Drew Sutherland**"),
    ("@**Drew|1234567**", "@_**Drew|1234567**"),
    ("@_**Drew**", "@_**Drew**"),
    ("mail me@example.com", "mail me@example.com"),
    ("@**all** says @**Alice**", f"`@{Z}**all**` says @_**Alice**"),
    ("` foo @**all**", f"` foo ``@{Z}**all**``"),  # a stray backtick cannot pair with the wrapper
    ("\\`@**all**`", f"\\` ``@{Z}**all**`` `"),  # spaces keep the wrapper a run of its own
    ("`` x @**everyone** `", f"`` x ```@{Z}**everyone**``` `"),
    ("\\@**all**", f"\\ `@{Z}**all**`"),  # a backslash must not escape the wrapper
    ("\\\\@**topic**", f"\\\\ `@{Z}**topic**`"),
    # Next to a placeholder (a protected span or fence) the wrapper is kept apart too.
    (
        "\x00zabcode0\x00@**all**\x00zabcode1\x00",
        f"\x00zabcode0\x00 `@{Z}**all**` \x00zabcode1\x00",
    ),
]


@pytest.mark.parametrize(("text", "expected"), MENTION_TABLE)
def test_neutralize_mentions(text: str, expected: str) -> None:
    assert neutralize_mentions(text) == expected
    assert not _active([expected])


DISARM_TABLE = [
    ("@**all**", f"@{Z}**all**"),
    ("`@**all**`", f"`@{Z}**all**`"),
    ("x@**Bob|12**y", f"x@{Z}**Bob|12**y"),
    ("@*admins*", f"@{Z}*admins*"),
    ("@_**Bob**", "@_**Bob**"),  # a silent mention stays a (notification-free) mention
    ("a@b.c and @*ARGS", "a@b.c and @*ARGS"),  # not a mention form: no closing `*`
    ("@** and @ and @_", "@** and @ and @_"),
    ("@*ARGS and @*", f"@{Z}*ARGS and @*"),  # Zulip would read this as a group mention
    (f"@{Z}**all**", f"@{Z}**all**"),  # idempotent
]


@pytest.mark.parametrize(("text", "expected"), DISARM_TABLE)
def test_disarm_mentions(text: str, expected: str) -> None:
    assert disarm_mentions(text) == expected
    assert disarm_mentions(expected) == expected
    assert not _active([expected]) or expected.startswith("@_")


# ------------------------------------------------------------------------------- §11.7 splitting


def _paragraphs(n: int, width: int = 80) -> str:
    return "\n\n".join(f"p{i} " + "x" * (width - 4) for i in range(n))


def test_split_fits() -> None:
    assert split_message("short", 1000) == ["short"]
    assert split_message("", 10) == [""]


def test_split_at_blank_lines() -> None:
    text = _paragraphs(10)  # 10 x 80 + 9 x 2 = 818 chars
    parts = split_message(text, 340)  # budget 328: four paragraphs per part
    assert len(parts) == 3
    assert [p.split("\n")[-1] for p in parts] == ["(1/3)", "(2/3)", "(3/3)"]
    for part in parts:
        assert len(part) <= 340
        body = part.rsplit("\n\n", 1)[0]
        assert not body.startswith("\n") and not body.endswith("\n")
    joined = "\n\n".join(p.rsplit("\n\n", 1)[0] for p in parts)
    assert joined == text


def test_split_labels_past_999_parts() -> None:
    """Issue #36: the label reserve grows with the number of parts."""
    text = "\n\n".join(f"p{i} " + "x" * 20 for i in range(1500))
    parts = split_message(text, 60)
    assert len(parts) >= 1000
    assert all(len(p) <= 60 for p in parts)
    assert parts[-1].endswith(f"({len(parts)}/{len(parts)})")
    assert "\n\n".join(p.rsplit("\n\n", 1)[0] for p in parts) == text


def test_split_hard_cuts_long_paragraph() -> None:
    text = "\n".join(f"line {i:02d}" for i in range(40))  # one paragraph, 359 chars
    parts = split_message(text, 120)
    assert len(parts) >= 3
    assert all(len(p) <= 120 for p in parts)
    assert "\n".join(p.rsplit("\n\n", 1)[0] for p in parts) == text


def test_split_chunks_overlong_line() -> None:
    parts = split_message("y" * 500, 100)
    assert all(len(p) <= 100 for p in parts)
    assert "".join(p.rsplit("\n\n", 1)[0].replace("\n", "") for p in parts) == "y" * 500


def test_split_keeps_lines_that_fit_intact() -> None:
    """A long code line that fits in a part of its own is never cut (codex round 1, P2)."""
    text = "```python\n" + "x" * 5000 + "\n```\n" + "y" * 6000
    parts = split_message(text, 9800)
    assert len(parts) == 2
    assert all(len(p) <= 9800 for p in parts)
    assert "x" * 5000 + "\n" in parts[0] and "x" * 5001 not in parts[0]
    assert parts[1].startswith("y" * 6000)


def test_split_fence_part_leaves_room_for_the_closer() -> None:
    line = "z" * 9770
    text = "````\n" + "\n".join([line, line, line]) + "\n````"
    parts = split_message(text, 9800)
    assert all(len(p) <= 9800 for p in parts)
    bodies = [p.rsplit("\n\n", 1)[0] for p in parts]
    assert all(b.startswith("````\n") and b.endswith("\n````") for b in bodies)
    assert sum(b.count(line) for b in bodies) == 3


def test_split_terminates_on_a_fence_opener_that_fills_a_part() -> None:
    """Codex round 2: an opener with no room left for content must not loop forever."""
    text = "```" + "x" * 9785 + "\ny\n```\n" + "z" * 100
    parts = split_message(text, 9800)
    assert all(len(p) <= 9800 for p in parts)
    joined = "\n".join(p.rsplit("\n\n", 1)[0] for p in parts)
    assert joined.count("x") == 9785 and "y" in joined and "z" * 100 in joined


def test_split_cuts_a_code_line_only_when_it_cannot_fit() -> None:
    text = "```sh\n" + "w" * 9795 + "\n```"  # the line plus the fence lines exceed a part
    parts = split_message(text, 9800)
    assert all(len(p) <= 9800 for p in parts)
    joined = "".join(p.rsplit("\n\n", 1)[0] for p in parts)
    assert joined.count("w") == 9795


def test_split_fence_closed_and_reopened() -> None:
    """AT-15: about 15 000 characters with a code block straddling the split point."""
    prose = _paragraphs(40)  # ~3300 chars
    code = "```python\n" + "\n".join(f"x{i} = {i} " + "#" * 60 for i in range(120)) + "\n```"
    text = prose + "\n\n" + code + "\n\n" + _paragraphs(40)
    assert 14_000 < len(text) < 16_000
    parts = split_message(text, 10_000 - 200)
    assert len(parts) == 2
    assert all(len(p) <= 9_800 for p in parts)
    first, second = (p.rsplit("\n\n", 1)[0] for p in parts)
    assert parts[0].endswith("(1/2)") and parts[1].endswith("(2/2)")
    assert first.endswith("\n```")
    assert second.startswith("```python\n")
    # No code line is lost or duplicated by the split.
    code_lines = [ln for ln in code.split("\n")[1:-1]]
    got = [ln for ln in (first + "\n" + second).split("\n") if ln.startswith("x")]
    assert got == code_lines


def test_split_reopens_nested_fences_inside_a_spoiler() -> None:
    """Codex round 5: a cut inside a block nested in a spoiler reopens both blocks."""
    text = (
        "````spoiler Example\n```python\n"
        + "\n".join(f"x = {i}" for i in range(30))
        + "\n```\n````"
    )
    parts = split_message(text, 140)
    assert len(parts) >= 3
    bodies = [p.rsplit("\n\n", 1)[0] for p in parts]
    assert all(len(p) <= 140 for p in parts)
    assert all(b.startswith("````spoiler Example\n```python\n") for b in bodies)
    assert all(b.endswith("\n```\n````") for b in bodies)
    code = [ln for b in bodies for ln in b.split("\n") if ln.startswith("x = ")]
    assert code == [f"x = {i}" for i in range(30)]


def test_split_reserves_the_closer_of_a_nested_opener() -> None:
    """Codex round 6: a nested opener that fits must leave room for its own closer."""
    fence = "`" * 250
    text = (
        "```spoiler\n" + "x" * 9500 + "\n" + fence + "python\ny\n" + fence + "\n```\n" + "z" * 100
    )
    parts = split_message(text, 9800)
    assert all(len(p) <= 9800 for p in parts)
    bodies = [p.rsplit("\n\n", 1)[0] for p in parts]
    assert len(bodies) == 2
    assert bodies[0] == "```spoiler\n" + "x" * 9500 + "\n```"
    assert bodies[1] == "```spoiler\n" + fence + "python\ny\n" + fence + "\n```\n" + "z" * 100


@pytest.mark.parametrize(
    "text",
    [
        "$a" + "b" * 1_000_000,  # never closed
        "$a" + "b " * 500_000 + "c$ d",  # closed at the end
        "$a" + "\\b" * 500_000,  # escapes
    ],
    ids=["open", "closed", "escapes"],
)
def test_convert_math_costs_bounded_memory(text: str) -> None:
    """#833: `_DOLLAR_MATH`'s lazy repeat of a group kept a frame of `re` per character of the
    run after a `$`, 147 MB on this megabyte, on every answer. It is possessive now."""
    tracemalloc.start()
    try:
        base = tracemalloc.get_traced_memory()[0]
        convert_math(text)
        peak = tracemalloc.get_traced_memory()[1] - base
    finally:
        tracemalloc.stop()
    assert peak < 16_000_000, f"{peak / 1e6:.0f} MB"


#: The bounds on time here are on CPU time (`process_time`), not the wall clock, which
#: other workers on a busy CI runner stretch: one took 1.33 s against its 1 s at -n 4 (#473).
@pytest.mark.parametrize("text", ["eyJA" * 30_000, "eyJ" * 40_000, "eyJ." * 30_000])
def test_redact_secrets_is_linear_on_jwt_like_runs(text: str) -> None:
    """PR #48 round 1: a long run of token characters without a JWT must not backtrack."""
    start = time.process_time()
    out = redact_secrets(text)
    assert time.process_time() - start < 0.5  # the unanchored rule took seconds
    assert out == text


def test_neutralize_mentions_is_linear() -> None:
    """Codex round 8: the wrapper run length is computed once, not per mention."""
    text = "@**all** " * 8000
    start = time.process_time()
    out = neutralize_mentions(text)
    assert time.process_time() - start < 1.0  # the per-mention rescan took about 3 s
    assert out == f"`@{Z}**all**` " * 8000
    assert not _active([out])


async def test_nul_split_secrets_are_redacted() -> None:
    """Codex round 9: NUL is dropped before redaction, so a split key cannot be reassembled."""
    assert normalize("a\x00b\x00") == "ab"
    r = MarkdownRenderer()
    assert r.render_inline("sk-\x00" + "a" * 24) == "[redacted openai key]"
    assert r.render_inline("k sk-ant-\x00" + "A" * 30) == "k [redacted anthropic key]"
    parts = await r.render_agent_text(
        "k sk-ant-\x00" + "A" * 30 + "\n\nghp_\x00" + "B" * 36, _ctx(FakeWorkspace(), None)
    )
    assert parts == ["k [redacted anthropic key]\n\n[redacted github token]"]


def test_deeply_nested_spoilers_do_not_recurse() -> None:
    """Codex round 9: nesting depth must not hit the interpreter's recursion limit."""
    text = "```spoiler\n" * 1100 + "hello @**all**\n" + "```\n" * 1100
    protected, table = protect_code(text)
    assert len(table) == 2200 and all(e.kind == "fence" for e in table)
    assert restore_code(protected, table) == text
    out = MarkdownRenderer().render_inline(text)
    assert "hello `@" in out and not _active([out])


def test_literal_placeholder_syntax_in_input_is_harmless() -> None:
    """Codex round 8: agent text holding the placeholder syntax cannot corrupt restoration."""
    r = MarkdownRenderer()
    assert r.render_inline("\x00zabcode0\x00") == "zabcode0"
    assert r.render_inline("`x` \x00zabcode0\x00 and `y`") == "`x` zabcode0 and `y`"
    assert restore_code(*protect_code("\x00zabcode99\x00 `q`")) == "zabcode99 `q`"


#: The three tests below bound how a cost grows, not how long it takes: their absolute bounds
#: of 1 s failed on a runner that took ten times this machine's time (#707). Each times a text
#: against the same work in pieces, in the same run (`cpu_ratio_below`, tests/conftest.py),
#: and runs alone (`timing`). Under Python 3.11 to 3.14, a text takes 0.97 to 1.11 times the
#: CPU of its pieces here (under 3.14 beside a busy thread on the same core too), and 0.96 to
#: 1.16 times on CI's runners (#734); the bound is 2. The quadratic versions that Codex found
#: take 3.8 times or more here, and fail it.
@pytest.mark.timing
@pytest.mark.parametrize("opener", ["\\[", "\\(", "\\begin{align}", "$$"])
def test_convert_math_is_linear_on_unmatched_openers(opener: str, cpu_ratio_below) -> None:
    """Codex round 10: an unmatched opener must not rescan the rest of the paragraph.

    8,000 openers against 2,000 four times. The rescanning version scanned the rest of the
    paragraph from each opener: 4.0 times (0.22 s and 3.5 s for 4,000 and 16,000 of `\\[`).
    `$$` never rescanned, as only the start of a paragraph could open it. 8,000 openers keep
    a regression's failure under 30 s here."""
    assert convert_math(opener * 8_000) == opener * 8_000
    cpu_ratio_below(convert_math, opener * 2_000, opener * 8_000, 2, repeat=4)


@pytest.mark.timing
@pytest.mark.parametrize("unit", ["$$x ", "$$x$$ "])
def test_convert_math_is_linear_on_dollar_candidates(unit: str, cpu_ratio_below) -> None:
    """Codex round 11: deciding whether `$$` starts a paragraph must not copy the prefix.

    128,000 units against 16,000 eight times. The prefix-copying version copied all the text
    before each candidate: 3.8 to 5.7 times. Its quadratic term is a copy, fast here: at the
    64,000 units the test timed before #707, it took 0.17 and 0.41 s, which its bound of 1 s
    let pass."""
    assert convert_math(unit * 128_000) == unit * 128_000
    cpu_ratio_below(convert_math, unit * 16_000, unit * 128_000, 2, repeat=8)


@pytest.mark.timing
def test_protect_code_is_linear_on_unmatched_run_lengths(cpu_ratio_below) -> None:
    """Codex round 11: an unmatched opener must not rescan the paragraph's runs for a closer.

    198 unmatched runs, of 2 to 199 backticks, before 100,000 single backticks, against the
    single backticks alone. The forward-search version scanned every later run from each
    unmatched one: 22 times. That cost is the product of the two counts, which a text twice
    as long would show only with twice the distinct lengths."""
    singles = "` " * 100_000
    text = "".join("`" * k + " " for k in range(2, 200)) + singles
    protected, table = protect_code(text)
    assert len(table) == 50_000 and restore_code(protected, table) == text
    cpu_ratio_below(protect_code, singles, text, 2)


def test_protect_code_is_linear_on_unmatched_backticks() -> None:
    """Codex round 5: an unmatched opener must not rescan the rest of the text."""
    text = "`unmatched\n\n" * 8000
    start = time.process_time()
    protected, table = protect_code(text)
    assert time.process_time() - start < 2.0  # the quadratic version took about 40 s
    assert protected == text and table == []


def test_split_moves_short_fence_instead_of_splitting() -> None:
    text = _paragraphs(3, 60) + "\n\n```\nonly line\n```\n\n" + _paragraphs(2, 60)
    parts = split_message(text, 200)
    bodies = [p.rsplit("\n\n", 1)[0] for p in parts]
    assert any(b.startswith("```\nonly line\n```") for b in bodies)
    assert all(len(p) <= 200 for p in parts)


# ------------------------------------------------------------------------------- the pipeline


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
    turn: int | None = 3,
    upload_limit: int = 10 << 20,
) -> RenderContext:
    return RenderContext(
        workspace=ws,  # type: ignore[arg-type]
        max_message_length=10_000,
        max_upload_bytes=upload_limit,
        upload=upload,
        turn_number=turn,
    )


AT13_INPUT = (
    "Inline $x^2$ and \\(a\\).\n\n$$\n\\int_0^1 f\n$$\n\n\\[ \\sum_{n} a_n \\]\n\n"
    "Prose: $100 and $200.\n\n```\nprice=$5 and $x$\n```\n"
)
AT13_OUTPUT = (
    "Inline $$x^2$$ and $$a$$.\n\n```math\n\\int_0^1 f\n```\n\n```math\n\\sum_{n} a_n\n```\n\n"
    "Prose: $100 and $200.\n\n```\nprice=$5 and $x$\n```\n"
)


async def test_render_agent_text_math_and_code() -> None:
    parts = await MarkdownRenderer().render_agent_text(AT13_INPUT, _ctx(FakeWorkspace(), None))
    assert parts == [AT13_OUTPUT]


async def test_render_agent_text_split_span_cannot_expose_a_mention() -> None:
    """Codex round 1, P1: a cut through an inline span must not free a wildcard mention."""
    text = "`" + "a" * 9700 + "\n@**all**\n" + "b" * 200 + "`"
    parts = await MarkdownRenderer().render_agent_text(text, _ctx(FakeWorkspace(), None))
    assert len(parts) == 2
    assert all(len(p) <= 9800 for p in parts)
    assert not _active(parts)


async def test_render_agent_text_adjacent_restored_span_keeps_mention_in_code() -> None:
    """Codex round 3, P1: the wrapper must not merge with a restored span's backticks."""
    parts = await MarkdownRenderer().render_agent_text("``x``@**all**", _ctx(FakeWorkspace(), None))
    assert parts == [f"``x`` `@{Z}**all**`"]


async def test_render_agent_text_parts_fit_after_neutralization() -> None:
    """Codex round 3, P2: neutralizing exposed mentions must not push a part over the limit."""
    text = "`" + "@**all** " * 1087 + "\n" + "b" * 100 + "`"
    parts = await MarkdownRenderer().render_agent_text(text, _ctx(FakeWorkspace(), None))
    assert len(parts) >= 2
    assert all(len(p) <= 9_800 for p in parts)
    assert all(p.endswith(f"({i}/{len(parts)})") for i, p in enumerate(parts, 1))
    bodies = [p.rsplit("\n\n", 1)[0] for p in parts]
    assert sum(b.count("@") for b in bodies) == 1087  # a hard cut may land inside a mention
    assert all(len(b) > 50 for b in bodies)  # no crumbs: an oversized part is retried whole
    assert not any(
        "@**all**" in b.replace("``@**all**``", "").replace("`@**all**`", "") for b in bodies
    )


async def test_render_agent_text_upload_head_fits_after_neutralization() -> None:
    text = "`" + "@**all** " * 3000 + "`"  # one span, one line, longer than a part
    ws, up = FakeWorkspace(), FakeUploader()
    parts = await MarkdownRenderer().render_agent_text(text, _ctx(ws, up))
    assert len(parts) == 1 and len(parts[0]) <= 9_800
    assert parts[0].endswith("Full answer: [answer-turn3.md](/user_uploads/1/abc/answer-turn3.md)")
    head = parts[0].rsplit("\n\n", 1)[0]
    assert "@**all**" not in head.replace("``@**all**``", "").replace("`@**all**`", "")


async def test_render_agent_text_paragraph_boundary_inside_backticks() -> None:
    """Codex round 4, P1: a whitespace-only line ends the paragraph, so the span never forms."""
    parts = await MarkdownRenderer().render_agent_text(
        "`a\n \n@**all**`", _ctx(FakeWorkspace(), None)
    )
    assert parts == [f"`a\n \n``@{Z}**all**`` `"]
    assert not _active(parts)
    assert MarkdownRenderer().render_inline("`a\n \n@**all**`") == parts[0]


async def test_render_agent_text_disarms_mentions_inside_code() -> None:
    text = "```\n@**all** @**Bob**\n```\n`@*ops*`"
    parts = await MarkdownRenderer().render_agent_text(text, _ctx(FakeWorkspace(), None))
    assert parts == [f"```\n@{Z}**all** @{Z}**Bob**\n```\n`@{Z}*ops*`"]
    assert not _active(parts)


async def test_render_agent_text_uploads_when_a_line_cannot_fit() -> None:
    text = "```\n" + "q" * 12_000 + "\n```"
    ws, up = FakeWorkspace(), FakeUploader()
    parts = await MarkdownRenderer().render_agent_text(text, _ctx(ws, up))
    assert len(parts) == 1 and len(parts[0]) <= 9_800
    assert parts[0].endswith("Full answer: [answer-turn3.md](/user_uploads/1/abc/answer-turn3.md)")
    assert ws.writes[up.uploads[0].path] == text.encode()


async def test_render_agent_text_neutralizes_inside_spoilers_and_quotes() -> None:
    """Codex round 2: spoiler and quote bodies are markdown, so mentions in them count."""
    text = "```spoiler Details\n@**all** and $x$\n```\n\n```quote\n@**Bob** `@**all**`\n```"
    parts = await MarkdownRenderer().render_agent_text(text, _ctx(FakeWorkspace(), None))
    assert parts == [
        f"```spoiler Details\n`@{Z}**all**` and $$x$$\n```\n\n"
        f"```quote\n@_**Bob** `@{Z}**all**`\n```"
    ]


async def test_render_agent_text_drops_nul_and_keeps_code() -> None:
    text = "see `a`\x00zabcode0\x00 and ```\ncode \x00zabcode1\x00\n```"
    parts = await MarkdownRenderer().render_agent_text(text, _ctx(FakeWorkspace(), None))
    assert parts == ["see `a`zabcode0 and ```\ncode zabcode1\n```"]


async def test_render_agent_text_mentions_and_secrets() -> None:
    text = "Ping @**all** and @**Bob**; key sk-ant-" + "z" * 40 + " in `@**all**`."
    parts = await MarkdownRenderer().render_agent_text(text, _ctx(FakeWorkspace(), None))
    assert parts == [
        f"Ping `@{Z}**all**` and @_**Bob**; key [redacted anthropic key] in `@{Z}**all**`."
    ]


def test_render_inline() -> None:
    r = MarkdownRenderer()
    assert r.render_inline("say $x$ to @**all** `$y$`") == f"say $$x$$ to `@{Z}**all**` `$y$`"
    assert r.render_inline("token ghp_" + "B" * 36) == "token [redacted github token]"
    assert r.render_inline("\\`@**all**`") == f"\\` ``@{Z}**all**`` `"  # codex round 1, P1
    assert r.render_inline("\\@**all**") == f"\\ `@{Z}**all**`"  # codex round 2, P1
    assert r.render_inline("``x``@**all**") == f"``x`` `@{Z}**all**`"  # codex round 3, P1
    assert r.render_inline("@**all**`y`") == f"`@{Z}**all**` `y`"


async def test_render_agent_text_two_parts() -> None:
    prose = _paragraphs(40)
    code = "```python\n" + "\n".join(f"x{i} = {i} " + "#" * 60 for i in range(120)) + "\n```"
    text = prose + "\n\n" + code + "\n\n" + _paragraphs(40)
    ws, up = FakeWorkspace(), FakeUploader()
    parts = await MarkdownRenderer().render_agent_text(text, _ctx(ws, up))
    assert len(parts) == 2
    assert parts[0].endswith("\n```\n\n(1/2)") and "```python\n" in parts[1]
    assert not up.uploads and not ws.writes


async def test_render_agent_text_overflow_uploads() -> None:
    """AT-15: about 45 000 characters -> the first part plus an uploaded .md."""
    text = _paragraphs(550)  # 550 x 80 + 549 x 2 ~ 45 000 chars
    assert len(text) > 4 * 9_800
    ws, up = FakeWorkspace(), FakeUploader()
    parts = await MarkdownRenderer().render_agent_text(text, _ctx(ws, up))
    assert len(parts) == 1
    assert len(parts[0]) <= 9_800
    assert parts[0].endswith("Full answer: [answer-turn3.md](/user_uploads/1/abc/answer-turn3.md)")
    assert "(1/5)" in parts[0].rsplit("\n\n", 1)[1]
    assert parts[0].startswith("p0 ")
    assert [u.name for u in up.uploads] == ["answer-turn3.md"]
    assert re.fullmatch(r"/repo/\.zab/outbox/answer-turn3-[0-9a-f]{8}\.md", up.uploads[0].path)
    assert ws.writes[up.uploads[0].path] == text.encode()


async def test_concurrent_overflow_renders_upload_their_own_text() -> None:
    """Codex round 7: sessions sharing a workspace and turn number must not overwrite files."""
    import asyncio

    ws = FakeWorkspace()

    class SlowUploader(FakeUploader):
        async def __call__(self, upload: Upload) -> str:
            await asyncio.sleep(0)  # let the other render write its file first
            return await super().__call__(upload)

    up = SlowUploader()
    a, b = _paragraphs(550).replace("x", "a"), _paragraphs(550).replace("x", "b")
    ctx = _ctx(ws, up)
    await asyncio.gather(
        MarkdownRenderer().render_agent_text(a, ctx), MarkdownRenderer().render_agent_text(b, ctx)
    )
    assert len(up.uploads) == 2 and len({u.path for u in up.uploads}) == 2
    assert {u.name for u in up.uploads} == {"answer-turn3.md"}
    assert {ws.writes[u.path] for u in up.uploads} == {a.encode(), b.encode()}


async def test_render_agent_text_overflow_beyond_upload_limit_posts_all_parts() -> None:
    """Codex round 4 on #23: an answer over ctx.max_upload_bytes is posted in parts (§11.4)."""
    text = _paragraphs(550)
    ws, up = FakeWorkspace(), FakeUploader()
    parts = await MarkdownRenderer().render_agent_text(text, _ctx(ws, up, upload_limit=1000))
    assert len(parts) == 5 and parts[-1].endswith("(5/5)")
    assert not up.uploads and not ws.writes
    parts = await MarkdownRenderer().render_agent_text(text, _ctx(ws, up, upload_limit=len(text)))
    assert len(parts) == 1 and up.uploads


async def test_render_agent_text_overflow_without_uploads_posts_all_parts() -> None:
    text = _paragraphs(550)
    parts = await MarkdownRenderer().render_agent_text(text, _ctx(FakeWorkspace(), None))
    assert len(parts) == 5
    assert parts[-1].endswith("(5/5)")
