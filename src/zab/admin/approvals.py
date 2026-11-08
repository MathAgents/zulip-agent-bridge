"""Who may approve a plan, and how (docs/ADMIN.md §2.5).

- **Classes.** `requirement` puts each plan in a class of §2.5's table: a change takes one
  approval, which the requester may give; a plan of `!stop`, `!rollout`, `!idle-stop` or
  `!uninstall` for more than half the workshop's fleet, its rows with a VM, also takes the
  typed phrase `<verb> <count>`, however the selection is written, since the count is that of
  `Action.targets` (spares included, which can only add a phrase); the destructive verbs,
  `!teardown`, `!deactivate-bots` (stage 3) and `!retire` (#559), and identity rebinds, one
  row per plan, take `destructive_approvals` distinct approvers and a phrase: `teardown
  <count>`, with `unsynced` after it when the command tears down rows that no `!final-sync`
  pushed (§3, action 7); `retire <count>`, and `unsynced` likewise; `month-end <count>` for a
  `!month-end schedule` (§11.5, #570). A row a `!retire` ended
  never counts in the fleet (#559). The rebind verb comes later, as do the deploy and server
  classes of the `[global]` bot with stages 6 and 7: the rules are here, and tested, before
  any verb reaches them. `Approvals.post` is the one way to post a plan, and it classes the
  plan itself.
- **A global admin approves alone** (Drew, Oct 22; #558). A plan that takes more than one
  approver (destructive, rebind, server) is complete with one approval from a global admin,
  as admin.toml has them at approval time, whatever was given before; an organizer who is not
  one still needs a second, distinct approver. The phrase is still required: it guards
  against a misclick, not against the admins. The answer says which rule completed the plan
  (`completed`).
- **Guards.** An `!approve` counts only if every check of `GUARDS` passes, in order: sent to the
  plan's own bot, in that bot's channel; from a human account, active at approval time
  (`GET /users/{id}`, as the bot passes it in `Sender`), that approves the plan's workshop or
  class; for a pending plan within its TTL; in a message later than the plan's approval
  message; naming the plan's hash and phrase; and from someone who hasn't approved it yet.
  Anything else is refused with its reason, and the plan stays as it was.
- **Veto.** `reject`, its own entry point, takes the guards up to the TTL: any approver of a
  pending plan may reject it on its bot, also after another approver's approval (§2.5).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from zab.admin.actions import Action, Fleet
from zab.admin.config import AdminConfig, Bot
from zab.admin.plans import (
    APPROVED,
    EXPIRED,
    PENDING,
    REJECTED,
    Plan,
    PlanError,
    PlanStore,
    Requirement,
)

CHANGE, ABOVE_HALF, DESTRUCTIVE, REBIND = "change", "above-half", "destructive", "rebind"
DEPLOY, SERVER = "deploy", "server"  # the [global] bot's !upgrade-server and !new-workshop
#: Above half the fleet, these verbs' plans take the phrase `<verb> <count>` (§2.5, §1.5).
PHRASE_ABOVE_HALF = frozenset({"stop", "rollout", "idle-stop", "uninstall"})
DESTRUCTIVE_VERBS = frozenset({"teardown", "deactivate-bots", "retire", "month-end schedule"})
#: A destructive verb's first word in the phrase, where it isn't the verb (§11.5, #570).
PHRASE_WORD = {"month-end schedule": "month-end"}
#: The answer to the approval that completes a plan of several approvers, by the rule that
#: completed it (#558).
BY_ADMIN, BY_COUNT = "a global admin's approval", "{} distinct approvers"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS approvals (
    plan INTEGER NOT NULL REFERENCES plans (id),
    approver INTEGER NOT NULL,
    message INTEGER NOT NULL,
    at REAL NOT NULL,
    PRIMARY KEY (plan, approver)
);
"""


def requirement(
    action: Action, fleet: Fleet | None, config: AdminConfig, *, rebinds: tuple[str, ...] = ()
) -> Requirement:
    """The class of `action`'s plan. `fleet` is the bot's own workshop, None for a server verb
    on the `[global]` bot; `rebinds` are the rows whose provisioned `email` or `github` a
    `!roster import` changes (stage 3): at most one per plan, as with `!rebind slug`."""
    verb, count, many = action.verb, str(len(action.targets)), config.destructive_approvals
    if verb in ("upgrade-server", "new-workshop"):
        if action.workshop or fleet is not None:
            raise PlanError(f"!{verb} is a server verb, for the [global] bot")
        if verb == "upgrade-server":
            return Requirement(DEPLOY)
        return Requirement(SERVER, many, ("new-workshop", action.args[0]))
    if fleet is None or fleet.workshop.name != action.workshop:
        raise PlanError("a workshop's plan is classed against its own fleet")
    if not isinstance(rebinds, tuple):
        raise PlanError("the rebound rows are a tuple of slugs")
    named = action.args[:1] if verb == "rebind" else ()
    slugs = tuple(dict.fromkeys((*rebinds, *named)))
    if len(slugs) > 1:
        raise PlanError("a plan rebinds one row: change one row's identity per plan")
    if slugs:
        if slugs[0] not in fleet.slugs:
            raise PlanError("a rebind names a row of this workshop")
        return Requirement(REBIND, many, ("rebind", slugs[0]))
    if verb in DESTRUCTIVE_VERBS:  # `unsynced` is said again, where work may be lost
        lose = verb in ("teardown", "retire") and "unsynced" in action.args
        said = ("unsynced",) if lose else ()
        return Requirement(DESTRUCTIVE, many, (PHRASE_WORD.get(verb, verb), count, *said))
    # the fleet: rows with a VM, never one a !retire ended (#559)
    vms = sum(1 for row in fleet.rows if row.ip and fleet.present(row.slug))
    if verb in PHRASE_ABOVE_HALF and 2 * len(action.targets) > vms:
        return Requirement(ABOVE_HALF, 1, (verb, count))
    return Requirement(CHANGE)


def plan_bot(config: AdminConfig, plan: Plan) -> Bot | None:
    """The bot `plan` is bound to, as admin.toml has it now; None if that bot has changed."""
    if plan.workshop:
        ws = config.workshop(plan.workshop)
        bot = ws.bot if ws else None
    else:
        bot = config.global_bot
    return bot if bot is not None and bot.user_id == plan.bot else None


def approvers(config: AdminConfig, plan: Plan) -> frozenset[int]:
    """Who may approve or reject `plan`: its workshop's organizers and the global admins, never
    another workshop's organizers; the deployers for a deploy plan; the global admins for a
    server plan."""
    klass = plan.requirement.klass if plan.requirement else ""
    if not plan.workshop:
        return {DEPLOY: config.deployers, SERVER: config.admins}.get(klass, frozenset())
    ws = config.workshop(plan.workshop)
    if ws is None or klass in ("", DEPLOY, SERVER):
        return frozenset()
    return config.approvers(ws)


def takes(needs: Requirement) -> str:
    """Who completes a plan of `needs`, in the plan's words: a server plan is a global admin's
    alone, since only they approve it, whatever `destructive_approvals` says; otherwise "" for
    one approver, or `needed` distinct approvers, or a global admin alone (#558)."""
    if needs.klass == SERVER:
        return "1 global admin"
    if needs.needed <= 1:
        return ""
    return f"{needs.needed} distinct approvers, or a global admin alone"


def completed(config: AdminConfig, needs: Requirement, approved: tuple[int, ...], user: int) -> str:
    """Why `user`'s approval, the last of `approved`, completes a plan of `needs`, or "" when
    it doesn't: the count of distinct approvers, or a global admin's approval (#558), which
    alone completes a server plan, whose approvers are all global admins."""
    if needs.klass != SERVER and len(approved) >= needs.needed:
        return BY_COUNT.format(needs.needed)
    return BY_ADMIN if user in config.admins else ""


@dataclass(frozen=True, slots=True)
class Sender:
    """The sender of an `!approve` or `!reject`, from `GET /users/{id}` when it arrives."""

    user_id: int
    is_bot: bool
    is_active: bool


@dataclass(frozen=True, slots=True)
class Received:
    """An `!approve` or `!reject`, as a bot's live event queue delivered it (§2.1)."""

    message_id: int
    bot: int  # the user ID of the bot whose queue delivered it
    channel: int  # the stream ID it was sent in
    sender: Sender


@dataclass(frozen=True, slots=True)
class Decision:
    """`code` names the guard that refused, or says what happened: "recorded" (another approver
    is needed), "approved" (the plan is APPROVED: recompute its hash and `begin_apply`) or
    "rejected". `reason` is one line for the reply."""

    ok: bool
    code: str
    reason: str
    plan: Plan | None


@dataclass(frozen=True, slots=True)
class _Ask:
    config: AdminConfig
    plan: Plan
    received: Received
    now: float
    hash6: str  # "" for a veto, which names no hash
    phrase: tuple[str, ...]
    approved: frozenset[int]  # who has approved the plan already


def _channels(a: _Ask) -> frozenset[int]:
    bot = plan_bot(a.config, a.plan)
    return bot.channels if bot else frozenset()


def _phrase(a: _Ask) -> bool:
    return a.plan.requirement is not None and a.phrase == a.plan.requirement.phrase


#: (code, the check, whether `!reject` takes it too), in order.
GUARDS: tuple[tuple[str, Callable[[_Ask], bool], bool], ...] = (
    ("other-bot", lambda a: a.received.bot == a.plan.bot, True),
    ("channel", lambda a: a.received.channel in _channels(a), True),
    ("bot-sender", lambda a: a.received.sender.is_bot is False, True),
    ("inactive", lambda a: a.received.sender.is_active is True, True),
    ("not-approver", lambda a: a.received.sender.user_id in approvers(a.config, a.plan), True),
    ("not-pending", lambda a: a.plan.state == PENDING, True),
    ("expired", lambda a: a.plan.expires is not None and a.now < a.plan.expires, True),
    ("early", lambda a: a.received.message_id > (a.plan.approval_message or 2**63), False),
    ("hash", lambda a: a.hash6 == a.plan.hash[:6], False),
    ("phrase", _phrase, False),
    ("again", lambda a: a.received.sender.user_id not in a.approved, False),
)


def _reason(code: str, a: _Ask) -> str:
    name, plan = a.plan.name, a.plan
    phrase = " ".join(plan.requirement.phrase) if plan.requirement else ""
    reasons = {
        "other-bot": f"{name} is another bot's plan: answer it on the bot that posted it",
        "channel": f"answer {name} in its bot's channel",
        "bot-sender": "bots never approve",
        "inactive": "a deactivated account never approves",
        "not-approver": f"not an approver of {name}: nothing was done",
        "not-pending": f"{name} is {plan.state}, not pending",
        "expired": f"{name} expired: request again",
        "early": f"this message is older than {name}",
        "hash": f"that is not {name}'s hash: read the plan again",
        "phrase": f"{name} needs the phrase `{phrase}` after the hash"
        if phrase
        else f"{name} takes no phrase",
        "again": f"you approved {name} already: it needs another approver",
    }
    return reasons[code]


class Approvals:
    """The approvals of the plans in `store`, checked against admin.toml (`config`)."""

    def __init__(self, store: PlanStore, config: AdminConfig) -> None:
        self.store, self.config = store, config
        store.db.executescript(_SCHEMA)

    def post(
        self,
        plan_id: int,
        action: Action,
        fleet: Fleet | None,
        plan_hash: str,
        *,
        plan_message: int,
        approval_message: int,
        rebinds: tuple[str, ...] = (),
    ) -> Plan:
        """Post `plan_id` for `action` (PLANNING → PENDING), classed here from the action, the
        bot's own `fleet` (None on the `[global]` bot) and the rows it `rebinds`."""
        needs = requirement(action, fleet, self.config, rebinds=rebinds)
        ids = {"plan_message": plan_message, "approval_message": approval_message}
        return self.store._post(plan_id, action, needs, plan_hash, **ids)

    def approve(
        self, received: Received, plan_id: int, hash6: str, phrase: tuple[str, ...] = ()
    ) -> Decision:
        """`!approve P-<plan_id> <hash6> [phrase…]`."""
        if not isinstance(hash6, str) or not isinstance(phrase, tuple):
            raise PlanError("an approval names the plan's hash, and its phrase as words")
        return self._answer(received, plan_id, hash6, phrase, veto=False)

    def reject(self, received: Received, plan_id: int) -> Decision:
        """`!reject P-<plan_id>`: the veto."""
        return self._answer(received, plan_id, "", (), veto=True)

    def approved_by(self, plan_id: int) -> tuple[int, ...]:
        rows = self.store.db.execute(
            "SELECT approver FROM approvals WHERE plan = ? ORDER BY message", (plan_id,)
        )
        return tuple(row[0] for row in rows)

    def _answer(
        self, received: Received, plan_id: int, hash6: str, phrase: tuple[str, ...], *, veto: bool
    ) -> Decision:
        plan = self.store.get(plan_id)
        if plan is None:
            return Decision(False, "unknown", f"no plan P-{plan_id} on this server", None)
        now, done = self.store.clock(), frozenset(self.approved_by(plan.id))
        ask = _Ask(self.config, plan, received, now, hash6, phrase, done)
        for code, passes, on_reject in GUARDS:
            if (on_reject or not veto) and not passes(ask):
                if code == "expired" and plan.state == PENDING:
                    plan = self.store.move(plan.id, (PENDING,), EXPIRED)
                return Decision(False, code, _reason(code, ask), plan)
        user = received.sender.user_id
        if veto:
            plan = self.store.move(plan.id, (PENDING,), REJECTED, f"rejected by user {user}")
            return Decision(True, "rejected", f"{plan.name} rejected: nothing was done", plan)
        assert plan.requirement is not None  # PENDING: posted with one
        needs = plan.requirement
        pid, needed, why = plan.id, needs.needed, ""

        def write(db: sqlite3.Connection) -> bool:  # in the store's transaction, with the move
            nonlocal why
            db.execute(
                "INSERT OR IGNORE INTO approvals (plan, approver, message, at) VALUES (?, ?, ?, ?)",
                (pid, user, received.message_id, now),
            )
            why = completed(self.config, needs, self.approved_by(pid), user)
            return bool(why)

        plan = self.store.record(pid, write)
        if plan.state != APPROVED:
            have = len(self.approved_by(pid))
            reason = f"{plan.name}: {have} of {needed} approvals, from distinct approvers"
            return Decision(True, "recorded", reason, plan)
        said = f" ({why})" if takes(needs) else ""  # a plan of one approver: no rule to name
        return Decision(True, "approved", f"{plan.name} approved{said}", plan)
