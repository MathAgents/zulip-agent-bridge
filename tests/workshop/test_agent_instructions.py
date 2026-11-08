"""`workshop/admin/agent_instructions.py`: the agents' standing instructions on a VM (PM on
issue #36, Mon 19:00): a managed block replaced on every push, the participant's section kept."""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

import agent_instructions as ai
import pytest

FILES = pathlib.Path(__file__).resolve().parents[2] / "workshop" / "ami" / "files"
SCRIPT = (
    pathlib.Path(__file__).resolve().parents[2] / "workshop" / "admin" / "agent_instructions.py"
)


def files(home: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    return home / ".codex" / "AGENTS.md", home / ".claude" / "CLAUDE.md"


def test_missing_files_are_created(tmp_path):
    assert ai.install(str(tmp_path), "# Workshop VM\n\nv1\n") == ("created", "created")
    agents, claude = files(tmp_path)
    text = agents.read_text()
    assert text.startswith(ai.START + "\n") and "v1" in text
    assert text.index(ai.END) < text.index(ai.PERSONAL_HEADER)
    assert "Both agents read this file at the start of every session." in text
    assert claude.read_text() == "@~/.codex/AGENTS.md\n"
    assert oct(agents.stat().st_mode & 0o777) == "0o644"
    assert oct(claude.stat().st_mode & 0o777) == "0o644"
    # a second run changes nothing
    assert ai.install(str(tmp_path), "# Workshop VM\n\nv1\n") == ("unchanged", "unchanged")


def test_a_rerun_replaces_only_the_block_and_keeps_the_personal_section(tmp_path):
    ai.install(str(tmp_path), "managed v1\n")
    agents, _ = files(tmp_path)
    notes = "\n- Works on modular forms; home page https://example.org/~alice\n"
    agents.write_text("Text before, kept.\n" + agents.read_text() + notes)
    before = agents.read_text()
    assert ai.install(str(tmp_path), "managed v2\n")[0] == "updated"
    after = agents.read_text()
    assert "managed v2" in after and "managed v1" not in after
    # nothing outside the markers changed
    head, tail = before.split(ai.START)[0], before.split(ai.END)[1]
    assert after.startswith(head) and after.endswith(tail)
    assert after.count(ai.PERSONAL_HEADER) == 1 and notes.strip() in after


def test_an_unmarked_file_is_replaced_and_a_missing_personal_section_added(tmp_path):
    agents, claude = files(tmp_path)
    agents.parent.mkdir()
    claude.parent.mkdir()
    old = (FILES / "agent-instructions.md").read_text().replace("the workshop's file server", "x")
    agents.write_text(old)  # the AMI's copy, no markers
    claude.write_text(old)
    assert ai.install(str(tmp_path), "managed\n") == ("updated", "updated")
    assert agents.read_text() == ai.block("managed\n") + "\n" + ai.PERSONAL
    assert claude.read_text() == "@~/.codex/AGENTS.md\n"
    # markers without the personal section's header below: the header goes first, then the
    # intro, then what was there (the participant's notes)
    agents.write_text("intro\n" + ai.block("old") + "extra\n")
    ai.install(str(tmp_path), "new\n")
    assert agents.read_text() == (
        "intro\n"
        + ai.block("new")
        + "\n"
        + ai.PERSONAL_HEADER
        + "\n\n"
        + ai.PERSONAL_INTRO
        + "\nextra\n"
    )


def test_an_unmarked_file_with_the_personal_header_keeps_the_notes(tmp_path):
    """PM on #36 (Mon 22:30, item 3): an agent rewrote the file and dropped the markers; the
    participant's section, from its header on, is kept."""
    agents, _ = files(tmp_path)
    agents.parent.mkdir()
    personal = ai.PERSONAL_HEADER + "\n\n- Works on Shimura curves.\n- Answers in French.\n"
    agents.write_text("# Workshop VM\n\nsome old text\n\n" + personal)
    assert ai.install(str(tmp_path), "managed\n")[0] == "updated"
    assert agents.read_text() == ai.block("managed\n") + "\n" + personal
    # the header must start a line: a mention of it in a sentence is not the section
    agents.write_text(f"see {ai.PERSONAL_HEADER} below\n")
    ai.install(str(tmp_path), "managed\n")
    assert agents.read_text() == ai.block("managed\n") + "\n" + ai.PERSONAL


def test_text_after_the_end_marker_on_its_line_is_kept(tmp_path):
    """PM nit (:64): the rest of the end marker's line was dropped."""
    agents, _ = files(tmp_path)
    agents.parent.mkdir()
    agents.write_text(ai.block("old").rstrip("\n") + " - Prefers Magma.\n\n" + ai.PERSONAL)
    ai.install(str(tmp_path), "new\n")
    assert agents.read_text() == ai.block("new") + "- Prefers Magma.\n\n" + ai.PERSONAL


def test_a_deleted_header_does_not_duplicate_the_intro(tmp_path):
    """PM nit (:67): only the header was deleted; it comes back, the intro is not repeated."""
    agents, _ = files(tmp_path)
    agents.parent.mkdir()
    agents.write_text(ai.block("old") + "\n" + ai.PERSONAL_INTRO + "\n- Likes tables.\n")
    ai.install(str(tmp_path), "new\n")
    text = agents.read_text()
    assert text.count("This section belongs to the participant.") == 1
    assert text == (
        ai.block("new")
        + "\n"
        + ai.PERSONAL_HEADER
        + "\n\n"
        + ai.PERSONAL_INTRO
        + "\n- Likes tables.\n"
    )


def test_a_file_that_is_not_utf8_is_left_alone_and_claude_md_still_installed(tmp_path):
    """PM on #36 (Mon 22:30, item 2): reported, not a crash; the other file still goes in."""
    agents, claude = files(tmp_path)
    agents.parent.mkdir()
    agents.write_bytes(b"notes in latin-1: \xe9\n")
    agents_outcome, claude_outcome = ai.install(str(tmp_path), "managed\n")
    assert agents_outcome.startswith("left alone: not UTF-8") and claude_outcome == "created"
    assert agents.read_bytes() == b"notes in latin-1: \xe9\n"
    assert claude.read_text() == "@~/.codex/AGENTS.md\n"


def test_the_personal_section_names_the_file():
    """PM on #36 (Mon 22:30, item 4): agents edit ~/.codex/AGENTS.md, not their own memory."""
    assert "`~/.codex/AGENTS.md`" in ai.PERSONAL_INTRO
    assert "don't use your own memory feature" in ai.PERSONAL_INTRO


@pytest.mark.parametrize(
    "broken",
    [
        f"{ai.START}\nno end\n",
        f"{ai.END}\n{ai.START}\n",
        f"{ai.START}\n{ai.START}\n{ai.END}\n",
    ],
)
def test_broken_markers_leave_the_file_alone(tmp_path, broken):
    agents, _ = files(tmp_path)
    agents.parent.mkdir()
    agents.write_text(broken + "my notes\n")
    outcome, claude = ai.install(str(tmp_path), "managed\n")
    assert outcome.startswith("left alone: ") and claude == "created"
    assert agents.read_text() == broken + "my notes\n"
    managed = tmp_path / "managed.md"
    managed.write_text("managed\n")
    res = subprocess.run(  # noqa: S603 - argument list, test only
        [sys.executable, str(SCRIPT), str(managed), "--home", str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 2 and "~/.codex/AGENTS.md left alone" in res.stderr
    assert "fix it by hand and rerun" in res.stderr


def test_claude_md_keeps_anything_beyond_the_import():
    assert ai.merge_claude(None) == "@~/.codex/AGENTS.md\n"
    assert ai.merge_claude("") == "@~/.codex/AGENTS.md\n"
    assert ai.merge_claude("# Workshop VM\n\nold copy\n") == "@~/.codex/AGENTS.md\n"
    assert ai.merge_claude("@~/.codex/AGENTS.md\n\nmy own note\n") is None  # left as it is
    assert ai.merge_claude("my own note\n") == "@~/.codex/AGENTS.md\n\nmy own note\n"


def test_the_script_runs_as_the_vm_runs_it(tmp_path):
    """The command push_config sends: the system Python, the script, the shipped text; the
    tools section from the VM's list (#475), here one with a tool, then none."""
    listing = tmp_path / "tools.json"
    listing.write_text(
        '{"schema": 1, "image": "img", "built": "2026-10-20", "tools": [{"name": "PARI/GP", '
        '"version": "2.17.4", "area": "Number theory", "call": "gp -q f.gp"}]}'
    )

    def run(*extra: str) -> list[str]:
        argv = [sys.executable, str(SCRIPT), str(FILES / "agent-instructions.md")]
        res = subprocess.run(  # noqa: S603 - argument list, test only
            [*argv, "--home", str(tmp_path), *extra], capture_output=True, text=True, check=True
        )
        return res.stdout.splitlines()

    assert run("--tools", str(listing)) == [
        "AGENTS.md created, CLAUDE.md created",
        f"tools: 1 from {listing}",
    ]
    text = files(tmp_path)[0].read_text()
    assert "the workshop's file server" in text and "only as downloads" not in text
    assert "### Number theory\n- **PARI/GP** 2.17.4: `gp -q f.gp`\n" in text
    assert "zab:tools" not in text
    assert run("--tools", str(tmp_path / "none.json")) == [
        "AGENTS.md updated, CLAUDE.md unchanged",
        f"tools: none in the instructions ({tmp_path / 'none.json'}: missing)",
    ]
    assert "- **PARI/GP**" not in files(tmp_path)[0].read_text()


def test_the_shipped_text_matches_the_pms_items():
    doc = (FILES / "agent-instructions.md").read_text()
    assert "professional research mathematician" in doc
    # any workshop's (#475), no longer "number theory (Oct 9\u201310, 2026)"
    assert "at a workshop on AI agents in\nmathematics. Be precise" in doc
    assert "number theory" not in doc and "Oct 9" not in doc
    assert "separate what is proved from what is observed" in doc
    flat = " ".join(doc.split())
    assert "the workshop's file server" in doc and "Anyone who has one such link" in flat
    # PM on #36 (Mon 22:30, #169 item 1): images and PDFs go to Zulip; SVG only inside HTML
    assert "Images (PNG, JPEG, GIF) and PDFs are uploaded to Zulip" in flat
    assert "use SVG only inside HTML pages" in flat
    assert "open every file of that snapshot" in flat
    assert "Don't use your own memory feature" in flat and "`~/.codex/AGENTS.md`" in flat
    assert "Present substantial results as a web page" in doc and "KaTeX" in doc
    # #256, #448: files render on the file server, and a line links as `path:42` or `#L42`
    assert "Markdown renders as on GitHub, with math" in flat
    assert "highlighted with line numbers" in flat
    assert "`src/check.gp:42`, or a link to `src/check.gp#L42`, opens at line 42" in flat
    # a directory's link is a bare file list (workshop/files/server.py): link the README
    assert "link the README itself (a directory's link shows only its file list)" in flat
    assert "linked from the README." in flat
    assert "do not produce HTML or SVG" not in doc  # false since the file server
    assert ai.START not in doc and ai.END not in doc  # the markers come from the script


def test_the_agents_stay_visible_and_responsive_during_a_turn():
    """#653 (Drew, Oct 25): a steer waits for the current tool call, so no call blocks for more
    than about 50 s (the 4-minute loops are gone); the agents say what they do, answer
    mid-turn messages at once, and note progress after each check. Sub-agents run in the
    background, as both agents can (DECISIONS, the 2026-10-25 rows of #653's VERIFY a and b),
    for thinking, one at a time, never nested, and are stopped before a turn ends (the review
    of #659)."""
    doc = (FILES / "agent-instructions.md").read_text()
    flat = " ".join(doc.split())
    for needle in (
        "## During a turn: stay visible and responsive",
        "all the participant sees in Zulip is the bridge's status message",
        # item 1
        "Before a step that may take more than about 10 seconds, write one short sentence: "
        "what you are about to do, and why.",
        "While you wait on a job or a sub-agent, write a note at least once a minute: never two "
        "waiting calls in a row without one.",
        # item 2
        "Answer mid-turn messages at once.",
        "the message reaches you after your current tool call",
        "Never keep the answer to a mid-turn question for the final reply only, and if it still "
        "matters at the end, repeat it there: the status message's notes are gone once the turn "
        "ends.",
        # item 3, and the rules kept from #189
        "never block for more than about 50 seconds in one call",
        # a call that can't be backgrounded (the 2026-10-25 #249 row: 1 min 22 s)
        "the first Lean tool call after the VM starts can take a minute or two), say so before "
        "you make it.",
        "Run a command in the foreground only when it finishes within about 50 seconds.",
        "check it every 30 to 50 seconds, in calls of at most 50 seconds each, so that your notes "
        "come less than a minute apart",
        # 50 s calls: with 60 s ones, the notes came 62-65 s apart (the live check)
        "`for i in $(seq 5); do kill -0 $PID 2>/dev/null || break; sleep 10; done; "
        "tail -n 5 job.log`",
        # Codex: `&` and `nohup` jobs die with the call; a unified-exec session lives on
        "Codex: start it with `exec_command` and a short `yield_time_ms`, with its output also in "
        "a log file (for example `python3 -u run.py 2>&1 | tee job.log`)",
        "`write_stdin` (no input) and a `yield_time_ms` of at most 50000",
        "A job put in the background with `&` or `nohup` is stopped when the call that started "
        "it ends.",
        "After each check, before the next call, write a one-line progress note: the time "
        "elapsed and what is known so far.",
        # the sanity check of the review round: Claude checked a sub-agent's log three times
        # with no note between, 171 s of silence
        "Never make two waiting calls in a row without a note between them, even when little "
        "has changed.",
        "Write the note right after each check's result, before the next check",
        "Never end a turn waiting for a job, and never promise to report back later.",
        "continuing needs a new request",
        # item 4, for both agents
        "## Sub-agents",
        "For substantial work that needs thinking (a literature search, a proof attempt, an "
        "independent check of a result)",
        "A long computation is not such work: run it as a background command",
        "the Agent tool with `run_in_background: true`",
        "Never run a long sub-agent in the foreground",
        "`spawn_agent`, then `wait_agent` with a timeout of at most 50 seconds per call",
        "Start one at a time, unless the participant asks for more.",
        "A sub-agent doesn't start sub-agents of its own: say so in its task.",
        "Don't end your turn while a sub-agent runs.",
        "stop it first (Codex: `interrupt_agent`), and say in your reply what it had found.",
        "The participant's `!stop` ends your turn but not a Codex sub-agent",
        "Sub-agents use up the participant's plan limits faster.",
        "`!usage`",
    ):
        assert needle in flat, needle
    assert "4 minutes" not in flat and "seq 8" not in flat and "60 seconds" not in flat
    assert "a long computation, a literature search" not in flat  # computations aren't delegated
    # the shell loop stays one code span on one line: a wrapped span would break it
    assert "\n    `for i in $(seq 5); do kill -0 $PID" in doc


def test_the_agents_note_progress_while_a_subagent_runs():
    """#668 (Drew, Nov 7: option 1 for both agents): while a background sub-agent runs, the
    agent keeps reading its log and notes progress after every check. Claude Code's launch
    message for a background agent says "You know nothing about its results until that
    notification arrives — do not report, assume, or predict them" (the 2.1.283 fixture
    `subagent_background.jsonl`), and with v0.2.11's text Claude went 130 s without a note
    (DECISIONS, 2026-10-26): the text says that the warning is about findings, not progress."""
    doc = (FILES / "agent-instructions.md").read_text()
    section = " ".join(doc.split("## Sub-agents")[1].split("\n## ")[0].split())
    for needle in (
        "While it runs, keep checking on it as above, every 30 to 50 seconds until it is done.",
        # the live check at 496b2f58: notes about 42 s apart, but two or three checks in a row
        # without one (88 s and 127 s) in two topics of three
        "Write the note right after each check's result, before the next check: never two "
        "checks in a row without a note between them.",
        "A note is one line: how long it has run and what its log shows.",
        # #922's review, N6: a sub-agent has no PID for the `kill -0` loop
        "Claude Code: check it with a bounded wait and its log's tail, for example "
        "`for i in $(seq 4); do sleep 10; done; tail -n 5 sub.log`.",
        "The message that starts a background sub-agent tells you not to report its results "
        "until you are told that it has finished.",
        "That is about its findings, not its progress: the notes are still due after every "
        "check, and a status question still gets an answer.",
        "Say what its log shows so far, and don't guess at what it will find.",
    ):
        assert needle in section, needle
    # the loop stays one code span on one line
    assert "\n    `for i in $(seq 4); do sleep 10; done; tail -n 5 sub.log`." in doc
    fixture = FILES.parents[2] / "tests/fixtures/claude/2.1.283/subagent_background.jsonl"
    assert "do not report, assume, or predict them" in fixture.read_text()  # what it answers


def test_the_agents_write_times_as_zulip_times():
    """#673 (Drew, Oct 27): a time goes in Zulip's `<time:…>`, in UTC, which each reader sees in
    their own time zone; Claude's answer had said "The job started at 01:08:38", in UTC with no
    zone, and quoted epoch seconds. Inside backticks it would not render."""
    sharing = (FILES / "agent-instructions.md").read_text().split("## Sharing results in Zulip")[1]
    flat = " ".join(sharing.split("\n## ")[0].split())
    for needle in (
        "Write a time of day or a date and time, in your notes and your reply, as Zulip's "
        "`<time:2026-10-27T01:08:38Z>`, in UTC and not in backticks: each reader sees it in "
        "their own time zone.",
        "Never a bare clock time (`01:08:38`), and no epoch seconds in prose.",
        # a time quoted from a log or code is quoted as it is (the review of #676)
        "Durations stay plain (`3 min 0 s`), and times inside quoted logs, command output or "
        "code stay exactly as they are.",
    ):
        assert needle in flat, needle


def test_every_language_the_instructions_name_is_rendered_with_line_numbers():
    """Reviewer on #449: the instructions name only languages the file server highlights with
    line numbers, which the bridge links lines of (`SERVED_LINE_EXT`, kept equal to the
    server's list by tests/workshop/files/test_render.py)."""
    from zab.render.links import SERVED_LINE_EXT

    flat = " ".join((FILES / "agent-instructions.md").read_text().split())
    named = flat.split("source files (", 1)[1].split(")", 1)[0].split(", ")
    extensions = {
        "Python/Sage": {"py", "sage"}, "Lean": {"lean"}, "PARI/GP": {"gp"}, "Magma": {"m"},
        "GAP": {"g", "gd", "gi"}, "Macaulay2": {"m2"}, "Julia": {"jl"}, "C/C++": {"c", "cpp"},
        "Rust": {"rs"}, "TeX": {"tex"}, "shell": {"sh"}, "JSON": {"json"}, "YAML": {"yaml"},
    }  # fmt: skip
    assert set(named) == set(extensions)
    for name in named:
        assert extensions[name] <= SERVED_LINE_EXT, name


def test_the_agents_share_files_with_zab_share_never_with_an_answers_link():
    """#710 criterion 6: a share link from `zab share`, what it opens, that it lasts until
    revoked, and how to revoke it; never an answer's own link, which opens the whole snapshot."""
    sharing = (FILES / "agent-instructions.md").read_text().split("## Sharing results in Zulip")[1]
    flat = " ".join(sharing.split("\n## ")[0].split())
    for needle in (
        "Never offer an answer's own link to the file server for sharing: it opens the whole "
        "snapshot.",
        "run `zab share PATH…` (see `zab share --help`) in the topic's checkout, and give the "
        "link it prints",
        "the link opens only what was shared, for anyone who has it",
        "it keeps working until it is revoked (after the VM ends, and even if the repository is "
        "deleted)",
        "ask an agent while the VM runs (`zab share --revoke NAME`), or the organizers after.",
    ):
        assert needle in flat, needle


def test_the_instructions_name_no_host_or_organization():
    """#693 criterion 3: every deployment's VMs get this text, the AMI's copy included, which
    is built before any file server is known: so it says "the workshop's file server", and
    names neither a host nor an organization."""
    doc = (FILES / "agent-instructions.md").read_text()
    assert "mathagents" not in doc.lower()
    assert "://" not in doc and not re.search(r"\b[a-z0-9-]+\.(?:org|com|net|io)\b", doc)
    flat = " ".join(doc.split())
    assert "open in the browser from the workshop's file server, in a snapshot" in flat
