"""Scrubbing of everything the admin server posts or logs (docs/ADMIN.md §2.7).

One invariant: **strip, never consume; then redact once; emit only that result.**

1. **strip:** every invisible character is deleted on its own: Unicode Cc (but newline and
   tab, which are layout), Cf (bidi controls, zero-width characters, BOM, soft hyphen), Zl, Zp,
   surrogates and the zero-width variation selectors. ESC goes too, but never the visible
   characters after it: an ANSI sequence leaves `[31m` behind, as cosmetic noise. Deleting
   only invisible characters can make a secret contiguous, never shorter.
2. **redact,** on the stripped text: every registered secret, and its base64 at each byte
   alignment (an HTTP Basic header), even inside a longer string; then the patterns: SPEC
   §11.6 (`zab.render.redact`), AWS secret keys and session tokens after their names (the
   instance role's come from IMDS and can't be registered), and a Zulip key after `key=` or
   `"api_key":` anywhere (the rule of `admin_common.redact` that §11.6 has only for a whole
   line). Matches are found on the stripped text, and also on views of it without its ANSI
   sequences, whose leftovers could split a secret or glue a letter to a token's start: one
   without 7-bit CSI (`ESC [`), and, when the text holds U+009B, one that also reads it as a
   CSI introducer in an 8-bit SGR (`U+009B 31m`). In each view, a pattern that wants a token
   boundary is also matched without it where a removal glued the token to a word, or where an
   escape sequence whose introducer alone was removed leaves the rest, at most 64 characters,
   glued to it or to the `-` before `sk-` (`U+009B 2K`, `ESC ( B`; `_anchored`). Every span
   found is redacted in the stripped text, overlapping ones as one span.
3. **mentions:** `disarm_mentions` then breaks every non-silent mention, in prose and code.

Nothing is redacted before stripping, and the raw text never reaches the output.

**Threat model.**

- **In scope:** a registered secret, or a §11.6 or field pattern, that a script prints
  contiguously, possibly next to or wrapped in control characters, ANSI or bidi sequences, or
  split by non-printable characters; in plain text or base64.
- **Out of scope:** output whose producer splices printable characters into the middle of a
  secret (the printable remnants of an escape sequence included), or re-encodes it
  (homoglyphs, URL encoding, a line wrapped inside a secret).
- **Why:** only two kinds of producer could do that. The server's own scripts never do. The
  participant VMs never hold the server's registered secrets (the provisioner key, the GitHub
  token, the bot keys), and the participants' own bot keys are theirs already.
- **Accepted limit, not claimed as covered (issue #351):** the anchored matches (`_anchored`)
  have three budgets per view and per call, of 4096 places each: places with a removal in the
  run, places inside a token already found, and escapes checked. Each match looks at most 8192
  characters ahead, and an escape's tail is checked only up to 64 characters. So thousands of
  glued token prefixes in one call can use up a budget, and a later *pattern-only* token that
  needs an anchor then goes unredacted; so does one after an escape whose tail is longer than
  64 characters. A pattern token over 8 KB that needs an anchor is redacted only in its first
  8192 characters, or not at all (a JWT). Registered secrets and their base64 are found in
  every view with no budget or window, and so are pattern tokens whose boundary the view
  shows: neither is affected. The cost is bounded per call but uneven: a U+009B before every
  token-like place makes 1 MB take about 1.4 s, against main's 0.6 s (`Scrubber.__call__`).
  Accepted (issue #351, closed): only output built on purpose uses up a budget, and the
  window and the tail limit need a token over 8 KB or an escape tail over 64 characters,
  which ordinary programs don't print. Only a VM could produce any of them, and the tokens
  a VM holds are its own participant's: a matcher with no budgets would add subtle code
  here without changing what an attacker can get.

`fence`, `span` and `Scrubber.block` keep output in code nothing inside can close, and at most
40 lines inline (§2.7 items 4 and 5). The only zab imports, `zab.render.redact` and
`zab.render.mentions`, import nothing from zab (§1.6).
"""

from __future__ import annotations

import base64
import configparser
import re
from array import array
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zab.render.mentions import disarm_mentions
from zab.render.redact import SECRET_PATTERNS

REDACTED = "[redacted secret]"
#: Shorter values are not registered: they would redact ordinary words, and the only short
#: values among the secrets are the last line of a private key, a fragment of a few bytes.
MIN_SECRET_LEN = 12
INLINE_LINES = 40
INLINE_CHARS = 4000
ATTACHMENT_BYTES = 1_000_000
#: The most one call scrubs: longer text is cut at its last newline before this many
#: characters, with a note. The scrubber's cost grows with its input, and the input is a VM's.
MAX_SCRUB_CHARS = 1_000_000
#: The most of one string value `record` keeps: an audit record names output files, it doesn't
#: hold their content.
RECORD_VALUE_CHARS = 65_536

#: Invisible code points, as of Unicode 15.0: Cc but tab and newline, Cf, Zl, Zp, surrogates,
#: and the zero-width variation selectors and combining grapheme joiner. A fixed table, so the
#: server scrubs the same way under any Python, and no process scans Unicode at import;
#: `tests/admin/test_scrub.py` checks it against the running interpreter's `unicodedata`.
_INVISIBLE_RANGES = (
    (0x0000, 0x0008), (0x000B, 0x001F), (0x007F, 0x009F), (0x00AD, 0x00AD), (0x034F, 0x034F),
    (0x0600, 0x0605), (0x061C, 0x061C), (0x06DD, 0x06DD), (0x070F, 0x070F), (0x0890, 0x0891),
    (0x08E2, 0x08E2), (0x180B, 0x180F), (0x200B, 0x200F), (0x2028, 0x202E), (0x2060, 0x2064),
    (0x2066, 0x206F), (0xD800, 0xDFFF), (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF), (0xFFF9, 0xFFFB),
    (0x110BD, 0x110BD), (0x110CD, 0x110CD), (0x13430, 0x1343F), (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A), (0xE0001, 0xE0001), (0xE0020, 0xE007F), (0xE0100, 0xE01EF),
)  # fmt: skip


def _class(points: Iterable[int]) -> re.Pattern[str]:
    """A regex matching one character among `points`, as ranges."""
    ranges: list[list[int]] = []
    for p in sorted(points):
        if ranges and p == ranges[-1][1] + 1:
            ranges[-1][1] = p
        else:
            ranges.append([p, p])
    body = "".join(
        re.escape(chr(a)) + ("-" + re.escape(chr(b)) if b > a else "") for a, b in ranges
    )
    return re.compile(f"[{body}]")


_INVISIBLE_POINTS = frozenset(c for a, b in _INVISIBLE_RANGES for c in range(a, b + 1))
_INVISIBLE = _class(_INVISIBLE_POINTS)
# The escape-free views, used only to find secrets. Every invisible character but the CSI
# introducers goes, then every ESC not followed by `[`, then each CSI (introducer, parameters,
# one final character: SGR colors and the like) and any introducer left. So only a sequence
# present in the text is removed; an ESC inserted inside one never extends it. U+009B alone
# is a CSI introducer, but also just an invisible character that a following letter might
# not belong to, so it is read both ways: the 7-bit view deletes it like any invisible
# character, and the 8-bit view, made only when the text holds one, reads it as an
# introducer, but only in an SGR's shape (`U+009B 31m`, parameters required), the one form
# 8-bit output takes in practice. So a lone U+009B before a letter never eats the letter.
_INVISIBLE_BUT_ESC = _class(c for c in _INVISIBLE_POINTS if c != 0x1B)
_INVISIBLE_BUT_CSI = _class(c for c in _INVISIBLE_POINTS if c not in (0x1B, 0x9B))
_LONE_ESC = re.compile(r"\x1b(?!\[)")
#: A JSON escape, `Scrubber.json_text` reads: a surrogate pair, any other `\uXXXX`, a short
#: escape of a control character the strip deletes, or an escaped backslash, which is matched
#: whole so that what follows it is never taken for an escape.
_JSON_ESCAPE = re.compile(
    r"\\(u[dD][89abAB][0-9a-fA-F]{2}\\u[dD][c-fC-F][0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|[bfr\\])"
)
#: The characters whose escapes stay: they would end a JSON string, or a line.
_KEPT_ESCAPED = frozenset({0x22, 0x5C, 0x0A, 0x09})
#: The escapes `json_text` keeps, for its view, in two passes with no callback: the short ones
#: first, an escaped backslash among them, so that a run of backslashes is read in pairs as
#: JSON reads it; then, every backslash left starting an escape, the long ones.
_KEPT_SHORT = re.compile(r"\\[\"\\/nt]")
_KEPT_LONG = re.compile(r"\\u00(?:22|5[cC]|0[aA9])")
_SHORT_ESCAPES = {"b": "\b", "f": "\f", "r": "\r"}


def _unescape(found: re.Match[str]) -> str:
    body = found.group(1)
    if len(body) == 1:
        return _SHORT_ESCAPES.get(body, found.group(0))  # an escaped backslash stays
    if len(body) == 11:  # a surrogate pair: one character, as the decoder reads it
        high, low = int(body[1:5], 16), int(body[7:11], 16)
        return chr(0x10000 + ((high - 0xD800) << 10) + (low - 0xDC00))
    point = int(body[1:], 16)
    return found.group(0) if point in _KEPT_ESCAPED else chr(point)


_CSI7 = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b")
_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x9b[0-9;]+m|[\x1b\x9b]")
_ZULIP_KEY_ANYWHERE = re.compile(
    r"(?i)(?P<prefix>key[\"']?[ \t]*[:=][ \t]*[\"']?)[A-Za-z0-9]{32}(?![A-Za-z0-9])"
)
# Only after a name: a bare 40-character base64 run is too common to redact on sight. The
# names are those of IMDS and STS JSON, `aws configure export-credentials`, the config files
# and botocore's `ReadOnlyCredentials(…, secret_key='…', token='…')`; a bare `token` only
# where a word starts, a guard in front, like the §11.6 patterns' (see `_anchored`).
_AWS_VALUE = r"[\"']?[ \t]*[:=][ \t]*[\"']?)[A-Za-z0-9/+=]{16,}"
_AWS_SECRET = re.compile(
    r"(?P<prefix>(?i:aws_secret_access_key|secretaccesskey|secret_key|sessiontoken)" + _AWS_VALUE
)
_AWS_TOKEN = re.compile(r"(?<![A-Za-z0-9])(?P<prefix>(?i:token)" + _AWS_VALUE)
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    *((p, t.replace(r"\g<prefix>", "")) for p, t in SECRET_PATTERNS),
    (_AWS_SECRET, "[redacted aws secret]"),
    (_AWS_TOKEN, "[redacted aws secret]"),
    (_ZULIP_KEY_ANYWHERE, "[redacted zulip api key]"),
)
# Boundary recovery (codex rounds 8 and 9). A pattern with a leading guard (`ghp_`, `sk-`,
# `AKIA`, `eyJ`, `token`: no word character just before) misses a token that a removed
# character glued to a word (`x<NUL>ghp_…`: `xghp_…` once stripped). Each such pattern is
# therefore also matched without its guard, anchored on the unsplit view at each place where
# one could start and a removal fell into the run of word characters before it. Nothing is
# inserted, so a removal inside a token, or a prefix inside a token, splits nothing.
_LEADING_GUARDS = re.compile(r"^(?:\(\?<![^)]*\))+")
_RELAXED: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(_LEADING_GUARDS.sub("", p.pattern), p.flags), marker)
    for p, marker in _PATTERNS
    if _LEADING_GUARDS.match(p.pattern)
)
# The same at a token's end (codex round 10): a pattern whose trailing guard wants no word
# character after it (a Zulip key: exactly 32) misses `key=<32><NUL>suffix`, glued once
# stripped. It is also matched without that guard, and kept only where a removal fell right
# at its end.
_TRAILING_GUARD = re.compile(r"\(\?![^)]*\)$")
_TRAIL_RELAXED: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(_TRAILING_GUARD.sub("", p.pattern), p.flags), marker)
    for p, marker in _PATTERNS
    if _TRAILING_GUARD.search(p.pattern)
)
#: Where a guarded pattern can start: every one of them, which a test checks.
_GUARDED_START = re.compile(r"(?=eyJ|sk-|gh[pousr]_|github_pat_|AKIA|ASIA|(?i:token))")
_GLUED_RUN = re.compile(r"[A-Za-z0-9_-]+")
# What an escape sequence leaves in a view when only its introducer was removed (issue #345):
# - after U+009B, a CSI's parameters, intermediates and final byte (`107;254u`, `1 q`);
# - after ESC, its intermediates and final byte only (`( B`, `M`). An ESC sequence has no
#   parameters: an Fp final such as `7` or `=` ends it (ECMA-48), and the next character is
#   text (`tput sc`, `smkx`).
_INTRODUCER = re.compile(r"[\x1b\x9b]")
_CSI_TAIL = re.compile(r"[0-?]*[ -/]*[@-~]")
_ESC_TAIL = re.compile(r"[ -/]*[0-~]")
#: A longer tail is not checked: an accepted limit (issue #351). It also bounds each check's
#: cost.
_ESCAPE_TAIL_MAX = 64
_INVISIBLE_RUN = re.compile(_INVISIBLE.pattern + "*")
#: A cost bound: per view, at most this many places with a removal in the run, as many
#: escapes checked, and as many places inside a token already found (`_anchored`), each place
#: matched within this many characters. Output with thousands of them, or a token over 8 KB,
#: reaches it: an accepted limit, set out in the threat model above (issue #351).
_MAX_ANCHORS = 4096
_ANCHOR_WINDOW = 8192
_URLSAFE = str.maketrans("+/", "-_")
#: A line that reads as a `key` option in any form configparser takes: `=` or `:`, any spacing
#: (Unicode's too, as `str.strip` has it), the name in any case (`_zuliprc_keys`). The value
#: runs to the line's end, its trailing spaces stripped after: greedy, so a match is linear.
_KEY_LINE = re.compile(r"(?im)^[^\S\n]*key[^\S\n]*[=:][^\S\n]*(\S.*)$")
#: Put before a zuliprc's text when it is parsed, so that options before any section header
#: are read as a section's (as Python 3.13's `allow_unnamed_section` reads them), not refused.
_PREAMBLE = "[zab scrub: before any section]\n"
#: The longest zuliprc text, in characters, that configparser reads; a real one has a few
#: hundred. Before 3.13, configparser is quadratic in a line's length (its option pattern, on a
#: long run of spaces with no `=` or `:`) and in its count of unreadable lines; the server runs
#: Ubuntu's 3.12. Its worst case measured here, on 3.11 and 3.12: 0.07 s at this bound, 0.7 s
#: at 16 KiB, 11 to 18 s at 64 KiB (the review of #530). A longer file is read line by line
#: alone, which loses only a value continued on indented lines.
_PARSE_MAX = 4096
#: An inline comment, as configparser reads one where it is told to: `#` or `;` after a space.
#: The loaders aren't, so a value keeps it, and the key is what comes before (`_zuliprc_keys`).
_INLINE_COMMENT = re.compile(r"\s[#;]")
_ARMOR = re.compile(r"^-----(?:BEGIN|END) [A-Z0-9 ]+-----$")
_BACKTICK_RUN = re.compile(r"`+")

# A view's character -> its index in the raw text (8 bytes each, not a list's ~36);
# None: the same text.
Index = array | None
Span = tuple[int, int, str]  # [start, end) in the raw text, and the marker that replaces it


def strip(text: str) -> str:
    """Step 1: every invisible character deleted on its own; nothing visible goes."""
    return _INVISIBLE.sub("", text)


def _bounded(text: str, limit: int) -> str:
    """`text`, or its lines up to the last newline within `limit` characters, and a note.

    A cut between lines splits no secret in scope: none holds a newline, every bounded pattern
    is on one line, and a cut private key block still matches through its `\\Z` alternative.
    """
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit) + 1
    return f"{text[:cut]}[output truncated: {len(text) - cut} more characters not scrubbed]\n"


def _without(text: str, *patterns: re.Pattern[str]) -> tuple[str, Index]:
    """`text` without each pattern's matches in turn, and each kept character's index in
    `text`."""
    index: Index = None
    for pattern in patterns:
        parts: list[str] = []
        inner = array("q")
        pos = 0
        for m in pattern.finditer(text):
            parts.append(text[pos : m.start()])
            inner.extend(range(pos, m.start()))
            pos = m.end()
        if pos:
            parts.append(text[pos:])
            inner.extend(range(pos, len(text)))
            text = "".join(parts)
            index = inner if index is None else array("q", map(index.__getitem__, inner))
    return text, index


def _anchored(text: str, index: Index, raw: str, introducers: array) -> list[Span]:
    """Spans of the guarded patterns, matched without their guard at each place `p` of a view
    where one could start (`_GUARDED_START`) and a removal could have made the boundary there:
    - a removal fell into the run of word characters before `p`: just before it, or earlier
      in the run (`word<U+009B>31mghp_…` in the 7-bit view);
    - or an escape sequence ends right before `p` whose introducer alone was removed
      (`_after_escape`): `x U+009B 2Kghp_…` shows `x 2Kghp_…`. Before `sk-`, it may end
      before a diff's deletion marker instead (`x U+009B 2K-sk-…`), which §11.6 counts.
    With nothing removed, or nothing glued before `p`, the guarded patterns themselves decide.
    Spans are in the view's coordinates.

    A run with no removal in `[start, p)` holds exactly `p - start` input characters there.
    """
    if index is None:
        return []
    runs = array("q", (m.start() for m in _GLUED_RUN.finditer(text)))
    found: list[Span] = []
    # Three kinds of place, each with its own budget, so that none can use up another's
    # (codex rounds 1 and 3):
    # - a removal in the run before `p`, outside the tokens found from such places (`done`):
    #   `runs_left`, exactly as on main;
    # - the same inside one of those tokens, which main skipped (`inside_left`): its own token
    #   can run past that one's end (`AKIA<12><NUL>ASIA<16>`, codex round 2);
    # - an escape's tail right before `p`, inside a token found or not (`tails_left`).
    # Only the first kind moves `done`; `_merge` joins overlapping spans.
    runs_left = inside_left = _MAX_ANCHORS
    tails_left = _MAX_ANCHORS if introducers else 0
    done = 0
    # `introducers[:k]` lie before the place's last glued character, `p - 1`; places only move
    # forward. A tail from the last of them reaches `p` unless at least `_ESCAPE_TAIL_MAX`
    # characters of the view lie between that introducer and `p`: then `beyond`.
    n, cap = len(introducers), _ESCAPE_TAIL_MAX
    k = pos = 0
    while (m := _GUARDED_START.search(text, pos)) is not None:
        p = m.start()
        pos = p + 1
        if not p:
            continue  # nothing can be glued before the first character
        inside = p < done
        if not (runs_left or tails_left or (inside and inside_left)):
            break  # `done` can't move any more, so no later place can be checked
        beyond = None  # not yet known for `p`
        # Where a tail before `p` would end: a final byte is never `-`, so after one, the tail
        # can only end before it (`-sk-`), and its 64 characters are counted from there.
        q = p - 1 if text[p - 1] == "-" else p
        if tails_left and not (runs_left or (inside and inside_left)):
            # Only a tail can be checked here. If none reaches `p`, go on from the view's first
            # character after the next introducer, or stop if there is none.
            while k < n and introducers[k] < index[p - 1]:
                k += 1
            beyond = not k or (q > cap and index[q - cap - 1] > introducers[k - 1])
            if not beyond:
                # The last introducer's tail has one end `e`, since its classes are disjoint:
                # only `e`, or `e + 1` after a `-`, can be a tail place (the re-review).
                j = introducers[k - 1]
                r = bisect_right(index, j, max(0, q - cap), p)  # within reach, so in there
                t = (_ESC_TAIL if raw[j] == "\x1b" else _CSI_TAIL).match(text, r, r + cap)
                e = t.end() if t else -2
                if p < e:
                    pos = e
                    continue
                beyond = p > e + 1
            if beyond:
                if k == n:
                    break
                pos = max(pos, bisect_right(index, introducers[k]))
                continue
        start = runs[bisect_right(runs, p) - 1]  # a guarded token starts with a word character
        if start == p:
            continue
        glued = index[p] - index[start] == p - start  # nothing removed in the run before `p`
        if glued:  # then only an escape's tail can end right before `p`
            if not tails_left:
                continue
            if beyond is None:
                while k < n and introducers[k] < index[p - 1]:
                    k += 1
                beyond = not k or (q > cap and index[q - cap - 1] > introducers[k - 1])
            if beyond:
                if k == n:
                    tails_left = 0  # no introducer is left ahead: no later tail reaches a place
                continue
            tails_left -= 1
            j = introducers[k - 1]
            dash = text[p - 1] == "-" and text.startswith("sk-", p)  # `-sk-` is §11.6's key
            if not (
                _after_escape(text, index, raw, j, p)
                or (dash and _after_escape(text, index, raw, j, p - 1))
            ):
                continue
        elif inside:
            if not inside_left:
                continue
            inside_left -= 1
        else:
            if not runs_left:
                continue
            runs_left -= 1
        for pattern, marker in _RELAXED:
            r = pattern.match(text, p, p + _ANCHOR_WINDOW)
            if r:
                found.append(
                    (r.end("prefix") if "prefix" in r.re.groupindex else p, r.end(), marker)
                )
                if not (glued or inside):
                    done = max(done, r.end())
    return found


def _after_escape(text: str, index: array, raw: str, j: int, p: int) -> bool:
    """Whether the view `text` shows, right before `p`, the rest of an escape sequence whose
    introducer, at `j` in `raw`, was removed alone: nothing visible follows it up to the view's
    next character, at `r`, and from `r` to `p` the view holds that introducer's tail
    (`_CSI_TAIL` after U+009B, `_ESC_TAIL` after ESC). In the input, the sequence ends there.

    `j` is the last introducer before the tail: it cut short any earlier sequence, and as a
    suffix of a tail is a tail, no earlier start matches where the last one doesn't. A removal
    that is a whole sequence (the 7-bit view's `ESC [ 1 m`) leaves nothing behind, so it never
    counts: `<ESC>[1mflask-…` is still one word.
    """
    r = bisect_right(index, j)
    tail = _ESC_TAIL if raw[j] == "\x1b" else _CSI_TAIL
    return (
        tail.fullmatch(text, r, p) is not None
        and _INVISIBLE_RUN.fullmatch(raw, j + 1, index[r]) is not None
    )


def _trailing(text: str, index: Index) -> list[Span]:
    """Spans of the patterns with a trailing guard, matched without it, where a removal fell
    right at the match's end: the boundary the input had there. In the view's coordinates."""
    if index is None:
        return []
    found: list[Span] = []
    for pattern, marker in _TRAIL_RELAXED:
        for r in pattern.finditer(text):
            e = r.end()
            if 0 < e < len(text) and index[e] - index[e - 1] > 1:
                found.append(
                    (r.end("prefix") if "prefix" in r.re.groupindex else r.start(), e, marker)
                )
    return found


def base64_forms(secret: str) -> set[str]:
    """The characters of base64 that encode `secret` alone, at each of the 3 byte alignments,
    in the standard and URL-safe alphabets. Each occurs, padded or not, in the base64 of any
    bytes that hold `secret` at that alignment (`git`'s `basic base64(x-access-token:…)`)."""
    raw = secret.encode("utf-8")
    forms: set[str] = set()
    for shift in range(3):
        enc = base64.b64encode(bytes(shift) + raw).decode("ascii")
        core = enc[-(-8 * shift // 6) : 8 * (shift + len(raw)) // 6]
        forms |= {core, core.translate(_URLSAFE)}
    return forms


def read_secrets(*, zuliprcs: Iterable[Path] = (), files: Iterable[Path] = ()) -> set[str]:
    """The secrets this process can read (§2.7 step 1).

    `zuliprcs` contribute their `key` values, in every form (`_zuliprc_keys`); each of `files`
    (a token, a private key) contributes every non-blank line except a key's BEGIN and END
    armor. Invisible characters (a BOM, a stray CR) are stripped first, as they are from the
    text a secret is sought in. A file that is missing or unreadable is skipped: a secret the
    process can't read is one it can't leak, and the registry stays per process (§1.2).
    """
    found: set[str] = set()
    for path in zuliprcs:
        found.update(_zuliprc_keys(_read(path)))
    for path in files:
        lines = (line.strip() for line in _read(path).splitlines())
        found.update(line for line in lines if line and not _ARMOR.match(line))
    return found


def _zuliprc_keys(text: str) -> set[str]:
    """The `key` values in a zuliprc's text, read two ways, both kept (#526):

    - **Line by line** (`_KEY_LINE`), always: each line that reads as a `key` option in a form
      configparser takes. Linear, and alone a superset of main's `key=` rule. It also keeps a
      duplicate's first value, which configparser replaces, and an indented `key` line, which
      it reads as the line before's continuation.
    - **As the loaders read it** (`ZulipClient.from_zuliprc`, `admin_common.load_zuliprc`),
      when the text is at most `_PARSE_MAX` characters: configparser with no interpolation,
      which adds a value continued on indented lines; from every section and `[DEFAULT]`, not
      `[api]` alone. Laxer only where they would refuse the file, since its keys are secrets
      all the same: a duplicate is allowed (`strict=False`, which changes nothing in a file
      they accept), options before the first section header are read (`_PREAMBLE`), and a line
      the parser can't read is skipped. Configparser reads on past such a line and raises
      `ParsingError` at the end, every value read and joined.

    Each value counts whole; a continued one also by line and with its lines joined, so that
    pieces shorter than `MIN_SECRET_LEN` still make the key. A value with an inline comment
    (`_INLINE_COMMENT`) also counts without it, and as its first word. Each of those counts
    also without the quotes around it, which configparser keeps. `Scrubber.set_secrets` then
    drops the short ones. A malformed file raises nothing.
    """
    values = [m.group(1) for m in _KEY_LINE.finditer(text)]
    if len(text) <= _PARSE_MAX:  # beyond it, configparser's cost: see `_PARSE_MAX`
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            parser.read_string(_PREAMBLE + text)
        except configparser.Error:
            pass  # a ParsingError, raised once the whole file is read: what it read is kept
        for name in (parser.default_section, *parser.sections()):
            value = parser[name].get("key")
            if isinstance(value, str):
                values.append(value)
    parts: set[str] = set()
    for value in values:
        lines = [line.strip() for line in value.splitlines()]
        parts |= {value.strip(), "".join(lines), *lines}
    for part in list(parts):
        if comment := _INLINE_COMMENT.search(part):  # never at 0: `part` is stripped
            head = part[: comment.start()].strip()
            parts |= {head, head.split()[0]}
    return {f for p in parts for f in (p, p.strip("\"'").strip())} - {""}


def _read(path: Path) -> str:
    try:
        return strip(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return ""


def fence(text: str, info: str = "text") -> str:
    """`text` in a code fence one backtick longer than its longest backtick run (at least 3).

    Zulip ends a fenced block only at a line equal to its opening fence, so no line of `text`
    can end it. `text` is already scrubbed; `info` is the bot's own constant.
    """
    tick = "`" * max(3, _longest_run(text) + 1)
    body = text if not text or text.endswith("\n") else text + "\n"
    return f"{tick}{info}\n{body}{tick}"


def span(text: str) -> str:
    """Scrubbed `text` as one line in an inline code span no backtick run inside can close."""
    text = " ".join(text.split("\n"))
    tick = "`" * (_longest_run(text) + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{tick}{pad}{text}{pad}{tick}"


def _longest_run(text: str) -> int:
    return max((len(m.group()) for m in _BACKTICK_RUN.finditer(text)), default=0)


def _merge(spans: list[Span]) -> list[Span]:
    """Sorted, with overlapping or adjacent spans as one, which keeps the first marker."""
    merged: list[Span] = []
    for a, b, marker in sorted(spans, key=lambda s: (s[0], -s[1])):
        if merged and a <= merged[-1][1]:
            start, end, first = merged[-1]
            merged[-1] = (start, max(end, b), first)
        else:
            merged.append((a, b, marker))
    return merged


def _clip(spans: list[Span], lo: int, hi: int) -> list[tuple[int, int]]:
    return [(max(a, lo) - lo, min(b, hi) - lo) for a, b, _ in spans if a < hi and b > lo]


@dataclass(frozen=True, slots=True)
class Block:
    """Scrubbed output for a post: `inline` is fenced; `full` is the attachment's text."""

    inline: str
    full: str
    truncated: bool  # `inline` leaves something out: attach `full`


class Scrubber:
    """The per-process scrubber: a registry of exact secrets and the pipeline above.

    The repr shows only how many secrets are registered, never one of them.
    """

    __slots__ = ("_count", "_needles")

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._count = 0
        self._needles: tuple[str, ...] = ()
        self.set_secrets(secrets)

    def set_secrets(self, secrets: Iterable[str]) -> None:
        """Replace the registry, as at start and on each change of `out/` (§2.7)."""
        values = {v for v in (strip(s).strip() for s in secrets) if len(v) >= MIN_SECRET_LEN}
        needles = values.union(*(base64_forms(v) for v in values))
        self._count = len(values)
        self._needles = tuple(sorted(n for n in needles if len(n) >= MIN_SECRET_LEN))

    def __len__(self) -> int:
        return self._count

    def __repr__(self) -> str:
        return f"Scrubber({self._count} secrets)"

    def __call__(self, text: str) -> str:
        """Strip, redact once on the stripped text, disarm mentions. Idempotent.

        At most `MAX_SCRUB_CHARS` of `text` are scrubbed; the rest is cut at a line, with a
        note (`_bounded`), so a VM's output can't make one call take gigabytes, nor more than
        about 1.4 s on a fast x86 core. That is the slowest input measured: a U+009B before
        every token-like place, once glued prefixes have spent the run budgets (ADMIN.md §2.7,
        an accepted limit; issue #351).
        """
        return self._redacted(*self._scan(_bounded(text, MAX_SCRUB_CHARS)))

    @staticmethod
    def _redacted(shown: str, index: Index, spans: list[Span]) -> str:
        """The stripped text with each span (of the raw text) redacted, mentions disarmed."""
        out: list[str] = []
        pos = 0
        for start, end, marker in spans:
            a, b = (
                (start, end)
                if index is None
                else (bisect_left(index, start), bisect_left(index, end))
            )
            out += [shown[pos:a], marker]
            pos = b
        out.append(shown[pos:])
        return disarm_mentions("".join(out))

    def _scan(self, raw: str) -> tuple[str, Index, list[Span]]:
        """The stripped text, its index into `raw`, and the spans of `raw` to redact.

        Spans are found in the stripped text and in its escape-free views (7-bit, and 8-bit
        when `raw` holds U+009B), each also by `_anchored`. Every view maps back to `raw`, so
        a span found in any covers everything between its first and last character. Finding
        in more views can only redact more; the output is still the stripped text.
        """
        shown, index = _without(raw, _INVISIBLE)
        views = [(shown, index)]
        if "\x1b" in raw:
            views.append(_without(raw, _INVISIBLE_BUT_ESC, _LONE_ESC, _CSI7))
        if "\x9b" in raw:
            views.append(_without(raw, _INVISIBLE_BUT_CSI, _LONE_ESC, _CSI))
        intro = array("q")  # where `raw` holds ESC or U+009B, for `_after_escape`
        if len(views) > 1:
            intro.extend(m.start() for m in _INTRODUCER.finditer(raw))
        found: list[Span] = []
        for text, idx in views:
            for a, b, marker in (
                self._detect(text) + _anchored(text, idx, raw, intro) + _trailing(text, idx)
            ):
                found.append((a, b, marker) if idx is None else (idx[a], idx[b - 1] + 1, marker))
        return shown, index, _merge(found)

    def _detect(self, text: str) -> list[Span]:
        """Every registered secret (or base64 form) and every pattern match in `text`."""
        found: list[Span] = []
        for needle in self._needles:
            i = text.find(needle)
            while i >= 0:
                found.append((i, i + len(needle), REDACTED))
                i = text.find(needle, i + 1)
        for pattern, marker in _PATTERNS:
            named = "prefix" in pattern.groupindex
            for m in pattern.finditer(text):
                start = m.end("prefix") if named else m.start()
                if m.end() > start:
                    found.append((start, m.end(), marker))
        return found

    def record(self, value: Any) -> Any:
        """A JSON value, such as an audit record, scrubbed throughout, keys included.

        Apart, a key and its value escape the patterns that need both (`"SecretAccessKey":
        "…"`, `"api_key": "…"`), so a string value that is redacted differently beside its
        key (`"key": "value"`, raw, through the same strip and views) than alone is replaced
        whole. Mappings nest; a slug as a key changes nothing, and a secret in a key leaves its
        value alone. A string longer than `RECORD_VALUE_CHARS` is cut at a line first, with a
        note.
        """
        if isinstance(value, str):
            return self(_bounded(value, RECORD_VALUE_CHARS))
        if isinstance(value, Mapping):
            return {self.record(k): self._value(k, v) for k, v in value.items()}
        if isinstance(value, list | tuple):
            return [self.record(v) for v in value]
        return value

    def json_text(self, text: str) -> str:
        """Script output that holds JSON (`status.py --json`), scrubbed with its escapes undone.

        A JSON escape hides a character from item 0's strip: `json.dumps` writes a VM's U+200B
        as `\\u200b`, so a token it splits is two printable runs in the escaped text (#361; the
        review of #380). So every escape becomes its character (`\\u…`, a surrogate pair as
        one, `\\b`, `\\f`, `\\r`), except those of `"`, `\\`, newline and tab, and the text is
        then scrubbed as text. JSON stays JSON: the escapes kept are those whose characters
        would end a string or a line, and the strip deletes every control character the pass
        made raw. Nothing is decoded, so it holds wherever the JSON lies, partial or
        malformed; an escaped backslash (`\\\\`) is never read as the start of an escape.

        A kept escape ends in a word character (`\\t`, `\\u0009`): a pattern that wants a
        boundary before its token (a JWT, `ghp_`, `token=`) would miss one right after it,
        which the decoded string shows after a tab (the review of #380). So spans are also
        found on a view of the same length in which each kept escape reads as spaces, and the
        union of both is redacted in the text.

        Bounds (§2.7 item 5): `MAX_SCRUB_CHARS` cuts the text first; one linear pass that
        only shortens it; then one scan of it, and a second of the view when it differs."""
        raw = _JSON_ESCAPE.sub(_unescape, _bounded(text, MAX_SCRUB_CHARS))
        shown, index, spans = self._scan(raw)
        view = _KEPT_LONG.sub(" " * 6, _KEPT_SHORT.sub("  ", raw))
        if view != raw:  # the same characters but the spaces: the same index into `raw`
            spans = _merge(spans + self._scan(view)[2])
        return self._redacted(shown, index, spans)

    def _value(self, key: Any, value: Any) -> Any:
        if not (isinstance(key, str) and isinstance(value, str)):
            return self.record(value)
        key, value = _bounded(key, RECORD_VALUE_CHARS), _bounded(value, RECORD_VALUE_CHARS)
        head = f'"{key}": "'
        lo, hi = len(head), len(head) + len(value)
        beside = _clip(self._scan(f'{head}{value}"')[2], lo, hi)
        if beside != _clip(self._scan(value)[2], 0, len(value)):
            return REDACTED  # its name is what marks it as a secret
        return self(value)

    def block(
        self, text: str, max_lines: int = INLINE_LINES, max_chars: int = INLINE_CHARS
    ) -> Block:
        """Script or VM output for a post: scrubbed first, then cut, never the other way.

        `inline` is at most `max_lines` lines, and at most `max_chars` characters with its
        fences, which grow with the longest backtick run. `full` is the scrubbed text, at most
        `ATTACHMENT_BYTES` of UTF-8, a closing note included. Input beyond `MAX_SCRUB_CHARS`
        is neither scrubbed nor shown: a note says how much was left out (the runner keeps
        any fuller copy, scrubbed in line-aligned pieces of at most that size).
        """
        text = self(text)
        lines = text.removesuffix("\n").split("\n")
        shown = "\n".join(lines[:max_lines])
        truncated = len(lines) > max_lines
        # Both fences, the info string and two newlines; a cut never lengthens the fence.
        budget = max_chars - 2 * max(3, _longest_run(shown) + 1) - len("text") - 2
        if len(shown) > budget:
            shown, truncated = shown[: max(0, budget)], True
        raw = text.encode("utf-8")
        if len(raw) > ATTACHMENT_BYTES:
            note = f"\n[cut at {ATTACHMENT_BYTES} bytes]\n"
            text = raw[: ATTACHMENT_BYTES - len(note)].decode("utf-8", errors="ignore") + note
        return Block(inline=fence(shown), full=text, truncated=truncated)
