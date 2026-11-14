"""Background jobs (SPEC §7.14, #655): a job's directory, its record and how it ended, and the
systemd calls for its unit. `zab job` writes them; the bridge's watcher and commands read them.

A job is the transient service `zab-job-<id>.service` of the user's systemd manager, in
`zab-jobs.slice`, and the directory `<state_dir>/jobs/<id>/` (mode 0700) holds:

- `job.json` (0600, `schema` 1): the id, the name, the bot, session and Codex thread that
  started it (`ZAB_BOT`, `ZAB_SESSION`, `CODEX_THREAD_ID`), `attached` (the session's id when
  `zab job start` attached the job, else null: decided once, whatever the session does later),
  its argument list, directory and scrubbed environment, its start time and boot, `report` and
  `max_hours`;
- `output.log`, the job's stdout and stderr, at most `LOG_CAP` bytes;
- `ended.json`, written by `zab job _ended` (`ExecStopPost`): systemd's `SERVICE_RESULT`,
  `EXIT_CODE` and `EXIT_STATUS`, the time, the boot id, the user manager's state (`stopping`
  at a clean shutdown) and a copy of `stopped.json` where there is one;
- `stopped.json` when a person (`!stop`) or the agent (`zab job stop`) stopped it: `by` and
  `at`, written before the stop, and removed again if the stop fails;
- `seen` once `zab job` showed the agent that it had ended.

What systemd reports, as #655's probe saw it (systemd 255): an exit is `exited` with its status
(`success`, or `exit-code` for a non-zero one); a signal to the main process `signal`, `killed`
and the signal's name; a stop `success`, `killed`, `TERM`; the cap (`RuntimeMaxSec`) `timeout`,
`killed`, `TERM`; a stop that needed SIGKILL after `TimeoutStopSec` `timeout`, `killed`, `KILL`,
so a stop is known by `stopped.json`, never by the result; running out of memory `oom-kill`.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import math
import os
import re
import tempfile
import time
from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from zab.config import BOT_NAME_RE
from zab.tools.common import Runner, RunResult, subprocess_runner

SCHEMA = 1
NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
ID_RE = re.compile(r"[0-9a-f]{12}")
#: `!stop jobs` stops them all, so no job takes the name (§7.14)
RESERVED_NAMES = frozenset({"jobs"})
UNIT_PREFIX = "zab-job-"
SLICE = "zab-jobs.slice"
LOG_CAP = 1 << 30
#: Removed from a job's environment, with Claude's turns' `SCRUBBED_ENV` and `NESTING_ENV`
SENSITIVE_WORDS = ("TOKEN", "SECRET", "KEY", "PASSWORD")
#: The unit's states in which its job still runs, or its `ExecStopPost` hasn't finished yet
PRESENT = frozenset({"active", "activating", "deactivating", "reloading", "refreshing"})
#: A path that may go into a unit's command line: systemd expands `$` and `%` there
SAFE_PATH_RE = re.compile(r"/[A-Za-z0-9._/+-]+")
BOOT_ID = "/proc/sys/kernel/random/boot_id"
#: `ZAB_SESSION` as the core writes it (`sessions.id`, a uuid4): any agent can set the variable,
#: so anything else is no session, and the job runs unattached (§7.14)
SESSION_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
#: `CODEX_THREAD_ID`, which Codex puts in every exec process: a thread id, a UUID
THREAD_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
#: A bot's name, the config's own pattern (Codex round 6 on #928: `_claude` is a valid name),
#: and a boot id (the kernel's is a UUID): plain tokens, never markup
BOT_RE = BOT_NAME_RE
BOOT_RE = re.compile(r"[A-Za-z0-9-]{1,64}")
LOG = logging.getLogger("zab.jobs")
#: `EXIT_STATUS` as zab repeats it: an exit status or a signal's name, ASCII only, since it
#: reaches the end post (review round 1 on #928: `@**all**` would ping everyone)
STATUS_RE = re.compile(r"[A-Z0-9]{1,16}")
#: How long a job may show no unit and no `ended.json` before it counts as ended: `zab job
#: start` writes `job.json` just before `systemd-run` (§7.14, "a minute later")
UNSEEN_GRACE_S = 60.0


def finite(value: Any) -> bool:
    """A real number: not a bool, not `NaN`, not an infinity, and no integer too large for a
    float (JSON's integers have any size, and `math.isfinite` raises on one of 400 digits:
    review round 4 on #928)."""
    if not isinstance(value, int | float) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def boot_id(path: str = BOOT_ID) -> str:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


def jobs_dir(state_dir: str | Path) -> Path:
    return Path(state_dir).expanduser() / "jobs"


def unit(job_id: str) -> str:
    return f"{UNIT_PREFIX}{job_id}.service"


def scrubbed(env: Mapping[str, str]) -> dict[str, str]:
    """A job's environment: the caller's, less what Claude's turns drop (§9.1) and any variable
    whose name says it holds a credential."""
    from zab.backends.claude import NESTING_ENV, SCRUBBED_ENV

    dropped = {*SCRUBBED_ENV, *NESTING_ENV}
    return {
        k: v
        for k, v in env.items()
        if k not in dropped and not any(word in k.upper() for word in SENSITIVE_WORDS)
    }


def session_of(env: Mapping[str, str]) -> str:
    """The caller's `ZAB_SESSION` if it is a session id, else "": no topic."""
    value = env.get("ZAB_SESSION", "")
    return value if SESSION_RE.fullmatch(value) else ""


def thread_of(env: Mapping[str, str]) -> str:
    """The caller's `CODEX_THREAD_ID` if it is a thread id, else "". Recorded, not used: SPEC
    §7.14 attaches a job by `ZAB_SESSION` alone. A later change could find with it a Codex
    job's session after `!detach` and `!attach`, which leave a loaded thread the old
    `ZAB_SESSION` (§5.6's exception)."""
    value = env.get("CODEX_THREAD_ID", "")
    return value if THREAD_RE.fullmatch(value) else ""


def user_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """An environment in which `systemctl --user` reaches the manager: `XDG_RUNTIME_DIR` set
    when it is missing, as in a Codex exec session (#655 V1: without it, and without the bus
    address, `systemd-run --user` fails with "No medium found")."""
    out = dict(os.environ if env is None else env)
    out.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return out


def write_json(path: Path, data: Mapping[str, Any], mode: int = 0o600) -> None:
    """Written whole or not at all: a reader never sees half a file, and two writers (`!stop`
    and `zab job stop`) each write a temporary file of their own (review round 1 on #928)."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


@dataclass
class Job:
    id: str
    name: str
    argv: list[str]
    cwd: str
    env: dict[str, str] = field(default_factory=dict)
    bot: str = ""
    session: str = ""
    codex_thread: str = ""
    #: The session the job belongs to (`sessions.id`), as `zab job start` decided it (§7.14,
    #: "Which topic it belongs to"); None: unattached, so nothing is posted about it
    attached: str | None = None
    started_at: float = 0.0
    boot_id: str = ""
    report: bool = True
    max_hours: float = 12.0
    schema: int = SCHEMA
    directory: Path = field(default=Path(), compare=False, repr=False)

    @property
    def log(self) -> Path:
        return self.directory / "output.log"

    def save(self) -> None:
        record = {k: v for k, v in asdict(self).items() if k != "directory"}
        write_json(self.directory / "job.json", record)

    @classmethod
    def load(cls, directory: Path) -> Job | None:
        """The job in `directory`, or None for anything else there; keys of a later schema are
        left out, so that a newer `zab job`'s record still reads."""
        data = read_json(directory / "job.json")
        if not data or not ID_RE.fullmatch(directory.name) or data.get("id") != directory.name:
            return None
        known = {f.name for f in fields(cls)} - {"directory"}
        try:
            job = cls(**{k: v for k, v in data.items() if k in known}, directory=directory)
        except TypeError:
            return None
        return job if job.typed() else None

    def typed(self) -> bool:
        """Whether each field has its type and, for an identifier, its form (review round 1 on
        #928; review of #940, B1 and B2): a record with a wrong one is no job, so that sorting,
        `ending()`, `_exec` and every post or line that names a job never meet it. Every reader
        goes through here: `zab job`, the watcher, `!status`. A number is finite (`NaN` and
        `Infinity` load from JSON), and the name, the session, the thread, the attachment, the
        bot and the boot have their patterns, so that a `job.json` edited by hand can't put
        markup such as `` x` @**all** `y `` into a post."""

        def strings(value: Any) -> bool:
            return isinstance(value, str)

        def number(value: Any) -> bool:
            return finite(value)

        def matches(pattern: re.Pattern[str], value: Any, *, empty: bool = True) -> bool:
            return strings(value) and ((empty and value == "") or bool(pattern.fullmatch(value)))

        return (
            all(strings(v) for v in (self.id, self.cwd))
            and matches(NAME_RE, self.name, empty=False)
            and matches(SESSION_RE, self.session)
            and matches(THREAD_RE, self.codex_thread)
            and (self.attached is None or matches(SESSION_RE, self.attached, empty=False))
            and matches(BOT_RE, self.bot)
            and matches(BOOT_RE, self.boot_id)
            and isinstance(self.argv, list)
            and bool(self.argv)
            and all(strings(a) for a in self.argv)
            and isinstance(self.env, dict)
            and all(strings(k) and strings(v) for k, v in self.env.items())
            and number(self.started_at)
            and number(self.max_hours)
            and self.max_hours > 0
            and isinstance(self.report, bool)
        )

    def ended(self) -> dict[str, Any] | None:
        """`ended.json`, its `at` left out unless it is a finite number: the ending still counts,
        with no time (review of #940, B1)."""
        data = read_json(self.directory / "ended.json")
        if data is not None and "at" in data and not finite(data["at"]):
            del data["at"]
        return data

    def stopped(self) -> dict[str, Any] | None:
        return read_json(self.directory / "stopped.json")

    @property
    def seen(self) -> bool:
        return (self.directory / "seen").exists()

    def mark_seen(self) -> None:
        with contextlib.suppress(OSError):
            (self.directory / "seen").touch()


#: The job directories whose record didn't read, warned of once each
_WARNED: set[str] = set()


def all_jobs(directory: Path) -> list[Job]:
    """Every job in the jobs directory, oldest first. A directory named like a job whose record
    doesn't read is logged and left out: this never raises (review round 1 on #928)."""
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return []
    found = []
    for entry in entries:
        if (job := Job.load(entry)) is not None:
            found.append(job)
        elif (
            ID_RE.fullmatch(entry.name)
            and (entry / "job.json").exists()
            and entry.name not in _WARNED
        ):
            _WARNED.add(entry.name)  # once per record, not at every pass (review round 2)
            LOG.warning("Background job %s: its job.json doesn't read; left out", entry.name)
    return sorted(found, key=lambda job: job.started_at)


@dataclass(frozen=True)
class Ending:
    """How a job ended, in zab's own words, as its end post says them (§7.14)."""

    kind: str  # finished, failed, signal, limit, oom, stopped, lost, unknown
    words: str
    ok: bool = False


def limit_words(max_hours: float) -> str:
    """`12-hour`, `90-minute`: the cap's length as the end post names it."""
    if float(max_hours).is_integer():
        return f"{int(max_hours)}-hour"
    return f"{max(1, round(max_hours * 60))}-minute"


def ending(job: Job, *, running: bool, boot: str, now: float | None = None) -> Ending | None:
    """How `job` ended, or None while it runs. Without `ended.json`, a job whose unit is gone
    ended unseen: cut off by the VM's stop when it started in another boot, else unknown once
    `UNSEEN_GRACE_S` have passed since its start (before, its unit may not be started yet)."""
    ended = job.ended()
    if ended is None:
        if running:
            return None
        if job.boot_id and boot and job.boot_id != boot:
            return Ending("lost", "was cut off when the VM stopped")
        if (time.time() if now is None else now) - job.started_at < UNSEEN_GRACE_S:
            return None
        return Ending("unknown", "ended, but zab couldn't tell how")
    # `_ended` copied `stopped.json` when it ran: a stop asked later, while it ran, stopped
    # nothing, and the job's own ending stands (review round 1 on #928)
    if ended.get("stopped"):
        return Ending("stopped", "was stopped")
    if ended.get("manager_state") == "stopping":
        # a clean shutdown stopped the manager, and so the job, which may then read as the
        # cap or out of memory (`killed/KILL/timeout` past `TimeoutStopSec`): cut off (§7.14)
        return Ending("lost", "was cut off when the VM stopped")
    result, code, status = (
        str(ended.get(k) or "") for k in ("SERVICE_RESULT", "EXIT_CODE", "EXIT_STATUS")
    )
    if not STATUS_RE.fullmatch(status):
        status = ""  # never free text in an end post
    if result == "oom-kill":
        return Ending("oom", "ran out of memory and was stopped")
    if result == "timeout":
        return Ending("limit", f"reached its {limit_words(job.max_hours)} limit and was stopped")
    if code == "exited" and status.isdigit():
        if int(status) == 0:
            return Ending("finished", "finished: exit 0", ok=True)
        return Ending("failed", f"failed: exit {int(status)}")
    if code in ("killed", "dumped") and status:
        name = f"SIG{status}" if status.isalpha() else f"signal {status}"
        return Ending("signal", f"was killed by signal {name}")
    return Ending("unknown", "ended, but zab couldn't tell how")


def span(seconds: float) -> str:
    """`12 s`, `41 min`, `2 h 13 min`: a job's length."""
    s = max(0, int(seconds))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    hours, minutes = divmod(s // 60, 60)
    return f"{hours} h {minutes} min" if minutes else f"{hours} h"


def unit_argv(zab: str, job_id: str, max_hours: float, directory: Path) -> list[str]:
    """The `systemd-run` that starts a job (§7.14), built only from values zab makes itself: its
    own path, the cap, and the job's directory, absolute, in the jobs directory `directory`.
    `_exec` and `_ended` take that directory as their argument: a service of the user manager
    has neither the caller's environment nor its `ZAB_CONFIG`, so they never look for it."""
    if not ID_RE.fullmatch(job_id):
        raise ValueError(f"not a job id: {job_id!r}")
    job = str(Path(directory).absolute() / job_id)
    for path in (zab, job):
        if not SAFE_PATH_RE.fullmatch(path) or ".." in Path(path).parts:
            raise ValueError(f"this path can't go into a systemd unit: {path!r}")
    seconds = max(1, round(max_hours * 3600))
    return [
        "systemd-run", "--user", f"--unit={UNIT_PREFIX}{job_id}", f"--slice={SLICE}",
        "--collect", "--quiet", f"--property=RuntimeMaxSec={seconds}",
        "--property=OOMScoreAdjust=500", "--property=Nice=10", "--property=NoNewPrivileges=yes",
        "--property=TimeoutStopSec=15", "--property=MemoryAccounting=yes",
        "--property=CPUAccounting=yes", f"--property=ExecStopPost={zab} job _ended {job}",
        "--", zab, "job", "_exec", job,
    ]  # fmt: skip


#: The job units, as `systemctl --user list-units` lists them: an argument list
LIST_UNITS = [
    "systemctl", "--user", "list-units", "--all", "--plain", "--no-legend", "--no-pager",
    f"{UNIT_PREFIX}*",
]  # fmt: skip


def units_from(stdout: str) -> set[str]:
    """The job units that `LIST_UNITS`' answer shows running, or still ending (`PRESENT`): the
    one reading of it, for the watcher, the bridge's `!vm` and the sentinel's (#941)."""
    out = set()
    for line in stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0].startswith(UNIT_PREFIX) and parts[2] in PRESENT:
            out.add(parts[0])
    return out


def present_units(runner: Runner = subprocess_runner) -> set[str] | None:
    """The job units that run (or are still ending), or None when no user manager answers."""
    done = runner(LIST_UNITS, env=user_env(), timeout_s=30)
    return units_from(done.stdout) if done.ok else None


def stop(job: Job, by: str, runner: Runner = subprocess_runner, *, block: bool = True) -> RunResult:
    """Stop `job`'s unit, its whole cgroup, after recording who stopped it (`stopped.json`, which
    tells a stop from the cap: both can read `timeout`), which goes again if the stop fails, as
    for a unit no longer loaded, so that the job's own ending stands (Codex round 1 on #928).
    `block=False` returns at once: a stop waits up to `TimeoutStopSec`, then for
    `ExecStopPost`."""
    record = job.directory / "stopped.json"
    write_json(record, {"by": by, "at": time.time()})
    argv = ["systemctl", "--user", "stop", *([] if block else ["--no-block"]), unit(job.id)]
    done = runner(argv, env=user_env(), timeout_s=60)
    if not done.ok:
        record.unlink(missing_ok=True)
    return done


@contextlib.contextmanager
def locked(directory: Path) -> Iterator[None]:
    """`<jobs>/.lock`, held while `zab job start` counts the running jobs and starts one."""
    fd = os.open(directory / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)
