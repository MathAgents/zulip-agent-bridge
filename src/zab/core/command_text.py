"""Commands as people type them (SPEC §4.6; #709): the bridge's DM `!login` and DM commands,
its topic commands, and `zab sentinel`'s `!vm` and `!restart` read a message through
`as_command`. Standard library and `zab.core.addressing` only: the sentinel imports it."""

from __future__ import annotations

import re

from zab.core.addressing import unaddressed

#: A message that is one inline code span holding a command, "`!login`": what Zulip's paste of
#: the rendered "Start here" gives back. Any run of backticks, closed by the same run, with the
#: spaces inside it, as CommonMark has code spans; on one line, so that a fenced block
#: ("```\n!pip install sympy\n```") is never a command. Its words and the spaces between them
#: are classes that share no character, so a long run of either is matched in linear time; the
#: repeat of a space and a word is possessive, as giving one back never lets the closing
#: backticks match, and a greedy one keeps a frame of `re` per pass, 145 MB on 1 MB (#833).
_CODE_SPAN = re.compile(r"(`+)[^\S\n]*(![^`\s]*(?:[^\S\n]+[^`\s]+)*+)[^\S\n]*\1")
_VERB = re.compile(r"!\S+")


def as_command(text: str, mention: re.Pattern[str] | None = None) -> str:
    """`text` as the command it was typed as, or `text` itself when it holds none.

    A command is recognised after a leading mention of the bot, `mention` (in a topic, the router
    has taken the address list off already: SPEC §4.5 step 4), and inside one code span that is
    the whole message; its verb is matched in any case (`!Login`, from a phone keyboard's capital).
    What follows the verb is kept as typed: a path, a model, a prompt. Surrounding whitespace is
    dropped, which Zulip does already (`normalize_body`). Any other message comes back unchanged:
    a prompt that starts with a code span is still a prompt."""
    body = text.strip()
    if mention is not None:
        body = unaddressed(body, [mention])[0].strip()
    if span := _CODE_SPAN.fullmatch(body):
        body = span[2]
    if not body.startswith("!"):
        return text
    return _VERB.sub(lambda verb: verb[0].lower(), body, count=1)
