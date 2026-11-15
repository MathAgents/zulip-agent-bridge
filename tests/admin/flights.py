"""Plans in each state a deploy's restart reaches (#989), made through the real `PlanStore` and
its approvals as the bot makes them, into a real `state.db`: what `zab.admin.inflight` reads,
here and in the deploy rig (`tests/workshop/test_deploy_plans.py`)."""

from __future__ import annotations

from zab.admin.plans import APPLYING, PLANNING, Plan, PlanStore

from .conftest import HASH, make, posted


def approve(store: PlanStore, plan: Plan) -> Plan:
    """PENDING → APPROVED, as the last approval's record moves it (`Approvals`)."""
    return store.record(plan.id, lambda db: True)


def each_state(store: PlanStore) -> dict[str, Plan]:
    """One plan of harvard-2026's bot in each state a restart reaches, in this order: P-1 being
    planned, P-2 pending, P-3 approved, P-4 applying."""
    planning = store.begin(workshop="harvard-2026", bot=901, requester=103, command="!stop alice")
    pending = posted(store, make("!stop alice"))
    approved = approve(store, posted(store, make("!stop bob")))
    applying = store.begin_apply(approve(store, posted(store, make("!stop carol"))).id, HASH)
    return {"planning": planning, "pending": pending, "approved": approved, "applying": applying}


def month_end_run(store: PlanStore, schedule: int = 1) -> Plan:
    """A month-end run's plan, as `planning.Planning._run` makes it: begun, then APPLYING with
    its note, no approval."""
    plan = store.begin(workshop="harvard-2026", bot=901, requester=901, command="!retire alice")
    return store.move(plan.id, (PLANNING,), APPLYING, f"a month-end run of P-{schedule}")


def server_plan(store: PlanStore) -> Plan:
    """A plan of the server's bot (workshop ""), applying: `!new-workshop`, say."""
    plan = store.begin(workshop="", bot=900, requester=101, command="!new-workshop mit-2027")
    return store.move(plan.id, (PLANNING,), APPLYING)
