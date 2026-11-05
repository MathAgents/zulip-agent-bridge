"""How a channel message addresses bots (SPEC §4.5): the bridge's router reads its mentions so,
and so does the waker, for the bots it acknowledges a message from (#683, #713). Standard library
and `zab.render.code` only: the waker runs on the admin server."""

from __future__ import annotations

import re
from collections.abc import Iterable

from zab.render.code import blank_quotes, protect_code

#: The agents' kinds, which a typed mention names: `@claude`, `@codex` (#713).
KINDS = ("claude", "codex")
#: A link, where an `@` is part of the address, not a mention (`https://medium.com/@claude/…`,
#: `github.com/@codex`): a word with a scheme, or with a domain name and a path. Where its
#: address starts, found once per word (`_before_link`): a scheme's `://`, or a top-level
#: domain's dot before a path, each read back over the name before it.
_LINK_MARK = re.compile(r"://|\.[a-z]{2,}/", re.IGNORECASE)
_WORD = re.compile(r"\S+")


def mention(name: str, user_id: int | None = None) -> re.Pattern[str]:
    """A mention of `name` as Zulip writes it, `@**name**` or `@**name|ID**`: with its own ID
    `user_id`, or with any ID when it is None (another bot's)."""
    suffix = r"\d+" if user_id is None else str(user_id)
    return re.compile(r"@\*\*" + re.escape(name) + rf"(?:\|{suffix})?\*\*")


def typed(kind: str) -> re.Pattern[str]:
    """A mention of the sender's bot of `kind` typed out, `@claude` or `@codex`, without Zulip's
    typeahead, in any case (#713). The `@` follows the text's start or a character that isn't a
    letter, digit, `_`, `.`, `@` or `*`; the name, the text's end or a character that isn't a
    letter, digit or `_`. So `@claude,` and `@claude's` count, `@claudette` and `x@claude.ai`
    don't, nor does a silent mention (`@_**…**`) or one in a link (`typed_kinds`). Search it in
    `visible_text`."""
    return re.compile(r"(?<![\w.@*])@(?i:" + re.escape(kind) + r")(?!\w)")


def typed_address(kind: str) -> re.Pattern[str]:
    """`typed`, as `unaddressed` takes it off a message's start: followed by whitespace, `,`,
    `:` or the end, so that `@claude's idea` keeps its words."""
    return re.compile(typed(kind).pattern + r"(?=[\s,:]|\Z)")


def _before_link(word: str) -> str:
    """`word` up to the link in it, if any (`_LINK_MARK`): in time linear in its length."""
    mark = _LINK_MARK.search(word)
    if mark is None:
        return word
    start = mark.start()
    while start and (word[start - 1].isalnum() or word[start - 1] in "_.+-"):
        start -= 1
    return word[:start]


def typed_kinds(visible: str) -> set[str]:
    """The kinds that a typed mention in `visible` (`visible_text`) names, outside links: each
    word with an `@` read once, up to the link in it (Codex round 2 on #723: a search for links
    from every word boundary took quadratic time on dotted text)."""
    kinds: set[str] = set()
    for word in _WORD.finditer(visible):
        if "@" in word[0]:
            head = _before_link(word[0])
            kinds.update(kind for kind in KINDS if typed(kind).search(head))
    return kinds


def visible_text(text: str) -> str:
    """Where a mention addresses a bot: outside code, and outside quotes, whose mentions Zulip
    silences (`blank_quotes`, #713). Escaped Markdown is not an address."""
    visible, _ = protect_code(blank_quotes(text))
    return re.sub(r"\\.", " ", visible)


def without_mention(text: str, pattern: re.Pattern[str]) -> str:
    """`text` without `pattern`'s matches (the bot's own mentions, before a prompt goes to its
    agent), except in quotes (`blank_quotes`), which Zulip shows as someone's earlier words and
    which are no address: `> @**Codex · X** said 42` arrives as it was (#713)."""
    shown = blank_quotes(text).split("\n")
    return "\n".join(
        pattern.sub("", line) if line == shown[i] else line
        for i, line in enumerate(text.split("\n"))
    )


def unaddressed(text: str, mentions: Iterable[re.Pattern[str]]) -> tuple[str, list[str]]:
    """`text` without its complete leading address list (`mentions`), so that co-addressed
    commands stay commands and asides (`//`) stay asides; and the addresses it took off."""
    normalized, removed = text.lstrip(), []
    patterns = [p.pattern for p in mentions]
    if not patterns:
        return normalized, removed
    leading = re.compile("(?:" + "|".join(patterns) + r")(?:\s|,|:(?![\w+-]+:))*")
    while match := leading.match(normalized):
        removed.append(match[0])
        normalized = normalized[match.end() :].lstrip()
    return normalized, removed


#: Each kind's typed mention as a leading address, for `unaddressed`.
TYPED_ADDRESSES = tuple(typed_address(kind) for kind in KINDS)


def without_typed_address(text: str, kind: str) -> str:
    """A direct message's `text` without a leading typed mention of the bot's own `kind`, so
    that `@claude !login` to Claude's bot is `!login` (#713); `text` as it is when it starts with
    none. The bridge's DM commands, its sign-in and `zab sentinel` read a DM through it."""
    if kind not in KINDS:
        return text
    rest, removed = unaddressed(text, [typed_address(kind)])
    return rest if removed else text
