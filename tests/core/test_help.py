"""Help stays complete and scannable (SPEC §4.6; PM #35, Mon 18:10)."""

import re
from pathlib import Path

import pytest

from zab.core.commands import DM_HELP, HELP

pytestmark = pytest.mark.contract


def test_help_covers_spec_command_table_and_fits_one_message():
    spec = (Path(__file__).resolve().parents[2] / "docs/SPEC.md").read_text()
    commands = spec.split("### 4.6 Commands", 1)[1].split("### 4.7", 1)[0]
    table = "\n".join(
        line.split("|")[1] for line in commands.splitlines() if line.startswith("| `!")
    )
    expected = set(re.findall(r"![a-z-]+", table)) | {"!usage", "!login"}
    assert expected <= set(re.findall(r"![a-z-]+", HELP))
    assert len(HELP) <= 10000 and len(DM_HELP) <= 10000


def test_help_has_one_command_or_tight_group_per_line():
    tight_groups = [
        {"!model", "!effort"},
        {"!approve", "!approve-session", "!deny"},
        {"!attach", "!detach"},
    ]
    lines = HELP.splitlines()[1:]
    for line in lines:
        assert line.startswith("`!")
        commands = set(re.findall(r"![a-z-]+", line))
        assert len(commands) == 1 or commands in tight_groups, line
    assert [line.split("`", 2)[1].split()[0] for line in lines[:3]] == [
        "!stop",
        "!status",
        "!queue",
    ]
    assert "queued messages are kept" in lines[0]
    assert "SSH" not in HELP


def test_dm_help_identifies_supported_commands_then_shares_topic_help():
    header, body = DM_HELP.split("\n\n", 1)
    assert set(re.findall(r"![a-z-]+", header)) == {
        "!login",
        "!status",
        "!help",
        "!vm",
        "!tools",
        "!restart",  # answered by `zab sentinel` (#204)
        "!unmentioned",  # the VM's rule for a message without a mention (#878)
    }
    assert "`!restart codex`" in header and "if your bots stop answering" in header.lower()
    assert "only" in header and "other commands" in header
    assert body == HELP
