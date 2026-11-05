"""Disarming Zulip mentions (SPEC §11.5), shared by the answer and diff renderers."""

from __future__ import annotations

import re

ZWSP = "\u200b"
# The `@` of anything shaped like a mention: `@**name**` or `@*group*`, but not `@_**name**`.
_MENTION_AT = re.compile(r"@(?=\*\*[^*\n]+\*\*|\*[^*\n]+\*)")


def disarm_mentions(text: str) -> str:
    """Break the syntax of every non-silent mention with a zero-width space after the `@`.

    Zulip recognizes a mention with a regex on the literal characters: `@`, an optional `_`
    for the silent form, then `**name**` or `*group*` (zerver/lib/mention.py). With U+200B
    between the `@` and the `*`, that regex can no longer match, in prose or inside code,
    whatever backticks, escapes, paragraph boundaries or message splits surround it; the
    character is invisible when rendered, inside a code span too. This is therefore the
    guarantee behind SPEC §11.5 for every message the bridge posts: the answer renderer runs
    it on the final text after code is restored, and the diff renderer on the diff message
    (file names and stat lines are agent-influenced text). Silent mentions `@_**Name**` are
    real mentions that notify nobody and stay parseable. Idempotent.
    """
    return _MENTION_AT.sub("@" + ZWSP, text)


__all__ = ["ZWSP", "disarm_mentions"]
