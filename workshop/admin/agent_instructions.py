#!/usr/bin/env python3
"""The agents' standing instructions on a workshop VM (README.md §2.2; PM on issue #36).

    python3 agent_instructions.py MANAGED.md [--home DIR] [--tools PATH]

The managed text's line `<!-- zab:tools -->` becomes the tools section, made from the VM's own
list of its software (`/etc/zab/tools.json`, or PATH; `tools_list.section`, #475), so that
the instructions never name a tool the VM lacks; the last line of output, `tools: …`, says what
it found. Without a readable list the section says so, and tells the agents to check a tool is
there before naming it.

Run on the VM as `agent` by `push_config.py` (uploaded with the managed text, argument lists
only): `~/.codex/AGENTS.md` is the one file both agents read. It holds the organizers' block
between `<!-- zab:managed:start -->` and `<!-- zab:managed:end -->`, taken from
`workshop/ami/files/agent-instructions.md`, and below it the participant's own section. A rerun
replaces only the block and keeps everything else, including text after the end marker on its
line. A file without markers (the AMI's copy) is replaced by the block plus an empty personal
section, unless it still has the personal section's header (an agent rewrote the file and
dropped the markers): then everything from the header on is kept. A missing file is created;
one whose markers are broken, or that is not UTF-8, is left alone and reported (exit 2), so
nobody's notes are lost, and CLAUDE.md is still installed (PM on issue #36, Mon 22:30).
`~/.claude/CLAUDE.md` is the single line `@~/.codex/AGENTS.md`: Claude Code loads that import
from the user-level file (VERIFY in docs/DECISIONS.md, Claude Code 2.1.290). A CLAUDE.md that
already imports the file is kept as it is; one with other content than the AMI's copy keeps
that content below the import line. Writes are atomic (a temporary file in the same directory,
then `os.replace`), mode 0644. Standard library only: the VM's system Python runs it.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile

START = "<!-- zab:managed:start -->"
END = "<!-- zab:managed:end -->"
#: Inside the block, under the start marker: agents edit only the section below.
NOTE = (
    "<!-- Maintained by the workshop organizers and replaced on every update. "
    "Do not edit between these markers; write notes in the section below. -->"
)
PERSONAL_HEADER = "## About me and standing instructions"
#: The file is named, so that neither agent reaches for its own memory feature (PM, Mon 22:30).
PERSONAL_INTRO = (
    "This section belongs to the participant. Notes about them (field, home page, how they like "
    "answers) and standing instructions go here. When they ask you to remember something across "
    "sessions, edit `~/.codex/AGENTS.md` (this file) and add it here; don't use your own memory "
    "feature. Both agents read this file at the start of every session.\n"
)
PERSONAL = f"{PERSONAL_HEADER}\n\n{PERSONAL_INTRO}"
_INTRO_START = "This section belongs to the participant."
_HEADER_LINE = re.compile(rf"^{re.escape(PERSONAL_HEADER)}", re.MULTILINE)
IMPORT = "@~/.codex/AGENTS.md"
#: The first line of the copy the AMI installed as ~/.claude/CLAUDE.md (workshop/ami/build.sh).
AMI_FIRST_LINE = "# Workshop VM"
#: The line of the managed text that the VM's tools section replaces (#475).
TOOLS_MARK = "<!-- zab:tools -->"
TOOLS_MANIFEST = "/etc/zab/tools.json"


class Malformed(ValueError):
    """The markers are there but not as one start before one end."""


def with_tools(managed: str, manifest: str = TOOLS_MANIFEST) -> tuple[str, str]:
    """`managed` with its TOOLS_MARK line replaced by the tools section of the manifest at
    `manifest`, and the outcome line (`tools: …`); text without the mark is left as it is."""
    if not any(line.strip() == TOOLS_MARK for line in managed.splitlines()):
        return managed, "tools: the instructions have no tools section"
    from tools_list import section  # beside this script, uploaded with it

    text, outcome = section(manifest)
    lines = managed.split("\n")
    return "\n".join(text.rstrip("\n") if x.strip() == TOOLS_MARK else x for x in lines), outcome


def block(managed: str) -> str:
    return f"{START}\n{NOTE}\n\n{managed.strip()}\n{END}\n"


def _personal(rest: str) -> str:
    """What follows the block: `rest` as it is if it has the personal header; otherwise the
    header goes first, with the intro only if `rest` lacks it (the header alone was deleted)."""
    if _HEADER_LINE.search(rest):
        return rest
    body = rest.strip("\n")
    if not body:
        return "\n" + PERSONAL
    intro = "" if _INTRO_START in body else PERSONAL_INTRO + "\n"
    return f"\n{PERSONAL_HEADER}\n\n{intro}{body}\n"


def merge_agents(existing: str | None, managed: str) -> str:
    """The new `~/.codex/AGENTS.md` (see the module docstring); raises Malformed."""
    new_block = block(managed)
    if existing is None or (START not in existing and END not in existing):
        header = _HEADER_LINE.search(existing or "")
        if header is None:
            return new_block + "\n" + PERSONAL
        return new_block + "\n" + existing[header.start() :]
    if existing.count(START) != 1 or existing.count(END) != 1:
        raise Malformed("expected one start and one end marker")
    start, end = existing.index(START), existing.index(END)
    if end < start:
        raise Malformed("the end marker comes before the start marker")
    # the rest of the end marker's line is someone's text, kept on its own line
    same_line, newline, rest = existing[end + len(END) :].partition("\n")
    if same_line.strip():
        rest = same_line.strip() + newline + rest
    return existing[:start] + new_block + _personal(rest)


def merge_claude(existing: str | None) -> str | None:
    """The new `~/.claude/CLAUDE.md`, or None to leave it as it is."""
    if existing is None or not existing.strip() or existing.lstrip().startswith(AMI_FIRST_LINE):
        return IMPORT + "\n"
    if IMPORT in (line.strip() for line in existing.splitlines()):
        return None
    return IMPORT + "\n\n" + existing


def _read(path: str) -> str | None:
    """The file's text, None if it is missing; raises Malformed if it is not UTF-8."""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None
    except UnicodeDecodeError as exc:
        raise Malformed(f"not UTF-8 (byte {exc.start})") from exc


def _write(path: str, text: str) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".zab-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _install_agents(path: str, managed: str) -> str:
    old = _read(path)
    new = merge_agents(old, managed)
    if new == old:
        return "unchanged"
    _write(path, new)
    return "created" if old is None else "updated"


def _install_claude(path: str) -> str:
    old = _read(path)
    new = merge_claude(old)
    if new is None or new == old:
        return "unchanged"
    _write(path, new)
    return "created" if old is None else "updated"


def install(home: str, managed: str) -> tuple[str, str]:
    """Update both files under `home`, each on its own: (AGENTS.md outcome, CLAUDE.md outcome),
    where a file left alone reads "left alone: <why>"."""
    outcomes = []
    for step in (
        lambda: _install_agents(os.path.join(home, ".codex", "AGENTS.md"), managed),
        lambda: _install_claude(os.path.join(home, ".claude", "CLAUDE.md")),
    ):
        try:
            outcomes.append(step())
        except Malformed as exc:
            outcomes.append(f"left alone: {exc}")
    return outcomes[0], outcomes[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install the agents' standing instructions.")
    parser.add_argument("managed", help="the organizers' text for the managed block")
    parser.add_argument("--home", default=os.path.expanduser("~"))
    parser.add_argument("--tools", default=TOOLS_MANIFEST, help="the VM's list of its software")
    args = parser.parse_args(argv)
    with open(args.managed, encoding="utf-8") as f:
        managed, tools = with_tools(f.read(), args.tools)
    agents, claude = install(args.home, managed)
    print(f"AGENTS.md {agents}, CLAUDE.md {claude}")
    print(tools)
    problems = [
        f"~/{where} {outcome}; fix it by hand and rerun"
        for where, outcome in ((".codex/AGENTS.md", agents), (".claude/CLAUDE.md", claude))
        if outcome.startswith("left alone")
    ]
    if problems:
        print("; ".join(problems), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
