"""Background jobs as the topic commands see them (SPEC §4.6, §7.14; #655): `!stop NAME`,
`!stop jobs`, and the line that `!stop` adds about the jobs that keep running.

The jobs are `zab job`'s, and the bridge learns of them from its watcher; the commands reach
them through `JobControl`. One serves every bot of a bridge process, the watcher
(`zab.core.jobwatch.JobWatcher`, which `Runtime` gives each bridge before its actors exist),
since the answer for a topic asks about other bots' sessions (§4.6). A bridge without one, as
before jobs, has `NO_JOBS`: no session has a running job, so `!stop NAME` answers that none
runs and `!stop` adds nothing.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from zab import jobs as zjobs
from zab.jobs import span

__all__ = ["span"]  # a job's length, as zab.jobs words it

#: A job's name (§7.14), as `zab job start` takes it
NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
#: `!stop jobs` stops all of the session's jobs, so no job takes this name
ALL = "jobs"


@dataclass(frozen=True, slots=True)
class RunningJob:
    """A running job of a session: its id (12 hex digits), name and start (unix seconds)."""

    id: str
    name: str
    started_at: float


class JobControl(Protocol):
    #: Where the jobs' directories are (`<state_dir>/jobs`), for their logs; None without jobs
    directory: Path | None

    def running(self, session_id: str) -> list[RunningJob]:
        """The session's running jobs, oldest first."""
        ...

    def jobs_of(self, session_id: str, since: float) -> list[dict[str, Any]]:
        """The session's jobs running, or ended since `since`, oldest first, as the store's
        `jobs` rows (§6.2): topic `!status`'s lines."""
        ...

    async def unrecorded(self, session_id: str) -> list[RunningJob]:
        """The session's running jobs that have no row yet (a job's first seconds)."""
        ...

    async def stop(self, session_id: str, job: RunningJob) -> float | None:
        """Stop `job` for `!stop`: its `stopped.json` first (`by: "!stop"`), then its unit,
        whose whole cgroup ends (§7.14). The seconds it ran, once its unit has stopped; None if
        it wasn't running any more or the stop failed, its `stopped.json` then removed."""
        ...


class NoJobs:
    """No session has a job: a bridge without a watcher."""

    directory: Path | None = None

    def running(self, session_id: str) -> list[RunningJob]:
        return []

    def jobs_of(self, session_id: str, since: float) -> list[dict[str, Any]]:
        return []

    async def unrecorded(self, session_id: str) -> list[RunningJob]:
        return []

    async def stop(self, session_id: str, job: RunningJob) -> float | None:
        return None


NO_JOBS = NoJobs()


def stop_target(command: str) -> str | None:
    """The word after `!stop`: a job's name, or `jobs`. None for `!stop` alone, any other
    command, or more than one word after it (`!stop that now`), which stays unknown."""
    verb, _, rest = command.partition(" ")
    words = rest.split()
    return words[0] if verb == "!stop" and len(words) == 1 else None


def listed(words: Sequence[str]) -> str:
    """`a`, `a and b`, `a, b and c`."""
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def keeps_running(jobs: Sequence[RunningJob]) -> str:
    """What `!stop` adds where the session's jobs run on: it stops the turn only (D44)."""
    if len(jobs) == 1:
        name = jobs[0].name
        how = f"`!stop {name}` stops it" if NAME_RE.fullmatch(name) else "`!stop NAME` stops it"
        return (
            f"Background job {name_code(name)} keeps running: {how}, and `!stop jobs` stops all "
            "of this topic's jobs."
        )
    names = listed([name_code(job.name) for job in jobs])
    return (
        f"Background jobs {names} keep running: `!stop NAME` stops one, and `!stop jobs` stops "
        "all of this topic's jobs."
    )


def stopped(done: Sequence[tuple[RunningJob, float]], failed: Sequence[RunningJob] = ()) -> str:
    """The reply to `!stop NAME` or `!stop jobs`, once the units have stopped: the end post of
    each job it stopped (§7.14), and one line for those it couldn't stop."""
    lines = []
    if len(done) == 1:
        job, seconds = done[0]
        lines.append(f"⏹ Stopped background job {name_code(job.name)} after {span(seconds)}.")
    elif done:
        each = listed([f"{name_code(job.name)} after {span(seconds)}" for job, seconds in done])
        lines.append(f"⏹ Stopped background jobs {each}.")
    if failed:
        names = listed([name_code(job.name) for job in failed])
        lines.append(f"⚠️ Couldn't stop {names}: send the command again.")
    return "\n".join(lines)


def none_running(target: str, code: str) -> str:
    """`!stop NAME` or `!stop jobs` where the session has no such job. `code` is the word as a
    code span, made by the caller from the person's text (`presentation.code`)."""
    if target == ALL:
        return "No background job is running here."
    return f"No background job {code} is running here."


#: How long topic `!status` lists a job after its end (§7.14)
STATUS_WINDOW_S = 24 * 3600
#: The endings a status line gives a length for
TIMED = frozenset({"finished", "failed", "signal", "stopped"})
#: A row's ending in words, where its directory is gone (`jobs.result`, `zab.jobs.Ending.kind`)
KIND_WORDS = {
    "finished": "finished",
    "failed": "failed",
    "signal": "was killed by a signal",
    "limit": "reached its limit and was stopped",
    "oom": "ran out of memory and was stopped",
    "stopped": "was stopped",
    "lost": "was cut off when the VM stopped",
    "unknown": "ended, but zab couldn't tell how",
}


def when(seconds: float) -> str:
    """A Zulip time, as the other status lines give one; a time that doesn't convert (a
    hand-written `job.json`'s `1e300` or NaN) is "an unknown time", never an error."""
    try:
        return f"<time:{datetime.fromtimestamp(seconds, UTC).isoformat()}>"
    except (OverflowError, ValueError, OSError):
        return "an unknown time"


def name_code(name: str) -> str:
    """A job's name as a code span: as written when the pattern allows it, as `zab job start`
    does, else made safe (`presentation.code`), since anything running as the agent's user can
    write a `job.json` (#941's review)."""
    from zab.core import presentation as view

    return f"`{name}`" if NAME_RE.fullmatch(name) else view.code(name)


#: Topic `!status`'s jobs shown of each kind, the newest: running, and ended (§7.14)
STATUS_RUNNING = STATUS_ENDED = 10


def status_lines(
    rows: Sequence[dict[str, Any]], directory: Path | None, boot: str | None = None
) -> list[str]:
    """Topic `!status`'s lines for `rows` (§7.14): the newest `STATUS_RUNNING` running jobs, with
    their start and log, then the newest `STATUS_ENDED` ended ones, with their ending in the end
    post's words and its time, and a line for the rest of each, so that a busy day's `!status`
    still fits a message. It reads the jobs' files, so it runs off the event loop.

    ⚙️ Job `rank-search` · running since <time:…> · log `<state_dir>/jobs/<id>/output.log`
    ⚙️ Job `build` · finished: exit 0 after 41 min · ended <time:…>
    … and 12 more ended in the last 24 hours
    """
    boot = zjobs.boot_id() if boot is None else boot
    running = [row for row in rows if row["state"] == "running"]
    ended = [row for row in rows if row["state"] != "running"]
    started = sorted(running, key=lambda r: r["started_at"])[-STATUS_RUNNING:]
    shown = sorted(ended, key=lambda r: r["ended_at"] or r["started_at"])[-STATUS_ENDED:]
    lines = []
    for row in started:
        line = f"⚙️ Job {name_code(row['name'])} · running since {when(row['started_at'])}"
        if directory is not None:
            line += f" · log `{directory / row['id'] / 'output.log'}`"
        lines.append(line)
    if len(running) > len(started):
        lines.append(f"… and {len(running) - len(started)} more running")
    for row in shown:
        lines.append(f"⚙️ Job {name_code(row['name'])} · {ended_words(row, directory, boot)}")
    if len(ended) > len(shown):
        lines.append(f"… and {len(ended) - len(shown)} more ended in the last 24 hours")
    return lines


def ended_words(row: dict[str, Any], directory: Path | None, boot: str) -> str:
    """`finished: exit 0 after 41 min · ended <time:…>`. The row's kind is the watcher's
    record; the job's `ended.json`, while it is kept, gives the same kind's words with their
    numbers (`failed: exit 1`)."""
    ended = row["ended_at"] or row["started_at"]
    kind = row["result"] or "unknown"
    job = zjobs.Job.load(directory / row["id"]) if directory is not None else None
    ending = None
    if job is not None and job.ended() is not None:
        ending = zjobs.ending(job, running=False, boot=boot, now=ended)
    if ending is not None and ending.kind == kind:
        words = ending.words
    else:
        words = KIND_WORDS.get(kind, KIND_WORDS["unknown"])
    if kind in TIMED and row["ended_at"]:
        words += f" after {span(row['ended_at'] - row['started_at'])}"
    return f"{words} · ended {when(ended)}"


def count_text(running: int) -> str:
    """`1 job running`, `2 jobs running` (§13.5), or "" for none."""
    if not running:
        return ""
    return f"{running} job{'' if running == 1 else 's'} running"


# ------------------------------------------- the machine's running jobs, for `!vm` (§13.5)


def unended(directory: Path) -> list[zjobs.Job]:
    """The jobs in `directory` without their `ended.json`, oldest first. It reads files."""
    return [job for job in zjobs.all_jobs(directory) if job.ended() is None]


def still_running(
    jobs: Sequence[zjobs.Job], answered: bool, units: str, boot: str
) -> list[zjobs.Job]:
    """Of `unended` jobs, those that run: whose unit `LIST_UNITS`' answer shows. With no answer
    from the manager, those of this boot: a job of an earlier boot without its `ended.json`
    was cut off (#941's review, N1)."""
    if answered:
        present = zjobs.units_from(units)
        return [job for job in jobs if zjobs.unit(job.id) in present]
    return [job for job in jobs if job.boot_id == boot]


def job_entry(name: str, started_at: float, memory: str, topic: str | None = None) -> str:
    """One running job in DM `!vm`'s list: `rank-search` in <topic> since <time>, 1.2 GB memory.
    The sentinel, which doesn't read the bridge's store, gives no topic."""
    where = f" in {topic}" if topic else ""
    return f"{name_code(name)}{where} since {when(started_at)}, {memory}"


def jobs_line(entries: Sequence[str]) -> str:
    return "Background jobs: " + " · ".join(entries)


def memory_argv(job_id: str) -> list[str] | None:
    """`systemctl --user show` of a job's `MemoryCurrent`, an argument list; None for anything
    but a job's id, which never names another unit."""
    if not zjobs.ID_RE.fullmatch(job_id):
        return None
    return ["systemctl", "--user", "show", "-p", "MemoryCurrent", "--value", zjobs.unit(job_id)]


def memory_text(ok: bool, stdout: str) -> str:
    """`1.2 GB memory` from that command's answer, or `memory unavailable` (`[not set]`, a unit
    gone, no answer)."""
    from zab.tools import vmstat

    value = stdout.strip()
    if not ok or not value.isdigit():
        return "memory unavailable"
    return f"{vmstat.size(int(value))} memory"
