"""Commands as people type them (SPEC §4.6; #709): `zab.core.command_text.as_command`."""

import time

import pytest

from zab.core.addressing import mention
from zab.core.command_text import as_command

BOT = mention("Claude · alice", 301)


@pytest.mark.parametrize(
    "typed, command",
    [
        ("!login", "!login"),
        ("!Login", "!login"),  # a phone keyboard's capital
        ("!LOGIN", "!login"),
        ("`!login`", "!login"),  # Zulip's paste of the rendered "Start here"
        ("`` !login ``", "!login"),
        ("  !login\n", "!login"),
        ("@**Claude · alice** !login", "!login"),
        ("@**Claude · alice|301** !login", "!login"),
        ("@**Claude · alice**: !Login", "!login"),
        ("@**Claude · alice** `!login`", "!login"),
        ("`!login cancel`", "!login cancel"),
        ("!Login work", "!login work"),  # the account is kept as typed
        ("!File Results/Table.csv", "!file Results/Table.csv"),
        ("`!restart codex`", "!restart codex"),
        ("!New --model M\nFirst Line", "!new --model M\nFirst Line"),  # the prompt as typed
    ],
)
def test_each_form_reads_as_the_command(typed, command):
    assert as_command(typed, BOT) == command


@pytest.mark.parametrize(
    "text",
    [
        "hello",
        "`!x` is a factorial, isn't it?",  # a prompt that starts with a code span
        "`code`",
        "@**Claude · alice** hello",
        "@**Codex · alice** !login",  # another bot's mention is no address of this one
        "@**Claude · bob** !login",
        "say !login",
        "@_**Claude · alice** !login",  # a silent mention is no mention
        # a fenced block is code, never a command, whatever its first line (review of #725)
        "```\n!pip install sympy\nimport sympy\n```",
        "```\n!stop\n```",
        "`!stop\n`",
        "@**Claude · alice** ```\n!stop\n```",
    ],
)
def test_anything_else_comes_back_unchanged(text):
    assert as_command(text, BOT) == text


@pytest.mark.parametrize(
    "shape",
    [
        lambda n: "`!" + " " * n + "x",  # the review's: a space run, never closed
        lambda n: "`!" + " x" * (n // 2),
        lambda n: "`!" + " " * n + "`x",
        lambda n: "`" * n,
        lambda n: "`" * (n // 2) + "!" + " " * (n // 2),
        lambda n: "@**Claude · alice** `!" + "\t " * (n // 2),
    ],
)
def test_long_runs_take_linear_time(shape):
    """Review of #725: the code span's pattern matched a long run of spaces in quadratic time,
    ~0.2 s for 10,000 characters, up to four times a DM. Ten times Zulip's longest message here
    takes a few milliseconds; a quadratic match would take tens of seconds. The bound is loose."""
    text = shape(100_000)
    started = time.perf_counter()
    for _ in range(4):
        as_command(text, BOT)
    assert time.perf_counter() - started < 2.0


def test_without_a_mention_only_case_and_code_count():
    """In a topic the router has taken the address list off already (§4.5 step 4)."""
    assert as_command("`!Stop`") == "!stop"
    assert as_command("@**Claude · alice** !stop") == "@**Claude · alice** !stop"
