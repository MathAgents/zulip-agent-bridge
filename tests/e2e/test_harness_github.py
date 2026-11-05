"""The e2e harness's GitHub helper and the shared flows, offline (issue #263)."""

from __future__ import annotations

import json
import re

import pytest

from e2e.harness import github
from e2e.harness.flows import STATUS_RE, answer


def test_commit_authorship_from_github_and_git():
    trailer = "Co-Authored-By: Claude <noreply@anthropic.com>"
    data = {"commit": {"author": {"name": "Claude · zab-test", "email": "c@x"},
                       "committer": {"name": "Claude · zab-test"},
                       "message": f"Add e2e file\n\n{trailer}\n"}}  # fmt: skip
    calls = []
    got = github.commit_authorship(
        "MathAgents/zab-test", "abc1234", run=lambda a: calls.append(a) or json.dumps(data)
    )
    assert calls == [["api", "repos/MathAgents/zab-test/commits/abc1234"]]
    assert got.problems("Claude · zab-test") == ["Co-Authored-By: Claude <noreply@anthropic.com>"]
    clean = github.parse_git_show("abc1234", "Codex · zab-test\x00c@x\x00Codex · zab-test\x00Msg\n")
    assert clean.problems("Codex · zab-test") == [] and clean.subject == "Msg"
    assert got.subject == "Add e2e file"  # the commit message's first line (reviewer on #284)
    assert clean.problems("Claude · zab-test") == [
        "author 'Codex · zab-test', expected 'Claude · zab-test'"
    ]
    for repo, sha in (("x", "abc1234"), ("a/b", "HEAD"), ("a/b", "abc; rm")):
        with pytest.raises(ValueError):
            github.commit_authorship(repo, sha, run=lambda a: "{}")


def test_answer_patterns_skip_status_messages():
    status = "✅ **Done** · 0m 05s · 1 actions\n> I will reply TOKEN-1"
    assert not re.search(answer("TOKEN-1"), status, re.S)
    assert re.search(answer("TOKEN-1"), "Sure.\n\nTOKEN-1", re.S)
    # every heading of zab.core.presentation.status (reviewer on #284)
    for heading in ("⏸ **Waiting for approval**", "⏹ stopping…", "⏹ **Stopped**", "❌ **Failed**"):
        assert STATUS_RE.match(heading + " · 0m 05s") and not re.search(
            answer("TOKEN-1"), heading + " TOKEN-1", re.S
        )


@pytest.mark.parametrize(
    "heading", ["error", "⚠️ **Error**", "**Backend reconnecting**", "🆕 **Anything new**"]
)
def test_any_heading_with_the_status_shape_is_a_status_message(heading):
    """#295, scenario 4 on 0bca627: a raw "error" heading over an action list quoting the token
    passed for the answer, posted (its first version) before the block."""
    status = (
        f"{heading} · 12m 20s · 3 actions · model default · effort default\n"
        "- ✅ `sleep 120`\n- ⏳ `P=$(pgrep -f '^sleep 120$'); echo OUTAGE-1`"
    )
    assert STATUS_RE.match(status)
    assert not re.search(answer("OUTAGE-1"), status, re.S)
    assert re.search(answer("OUTAGE-1"), "OUTAGE-1", re.S)
    assert STATUS_RE.match(f"{heading} · 0m 05s · 0 actions · ` gpt-6 ` · effort ` high `")
    started = "started <time:2026-11-02T10:00:00+00:00>"  # while it runs (#754)
    assert STATUS_RE.match(f"{heading} · {started} · 0 actions · model default")
    # an answer is not a status message just for mentioning a duration; nor is `!status`'s reply
    assert not STATUS_RE.match("It took 2m 05s · 3 attempts.\nOUTAGE-1")
    assert not STATUS_RE.match("⏳ Running · 1m 05s · 3 actions · 0 messages held")
    assert STATUS_RE.match("⏳ **Working** · via terminal")
