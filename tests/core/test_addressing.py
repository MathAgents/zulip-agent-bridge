"""Typed mentions and quotes, as the router and the waker read a message's addressees (#713,
SPEC §4.5 "Mention detection")."""

import pytest

from zab.core.addressing import (
    TYPED_ADDRESSES,
    mention,
    typed_kinds,
    unaddressed,
    visible_text,
    without_mention,
    without_typed_address,
)
from zab.render.code import blank_quotes


def kinds(text: str) -> set[str]:
    return typed_kinds(visible_text(text))


@pytest.mark.parametrize(
    ("name", "kind"),
    [("@claude", "claude"), ("@Claude", "claude"), ("@cLaUdE", "claude"), ("@CODEX", "codex"),
     ("@codex", "codex"), ("@Codex", "codex")],
)  # fmt: skip
@pytest.mark.parametrize(
    "template",
    [
        "{} prove it",  # at the start
        "{}, prove it",
        "{}: prove it",
        "{}'s idea is wrong",
        "{}\u2019s idea is wrong",
        "compare with what {} said",  # mid-text
        "what do you think, {}?",
        "(cc {})",
        "thanks {}!",
        "ask {}.",  # at the end, after punctuation
        "ask {}",  # at the very end
        "first line\n{} on the second",
        "-{}",
        "»{}",
        "```spoiler hidden\n{} in a spoiler, which is the sender's own text\n```",
    ],
)
def test_a_typed_mention_counts(name, kind, template):
    assert kinds(template.format(name)) == {kind}


@pytest.mark.parametrize(
    "text",
    [
        "`@claude` in inline code",
        "``x @claude y``",
        "```\n@claude in a code fence\n```",
        "~~~python\n@claude\n~~~",
        "```quote\n@claude in someone's words\n```",
        "````quote\n@claude\n```\nstill quoted\n```\n````",
        "> @claude in a quote",
        "   > @claude, indented quote",
        "> quoted\n@claude on a line Zulip continues the quote with",
        "```spoiler\n> @claude in a spoiler's quote\n```",
        "\\@claude escaped",
        "x@claude.ai",
        "mail drew@claude.example",
        "@claudette",
        "@claude_bot",
        "@claude2",
        "@claudeé",
        ".@claude",
        "@@claude",
        "*@claude*",
        "_@claude_",
        "@_**Claude · alice** said",  # a silent mention
        "@_**Claude · alice|12** [said](link):",
        "@**Claude · alice** completed",  # completed: the `mentioned` flag's business
        "claude, prove it",
        "@ claude",
        "https://medium.com/@claude/post",  # in a link (review of #723)
        "see github.com/@codex for it",
        "<https://example.org/@claude>",
        "[a post](https://medium.com/@claude)",
        "ftp://host/@codex",
    ],
)
def test_a_typed_mention_does_not_count(text):
    assert kinds(text) == set()


def test_beside_a_link_and_with_a_hyphen():
    """A link takes its own `@` only; `@claude-code` and `@codex-cli` count, as the rule has
    it (a hyphen is no letter, digit or `_`), and so does `@claude/@codex`."""
    assert kinds("https://medium.com/@claude/post, then @codex") == {"codex"}
    assert kinds("@claude,https://x.org/@codex") == {"claude"}  # before the link in its word
    assert kinds("@claude.com/x and claude.ai") == set()  # a domain, not a mention
    assert kinds("ask @claude/@codex") == {"claude", "codex"}
    assert kinds("@claude-code and @codex-cli") == {"claude", "codex"}


@pytest.mark.parametrize(
    "text",
    [
        "a." * 4995 + " @claude",
        "a.b" * 3333 + "@claude",
        "x" * 9990 + " @claude",
        ".com" * 2490 + "/@codex @claude",
        "@claude/" * 1249,
    ],
)
def test_links_are_found_in_linear_time(text):
    """Codex round 2 on #723: `"a." * 4995 + " @claude"` took 1.2 CPU seconds, in the bridge's
    and the waker's event loops. A message of 10,000 characters now takes about a millisecond;
    the bound leaves room for a loaded runner."""
    import time

    started = time.process_time()
    kinds(text)
    assert time.process_time() - started < 0.1


def test_without_mention_keeps_quotes():
    """The bot's own completed mentions leave a prompt, but not from a quote (#723's review):
    quoted words arrive as they were."""
    codex = mention("Codex · X", 10)
    text = (
        "@**Codex · X** compare\n> @**Codex · X** said 42\n\nand `@**Codex · X**` @**Codex · X|10**"
    )
    assert without_mention(text, codex) == " compare\n> @**Codex · X** said 42\n\nand `` "


def test_both_kinds_and_text_after_a_quote():
    """A blank line ends a `>` quote and a quote fence ends at its closer: what follows is the
    sender's own text."""
    assert kinds("@claude and @codex") == {"claude", "codex"}
    assert kinds("> @codex said this\n\n@claude what do you think?") == {"claude"}
    assert kinds("```quote\n@codex said this\n```\n@claude what now?") == {"claude"}


def test_completed_mentions_in_a_quote_no_longer_count():
    """Zulip silences the mentions inside a quote (its blockquote processor), so the quoted bot
    never had the `mentioned` flag; the other bots now read the quote the same way and don't
    take the message for one to the quoted bot (#713, the quote decision)."""
    codex = mention("Codex · X")
    for quoted in [
        "```quote\n@**Codex · X** prove it\n```\nwhat do you think?",
        "@_**Owner|20** [said](link):\n```quote\n@**Codex · X|31** prove it\n```\nand?",
        "> @**Codex · X** prove it\n\nwhat do you think?",
    ]:
        assert codex.search(quoted)
        assert not codex.search(visible_text(quoted))
    assert codex.search(visible_text("```spoiler\n@**Codex · X** prove it\n```"))


def test_blank_quotes_keeps_lines_and_code():
    text = "a\n```quote\nq\n```\n> b\nlazy\n\n```\n> code\n```\nc"
    assert blank_quotes(text) == "a\n\n\n\n\n\n\n```\n> code\n```\nc"
    assert blank_quotes("\\> not a quote") == "\\> not a quote"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("@claude !stop", "!stop"),
        ("@Claude: !stop", "!stop"),
        ("@claude, @codex !status", "!status"),
        ("@codex // a note", "// a note"),
        ("@CODEX\n!diff", "!diff"),
        ("@claude", ""),
        ("@claude's idea is wrong", "@claude's idea is wrong"),  # the words stay
        ("@claude. What next?", "@claude. What next?"),
        ("ask @claude !stop", "ask @claude !stop"),  # only a leading one goes
        ("@claudette !stop", "@claudette !stop"),
        ("@**Claude · X** @codex !stop", "!stop"),
    ],
)
def test_a_leading_typed_mention_is_taken_off(text, expected):
    assert unaddressed(text, [mention("Claude · X"), *TYPED_ADDRESSES])[0] == expected


@pytest.mark.parametrize(
    ("text", "kind", "expected"),
    [
        ("@claude !login", "claude", "!login"),
        ("@Claude: !login work", "claude", "!login work"),
        ("@CODEX !vm", "codex", "!vm"),
        ("@codex !login", "claude", "@codex !login"),  # the other kind: not this bot's
        ("@claude's code", "claude", "@claude's code"),
        ("!login", "claude", "!login"),
        ("  a pasted code ", "claude", "  a pasted code "),  # as it came
        ("@claude !login", "", "@claude !login"),  # a bot of no kind a typed mention names
    ],
)
def test_a_dm_s_leading_typed_mention_of_the_bot_s_own_kind(text, kind, expected):
    """A DM to the bot: `@claude !login` is `!login` (#709's reviewer on #713)."""
    assert without_typed_address(text, kind) == expected
