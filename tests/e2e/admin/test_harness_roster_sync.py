"""A22's helpers, offline (#627): the plan's added row, the set-aside script's refusals, and the
rig's channel for it."""

from __future__ import annotations

import subprocess

import pytest

from e2e.admin.harness import rig as rigs
from e2e.admin.test_scenarios_5_roster_sync import SET_ASIDE, added

PLAN = """**P-12** · zab-admin-e2e2 · `!roster sync` · requested by …
roster sync from #zab-dev-participants-2 (channel 36): 2 subscribers; "zab-tester" (user 11), …
roster sync: 1 added, 0 renamed, 0 removed, 0 left the channel, 0 excluded, 0 unchanged
+ zab-tester2  (Zulip user 12)  "zab-tester2"  "user12@zulip.example"
"""


def test_the_added_row_is_read_from_the_plan():
    assert added(PLAN, 12) == "zab-tester2"
    with pytest.raises(AssertionError, match="adds no row for user 11"):
        added(PLAN, 11)


@pytest.mark.parametrize("word", ["", "x", "away; rm -rf /", "../back"])
def test_the_set_aside_script_takes_away_or_back_alone(word):
    argv = ["/usr/bin/bash", "-c", SET_ASIDE, "bash", word]  # a fixed script, as the server runs it
    done = subprocess.run(argv, capture_output=True, check=False)  # noqa: S603
    assert done.returncode == 2


def test_away_refuses_an_earlier_set_aside_rather_than_overwrite_it():
    """Review of #641 (6): a run cut short leaves `roster.csv.a22`, e2e2's own roster; the next
    `away` exits 3, never `mv -f` over it."""
    assert "mv -f" not in SET_ASIDE.split("back)")[0]
    assert 'if [ -e "$d/$f.a22" ]; then' in SET_ASIDE and "exit 3" in SET_ASIDE


def test_e2e2_alone_has_a_participants_channel():
    assert rigs.E2E2.participants == "zab-dev-participants-2" and not rigs.E2E.participants
