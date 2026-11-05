"""The suite's report (issue #263): one JSON file and a Markdown summary for the PR.

Every scenario records its outcome and its timings, each against the issue's threshold. A
timing marked `target` is recorded and shown but does not fail the run (the waker's 45 s).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

#: The issue's scenarios: number → (title, group). The `slow` ones stop the bridge or the VM,
#: or wait for minutes, so they run together, once per batch (`run.py --group slow`).
SCENARIOS: dict[int, tuple[str, str]] = {
    1: ("Basics, both agents", "fast"),
    2: ("Catch-up", "fast"),
    3: ("Watchdog", "slow"),
    4: ("Zulip outage", "slow"),
    5: ("Steering", "fast"),
    6: ("!vm and !usage (#204)", "fast"),
    7: ("Commit identity (#214)", "fast"),
    8: ("Lean (#205)", "fast"),
    9: ("File server (#256)", "fast"),
    10: ("Waker", "slow"),
    11: ("Idle stop", "slow"),
    12: ("zab history (#248), !fork (#202) and !btw (#716)", "fast"),
    13: ("Negative: zab-tester2 is ignored", "fast"),
    14: ("Turn begun during startup", "fast"),  # the bridge stopped for seconds (#305)
    15: ("zab sentinel: DM !vm and !restart (#204)", "slow"),
    16: ("Commands as people type them (#709)", "fast"),
}


def select(spec: str) -> list[int]:
    """`all`, `fast`, `slow`, or numbers and ranges (`1,2,5-7`) → scenario numbers, sorted."""
    chosen: set[int] = set()
    for part in filter(None, (p.strip() for p in spec.split(","))):
        if part == "all":
            chosen |= set(SCENARIOS)
        elif part in ("fast", "slow"):
            chosen |= {n for n, (_, group) in SCENARIOS.items() if group == part}
        else:
            lo, _, hi = part.partition("-")
            first, last = int(lo), int(hi or lo)
            numbers = set(range(first, last + 1))
            if not numbers or not numbers <= set(SCENARIOS):
                raise ValueError(f"no scenario {part!r} (1-{max(SCENARIOS)})")
            chosen |= numbers
    if not chosen:
        raise ValueError("no scenario selected")
    return sorted(chosen)


@dataclass
class Timing:
    name: str
    seconds: float
    low: float | None = None  # at least (idle stop: 10 minutes)
    high: float | None = None  # at most
    target: bool = False  # recorded, never a failure

    @property
    def ok(self) -> bool:
        return (self.low is None or self.seconds >= self.low) and (
            self.high is None or self.seconds <= self.high
        )

    def text(self) -> str:
        bounds = []
        if self.low is not None:
            bounds.append(f"≥ {self.low:g} s")
        if self.high is not None:
            bounds.append(f"≤ {self.high:g} s")
        limit = f" ({' and '.join(bounds)}{', target' if self.target else ''})" if bounds else ""
        mark = "" if not bounds else (" ✅" if self.ok else (" ⚠️" if self.target else " ❌"))
        return f"{self.name} {self.seconds:.1f} s{limit}{mark}"


@dataclass
class ScenarioResult:
    number: int
    name: str
    outcome: str = "passed"  # passed | failed | skipped
    seconds: float = 0.0
    timings: list[Timing] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    reason: str = ""  # why it failed or was skipped (one line)
    #: The parts (test functions) that were skipped, even when another part passed: a passed
    #: row with one is only partly tested (codex on #286)
    skipped_parts: list[str] = field(default_factory=list)
    #: Every part the scenario has (collected, selected or not) and the parts that passed: a
    #: part that never ran (a slow part left out of a fast run) keeps it incomplete
    parts: list[str] = field(default_factory=list)
    passed_parts: list[str] = field(default_factory=list)
    #: How the report names it, when not by its number (the admin suite's `A10`, #356)
    label: str = ""

    @property
    def shown(self) -> str:
        return self.label or str(self.number)

    def open_skips(self) -> list[str]:
        """Skipped parts that no other report of the build passed."""
        return [p for p in self.skipped_parts if p not in self.passed_parts]

    def unrun(self) -> list[str]:
        """Parts that neither passed nor were skipped: left out of the selection, or cut off."""
        done = set(self.passed_parts) | set(self.skipped_parts)
        return [p for p in self.parts if p not in done]

    @property
    def complete(self) -> bool:
        """Every part of the scenario ran and passed: what `--require` asks for."""
        return (
            self.effective == "passed"
            and not self.open_skips()
            and all(p in self.passed_parts for p in self.parts)
        )

    @property
    def partial(self) -> bool:
        """Nothing failed, but the scenario, or a part of it, was skipped: no result for it."""
        return self.effective == "skipped" or (
            self.effective == "passed" and bool(self.open_skips())
        )

    def timing(self, name: str, seconds: float, **limits: Any) -> Timing:
        t = Timing(name, round(seconds, 1), **limits)
        self.timings.append(t)
        return t

    @property
    def timings_ok(self) -> bool:
        return all(t.ok or t.target for t in self.timings)

    @property
    def effective(self) -> str:
        """The outcome the report counts: a scenario whose checks passed but which missed a
        threshold (a target aside) failed."""
        return "failed" if self.outcome == "passed" and not self.timings_ok else self.outcome

    def explanation(self) -> str:
        missed = [t.text() for t in self.timings if not (t.ok or t.target)]
        over = f"over its threshold: {', '.join(missed)}" if missed else ""
        return "; ".join(filter(None, [self.reason, over, *self.notes]))


_RANK = {"skipped": 0, "passed": 1, "failed": 2}


def combine(a: ScenarioResult, b: ScenarioResult) -> ScenarioResult:
    """One row from two parts of a scenario: the worse outcome, both parts' timings and notes."""
    return ScenarioResult(
        a.number,
        a.name,
        max(a.outcome, b.outcome, key=lambda o: _RANK.get(o, 2)),
        round(a.seconds + b.seconds, 1),
        a.timings + b.timings,
        a.notes + b.notes,
        "; ".join(filter(None, [a.reason, b.reason])),
        a.skipped_parts + b.skipped_parts,  # a skipped half stays visible under a passed one
        list(dict.fromkeys(a.parts + b.parts)),
        list(dict.fromkeys(a.passed_parts + b.passed_parts)),
        a.label or b.label,
    )


@dataclass
class Report:
    commit: str = ""
    wheel: str = ""
    vm: dict[str, str] = field(default_factory=dict)
    started: str = ""
    finished: str = ""
    deploy: str = ""  # the push_config outcome, one line
    results: list[ScenarioResult] = field(default_factory=list)
    #: What went wrong with the run itself (an aborted deploy, pytest's exit status): any fails it
    problems: list[str] = field(default_factory=list)
    #: Scenarios that must pass (`run.py --require`): a skip of one of these is a failure
    required: list[int] = field(default_factory=list)
    #: The summary's heading: the admin suite's report is another (#356)
    title: str = "End-to-end"

    def missed(self) -> list[int]:
        """The required scenarios not completely passed: skipped, a part of them skipped, or
        without a row at all."""
        complete = {r.number for r in self.results if r.complete}
        return [n for n in self.required if n not in complete]

    @property
    def verdict(self) -> str:
        """`FAIL`: a run problem, a scenario failed, or a required one did not pass every part
        (a part skipped, or never run: `missed`). Else `NO-RESULT`: no scenario passed, or one
        was skipped in whole or in part: the run vouches for less than was asked, so it is no
        merge gate (reviewer on #286). Else `PASS`. A part left out of the selection (a slow
        part in a fast run) counts only for a required scenario."""
        if self.problems or self.missed() or any(r.effective == "failed" for r in self.results):
            return "FAIL"
        passed = any(r.effective == "passed" for r in self.results)
        if not passed or any(r.partial for r in self.results):
            return "NO-RESULT"
        return "PASS"

    @property
    def passed(self) -> bool:
        return self.verdict == "PASS"

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")

    @classmethod
    def read(cls, path: Path) -> Report:
        data = json.loads(path.read_text(encoding="utf-8"))
        results = []
        for r in data.pop("results", []):
            timings = [Timing(**t) for t in r.pop("timings", [])]
            results.append(ScenarioResult(**r, timings=timings))
        return cls(**data, results=results)

    def merge(self, other: Report) -> None:
        """Add another run's results (the slow scenarios, run after the fast ones). A scenario in
        both keeps both halves (scenario 8's fast checks and its slow cold import, codex on
        #286): their timings and notes, and the worse outcome, so nothing that failed is lost.
        Both runs must have tested the same known build: a merge across commits or wheels, or
        with a run whose build is unknown, would credit one build with another's results
        (codex on #276 and #283), so it raises ValueError."""
        for name in ("commit", "wheel"):
            mine, theirs = getattr(self, name), getattr(other, name)
            if not mine or not theirs or mine != theirs:
                raise ValueError(
                    f"cannot merge reports of different or unknown builds: {name} "
                    f"{mine or '?'} / {theirs or '?'}"
                )
        self.problems = self.problems + other.problems
        self.required = sorted(set(self.required) | set(other.required))
        rows = {r.number: r for r in self.results}
        for row in other.results:
            mine = rows.get(row.number)
            rows[row.number] = row if mine is None else combine(mine, row)
        self.results = sorted(rows.values(), key=lambda r: r.number)

    def markdown(self) -> str:
        counts = {o: sum(r.effective == o for r in self.results) for o in ("passed", "failed")}
        skipped = sum(r.effective == "skipped" for r in self.results)
        verdict = self.verdict
        vm = ", ".join(f"{k} {v}" for k, v in self.vm.items())
        lines = [
            f"## {self.title}: {verdict} ({counts['passed']} passed, {counts['failed']} failed, "
            f"{skipped} skipped)",
            "",
            f"Commit `{self.commit[:12]}` · wheel `{self.wheel}` · {vm}",
            f"Started {self.started} · finished {self.finished}",
        ]
        if self.deploy:
            lines.append(f"Deploy: {self.deploy}")
        lines += [f"**Run problem:** {p}" for p in self.problems]
        if self.required:
            names = {r.number: r.shown for r in self.results}
            missed = [names.get(n, str(n)) for n in self.missed()]
            lines.append(
                f"Required: {', '.join(names.get(n, str(n)) for n in self.required)}"
                + (f" · **not passed: {', '.join(missed)}**" if missed else "")
            )
        lines += ["", "| # | Scenario | Result | Time | Timings |", "|---|---|---|---|---|"]
        icon = {"passed": "✅ pass", "failed": "❌ FAIL", "skipped": "⏭ skip"}
        for r in sorted(self.results, key=lambda r: r.number):
            timings = "<br>".join(t.text() for t in r.timings) or "-"
            lines.append(
                f"| {r.shown} | {r.name} | {icon.get(r.effective, r.effective)} | "
                f"{r.seconds:.0f} s | {timings} |"
            )
        skips = [r for r in self.results if r.effective != "failed" and not r.complete]
        if skips:
            lines += ["", "### Not completely run (no result for these parts)", ""]
            for r in skips:
                said = [f"skipped: {', '.join(r.open_skips())}"] if r.open_skips() else []
                said += [f"not run: {', '.join(r.unrun())}"] if r.unrun() else []
                parts = f" ({'; '.join(said)})" if said else ""
                lines.append(f"- **{r.shown}. {r.name}**{parts}: {r.reason or 'no reason given'}")
        details = [r for r in self.results if r.explanation() and r not in skips]
        if details:
            lines += ["", "### Details", ""]
            for r in details:
                lines.append(f"- **{r.shown}. {r.name}** ({r.effective}): {r.explanation()}")
        return "\n".join(lines) + "\n"
