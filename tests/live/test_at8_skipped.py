"""AT-8's live tests skip, checked offline (#672): the dev organization's public channel is
archived (SPEC §15.3), and the offline tests their reason names cover the refusal (SPEC §17).
Nothing here reaches Zulip: it reads the live modules' marks, and the offline tests' sources."""

from __future__ import annotations

import re
from pathlib import Path

import at8
import pytest
import test_claude_at
import test_codex_m1

TESTS = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "test",
    [test_claude_at.test_at8_public_refusal, test_codex_m1.test_public_refusal],
    ids=["claude", "codex"],
)
def test_at8_skips_unconditionally_and_says_why(test):
    skips = [m for m in getattr(test, "pytestmark", []) if m.name == "skip"]
    assert len(skips) == 1 and not skips[0].args  # `skip`, never a `skipif` that might run it
    reason = skips[0].kwargs["reason"]
    assert reason == at8.AT8_SKIP and "is archived" in reason  # one reason, from `at8` (#805)
    cited = re.findall(r"tests/(\S+\.py)::(\w+)", reason)
    assert len(cited) == 3
    for path, name in cited:  # the offline coverage it names exists
        assert re.search(rf"^(async )?def {name}\(", (TESTS / path).read_text(), re.M), name
