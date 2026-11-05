"""Collects the scenarios' results during a pytest run of the suite (issue #263).

A scenario may have several test functions (both agents, or `zab history` and `!fork`): they
share one row. The row failed when any part failed, passed when any part passed and none
failed, and was skipped when every part was; the parts' timings, notes and times add up.
"""

from __future__ import annotations

from collections.abc import Mapping

from e2e.harness.report import SCENARIOS, Report, ScenarioResult

_RANK = {"skipped": 0, "passed": 1, "failed": 2}


class Recorder:
    def __init__(
        self, titles: Mapping[int, str] | None = None, labels: Mapping[int, str] | None = None
    ) -> None:
        """The #263 scenarios by default; the admin suite (#356) passes its own, labelled."""
        self.titles = titles if titles is not None else {n: t for n, (t, _) in SCENARIOS.items()}
        self.labels = labels or {}
        self.rows: dict[int, ScenarioResult] = {}
        self._seen: set[int] = set()
        #: What went wrong with the run itself: a clean-up that failed (`conftest`, #517)
        self.problems: list[str] = []
        self._stopped = ""  # why the run stopped early, for the scenarios it never ran

    def row(self, number: int) -> ScenarioResult:
        """The scenario's row, for a test to record its timings and notes in."""
        if number not in self.titles:
            raise ValueError(f"no scenario {number}")
        if number not in self.rows:
            label = self.labels.get(number, "")
            self.rows[number] = ScenarioResult(
                number, self.titles[number], outcome="skipped", label=label
            )
        return self.rows[number]

    def expect(self, number: int, parts: list[str]) -> None:
        """Every part the scenario has, selected or not (`conftest`): one never run keeps the
        scenario incomplete for `--require` (codex on #286)."""
        row = self.row(number)
        row.parts = list(dict.fromkeys(row.parts + parts))

    def finish(self, number: int, part: str, outcome: str, seconds: float, reason: str) -> None:
        """One test function of the scenario is over: fold its outcome into the row."""
        row = self.row(number)
        row.seconds = round(row.seconds + seconds, 1)
        first = number not in self._seen
        self._seen.add(number)
        if first or _RANK[outcome] > _RANK[row.outcome]:
            row.outcome = outcome
        if outcome == "skipped" and part not in row.skipped_parts:
            row.skipped_parts.append(part)  # stays visible when another part passes
        if outcome == "passed" and part not in row.passed_parts:
            row.passed_parts.append(part)
        if outcome == "failed" and part in row.passed_parts:  # a teardown failed after the call
            row.passed_parts.remove(part)
        if reason and outcome != "passed":
            row.reason = "; ".join(filter(None, [row.reason, f"{part}: {reason}"]))

    def stop(self, problem: str, *, why: str) -> None:
        """The run stops before every scenario ran (#517): `problem` is a run problem, and a
        selected scenario not begun by then gives `why` as its reason."""
        self.problems.append(problem)
        self._stopped = self._stopped or why

    def report(self) -> Report:
        if self._stopped:
            for number, row in self.rows.items():
                if number not in self._seen and not row.reason:
                    row.reason = self._stopped
        rows = [self.rows[n] for n in sorted(self.rows)]
        return Report(results=rows, problems=list(self.problems))
