"""Secret redaction (SPEC §11.6), shared by the answer, diff and status renderers."""

from __future__ import annotations

import re

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
    # A JWT: three base64url segments, the first two starting with `eyJ` (`{"`). The token
    # boundary before `eyJ` keeps the engine from retrying inside one long run of token
    # characters, which would be quadratic on `eyJ...` with no dot (PR #48 round 1).
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
        re.compile(r"(?<![A-Za-z0-9_])(?<![A-Za-z0-9_]-)sk-ant-[A-Za-z0-9_-]{20,}"),
        "[redacted anthropic key]",
    ),
    (
        re.compile(r"(?<![A-Za-z0-9_])(?<![A-Za-z0-9_]-)sk-(?:proj-)?[A-Za-z0-9_-]{20,}"),
        "[redacted openai key]",
    ),
    (
        re.compile(r"(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{50,})"),
        "[redacted github token]",
    ),
    (re.compile(r"(?<![A-Za-z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}"), "[redacted aws access key]"),
    (
        # A zuliprc line, `key = ...` included; it may end in CRLF: the `\r` is tolerated by
        # the lookahead and kept.
        re.compile(
            r"^(?P<prefix>[+\- ]?[ \t]*)key[ \t]*=[ \t]*[A-Za-z0-9]{32}[ \t]*(?=\r?$)",
            re.MULTILINE,
        ),
        r"\g<prefix>[redacted zulip api key]",
    ),
    (
        # The key in an environment assignment, quoted or not.
        re.compile(r"(?P<prefix>ZULIP_API_KEY[ \t]*=[ \t]*)[\"']?[A-Za-z0-9]{32}[\"']?"),
        r"\g<prefix>[redacted zulip api key]",
    ),
    (
        # The key in `curl -u user:key` or `--user user:key`.
        re.compile(r"(?P<prefix>(?:-u|--user)[ \t]+[\"']?[^\s\"':]+:)[A-Za-z0-9]{32}"),
        r"\g<prefix>[redacted zulip api key]",
    ),
)


#: The line ends `str.splitlines` knows besides `\n`, which `^` and `$` do not. A caller that
#: takes a line out of text with one of them (a progress `\r` in stderr) has a whole zuliprc
#: line the rule above misses (review of #663). Only text that holds one gets the second pass
#: below, so other text costs one scan for these characters; `SECRET_PATTERNS`, which the
#: scrubber matches with its own rule for a key anywhere, is unchanged.
_OTHER_LINE_ENDS = "\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029"
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
    Redaction is a safety net, not a guarantee; it applies to answers, diffs and status titles
    alike.
    """
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    if any(end in text for end in _OTHER_LINE_ENDS):
        text = _ZULIP_KEY_ANY_LINE_END.sub(r"\g<prefix>[redacted zulip api key]", text)
    return text


#: The (pattern, replacement) pairs, for a caller that needs where each match is rather than
#: the replaced text: the admin scrubber (`zab.admin.scrub`) finds spans in several views of
#: its input and redacts them once.
SECRET_PATTERNS = _SECRET_PATTERNS

__all__ = ["SECRET_PATTERNS", "redact_secrets"]
