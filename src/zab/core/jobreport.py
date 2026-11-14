"""The report turn after a background job's end (SPEC §7.14 step 3, Appendix A.3; #655).

`ReportHook` is the job watcher's `EndHook`. For each end post (one per session per pass):

- **A report turn** when one is due: the post's last sentence says "Claude will report on it.",
  and the post becomes a message to the session from the job's owner whose text is Appendix
  A.3's prompt, delivered as `!handover` delivers its own (`SessionActor.deliver`): a turn
  starts with the session idle (👀 on the end post), or the prompt is steered into the running
  turn, or held for after it. It is recorded as a pending delivery first, so that a restart in
  between delivers it again (`BotBridge.make_actor`).
- **No report turn** for a job started with `--no-report` or under `[jobs] report = false`
  (`job.json`'s `report`), for a session detached since the job started, and for a job lost
  at boot, whose note waits (below).
- **The cap:** at most `CAP` report turns in a row in a session with no message from a person in
  between (`job_reports:<session id>`: a turn of report prompts alone adds one when it starts,
  and a person's message delivered to the session sets it back to 0). Past it, the post ends
  "Send a message to have Claude report on it.", and the prompt waits as a note.
- **Notes that wait** (`job_notes:<session id>`, a JSON list): a lost job's note, a prompt past
  the cap, a prompt whose end post couldn't go out, one that reached a read-only or detached
  session (`not_taken`), and a later part's whose end post went to the session already
  (`ReportHook.record`, logged). They never start a turn. The next turn
  the session starts for a person's message carries them at its head (`notes_block`), between
  `[zab: background jobs]` and `[end of background jobs]`; a steer doesn't. They are passed on
  once, when that turn's start is accepted.

The prompt holds only zab's own words, the job's name and command line (the agent's own
arguments, which it typed) and paths: never the job's output, which the agent reads with its
own tools, where it is a tool result (Appendix A.3).
"""

from __future__ import annotations

import logging
import re
import shlex
import time
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from zab import jobs as zjobs
from zab.core.presentation import AGENT_NAMES, code
from zab.render.redact import redact_secrets

if TYPE_CHECKING:
    from zab.core.bridge import BotBridge
    from zab.core.jobwatch import EndBatch, JobEnd
    from zab.core.session import Delivery
    from zab.core.store import Store

LOG = logging.getLogger("zab.jobs")
#: Report turns in a row in a session with no message from a person (§7.14, D44)
CAP = 3
#: The command line as a prompt line gives it, cut after this many characters (A.3)
COMMAND_CHARS = 200
#: The endings whose line gives how long the job ran, as the end post does
TIMED = frozenset({"finished", "failed", "signal"})
REQUEST = (
    "Read what the job left, and report the result to the user briefly, as the answer to what "
    "they asked. Start nothing new unless they asked for it."
)
NOTES_OPEN, NOTES_CLOSE = "[zab: background jobs]", "[end of background jobs]"
#: C0 controls (newline and tab included), DEL, C1 controls and the Unicode line separators
CONTROLS = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029]")


def notes_key(session_id: str) -> str:
    return f"job_notes:{session_id}"


def count_key(session_id: str) -> str:
    return f"job_reports:{session_id}"


def reports_key(session_id: str) -> str:
    """The message ids (end posts) delivered to the session as report prompts: no person's."""
    return f"job_report_ids:{session_id}"


def utc(t: float, form: str = "%H:%M") -> str:
    return time.strftime(form + " UTC", time.gmtime(t))


def one_line(command: str) -> str:
    """A command line as a prompt line gives it: secrets redacted (§11.6), and again once on
    one line, which can make a key's context whole, as the approvals' code spans do; every
    control character, newlines included, written as an escape (`\\n`, `\\x1b`), so that an
    argument can't end the line and forge `[end of background jobs]` or a request in the user's
    voice (review round 1 on #942)."""
    text = redact_secrets(command)
    text = CONTROLS.sub(lambda m: repr(m.group())[1:-1], text)
    return redact_secrets(text)


def job_line(end: JobEnd) -> str:
    """A.3's line for one job: its name, start, command line (a code span with a fence longer
    than any backtick run in it, review round 2 on #942), end and log."""
    job, ending = end.job, end.ending
    command = one_line(shlex.join(job.argv))
    if len(command) > COMMAND_CHARS:
        command = command[:COMMAND_CHARS] + "…"
    if ending.kind == "lost":
        how = f"{ending.words}; zab doesn't restart it"
    else:
        head, colon, rest = ending.words.partition(":")
        at = f" at {utc(end.ended_at)}" if end.ended_at is not None else ""
        after = ""
        if ending.kind in TIMED and end.ended_at is not None:
            after = f" after {zjobs.span(end.ended_at - job.started_at)}"
        how = f"{head}{at}{colon}{rest}{after}"
    return (
        f"[zab: a background job ended] `{job.name}`, started "
        f"{utc(job.started_at, '%Y-%m-%d %H:%M')} in this topic ({code(command)}), {how}. "
        f"Its log: {job.log} (`zab job status {job.name}` shows its last 20 lines)."
    )


def prompt(ends: Sequence[JobEnd]) -> str:
    """The report turn's prompt (A.3): a line per job, then the request once."""
    return "\n".join([*(job_line(end) for end in ends), REQUEST])


def notes_block(notes: Sequence[str]) -> str:
    """The notes that wait, as the head of a person's turn (§7.14)."""
    return "\n".join([NOTES_OPEN, *notes, NOTES_CLOSE]) + "\n"


def nothing() -> None:
    """What follows the commit when no prompt is delivered."""


class ReportHook:
    """The watcher's `EndHook` for the report turn (module doc)."""

    def __init__(self, store: Store, bridges: Sequence[BotBridge]) -> None:
        self.store, self.bridges = store, bridges

    @staticmethod
    def reportable(batch: EndBatch) -> list[JobEnd]:
        """The ends a report turn reports: not lost, and started without `--no-report`."""
        return [e for e in batch.ends if e.ending.kind != "lost" and e.job.report]

    def decide(self, batch: EndBatch) -> str:
        """`report`, `wait` (past the cap) or `none`."""
        if not self.reportable(batch) or batch.session.get("state") == "detached":
            return "none"
        if not any(e.owner_id for e in self.reportable(batch)):
            return "wait"  # no person to report to: the next one's turn will carry it
        session_id = batch.session["id"]
        if self.store.get(count_key(session_id), 0) + self.coming(batch) >= CAP:
            return "wait"
        return "report"

    def coming(self, batch: EndBatch) -> int:
        """The report prompts delivered to the session that haven't started their turn yet,
        held or still on their way to the actor: report turns to come, which the cap counts
        already. Without them, a prompt decided while the previous one's turn was starting
        made a fourth report turn in a row (review round 2 on #942). Each counts as a turn of
        its own: a held one whose start is under way and a pending one that reaches the actor
        after that start make two."""
        session_id = batch.session["id"]
        held = {m["message_id"] for m in self.store.held(session_id)}
        return sum(
            1
            for mid in self.store.get(reports_key(session_id), [])
            if mid in held or self.store.get(f"pending_delivery:{batch.bot}:{mid}") is not None
        )

    def agent(self, batch: EndBatch) -> str:
        bridge = self.bridge(batch.bot)
        return AGENT_NAMES.get(bridge.backend.kind if bridge else "", "the agent")

    def bridge(self, bot: str) -> BotBridge | None:
        return next((b for b in self.bridges if b.bot.name == bot), None)

    async def sentence(self, batch: EndBatch) -> str:
        """The post's last sentence. Called again on each try of a post that failed, each time
        on a new batch, so it changes nothing lasting: its decision is kept on the batch alone,
        for `record`, which comes once with the batch whose try went out (#940's contract)."""
        decision = self.decide(batch)
        batch.report_decision = decision  # type: ignore[attr-defined]
        if decision == "report":
            return f"{self.agent(batch)} will report on it."
        if decision == "wait":
            return f"Send a message to have {self.agent(batch)} report on it."
        return ""

    def record(self, batch: EndBatch, message_id: int | None) -> Callable[[], None]:
        """The note or the pending delivery, in the watcher's transaction that marks the jobs
        handled (Codex round 2 on #970): every write joins it, so that the report the post
        announced is never lost between the two. Returned: the prompt put to the actor, for
        after the commit."""
        # what the post that went out said; a batch posted without a sentence decides now
        decision = getattr(batch, "report_decision", None) or self.decide(batch)
        session_id = batch.session["id"]
        notes = [job_line(e) for e in batch.ends if e.ending.kind == "lost"]
        reportable = self.reportable(batch)
        after = nothing
        if decision == "report" and message_id is None:
            decision = "wait"  # no end post to deliver: the prompt waits for a person's turn
        elif decision == "report" and self.delivered(batch.bot, message_id):
            # found landed for its other jobs on an earlier try, the post's message went to
            # the session with their prompt: delivered twice, it is dropped (Codex 6721684520)
            decision, after = "wait", self.noted(batch, reportable, message_id)
        if decision == "wait":
            notes.append(prompt(reportable))
        if notes:
            with self.store.transaction():
                key = notes_key(session_id)
                self.store.set(key, [*self.store.get(key, []), *notes])
        if decision == "report" and message_id is not None:
            return self.deliver(batch, message_id, reportable)
        return after

    @staticmethod
    def noted(batch: EndBatch, ends: list[JobEnd], message_id: int) -> Callable[[], None]:
        """The log line, after the commit, of a prompt that waits as a note because its end
        post's message went to the session already: the post said that the agent will report
        on these jobs too, and the journal says why their report waits for a person's turn
        (review round 2 on #970, N6). The jobs' ids and names, never the prompt."""

        def log() -> None:
            LOG.info(
                "Report on %s waits as a note: end post %d went to the session already",
                ", ".join(f"{e.job.id} `{e.job.name}`" for e in ends),
                message_id,
                extra={"event": "job_report", "bot": batch.bot, "session": batch.session["id"]},
            )

        return log

    def delivered(self, bot: str, message_id: int) -> bool:
        """Whether the end post's message went to a session already, as a report prompt: still
        pending, or received by the actor (the inbound receipt that drops a second copy, which
        stays after its turn starts, unlike `reports_key`)."""
        if self.store.get(f"pending_delivery:{bot}:{message_id}") is not None:
            return True
        receipts = self.store.rows("inbound", bot=bot, message_id=message_id)
        return any(r["disposition"] == "delivered" for r in receipts)

    def deliver(self, batch: EndBatch, message_id: int, ends: list[JobEnd]) -> Callable[[], None]:
        """The prompt as a message to the session from the job's owner, held as a pending
        delivery (a restart delivers it again), in the caller's transaction; the returned call
        puts it to the session's actor, after the commit. It counts against the cap once a turn
        of report prompts starts (`turn_started`), not here: one steered into a running turn is
        no report turn (review round 1 on #942)."""
        from zab.core.session import Delivery

        session_id = batch.session["id"]
        owner = next((e.owner_id for e in ends if e.owner_id), None)
        if owner is None:  # no one to report to (`decide` leaves this out): the prompt waits
            with self.store.transaction():
                key = notes_key(session_id)
                self.store.set(key, [*self.store.get(key, []), prompt(ends)])
            return nothing
        bridge = self.bridge(batch.bot)
        name = str(
            (bridge.users.get(owner, {}) if bridge else {}).get("full_name")
            or self.store.get(f"sender:{session_id}", "user")
        )
        text = prompt(ends)
        with self.store.transaction():
            ids = self.store.get(reports_key(session_id), [])
            self.store.set(reports_key(session_id), [*ids, message_id][-100:])
            self.store.set(
                f"pending_delivery:{batch.bot}:{message_id}",
                dict(message_id=message_id, sender_id=owner, sender=name, text=text,
                     session_id=session_id, message=None, report=True),
            )  # fmt: skip

        def put() -> None:
            actor = bridge.actors.get(batch.session["native_id"]) if bridge else None
            if actor is not None:
                actor.put(Delivery(message_id, owner, name, text, report=True))
            LOG.info(
                "Report turn for %s",
                ", ".join(e.job.id for e in ends),
                extra={"event": "job_report", "bot": batch.bot, "session": session_id},
            )

        return put


def not_taken(store: Store, bot: str, session_id: str, delivery: Delivery) -> None:
    """A report prompt reached a session that takes no turn (read-only or detached): it waits
    as a note, and its pending delivery goes, which a restart would otherwise deliver again and
    `ReportHook.coming` would count as a report turn to come until then (review of #942). A
    copy whose pending delivery is gone already was handled: nothing is added twice."""
    pending = f"pending_delivery:{bot}:{delivery.message_id}"
    with store.transaction():
        if store.get(pending) is None:
            return
        store.delete(pending)
        store.set(notes_key(session_id), [*take_notes(store, session_id), delivery.text])
        ids = store.get(reports_key(session_id), [])
        if delivery.message_id in ids:
            store.set(reports_key(session_id), [i for i in ids if i != delivery.message_id])


def take_notes(store: Store, session_id: str) -> list[str]:
    """The notes that wait for the session's next turn for a person, left in place."""
    notes = store.get(notes_key(session_id), [])
    return [n for n in notes if isinstance(n, str)] if isinstance(notes, list) else []


def is_report(store: Store, session_id: str, message_id: int) -> bool:
    return message_id in store.get(reports_key(session_id), [])


def person_spoke(store: Store, session_id: str) -> None:
    """A person's message reached the session: report turns may follow again (the cap)."""
    if store.get(count_key(session_id), 0):
        store.set(count_key(session_id), 0)


def turn_started(
    store: Store, session_id: str, message_ids: Sequence[int], notes: Sequence[str]
) -> None:
    """A turn's start was accepted: the notes it carried are passed on, and its report
    prompts no longer need telling apart. Only those notes go: the watcher may have added one
    while the start was awaited, which waits for the next turn (Codex round 1 on #942). Notes
    are only added at the end, so the ones the turn carried are the list's head. A turn of
    report prompts alone is a report turn, and counts against the cap (§7.14: "3 report turns
    in a row"; review round 1 on #942)."""
    with store.transaction():
        if notes:
            current = take_notes(store, session_id)
            if current[: len(notes)] == list(notes):
                rest = current[len(notes) :]
            else:  # not as read: drop exactly what was carried, keep the rest
                rest = list(current)
                for note in notes:
                    if note in rest:
                        rest.remove(note)
            if rest:
                store.set(notes_key(session_id), rest)
            else:
                store.delete(notes_key(session_id))
        ids = store.get(reports_key(session_id), [])
        if message_ids and all(i in ids for i in message_ids):
            store.set(count_key(session_id), store.get(count_key(session_id), 0) + 1)
        if ids and set(ids) & set(message_ids):
            store.set(reports_key(session_id), [i for i in ids if i not in message_ids])
