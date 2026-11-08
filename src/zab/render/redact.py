"""Secret redaction (SPEC §11.6), shared by the answer, diff and status renderers."""

from __future__ import annotations

import re

#: A token's characters, which the guards below look at.
_WORD = "[A-Za-z0-9_-]"
#: A token character, except where the context of a Zulip key starts (`ZULIP_API_KEY=`, `-u `,
#: `--user `): the two Zulip rules for those have no token guard, so no tail may take one.
_CHAR = rf"(?:(?!ZULIP_API_KEY[ \t]*=|-u[ \t]|--user[ \t]){_WORD})"
#: The payload and signature of a JWT glued behind a token, whose header is then the end of
#: that token: with a header, nearly always one of a few, they are the bearer token. And every
#: dot-joined segment after them, possibly empty: where the JWTs start in such a run is not
#: known here (the run may itself be a JWT, `H.P.eyJ…`, or a key and a JWT after a dot), and
#: a segment left over would be a JWT's payload or signature (#821). Possessive, as `_TAIL`.
_GLUED_JWT = rf"(?:\.eyJ{_CHAR}++(?:\.{_CHAR}*+)++)?"
#: The rest of a token, which a match takes with it, and a JWT glued behind it (#666). Without
#: it, `AKIA<16>ghp_<36>` lost only its first 20 characters on one pass: the GitHub rule had run
#: already, and it saw a token start before `ghp_` only on a second pass, once a placeholder
#: stood in front. Every match below ends at the end of its token, on a character outside
#: `_WORD` (a quote, a line end), or before the `Z` or `-` of a Zulip key's context, which no
#: guarded token starts with or has as its second character; a private key block's, whose
#: rules run before any rule with a guard, is the exception. So a placeholder never changes
#: what a guard after it sees, which looks back at most two characters, and one pass leaves
#: nothing for a second (`tests/render/test_redact.py`).
#:
#: Its repeats are possessive (`*+`, `++`, Python 3.11): `re` keeps a frame for each pass of a
#: repeated group, which `_CHAR` is, so a greedy one held about 65 bytes per character of the
#: tail, and the nested dot segments about 210, for a backtrack that never helps: `_CHAR` is
#: never a dot, and what follows a tail is optional. A 16 MB tail took gigabytes (review of
#: #828). The other rules repeat single characters, which `re` does in constant memory.
_TAIL = _CHAR + "*+" + _GLUED_JWT

# (pattern, replacement template): the Zulip key rule keeps the line's prefix, so a diff line
# (`+key=...`, `-key=...`, ` key=...`) or a list item (`- key=...`) is still a whole line.
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"-----BEGIN (?P<kind>[A-Z ]*)PRIVATE KEY-----"
            r".*?(?:-----END (?P=kind)PRIVATE KEY-----|\Z)",
            re.DOTALL,
        ),
        "[redacted private key]",
    ),
    (
        re.compile(
            r"-----BEGIN PGP PRIVATE KEY BLOCK-----.*?(?:-----END PGP PRIVATE KEY BLOCK-----|\Z)",
            re.DOTALL,
        ),
        "[redacted pgp private key]",
    ),
    # The two Zulip rules that have no token guard run before every rule that has one, whose
    # match would otherwise take their context when it is glued behind a token (`AKIA<16>-u
    # x:<32>`, a JWT's last segment), or put a placeholder, with its blanks, in the user of
    # `-u` (#666). A JWT as their value goes whole: the rest of its header and every segment
    # dot-joined after it are their tail (#821).
    (
        # The key in an environment assignment, quoted or not.
        re.compile(
            r"(?P<prefix>ZULIP_API_KEY[ \t]*=[ \t]*)[\"']?[A-Za-z0-9]{32}" + _TAIL + "[\"']?"
        ),
        r"\g<prefix>[redacted zulip api key]",
    ),
    (
        # The key in `curl -u user:key` or `--user user:key`.
        re.compile(r"(?P<prefix>(?:-u|--user)[ \t]+[\"']?[^\s\"':]+:)[A-Za-z0-9]{32}" + _TAIL),
        r"\g<prefix>[redacted zulip api key]",
    ),
    # A JWT: three base64url segments, the first two starting with `eyJ` (`{"`). The token
    # boundary before `eyJ` keeps the engine from retrying inside one long run of token
    # characters, which would be quadratic on `eyJ...` with no dot (PR #48 round 1). Its last
    # segment takes the header of a JWT glued behind it, whose other two then show: the rule is
    # copied word for word into `workshop/admin/admin_common.py`.
    (
        re.compile(
            r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+(?![A-Za-z0-9_-])"
        ),
        "[redacted jwt]",
    ),
    # Key prefixes only at the start of a token: `sk-` also occurs inside hyphenated URL slugs
    # (`…-the-unit-disk-to-the-upper-half-plane`, `task-`, `risk-`, `mask-`), which the PM
    # review of PR #48 found redacted as OpenAI keys. A preceding `-` only counts when it is
    # itself attached to a word (`foo-sk-…`): a diff's deletion marker (`-sk-…` at the start of
    # a line) must not hide a key (codex round 2 on PR #48). The same guard keeps `highp_…`
    # and `MALAYSIA…` out of the GitHub and AWS rules.
    (
        re.compile(r"(?<![A-Za-z0-9_])(?<![A-Za-z0-9_]-)sk-ant-[A-Za-z0-9_-]{20,}" + _TAIL),
        "[redacted anthropic key]",
    ),
    (
        re.compile(r"(?<![A-Za-z0-9_])(?<![A-Za-z0-9_]-)sk-(?:proj-)?[A-Za-z0-9_-]{20,}" + _TAIL),
        "[redacted openai key]",
    ),
    (
        re.compile(
            r"(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{50})" + _TAIL
        ),
        "[redacted github token]",
    ),
    (
        re.compile(r"(?<![A-Za-z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}" + _TAIL),
        "[redacted aws access key]",
    ),
    (
        # A zuliprc line, `key = ...` included; it may end in CRLF: the `\r` is tolerated by
        # the lookahead and kept.
        re.compile(
            r"^(?P<prefix>[+\- ]?[ \t]*)key[ \t]*=[ \t]*[A-Za-z0-9]{32}[ \t]*(?=\r?$)",
            re.MULTILINE,
        ),
        r"\g<prefix>[redacted zulip api key]",
    ),
)


#: The line ends `str.splitlines` knows besides `\n`, which `^` and `$` do not. A caller that
#: takes a line out of text with one of them (a progress `\r` in stderr) has a whole zuliprc
#: line the rule above misses (review of #663). Only text that holds one gets the second pass
#: below, a `\r` counting only outside a CRLF pair, whose line end the rule above takes
#: (`\r?$`): CRLF text paid about 30% more for nothing (#666). Other text costs a few scans
#: for these characters; `SECRET_PATTERNS`, which the scrubber matches with its own rule for
#: a key anywhere, is unchanged.
_OTHER_LINE_ENDS = "\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029"
_OTHER_LINE_ENDS_BUT_CR = _OTHER_LINE_ENDS.replace("\r", "")
_ZULIP_KEY_ANY_LINE_END = re.compile(
    rf"(?:^|(?<=[{_OTHER_LINE_ENDS}]))(?P<prefix>[+\- ]?[ \t]*)"
    rf"key[ \t]*=[ \t]*[A-Za-z0-9]{{32}}[ \t]*(?=[{_OTHER_LINE_ENDS}]|$)",
    re.MULTILINE,
)


def redact_secrets(text: str) -> str:
    """Replace known secret formats with `[redacted <kind>]` (SPEC §11.6).

    The Anthropic pattern runs before the generic OpenAI one, which would otherwise match
    `sk-ant-...` as an OpenAI key. A private key block extends from its BEGIN line to the
    matching END line, or to the end of the text if that line is missing. The Zulip rule is
    a whole line `key=<32 alphanumerics>` (blanks around the `=` allowed), with one leading
    diff or list prefix character (`+`, `-` or a space) and blanks, which are kept; a line
    ends wherever `str.splitlines` would end it, a lone carriage return included; the key
    is also caught in `ZULIP_API_KEY=...` and in `curl -u user:key`. JWTs, AWS temporary keys
    (`ASIA...`) and PGP private key blocks are redacted too (issue #36, SPEC §11.6 table and
    docs/DECISIONS.md). A key prefix matches only at the start of a token, never inside a longer
    hyphenated one such as a URL slug; a diff's deletion marker in front of it does not count.
    A match runs to the end of its token, short of a Zulip key's context, and takes a JWT glued
    behind it, so one pass leaves nothing for a second (#666).
    Redaction is a safety net, not a guarantee; it applies to answers, diffs and status titles
    alike.
    """
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    if _other_line_end(text):
        text = _ZULIP_KEY_ANY_LINE_END.sub(r"\g<prefix>[redacted zulip api key]", text)
    return text


def _other_line_end(text: str) -> bool:
    r"""Whether `text` ends a line where `^` and `$` don't see it: at a `\r` that no `\n`
    follows, or at one of the other `_OTHER_LINE_ENDS`."""
    if "\r" in text and text.count("\r") != text.count("\r\n"):
        return True
    return any(end in text for end in _OTHER_LINE_ENDS_BUT_CR)


#: The (pattern, replacement) pairs, for a caller that needs where each match is rather than
#: the replaced text: the admin scrubber (`zab.admin.scrub`) finds spans in several views of
#: its input and redacts them once.
SECRET_PATTERNS = _SECRET_PATTERNS

__all__ = ["SECRET_PATTERNS", "redact_secrets"]
