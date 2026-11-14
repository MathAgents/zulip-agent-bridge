"""The background jobs' watcher (SPEC §7.14, §6.2; #655): the bridge's side of `zab job`.

One `JobWatcher` per bridge process (`Runtime`), shared by all its bots: it reads
`<state_dir>/jobs/` every `PASS_S` seconds, keeps the store's `jobs` table, and handles each
job's end once:

- **Which topic.** A job belongs to the session `zab job start` recorded in `job.json`
  (`attached`) when that session is the job's bot's, in the store, and a turn of it was running
  at the job's start; otherwise it is unattached, and nothing is posted about it.
- **Its end** is `zab.jobs.ending`: `ended.json`, or no unit and no `ended.json` (cut off by
  a VM stop when the job started in another boot, else unknown). The first pass after a start
  handles the ends missed while the bridge was down, the jobs a VM stop cut off included.
- **The end post**, one new message in the session's topic at its card's location, a bridge
  note never edited (#754), mentioning the owner of each job (the sender of the message that
  started the turn the job started in): one post per session for the ends one pass finds. No
  post for an unattached job, nor for one stopped (`!stop NAME` replies itself; the agent's
  `zab job stop` was in its turn). A job that ends while the turn that started it still runs
  waits for that turn's end, and isn't posted if `zab job` showed the agent its end meanwhile
  (`seen`). A detached session's post still goes to its topic. A post whose card can't be
  confirmed (SPEC §4.4), or that Zulip refused or whose outcome is unknown, is tried again at
  about 1, 3, 7, 15 and 31 minutes, never once a pass (Zulip Cloud's limits, #754), then
  dropped with a log line within the hour. Its tries are kept in the store (`job_posts`), each
  counted before it goes out, so that they survive a restart: a send whose outcome isn't known
  (a failure, a crash, a restart, a write that failed after it) is first looked for, by the
  exact tag it carried, where it went and at the card's location now, and isn't sent again if
  found. It is dropped at once when the card is found gone or in a public channel (a read-only
  session). An end that doesn't read when its post's hour runs out is handled then with no
  post, and the hook never hears of it: the hook takes the end, which can't be read; the log
  line names it.
- **The bound on duplicates** (Codex round 9). Recovery after a send of unknown outcome is
  best effort: the look misses a post that landed among more than `LANDED_LOOK` newer messages
  of the bot where it went, or that a person moved elsewhere. Its worst case is one duplicate
  end post for a send whose outcome was lost and whose look missed, and another only if that
  happens again on a later send. A post is sent at most 6 times, within its hour (`RETRY_S`);
  with every post refused and a steady stream of ends, one session sees about 12 sends an
  hour, as each post's row ends at its 6th failure and the next post starts its own. End posts
  are never edited, so a duplicate is harmless.
- **Only this process's bots.** Two bridge processes with different bots may share one state
  directory (the locks are per bot): each watcher keeps and handles only its own bots' jobs.
- **What follows the post** is the `EndHook`'s: the post's last sentence, and the report turn
  or the note that waits (#655's report turn, Appendix A.3). Without one (`NoReports`), the post
  ends with the job's end.
- **Retention:** a job's directory and row go 14 days after its end was handled. A job with no
  row of anyone's (`zab job start` in an ordinary shell, or a bot no process runs any more) has
  no post, and any watcher removes its directory 14 days after its end, its cap's end for one
  that left no `ended.json`.
- **One job's trouble is its own:** a job whose end can't be read, handled or posted is logged
  once and skipped, and every other job is handled.

It is also the bridge's `JobControl` for `!stop NAME` and `!stop jobs` (#934), for every
session of the machine: one instance per process, given to every bridge before its
`initialize()`, so that the actors made at start have it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import shutil
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from zab import jobs as zjobs
from zab.core import presentation as view
from zab.core import unmentioned
from zab.core.jobcontrol import RunningJob, still_running, unended
from zab.tools.common import Runner, subprocess_runner
from zab.zulip.client import ZulipError
from zab.zulip.poster import ACK

if TYPE_CHECKING:
    from zab.core.bridge import BotBridge
    from zab.core.store import Store

LOG = logging.getLogger("zab.jobs")
#: Seconds between two passes (§7.14: "every 10 s")
PASS_S = 10.0
#: How long a job's directory and row stay once its end was handled (§7.14)
KEEP_S = 14 * 86400
#: The endings whose post gives how long the job ran
TIMED = frozenset({"finished", "failed", "signal"})
#: How long an end post may wait for its session's card to be confirmed (SPEC §4.4), or for
#: Zulip to take it, before it is dropped with a log line (Codex round 1, the review's N5)
CONFIRM_S = 3600.0
#: The waits before each new try of an end post that failed, the card unread or the post
#: refused or of unknown outcome: never one a pass, which Zulip Cloud's limits forbid (#754),
#: but at about 1, 3, 7, 15 and 31 minutes, 6 tries at most within `CONFIRM_S`
RETRY_S = (60.0, 120.0, 240.0, 480.0, 960.0, 1800.0)
#: How many of the bot's latest messages in the topic are read for a post that may have landed
LANDED_LOOK = 50
#: A landed post counts from this long before its row's first try: Zulip's message times are on
#: Zulip's clock, the row's on the VM's, and a margin this wide covers any skew between them
#: (review of #940); an end post's exact tag, of jobs' new ids, can't match an older post
LANDED_MARGIN_S = 600.0


class Refused(Exception):
    """Zulip refused the post outright (a 4xx other than 429): it never landed."""


@dataclass(frozen=True)
class JobEnd:
    """One job's end, as the watcher handles it."""

    job: zjobs.Job
    ending: zjobs.Ending
    ended_at: float | None  # `ended.json`'s time; None when the job left none
    owner_id: int | None  # who started the turn the job started in
    turn_id: int | None  # that turn (`turns.id`)

    def line(self) -> str:
        """`background job `x` finished: exit 0 after 2 h 13 min`: only zab's own words, the
        name (which the pattern allows) and numbers (§7.14)."""
        words = self.ending.words
        if self.ending.kind in TIMED and self.ended_at is not None:
            words += f" after {zjobs.span(self.ended_at - self.job.started_at)}"
        return f"background job `{self.job.name}` {words}"


@dataclass
class EndBatch:
    """The ends of one session's jobs that one pass handles: one post."""

    bot: str
    session: dict[str, Any]  # its `sessions` row
    ends: list[JobEnd]

    @property
    def ok(self) -> bool:
        return all(end.ending.ok for end in self.ends)


class EndHook(Protocol):
    """What follows a job's end post (#655's report turn, Appendix A.3). For each batch,
    `sentence` comes first, then `record`, with the very same `EndBatch` object, so that a
    decision made in one (the cap's) can be kept on it for the other. Every attached end that
    is neither stopped nor seen (shown to the agent during the turn that started the job)
    comes through, a job lost at a VM stop included, so that its note can wait; an end whose
    session row is gone doesn't either. Where nothing may be posted (the card unconfirmed, or
    the post refused, for an hour; the card gone, or in a public channel), only `record`
    comes, with no message."""

    async def sentence(self, batch: EndBatch) -> str:
        """The post's last sentence: "Claude will report on it.", "" when nothing follows, or
        "Send a message to have Claude report on it." past the cap. It comes again for each
        try of a post that failed, on a new batch, so it must change nothing lasting: `record`
        comes once per end, with the batch of the try that went out."""
        ...

    def record(self, batch: EndBatch, message_id: int | None) -> Callable[[], None]:
        """The post went out (`message_id`; None when it couldn't be posted): the report turn's
        pending delivery, or its note for later, written in the store. Called inside the
        transaction that marks the batch's jobs handled, so that both commit or neither does
        (Codex round 2 on #970): synchronous, never awaiting. Returns what follows the commit
        (the prompt put to the session's actor), which the watcher calls once."""
        ...


class NoReports:
    """Nothing follows an end post: a bridge without report turns."""

    async def sentence(self, batch: EndBatch) -> str:
        return ""

    def record(self, batch: EndBatch, message_id: int | None) -> Callable[[], None]:
        return nothing


def nothing() -> None:
    """What follows a commit when nothing does."""


def id_tag(ids: Sequence[str]) -> str:
    """`(job 3f9c2a7b1d4e)`, `(jobs a, b)`: the end post's last words, its jobs' ids, which
    `zab job` shows, and by which a post of unknown outcome is found (`JobWatcher.landed`)."""
    return f"({'job' if len(ids) == 1 else 'jobs'} {', '.join(ids)})"


def end_post(kind: str, mentions: Sequence[str], batch: EndBatch, sentence: str) -> str:
    """The end post of `batch` (§7.14): a bridge note, 🔧 when every job finished well, else
    ⚠️, mentioning each owner once; a line per job when there are several."""
    lead = " ".join(dict.fromkeys(mentions))
    lead = f"{lead} " if lead else ""
    level = "info" if batch.ok else "warning"
    tag = id_tag([end.job.id for end in batch.ends])
    if len(batch.ends) == 1:
        text = f"{lead}{batch.ends[0].line()}."
        return view.bridge_note(kind, " ".join(filter(None, [text, sentence, tag])), level)
    lines = [f"{lead}{len(batch.ends)} background jobs ended:"]
    lines += [f"- {end.line().removeprefix('background job ')}" for end in batch.ends]
    lines += [" ".join(filter(None, [sentence, tag]))]
    return view.bridge_note(kind, "\n".join(lines), level)


class JobWatcher:
    """The machine's background jobs, as the bridge sees them (module doc)."""

    def __init__(
        self,
        state_dir: str | Path,
        store: Store,
        bridges: Sequence[BotBridge],
        *,
        runner: Runner = subprocess_runner,
        clock: Callable[[], float] = time.time,
        boot: Callable[[], str] = zjobs.boot_id,
        hook: EndHook | None = None,
    ) -> None:
        self.directory = zjobs.jobs_dir(state_dir)
        self.store, self.bridges = store, bridges
        self.runner, self.clock, self.boot = runner, clock, boot
        self.hook: EndHook = hook or NoReports()
        #: The jobs a pass skipped after a failure, logged once each (`bad`)
        self.logged: set[str] = set()

    # -------------------------------------------------------------- JobControl (#934)

    def running(self, session_id: str) -> list[RunningJob]:
        """The session's running jobs, oldest first; any session of the machine, whichever
        bot asks (#934's N3)."""
        rows = self.store.rows("jobs", session_id=session_id, state="running")
        found = [RunningJob(r["id"], r["name"], r["started_at"]) for r in rows]
        return sorted(found, key=lambda job: job.started_at)

    async def unrecorded(self, session_id: str) -> list[RunningJob]:
        """The session's jobs that the watcher has no row for yet: in a job's first seconds,
        before its next pass (`PASS_S`), `zab job start` has attached it (`job.json`'s
        `attached`) and its unit runs. Topic `!status`, `!stop NAME` and `!stop jobs` take them
        too, so that a job is never "not running" just after it started (#935's review). The
        attachment is `job.json`'s word until the pass checks it, which alone decides an end
        post. Read-only: the job directories and systemd, off the event loop, then the store."""
        jobs = await asyncio.to_thread(unended, self.directory)
        jobs = [
            job
            for job in jobs
            if job.attached == session_id and not self.store.rows("jobs", id=job.id)
        ]
        if not jobs:
            return []
        done = await asyncio.to_thread(
            self.runner, zjobs.LIST_UNITS, env=zjobs.user_env(), timeout_s=30
        )
        live = still_running(jobs, done.ok, done.stdout, self.boot())
        found = [RunningJob(job.id, job.name, job.started_at) for job in live]
        return sorted(found, key=lambda job: job.started_at)

    def jobs_of(self, session_id: str, since: float) -> list[dict[str, Any]]:
        """The session's jobs running, or ended since `since`, oldest first: `!status`'s lines
        (§7.14)."""
        rows = self.store.rows("jobs", session_id=session_id)
        kept = [r for r in rows if r["state"] == "running" or (r["ended_at"] or 0) >= since]
        return sorted(kept, key=lambda r: r["started_at"])

    async def stop(self, session_id: str, job: RunningJob) -> float | None:
        """Stop `job` for `!stop NAME` or `!stop jobs`: `stopped.json` first (`by: "!stop"`),
        then its unit, waiting for its `_ended` (`zab.jobs.stop`). The seconds it ran; None if it
        wasn't running or the stop failed. Its end is then handled, with no post: the command's
        reply is its end post (§7.14)."""
        recorded = bool(self.store.rows("jobs", id=job.id, session_id=session_id, state="running"))
        if not recorded and self.store.rows("jobs", id=job.id):
            return None  # a row of another session, or an ended one
        try:
            found = await asyncio.to_thread(zjobs.Job.load, self.directory / job.id)
            if found is None:
                return None
            if not recorded and (found.attached != session_id or found.ended() is not None):
                return None  # no row yet: only a running job `zab job start` attached here
            done = await asyncio.to_thread(zjobs.stop, found, "!stop", self.runner)
        except OSError:  # its directory gone meanwhile: nothing stopped (review of #940, N6)
            return None
        if not done.ok:
            return None
        # A narrow race, left open (N7): a job that ends by itself while the stop runs, its
        # `_ended` having seen no `stopped.json`, still reads "stopped" here, and its own end
        # is never posted; the reply says it was stopped.
        now = self.clock()
        if not recorded:
            # The next pass makes its row and reads its end as a stop: no post (§7.14).
            return max(0.0, now - found.started_at)
        self.store.update(
            "jobs", {"id": job.id}, state="ended", ended_at=now, result="stopped", handled_at=now
        )
        return max(0.0, now - found.started_at)

    # ------------------------------------------------------------------- the passes

    async def run(self) -> None:
        """Every `PASS_S` seconds, until cancelled; a failed pass is logged and the next one
        tries again."""
        while True:
            try:
                await self.pass_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Background jobs' pass failed", extra={"event": "job_watch"})
            await asyncio.sleep(PASS_S)

    def read(
        self, done: frozenset[str] = frozenset()
    ) -> tuple[list[tuple[zjobs.Job, zjobs.Ending | None, float | None, bool]], dict[str, str]]:
        """Each job of the directory, how it ended (None: it runs, or its end is handled
        already, `done`), `ended.json`'s time and whether the agent saw its end; and the jobs
        that couldn't be read, by id, with why, each left out (review of #940, B1). Files and
        systemd only, in a thread; systemd only when a job not yet handled lacks its
        `ended.json` (N3)."""
        found = zjobs.all_jobs(self.directory)
        units: set[str] | None = set()
        if any(job.id not in done and job.ended() is None for job in found):
            units = zjobs.present_units(self.runner)
        boot, now, out, failed = self.boot(), self.clock(), [], {}
        for job in found:
            try:
                at = (job.ended() or {}).get("at")
                ok = isinstance(at, int | float) and not isinstance(at, bool) and math.isfinite(at)
                when = float(at) if ok else None
                if job.id in done:
                    out.append((job, None, when, False))
                    continue
                # no answer from the manager: a job without `ended.json` counts as running
                running = units is None or zjobs.unit(job.id) in units
                ending = zjobs.ending(job, running=running, boot=boot, now=now)
                out.append((job, ending, when, job.seen))
            except Exception as exc:
                failed[job.id] = type(exc).__name__
        return out, failed

    def bad(self, job_id: str, what: str, why: str) -> None:
        """A job that couldn't be handled this pass: logged once per process, and skipped."""
        if job_id in self.logged:
            return
        self.logged.add(job_id)
        LOG.warning(
            "Background job %s skipped: %s failed (%s)", job_id, what, why,
            extra={"event": "job_watch"},
        )  # fmt: skip

    async def pass_once(self) -> list[EndBatch]:
        """One pass: the table kept, each new end handled (posted or not), old jobs removed.
        The batches posted, for the tests."""
        now = self.clock()
        ends: dict[tuple[str, str], list[JobEnd]] = {}
        unowned: list[str] = []
        done = frozenset(r["id"] for r in self.store.rows("jobs") if r["handled_at"] is not None)
        found, failed = await asyncio.to_thread(self.read, done)
        for job_id, why in failed.items():
            self.bad(job_id, "reading its end", why)
        for job, ending, ended_at, seen in found:
            try:
                if self.bridge(job.bot) is None:
                    # no bot of this process: its own process's to keep and handle (the locks
                    # are per bot: Codex round 3); with no row of anyone's, as for a job from
                    # an ordinary shell or of a bot no process runs any more, any watcher
                    # removes its directory `KEEP_S` after its end (Codex round 4; N4)
                    old = now - self.end_of(job, ended_at) >= KEEP_S
                    if old and not self.store.rows("jobs", id=job.id):
                        unowned.append(job.id)
                    continue
                end = self.consider(job, ending, ended_at, seen, now)
                if end is not None:
                    ends.setdefault((job.bot, end[0]), []).append(end[1])
            except Exception as exc:  # one job's failure skips that job only (B1)
                self.bad(job.id, "handling its end", type(exc).__name__)
        pending = {end.job.id: end for each in ends.values() for end in each}
        try:
            closed = await self.close_posts(now, pending)
        except Exception as exc:  # the store unread: the batches still try (N3)
            closed = set()
            self.once("job_posts", "The pending end posts couldn't be settled (%s)", exc)
        batches = []
        for key, each in ends.items():
            each = [end for end in each if end.job.id not in closed]
            if not each:
                continue
            try:
                post = self.pending_post(key)
                if post is not None and now < post["next_at"]:
                    continue  # its next try isn't due
                # a post being tried again takes no new end: it comes in the next post
                kept = [e for e in each if post is None or e.job.id in post["ids"]]
                if not kept:
                    continue  # its post's row couldn't be settled (a write failed)
                session = self.store.rows("sessions", id=key[1])[0]
                batch = EndBatch(key[0], session, kept)
                batches.append(batch)
                await self.post(batch, post)
            except Exception as exc:
                for end in each:
                    self.bad(end.job.id, "posting its end", type(exc).__name__)
        await self.prune(now)
        if unowned:
            await asyncio.to_thread(self.remove, unowned)
        return batches

    # ------------------------------------------------- the pending end posts (§6.2)

    @staticmethod
    def decoded(row: dict[str, Any]) -> dict[str, Any]:
        """A `job_posts` row, its job ids as `ids`, and its sends as `sends`: each one's jobs
        (`ids`) and destination (`stream_id`, `topic`)."""
        sends = tuple({**send, "ids": tuple(send["ids"])} for send in json.loads(row["sends"]))
        return {**row, "ids": tuple(json.loads(row["job_ids"])), "sends": sends}

    def pending_post(self, key: tuple[str, str]) -> dict[str, Any] | None:
        """The (bot, session)'s end post being tried, from its `job_posts` row, or None."""
        rows = self.store.rows("job_posts", bot=key[0], session_id=key[1])
        return self.decoded(rows[0]) if rows else None

    async def close_posts(self, now: float, pending: dict[str, JobEnd]) -> set[str]:
        """The posts being tried that end at the start of a pass, whether or not a batch forms
        for them (review round 2 of #940, B2):

        - one past `CONFIRM_S` from its first try: its jobs still unhandled are handled with no
          post, with a log line, and the hook hears of those this pass found (`pending`);
        - one none of whose jobs is live any more (`live`: handled some other way, as `seen`,
          or its directory gone): any unhandled one is handled with no post.

        A row with a live job stays, its stored sends and backoff with it, even while that job
        doesn't read for a pass (Codex 6711044076): its batch, or a later one, looks for its
        sends before sending anything. Returned: the jobs of the posts ended, none of which is
        posted in this pass, even when a write failed. Another process's bots' rows are its
        own. A row that doesn't decode is logged once and ended (`malformed`), never stopping
        the pass (review of #940, N3)."""
        closed: set[str] = set()
        for raw in self.store.rows("job_posts"):
            if self.bridge(raw["bot"]) is None:
                continue  # another process's (Codex round 3)
            ids: tuple[str, ...] = ()
            try:
                try:
                    row = self.decoded(raw)
                except Exception as exc:
                    await self.malformed(raw, pending, now, type(exc).__name__, closed)
                    continue
                ids = row["ids"]
                if now - row["first_at"] >= CONFIRM_S:
                    closed.update(ids)
                    left = [pending[job_id] for job_id in ids if job_id in pending]
                    batch = self.left_batch(row, left)
                    self.close(row, left, now, "an hour of failed tries", batch)()
                elif not any(self.live(job_id) for job_id in ids):
                    closed.update(ids)
                    self.close(row, [], now, "its record went")
            except Exception as exc:
                for job_id in ids:
                    self.bad(job_id, "settling its end post", type(exc).__name__)
                if not ids:
                    self.once(f"job_posts:{raw['id']}", "A pending end post failed (%s)", exc)
        return closed

    async def malformed(
        self,
        raw: dict[str, Any],
        pending: dict[str, JobEnd],
        now: float,
        why: str,
        closed: set[str],
    ) -> None:
        """A `job_posts` row that doesn't decode (a hand edit, say): logged once and ended, its
        jobs, where its `job_ids` still read, handled with no post and the hook told of those
        this pass found, so that one bad row never stops the watcher (review of #940, N3)."""
        try:
            decoded = json.loads(raw["job_ids"])
            ids = (
                tuple(i for i in decoded if isinstance(i, str)) if isinstance(decoded, list) else ()
            )
        except Exception:
            ids = ()
        closed.update(ids)
        self.once(
            f"job_posts:{raw['id']}",
            "A pending end post's record doesn't read (%s): ended with no post",
            why,
        )
        left = [pending[job_id] for job_id in ids if job_id in pending]
        self.close({**raw, "ids": ids}, left, now, batch=self.left_batch(raw, left))()

    def left_batch(self, row: dict[str, Any], left: list[JobEnd]) -> EndBatch | None:
        """The batch the hook hears of for a post ended with none: those of its jobs this pass
        found (`left`), in the post's session; None when there are none."""
        if not left:
            return None
        session = self.store.rows("sessions", id=row["session_id"])[0]
        return EndBatch(row["bot"], session, left)

    def once(self, key: str, message: str, why: object) -> None:
        """A warning logged once per `key` for the process, as `bad` logs a job's."""
        if key in self.logged:
            return
        self.logged.add(key)
        reason = why if isinstance(why, str) else type(why).__name__
        LOG.warning(message, reason, extra={"event": "job_watch"})

    def live(self, job_id: str) -> bool:
        """Whether a pending post's job may still be posted: unhandled, and its directory there,
        whether or not its record read in this pass (Codex 6711044076)."""
        rows = self.store.rows("jobs", id=job_id)
        return bool(rows) and rows[0]["handled_at"] is None and (self.directory / job_id).exists()

    def close(
        self,
        post: dict[str, Any],
        left: list[JobEnd],
        now: float,
        why: str = "",
        batch: EndBatch | None = None,
    ) -> Callable[[], None]:
        """A post being tried ends with none: its jobs still unhandled (`left`, those this pass
        found, and any other) handled, in one transaction with the row's removal and the hook's
        record of `batch`, told of no message; then the log of those, its reason (`why`) unless
        already given. Returned: what follows the hook's commit, for the caller to call."""
        after = nothing
        with self.store.transaction():
            unhandled = [
                job_id
                for job_id in post["ids"]
                for row in self.store.rows("jobs", id=job_id)
                if row["handled_at"] is None
            ]
            for job_id in unhandled:
                self.store.update("jobs", {"id": job_id}, handled_at=now, end_message_id=None)
            self.store.forget_job_post(post["id"])
            if batch is not None:
                after = self.hook.record(batch, None)
        if why and unhandled:
            LOG.warning(
                "No end post for %s: %s%s", ", ".join(unhandled), why,
                " (it may have landed)" if post["unknown"] else "",
                extra={"event": "job_ended", "bot": post["bot"], "session": post["session_id"]},
            )  # fmt: skip
        for end in left:
            self.logged_end(end, post["session_id"], now, None)
        return after

    def consider(
        self,
        job: zjobs.Job,
        ending: zjobs.Ending | None,
        ended_at: float | None,
        seen: bool,
        now: float,
    ) -> tuple[str, JobEnd] | None:
        """One of this process's jobs: its row kept; its end, with its session, when it is to
        be posted now."""
        row = self.row(job)
        if ending is None:
            return None
        if row["state"] == "running":
            state = "lost" if ending.kind == "lost" else "ended"
            self.store.update(
                "jobs", {"id": job.id}, state=state, ended_at=ended_at or now, result=ending.kind,
            )  # fmt: skip
        if row["handled_at"] is not None:
            return None
        end = self.decide(job, ending, ended_at, seen, row["session_id"], now)
        return (row["session_id"], end) if end is not None else None

    @staticmethod
    def end_of(job: zjobs.Job, ended_at: float | None) -> float:
        """When a job ended at the latest: `ended.json`'s time, or for one that left none, its
        start and its cap, by which systemd stopped it (review of #940, N4)."""
        if ended_at is not None:
            return ended_at
        latest = job.started_at + job.max_hours * 3600
        return latest if math.isfinite(latest) else math.inf

    def row(self, job: zjobs.Job) -> dict[str, Any]:
        """The job's row, made at its first sight, with the session it belongs to."""
        rows = self.store.rows("jobs", id=job.id)
        if rows:
            return rows[0]
        session = self.attached(job)
        self.store.insert(
            "jobs", id=job.id, bot=job.bot, session_id=session, name=job.name,
            state="running", started_at=job.started_at,
        )  # fmt: skip
        return self.store.rows("jobs", id=job.id)[0]

    def attached(self, job: zjobs.Job) -> str | None:
        """`job.json`'s `attached`, checked: a session of the job's bot, with a turn of it
        running at the job's start (§7.14). None: unattached."""
        if not job.attached:
            return None
        if not self.store.rows("sessions", id=job.attached, bot=job.bot):
            return None
        return job.attached if self.turn_at(job.attached, job.started_at) else None

    def turn_at(self, session_id: str, at: float) -> dict[str, Any] | None:
        """The session's turn that was running at `at`: the job's starting turn."""
        for turn in sorted(
            self.store.rows("turns", session_id=session_id), key=lambda t: -t["started_at"]
        ):
            if turn["started_at"] <= at and (turn["ended_at"] is None or turn["ended_at"] >= at):
                return turn
        return None

    def decide(
        self,
        job: zjobs.Job,
        ending: zjobs.Ending,
        ended_at: float | None,
        seen: bool,
        session_id: str | None,
        now: float,
    ) -> JobEnd | None:
        """The end to post, or None: handled now with no post (marked), or left for a later
        pass while the job's starting turn runs."""
        bridge = self.bridge(job.bot)
        unposted = session_id is None or bridge is None or ending.kind == "stopped"
        if unposted or not self.store.rows("sessions", id=session_id):
            self.handled(job, ending, ended_at, session_id, now, None)
            return None
        turn = self.turn_at(session_id, job.started_at)
        if turn is not None and self.turn_runs(bridge, session_id, turn):
            return None  # the agent's to see: posted when that turn ends (§7.14)
        turn_end = (turn or {}).get("ended_at")
        if seen and turn_end is not None and (ended_at or now) <= turn_end:
            self.handled(job, ending, ended_at, session_id, now, None)
            return None
        owner = (turn or {}).get("sender_id")
        return JobEnd(job, ending, ended_at, owner, (turn or {}).get("id"))

    def bridge(self, bot: str) -> BotBridge | None:
        return next((b for b in self.bridges if b.bot.name == bot), None)

    def turn_runs(self, bridge: BotBridge, session_id: str, turn: dict[str, Any]) -> bool:
        """Whether `turn` is its session's running turn now, by the session's actor: a row a
        crash left unended doesn't hold a post back for good."""
        session = self.store.rows("sessions", id=session_id)[0]
        actor = bridge.actors.get(session["native_id"])
        current = getattr(actor, "turn", None) if actor else None
        return bool(current) and current.get("id") == turn["id"]

    def handled(
        self,
        job: zjobs.Job,
        ending: zjobs.Ending,
        ended_at: float | None,
        session_id: str | None,
        now: float,
        message_id: int | None,
    ) -> None:
        """An end handled with no batch (unattached, stopped, seen): its row marked, logged."""
        self.store.update("jobs", {"id": job.id}, handled_at=now, end_message_id=message_id)
        self.logged_end(JobEnd(job, ending, ended_at, None, None), session_id, now, message_id)

    def logged_end(
        self, end: JobEnd, session_id: str | None, now: float, message_id: int | None
    ) -> None:
        """The log line of an end handled, its post's or none (event `job_ended`)."""
        job, ran = end.job, ""
        if end.ended_at is not None and math.isfinite(end.ended_at - job.started_at):
            ran = f" after {zjobs.span(end.ended_at - job.started_at)}"
        LOG.info(
            "Job %s `%s` ended: %s%s%s",
            job.id,
            job.name,
            end.ending.kind,
            ran,
            "" if message_id else " (no post)",
            extra={"event": "job_ended", "bot": job.bot, "session": session_id},
        )

    async def confirmed(
        self, bridge: BotBridge, batch: EndBatch, post: dict[str, Any]
    ) -> bool | None:
        """Whether the batch may be posted at its session's card location, which this process
        must have confirmed (SPEC §4.4; Codex round 1 on #940). None: not now, as the card
        couldn't be read, a failed try (`failed`); the batch waits for its next. False: no
        post, once its tries are spent, for a card found gone, by this check or an earlier one
        (`gone`), and for a read-only session (its card in a public channel, where the bridge
        posts nothing: Codex round 2 on #940), each logged. A session detached by `!detach` is
        posted at its last location, as §7.14 says: `verify_card` no longer checks it."""
        ids = [end.job.id for end in batch.ends]
        if self.gone(batch, ids):  # found gone before, at a start's check or an earlier pass
            return False
        failure = ""
        try:
            await bridge.verify_card(batch.session["id"])
            # three tries undone by moves or a new epoch return with it still unconfirmed
            # (Codex round 3 on #940): a failure like any other
            if batch.session["id"] in bridge.unverified:
                failure = "still unconfirmed"
        except Exception as exc:
            failure = type(exc).__name__
        if failure:
            return None if self.waits(batch, post, f"the session's card ({failure})") else False
        after = self.store.rows("sessions", id=batch.session["id"])[0]["state"]
        if after == "readonly":  # its card is in a public channel: nothing posted there (§4.4)
            LOG.warning(
                "No end post for %s: the session's card is in a public channel",
                ", ".join(ids),
                extra={"event": "job_ended", "bot": batch.bot, "session": batch.session["id"]},
            )
            return False
        return not self.gone(batch, ids)  # or by this check

    def tried(self, batch: EndBatch, post: dict[str, Any] | None) -> dict[str, Any]:
        """A try of the batch's post counted in its `job_posts` row, made at its first try,
        and committed before the try makes any call to Zulip (review round 2 of #940): its
        count, and its next try, after the next of `RETRY_S`. A write that fails raises, and
        nothing is done on this try. The row, as written."""
        now = self.clock()
        key = (batch.bot, batch.session["id"])
        with self.store.transaction():
            if post is None:
                self.store.insert(
                    "job_posts", bot=batch.bot, session_id=batch.session["id"], first_at=now,
                    job_ids=json.dumps([end.job.id for end in batch.ends]), sends="[]",
                    attempts=0, next_at=now, unknown=0,
                )  # fmt: skip
                post = self.pending_post(key)
                assert post is not None
            attempts = post["attempts"] + 1
            wait = RETRY_S[attempts - 1] if attempts <= len(RETRY_S) else math.inf
            self.store.update(
                "job_posts", {"id": post["id"]}, attempts=attempts, next_at=now + wait
            )
        return {**post, "attempts": attempts, "next_at": now + wait}

    def sending(
        self, batch: EndBatch, post: dict[str, Any], session: dict[str, Any]
    ) -> dict[str, Any]:
        """The send about to go out, marked in the row and committed first: the jobs it carries,
        whose `id_tag` ends it, where it goes (the session's card location now: Codex
        6711200018), and that its outcome is unknown until known, so that no failure, crash,
        restart or failed write after it has it sent again unlooked for: the next try looks
        for it (`landed`). A write that fails raises, and nothing is sent. The row, as
        written."""
        send = {
            "ids": tuple(end.job.id for end in batch.ends),
            "stream_id": session["stream_id"],
            "topic": session["topic"],
        }
        sends = post["sends"] if send in post["sends"] else (*post["sends"], send)
        self.store.update("job_posts", {"id": post["id"]}, unknown=1, sends=json.dumps(sends))
        return {**post, "unknown": 1, "sends": sends}

    def waits(self, batch: EndBatch, post: dict[str, Any], what: str) -> bool:
        """After a failed try: whether the post waits for its next try, which must come within
        `CONFIRM_S` of its first; else False, logged, and the caller ends it with no post."""
        extra = {"event": "job_ended", "bot": batch.bot, "session": batch.session["id"]}
        now = self.clock()
        if post["next_at"] - post["first_at"] <= CONFIRM_S:
            LOG.warning(
                "A job's end post waits: %s failed; trying again in %d s",
                what, post["next_at"] - now, extra=extra,
            )  # fmt: skip
            return True
        LOG.warning(
            "No end post for %s: %s failed, %d tries in %d min%s",
            ", ".join(post["ids"]), what, post["attempts"], int((now - post["first_at"]) // 60),
            " (it may have landed)" if post["unknown"] else "", extra=extra,
        )  # fmt: skip
        return False

    async def dropped(self, batch: EndBatch, post: dict[str, Any], what: str) -> bool:
        """After a failed try: whether the post ends here, with none, its tries spent (`waits`)."""
        if self.waits(batch, post, what):
            return False
        await self.unposted(batch, post)
        return True

    async def finish(
        self,
        batch: EndBatch,
        post: dict[str, Any],
        message_id: int,
        covered: frozenset[str] | None = None,
    ) -> None:
        """The post went out, or was found landed: the batch's jobs it carries (`covered`; all,
        for a send) handled with its message, in one transaction with the row's removal once
        none of its jobs is left unhandled, and with the hook's record (the report prompt's
        pending delivery, or its note), so that a crash or a failed write between them never
        loses the report the post announced (Codex round 2 on #970); then the log, and what
        follows the hook's commit. A row with a job left (one that didn't read in this pass)
        stays, its sends with it, for that job's next try. A write that fails, the hook's
        included, leaves the row, its outcome unknown, for the next try's look: never a blind
        second send (review round 2 of #940, B1)."""
        now = self.clock()
        ends = [e for e in batch.ends if covered is None or e.job.id in covered]
        if len(ends) < len(batch.ends):
            batch = EndBatch(batch.bot, batch.session, ends)
            await self.hook.sentence(batch)  # as for any post: `sentence`, then `record`
        with self.store.transaction():
            for end in batch.ends:
                self.store.update(
                    "jobs", {"id": end.job.id, "handled_at": None},
                    handled_at=now, end_message_id=message_id,
                )  # fmt: skip
            if not any(
                row["handled_at"] is None
                for job_id in post["ids"]
                for row in self.store.rows("jobs", id=job_id)
            ):
                self.store.forget_job_post(post["id"])
            after = self.hook.record(batch, message_id)
        for end in batch.ends:
            self.logged_end(end, batch.session["id"], now, message_id)
        after()

    async def unposted(self, batch: EndBatch, post: dict[str, Any] | None) -> None:
        """No post for the batch (the card gone or in a public channel, or its tries spent,
        each logged already): its jobs handled with none, its row gone, in one transaction with
        the hook's record, told of no message (Codex round 2 on #970)."""
        now = self.clock()
        if post is not None:
            after = self.close(post, batch.ends, now, batch=batch)
        else:
            with self.store.transaction():
                for end in batch.ends:
                    self.store.update("jobs", {"id": end.job.id}, handled_at=now)
                after = self.hook.record(batch, None)
            for end in batch.ends:
                self.logged_end(end, batch.session["id"], now, None)
        after()

    async def landed(
        self,
        bridge: BotBridge,
        session: dict[str, Any],
        sends: Sequence[dict[str, Any]],
        want: frozenset[str],
        since: float,
    ) -> tuple[int, frozenset[str]]:
        """A send of the post found among the bot's latest messages, with the jobs of `want` it
        carries; else (0, nothing). Only the sends that carried a job of `want` are looked for,
        each where it went and at the card's location now (`session`), which a move may have
        changed (Codex 6711200018). Only an end post counts (Codex 6710597274): a bridge note of
        this bot, ending with the exact `id_tag` of such a send (kept in the row: Codex
        6710713831), sent since `since`; never, say, an answer that named a job's id, a log's
        path or a `!status` line.

        Best effort, within the module's bound: a miss (a post among more than `LANDED_LOOK`
        newer bot messages where it went, or moved elsewhere by a person) sends the post
        again, a duplicate end post, never edited, at most the 6 sends of `RETRY_S` an hour."""
        carried = [send for send in sends if frozenset(send["ids"]) & want]
        if not carried:
            return 0, frozenset()
        here = (session["stream_id"], session["topic"])
        places = dict.fromkeys([*((send["stream_id"], send["topic"]) for send in carried), here])
        marks = tuple(view.bridge_note(bridge.backend.kind, "", lvl) for lvl in ("info", "warning"))
        for stream_id, topic in places:
            result = await bridge.poster.call(
                lambda stream_id=stream_id, topic=topic: bridge.client.request(
                    "GET", "messages", anchor="newest", num_before=LANDED_LOOK, num_after=0,
                    apply_markdown=False, narrow=[
                        {"operator": "channel", "operand": stream_id},
                        {"operator": "topic", "operand": topic},
                        {"operator": "sender", "operand": bridge.router.user_id},
                    ],
                )
            )  # fmt: skip
            for message in reversed(result.get("messages", [])):
                content = str(message.get("content", "")).rstrip()
                at = message.get("timestamp")
                if not (isinstance(at, int | float) and at >= since):
                    continue
                if not content.startswith(marks):
                    continue  # a person's or an agent's words, not an end post
                for send in carried:
                    if content.endswith(id_tag(send["ids"])):
                        return int(message["id"]), frozenset(send["ids"]) & want
        return 0, frozenset()

    def gone(self, batch: EndBatch, ids: list[str]) -> bool:
        """Whether a check of the session's card found it gone (`unmentioned.card_gone`, which
        `verify_card` records with the detach): no post, which could recreate a deleted topic
        at a stale location (Codex round 5 on #940). A `!detach` records no such flag, and its
        jobs' ends are still posted in its topic (§7.14)."""
        if not self.store.get(unmentioned.card_gone(batch.session["id"])):
            return False
        LOG.warning(
            "No end post for %s: the session's card is gone",
            ", ".join(ids),
            extra={"event": "job_ended", "bot": batch.bot, "session": batch.session["id"]},
        )
        return True

    async def post(self, batch: EndBatch, post: dict[str, Any] | None) -> None:
        """One try of the batch's end post (`post`: its row, from an earlier try), at the
        session's confirmed card location (§4.4), then the hook. The try is counted in the row
        and committed before any call to Zulip (`tried`), and the row holds the next try back
        (`RETRY_S`); a send is marked in it before it goes out (`sending`). A post one of whose
        sends may have landed unseen (a failure, a crash, a restart, a write that failed after
        it) is looked for in the topic first (`landed`), and never sent again if found."""
        bridge = self.bridge(batch.bot)
        assert bridge is not None
        post = self.tried(batch, post)  # counted and committed, or nothing is done
        allowed = await self.confirmed(bridge, batch, post)
        if allowed is None:
            return  # waits for its next try
        if not allowed:
            await self.unposted(batch, post)
            return
        session = self.store.rows("sessions", id=batch.session["id"])[0]
        mentions = [await self.mention(bridge, end.owner_id) for end in batch.ends]
        sentence = await self.hook.sentence(batch)
        text = end_post(bridge.backend.kind, [m for m in mentions if m], batch, sentence)
        if post["unknown"]:
            want = frozenset(end.job.id for end in batch.ends)
            try:
                # best effort, within the module's bound (Codex round 9)
                since = post["first_at"] - LANDED_MARGIN_S
                found, covered = await self.landed(bridge, session, post["sends"], want, since)
            except Exception as exc:  # the topic unread: nothing sent on this try
                await self.dropped(batch, post, f"the look for it ({type(exc).__name__})")
                return
            if found:  # a part not covered waits for the next try, looked for again
                await self.finish(batch, post, found, covered)
                return
        post = self.sending(batch, post, session)  # marked and committed, or nothing is sent
        try:
            message_id = await self.send(bridge, session, text)
        except Refused as exc:  # it never landed, nor did the sends looked for above
            self.store.update("job_posts", {"id": post["id"]}, unknown=0)
            await self.dropped(batch, {**post, "unknown": 0}, f"the post ({exc})")
            return
        except Exception as exc:  # its outcome unknown: looked for at the next try
            await self.dropped(batch, post, f"the post ({type(exc).__name__})")
            return
        await self.finish(batch, post, message_id)

    @staticmethod
    async def send(bridge: BotBridge, session: dict[str, Any], text: str) -> int:
        """The post sent; `Refused` when Zulip refused it outright."""
        try:
            return await bridge.poster.send(session["stream_id"], session["topic"], text, ACK)
        except ZulipError as exc:
            if 400 <= exc.status < 500 and exc.status != 429:
                raise Refused(f"{exc.code}, HTTP {exc.status}") from exc
            raise

    async def mention(self, bridge: BotBridge, user_id: int | None) -> str:
        """`@**Alice|12**`, or "" when there is no owner or no name for it."""
        if not user_id:
            return ""
        user = bridge.users.get(user_id)
        if user is None:
            with contextlib.suppress(Exception):
                response = await bridge.poster.call(
                    lambda: bridge.client.request("GET", f"users/{user_id}")
                )
                user = bridge.users[user_id] = response["user"]
        name = (user or {}).get("full_name")
        return f"@**{name}|{user_id}**" if name else ""

    async def prune(self, now: float) -> None:
        """The directories of jobs whose end was handled `KEEP_S` ago go, with their rows; a
        directory that isn't a job's own (a symlink, a stray name) is never touched."""
        old = [
            r["id"]
            for r in self.store.rows("jobs")
            if r["handled_at"] is not None
            and now - r["handled_at"] >= KEEP_S
            and zjobs.ID_RE.fullmatch(r["id"])
        ]
        if old:
            gone = await asyncio.to_thread(self.remove, old)
            for job_id in gone:
                self.store.forget_job(job_id)

    def remove(self, ids: list[str]) -> list[str]:
        """Each job's directory removed, never through a symlink nor outside the jobs
        directory; another watcher removing it meanwhile is no failure. The ids whose directory
        is gone."""
        gone = []
        try:
            home = self.directory.resolve(strict=True)
        except OSError:
            return gone
        for job_id in ids:
            if not zjobs.ID_RE.fullmatch(job_id):
                continue
            path = self.directory / job_id
            inside = path.parent.resolve() == home
            if inside and path.is_dir() and not path.is_symlink():
                shutil.rmtree(path, ignore_errors=True)
            if inside and not path.exists() and not path.is_symlink():
                gone.append(job_id)
        return gone
