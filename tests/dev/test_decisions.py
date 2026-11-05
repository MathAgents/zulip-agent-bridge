"""docs/DECISIONS.md, which parallel pull requests append to: each decision number `D<n>` once
in the whole file, the Decisions table's in increasing order, and no table row twice.

`.gitattributes` used to merge the file with `merge=union`, which never conflicts. On Oct 23 a
merge kept D36 in both its old and its new form, and two D37s from parallel PRs, and nobody
saw either. The file now merges as text, so the second PR to take a number meets a conflict
and renumbers. These checks catch what still slips through, a hand merge for instance.
Numbers may skip (a PR can take D39 while D38's is open), but never repeat or go back. Until
Oct 23 the table of implementation decisions after kickoff had its own D18, now D40 (#612).
"""

from __future__ import annotations

import re
from collections import Counter
from itertools import pairwise
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DECISIONS = ROOT / "docs" / "DECISIONS.md"
#: A table's header separator row, `|---|---|`.
_SEPARATOR = re.compile(r"^\|(?:\s*:?-+:?\s*\|)+\s*$")


#: A decision's row: its number in the first cell.
_NUMBERED = re.compile(r"^\|\s*D(\d+)\s*\|", re.MULTILINE)


def decision_numbers(text: str) -> list[int]:
    """The `n` of each `D<n>` row of the table under `## Decisions`, in order."""
    _, found, rest = text.partition("\n## Decisions\n")
    assert found, "no `## Decisions` section"
    return [int(n) for n in _NUMBERED.findall(rest.split("\n## ", 1)[0])]


def repeated_rows(text: str) -> list[str]:
    """Each table row that appears more than once in `text`. Header rows (each followed by a
    separator) and separators are left out, since tables share them."""
    lines = text.splitlines()
    rows = [
        line
        for line, after in zip(lines, [*lines[1:], ""], strict=True)
        if line.startswith("|") and not _SEPARATOR.match(line) and not _SEPARATOR.match(after)
    ]
    return [row for row, count in Counter(rows).items() if count > 1]


def numbering_problems(text: str) -> list[str]:
    """What is wrong with the decisions' numbers: one given to two rows anywhere in `text`, or
    one out of order in the Decisions table."""
    counts = Counter(int(n) for n in _NUMBERED.findall(text))
    problems = [f"D{n} appears {k} times" for n, k in counts.items() if k > 1]
    problems += [f"D{b} follows D{a}" for a, b in pairwise(decision_numbers(text)) if b < a]
    return problems


def test_each_decision_is_numbered_once_in_increasing_order():
    text = DECISIONS.read_text(encoding="utf-8")
    assert decision_numbers(text)[:3] == [1, 2, 3]  # the table was found, from its start
    assert numbering_problems(text) == []


def test_no_table_row_appears_twice():
    assert repeated_rows(DECISIONS.read_text(encoding="utf-8")) == []


def test_the_checks_find_what_a_union_merge_leaves():
    """Oct 23's cases, on a small file shaped like the real one: a decision kept in its old and
    its new form, one number taken by two PRs, and one by two tables (the old D18); and a
    verification row kept twice."""
    head = "| # | Decision | Why |\n|---|---|---|\n"
    log = "## Verification log\n\n| VERIFY | Result |\n|---|---|\n| x | ok |\n"

    def doc(*rows: str, verified: str = "", later: str = "| D9 | later | . |\n") -> str:
        kickoff = "## Implementation decisions after kickoff\n" + head + later
        return "# D\n\n## Decisions\n\n" + head + "".join(rows) + "\n" + log + verified + kickoff

    one, two, three = "| D1 | a | . |\n", "| D2 | b | . |\n", "| D3 | c | . |\n"
    clean = doc(one, two, three)
    assert numbering_problems(clean) == [] and repeated_rows(clean) == []
    assert decision_numbers(clean) == [1, 2, 3]  # not the later table's D9
    assert numbering_problems(doc(one, three)) == []  # a skip is fine
    amended = doc(one, two, "| D2 | b, amended | . |\n", three)
    assert numbering_problems(amended) == ["D2 appears 2 times"]
    assert repeated_rows(amended) == []  # the two forms differ: only the number tells
    taken = doc(one, two, three, "| D3 | another PR's | . |\n")
    assert numbering_problems(taken) == ["D3 appears 2 times"]
    assert numbering_problems(doc(one, three, two)) == ["D2 follows D3"]
    reused = doc(one, two, three, later="| D2 | another decision | . |\n")
    assert numbering_problems(reused) == ["D2 appears 2 times"]
    twice = doc(one, two, three, three)
    assert repeated_rows(twice) == [three.strip()]
    assert repeated_rows(doc(one, two, verified="| x | ok |\n")) == ["| x | ok |"]
