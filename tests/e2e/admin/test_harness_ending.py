"""A9's harness, offline (docs/ADMIN.md §7, "A9 in detail"; #356, #357): the readers against
what the product itself prints (the bot's teardown plan and approval widget, the scripts'
tables, teardown.py's dry run), the guard's wait after a `!protect` (#378), the throwaway row,
and the two-approver admin.toml under the strict loader. The product's texts that A9's live
checks match (`ending.refused_unsynced`, `stale_by_the_guard`, `recorded`, `needs_phrase`,
`approved_already`) are rendered here by the product's own code: a change of wording fails
here, not 40 minutes into a live run (review of #385)."""

from __future__ import annotations

import dataclasses
import datetime as dt
import importlib
import json
import re
from pathlib import Path

import pytest

from e2e.admin.fakes import fake_ids, test_toml
from e2e.admin.harness import admin_toml, ending
from e2e.admin.harness import rig as rigs
from e2e.admin.harness.chat import Refused, Talk, parse_plan, table, widget_replies
from e2e.admin.harness.ending import SYNCED, UNSYNCED
from e2e.admin.test_scenarios_2_bot import ROSTER_COLUMNS
from zab.admin import actions, planning
from zab.admin.actions import ActionError, Ending, Fleet, Layout, Protection, Row, build
from zab.admin.approvals import Approvals, Received, Sender
from zab.admin.audit import Head
from zab.admin.config import parse_admin_config
from zab.admin.grammar import RESERVED, SLUG_RE, SPARE_RE, Command, Context, Roster, parse
from zab.admin.plans import Plan, PlanStore, Requirement

WORKSHOP = Path(__file__).resolve().parents[3] / "workshop"
A, B = "zab-test-a", "zab-test-b"
TMP = ending.throwaway(1_760_000_000)
DIGEST = "3f9a2c" + "ab" * 29
UNINSTALLED = "P-7 at 2026-10-15 14:02 UTC"
SYNC = "P-8 at 2026-10-15 14:09 UTC"


@pytest.fixture
def scripts(monkeypatch):
    """The standalone scripts, imported by path as tests/workshop does."""
    for sub in ("aws", "admin"):
        monkeypatch.syspath_prepend(str(WORKSHOP / sub))
    return importlib.import_module


def fleet(ending_of: dict[str, Ending], rows: tuple[Row, ...] = (), **more) -> Fleet:
    config = parse_admin_config(test_toml())
    ws = config.workshop(rigs.E2E.name)
    assert ws is not None
    here = ws.aws.region
    rows = rows or (Row(A, "198.51.100.1", here), Row(B, "198.51.100.2", here), Row(TMP, "", here))
    return Fleet(ws, rows, ("zab-spare-1",), config.release_pattern, ending=ending_of, **more)


def action(message: str, the_fleet: Fleet) -> actions.Action:
    roster = Roster(the_fleet.slugs, the_fleet.spares)
    context = Context(21, "Workshop admin · e2e", the_fleet.release_pattern, roster)
    command = parse(message, context)
    assert isinstance(command, Command), command
    return build(command, the_fleet, layout=Layout())


def vm(slug: str, iid: str) -> dict:
    tags = [{"Key": "Name", "Value": f"ws-{slug}"}, {"Key": "participant", "Value": slug}]
    return {"InstanceId": iid, "Tags": tags, "State": {"Name": "running"}}


def plan_text(act: actions.Action, needs: Requirement, dry: str, limit: int = 10000) -> str:
    """The plan message as the bot posts it, with its attachment when cut (`whole`)."""
    plan = Plan(42, rigs.E2E.name, 21, 11, "!teardown " + " ".join(act.args), "planning", 0.0,
                None, (), "", None, None, None, "", None)  # fmt: skip
    message = planning.plan_message(plan, act, DIGEST, needs, requester="@_**Zab Tester|11**",
                                    expires=0.0, output=dry, head=Head(9, "c" * 64),
                                    limit=limit)  # fmt: skip
    return message.text + ("\n" + message.full if message.full else "")


# ------------------------------------------------------------------------- the teardown plan


def test_the_teardown_of_all_as_the_bot_plans_it(scripts):
    teardown = scripts("teardown")
    act = action("!teardown all unsynced", fleet({A: Ending(UNINSTALLED), B: Ending(UNINSTALLED)}))
    addresses = [{"AllocationId": "eipalloc-0a", "PublicIp": "198.51.100.1"},
                 {"AllocationId": "eipalloc-0b", "PublicIp": "198.51.100.2"}]  # fmt: skip
    dry = teardown.describe([vm(A, "i-0a"), vm(B, "i-0b")], addresses) + "\ndry run: nothing"
    needs = Requirement("destructive", 2, ("teardown", "3", "unsynced"))
    for limit in (10000, 1500):  # whole, and cut to fit with the whole plan attached
        text = plan_text(act, needs, dry, limit)
        plan = parse_plan(text)
        assert plan is not None and plan.targets == [A, B, TMP]
        assert plan.phrase == "teardown 3 unsynced" and "2 distinct approvers" in text
        count, unsynced, rows = ending.participant_rows(text)
        assert (count, unsynced) == (3, 3) and list(rows) == [A, B, TMP]
        assert re.fullmatch(UNSYNCED, rows[A]) and re.fullmatch(UNSYNCED, rows[B])
        assert rows[TMP] == "no VM in the roster (one with its tag goes too), not synced: unsynced"
        found = ending.dry_run(text)
        assert found.terminate == {A: "i-0a", B: "i-0b"} and not found.keep
        assert found.release == ["eipalloc-0a", "eipalloc-0b"]


def test_a_cut_plan_lists_every_row_from_its_attachment(scripts):
    teardown = scripts("teardown")
    here = parse_admin_config(test_toml()).workshop(rigs.E2E.name).aws.region
    rows = tuple(Row(f"zab-test-{i:02d}", f"198.51.100.{i + 1}", here) for i in range(60))
    act = action("!teardown all unsynced", fleet({}, rows))
    needs = Requirement("destructive", 2, ("teardown", "60", "unsynced"))
    text = plan_text(act, needs, teardown.describe([], []), limit=2000)
    shown = text.split("Participant rows", 2)  # the message's list, then the attachment's
    assert len(shown) == 3 and shown[1].count("\n- zab-test-") < 60  # the message's is cut
    count, unsynced, found = ending.participant_rows(text)
    assert (count, unsynced, len(found)) == (60, 60, 60)


def test_a_synced_row_and_a_protected_vm(scripts):
    teardown = scripts("teardown")
    act = action("!teardown zab-test-a", fleet({A: Ending(UNINSTALLED, SYNC, True, UNINSTALLED)}))
    dry = teardown.describe([], [], [vm(A, "i-0a")])
    text = plan_text(act, Requirement("destructive", 2, ("teardown", "1")), dry)
    count, unsynced, rows = ending.participant_rows(text)
    assert (count, unsynced) == (1, 0) and re.fullmatch(SYNCED, rows[A]), rows
    assert not re.fullmatch(UNSYNCED, rows[A])
    found = ending.dry_run(text)
    assert found == ending.TeardownDryRun(keep={A: "i-0a"})
    assert ending.dry_run(text.replace("protection is on", "protection is off")).keep == {}


def test_a_teardown_of_spares_has_no_rows(scripts):
    teardown = scripts("teardown")
    act = action("!teardown spares", fleet({}))
    dry = teardown.describe([vm("zab-spare-1", "i-0c")], [{"AllocationId": "eipalloc-0c"}])
    text = plan_text(act, Requirement("destructive", 2, ("teardown", "1")), dry)
    assert ending.dry_run(text).terminate == {"zab-spare-1": "i-0c"}
    with pytest.raises(AssertionError, match="no participant rows"):
        ending.participant_rows(text)


def test_no_widget_where_a_phrase_is_typed():
    """#542: A9 reads the approval message of a plan with a phrase as having no buttons."""

    def posted(needs: Requirement) -> dict:
        _, widget = planning.approval_message(42, DIGEST, needs, "!teardown spares")
        if widget is None:
            return {"id": 7, "submessages": []}
        sub = {"msg_type": "widget", "content": json.dumps(widget), "sender_id": 21}
        return {"id": 7, "submessages": [sub]}

    phrase = Requirement("destructive", 2, ("teardown", "1"))
    assert widget_replies(posted(phrase)) is None
    assert widget_replies(posted(Requirement("change"))) == ["!approve P-42 3f9a2c", "!reject P-42"]
    poll = {"widget_type": "poll", "extra_data": {"choices": [{"reply": "!approve P-42"}]}}
    sub = {"msg_type": "widget", "content": json.dumps(poll)}
    assert widget_replies({"id": 7, "submessages": [sub]}) is None
    assert widget_replies({"id": 7, "submessages": []}) is None
    assert widget_replies({"id": 7}) is None


def test_the_tables_as_the_scripts_print_them(scripts):
    common, final_sync = scripts("admin_common"), scripts("final_sync")
    collect, provision = scripts("collect_exports"), scripts("zulip_provision")
    rerun = {"worktrees": [{"state": "clean", "push": "up to date"}], "index": "unchanged"}
    synced = common.format_table(final_sync.COLUMNS, [
        final_sync.SyncResult(A, "198.51.100.1", rerun).row()])  # fmt: skip
    result = f"✅ **P-9** · `!final-sync {A}`: done in 9 s, every step exited 0\n```\n{synced}\n"
    result += "\nreport: /var/lib/x.json; no problems\n```\n`audit #12 · chain cccccccccccc`"
    row = table(result, "participant")[0]
    assert (row["committed"], row["pushed"], row["TOPICS.md"], row["problems"]) == (
        "0", "0", "unchanged", "")  # fmt: skip
    skipped = "skipped: not provisioned, nothing to export"
    for index in ending.SYNC_IDLE:  # what a rerun with nothing to do writes: fine to the script
        assert index in final_sync.INDEX_FINE or index.startswith(final_sync.INDEX_SKIPS_FINE)
        idle = {"worktrees": [{"state": "clean", "push": "up to date"}], "index": index}
        assert final_sync.SyncResult(A, "198.51.100.1", idle).row()[-2:] == [index, ""]
    exported = common.format_table(collect.COLUMNS, [
        collect.ExportResult(A, "198.51.100.1", "export-zab-test-a.tar.gz", "2 KB").row(),
        collect.ExportResult(B, note=skipped).row()])  # fmt: skip
    rows = {r["participant"]: r for r in table(f"```\n{exported}\n```", "participant")}
    assert rows[A]["result"] == "ok" and rows[B]["result"].startswith("skipped")
    report = provision.Report(TMP, bots={"codex": "deactivated", "claude": "deactivated"})
    rows = table(provision.render([report]), "participant")
    assert rows[0]["bots"] == "codex: deactivated, claude: deactivated"
    with pytest.raises(AssertionError, match="no table"):
        table("participant  host\nnot a rule", "participant")


def test_the_uninstall_table(scripts):
    push = scripts("push_config")
    results = [push.VmResult(slug=s, host=ip, installed=True, doctor_ok=True) for s, ip in
               ((A, "198.51.100.1"), (B, "198.51.100.2"))]  # fmt: skip
    rows = {r["participant"]: r for r in table(push.render(results, uninstall=True), "participant")}
    assert {s: rows[s]["uninstall"] for s in (A, B)} == {A: "ok", B: "ok"}


# ------------------------------------------------------------------------------- the guard


#: When the guard's example `!protect` applied: its refusal names 23:56, and 00:02 to retry.
AT = dt.datetime(2026, 10, 15, 23, 56, 30, tzinfo=dt.UTC)


def guarded(plan: str = "P-12", now: dt.datetime = AT + dt.timedelta(seconds=90)) -> str:
    """The guard's refusal of `!teardown all unsynced` at `now`, as the bot posts it: the build's
    `ActionError` (#382), in `Planning._refuse`'s words."""
    protection = Protection("P-11", "protect", AT.timestamp())
    the_fleet = fleet({}, protection=protection, read_at=now.timestamp())
    with pytest.raises(ActionError) as refused:
        action("!teardown all unsynced", the_fleet)
    why = " ".join(str(refused.value).split())[: planning.NOTE_CHARS]
    return f"❌ {plan} refused: {why}; nothing was done"


def stale(plan: str = "P-12") -> str:
    """The same guard at apply (`Planning.rebuild`), in `Planning._apply`'s words."""
    why = guarded().split(" refused: ", 1)[1].removesuffix("; nothing was done")
    note = " ".join(f"it no longer builds: {why}".split())[: planning.NOTE_CHARS]
    return f"❌ {plan} is stale: {note}. Request again."


def test_the_time_to_request_again():
    assert actions.PROTECT_SETTLE == ending.PROTECT_SETTLE_S
    said = guarded()
    assert "P-11 (!protect) changed termination protection here at 23:56 UTC" in said
    late = dt.datetime(2026, 10, 15, 23, 58, 30, tzinfo=dt.UTC)
    assert ending.retry_at(said, late) == dt.datetime(2026, 10, 16, 0, 2, tzinfo=dt.UTC)
    early = dt.datetime(2026, 10, 16, 0, 1, tzinfo=dt.UTC)  # past midnight already
    assert ending.retry_at(said, early) == dt.datetime(2026, 10, 16, 0, 2, tzinfo=dt.UTC)
    assert ending.retry_at(stale(), early) is not None
    launch = Protection("P-11", "launch", AT.timestamp()).wait(AT.timestamp())
    assert "(!launch … protect)" in launch and ending.retry_at(launch, early) is not None
    assert ending.retry_at("❌ P-12 refused: the dry run failed; nothing was done", early) is None
    # a time to retry is the guard's only: not any text that names one
    assert ending.retry_at("❌ P-1 refused: busy. Request it again from 00:02 UTC", early) is None


def test_the_texts_a9_matches_as_the_product_renders_them():
    """Step 5's refusal, step 4's STALE through the guard (note within `_apply`'s
    `NOTE_CHARS`, and its time to retry kept), and step 6's refusal by the guard."""
    with pytest.raises(ActionError) as refused:
        action("!teardown zab-test-b", fleet({B: Ending(UNINSTALLED)}))
    assert ending.refused_unsynced(B) in str(refused.value)
    assert re.match(ending.stale_by_the_guard("P-12"), stale())
    assert "Request it again from 00:02 UTC" in stale() and len(stale()) < 340
    assert "changed termination protection" in guarded()  # the `refused` record's reason
    assert not re.match(ending.stale_by_the_guard("P-1"), stale())  # P-12's, not P-1's


@pytest.mark.parametrize("alone", [False, True])
def test_the_approval_answers_as_the_product_gives_them(tmp_path, alone):
    """An organizer's approval isn't enough, the phrase must be the plan's, the same approver
    counts once, and a global admin's completes it, alone too (#558): `Approvals` with A9's
    admin.toml, where `zab-tester2` organizes and `zab-tester` is a global admin, as the bot
    answers (`Planning.answer`: mark, reason)."""
    ids = fake_ids()
    groups = {rigs.E2E.name: "sg-0123456789abcdef0", rigs.E2E2.name: "sg-0123456789abcdef1"}
    shape = dict(region="us-east-1", ami="ami-0123456789abcdef0", groups=groups)
    config = parse_admin_config(admin_toml.ending(ids, **shape))
    the_fleet = fleet({A: Ending(UNINSTALLED), B: Ending(UNINSTALLED)})
    act = action("!teardown all unsynced", the_fleet)
    store = PlanStore(tmp_path / "state.db", ttl_minutes=15, floor=0, clock=lambda: 1000.0)
    try:
        approvals = Approvals(store, config)
        plan = store.begin(workshop=rigs.E2E.name, bot=21, requester=ids.tester,
                           command="!teardown all unsynced")  # fmt: skip
        posted = approvals.post(plan.id, act, the_fleet, "3f9a2c" + "0" * 58, plan_message=10,
                                approval_message=11)  # fmt: skip
        assert posted.requirement.phrase == ("teardown", "3", "unsynced")
        name, n = plan.name, iter(range(12, 100))

        def answer(user: int, *phrase: str) -> str:
            sender = Sender(user, is_bot=False, is_active=True)
            got = approvals.approve(Received(next(n), 21, 31, sender), plan.id, "3f9a2c", phrase)
            return f"{'✅' if got.ok else '❌'} {got.reason}"

        for who in (ids.tester, ids.tester2):
            for wrong in ((), ("teardown", "3")):
                said = answer(who, *wrong)
                assert said.startswith("❌") and ending.needs_phrase("teardown 3 unsynced") in said
        if alone:
            said = answer(ids.tester, "teardown", "3", "unsynced")
            assert said == ending.approved_by_an_admin(name), said
            return
        said = answer(ids.tester2, "teardown", "3", "unsynced")
        assert re.match(ending.recorded(name), said), said
        assert re.match(rf"✅ {name}: \d+ of \d+ approvals\b", said)  # `Talk.endorse`'s
        said = answer(ids.tester2, "teardown", "3", "unsynced")
        assert said.startswith("❌") and ending.approved_already(name) in said, said
        said = answer(ids.tester, "teardown", "3", "unsynced")
        assert said == ending.approved_by_two(name), said  # `Talk.approve`'s `✅ P-n `
    finally:
        store.close()


class Answers:
    """A test user whose bot answers each post of a text with the next of its answers."""

    def __init__(self, answers: dict[str, list[list[str]]]) -> None:
        self.answers, self.posted, self.ids, self.last = answers, [], 100, ""

    async def send(self, stream, topic, content):
        self.ids += 1
        self.posted.append(content)
        self.batch = self.answers[content].pop(0) if content in self.answers else []
        return self.ids

    async def wait_reply(self, narrow, *, after_id, senders, pattern, within):
        for n, content in enumerate(self.batch):
            if re.search(pattern, content, re.S):
                return {"id": after_id + 1 + n, "content": content}
        raise TimeoutError(f"no reply matching {pattern!r}")


def a_plan() -> list[str]:
    act = action("!teardown spares", fleet({}))
    text = plan_text(act, Requirement("destructive", 2, ("teardown", "1")), "nothing matches")
    approval, _ = planning.approval_message(
        42, DIGEST, Requirement("destructive", 2, ("teardown", "1")), "!teardown spares"
    )
    return [text, approval]


async def test_a_teardown_refused_by_the_guard_is_sent_again_at_its_time():
    now = dt.datetime(2026, 10, 15, 23, 58, 30, tzinfo=dt.UTC)
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    refused = guarded()
    user = Answers({"!teardown spares": [[refused], a_plan()]})
    plan, said = await ending.after_the_guard(Talk(user, 21, 31, "e2e-A9"), "!teardown spares",
                                              now=lambda: now, sleep=sleep)  # fmt: skip
    assert plan.id == "P-42" and said == refused and user.posted == ["!teardown spares"] * 2
    assert plan.approval_id > plan.message_id > 0  # the widget is read from that message
    assert slept == [210 + ending.GUARD_MARGIN_S]  # 23:58:30 to 00:02
    user = Answers({"!teardown spares": [a_plan()]})
    plan, said = await ending.after_the_guard(Talk(user, 21, 31, "e2e-A9"), "!teardown spares",
                                              now=lambda: now, sleep=sleep)  # fmt: skip
    assert said == "" and len(slept) == 1  # no wait without the guard
    for answers in ([["❌ P-12 refused: the dry run failed; nothing was done"]],
                    [[refused], [refused]]):  # fmt: skip
        user = Answers({"!teardown spares": answers})
        with pytest.raises(Refused):
            await ending.after_the_guard(Talk(user, 21, 31, "e2e-A9"), "!teardown spares",
                                         now=lambda: now, sleep=sleep)  # fmt: skip


async def test_a_time_further_off_than_the_guard_waits_is_a_misreading():
    now = dt.datetime(2026, 10, 15, 12, 0, tzinfo=dt.UTC)

    async def sleep(seconds: float) -> None:
        raise AssertionError("slept")

    with pytest.raises(AssertionError, match="away"):
        await ending.wait_until(now + dt.timedelta(minutes=30), now=lambda: now, sleep=sleep)


# ------------------------------------------------------------------- rows, spares and bots


def test_the_throwaway_row_is_a_slug_no_one_else_has():
    assert SLUG_RE.fullmatch(TMP) and not SPARE_RE.fullmatch(TMP) and TMP not in RESERVED
    assert TMP.startswith(ending.THROWAWAY) and ending.throwaway(2**40) != TMP
    assert re.fullmatch(r"zab-test-tmp-[0-9a-f]+", ending.throwaway(4_102_444_800))  # 2100


def test_the_rosters_input_columns():
    head = "slug,first_name,last_name,email,repo,region,notes,host,ip,zulip_user_id,repo_status"
    inputs = ("zab-test-a", "Zab", "Test A", "a@example.com", "MathAgents/zab-test-a",
              "us-east-1", "e2e")  # fmt: skip
    roster = "\N{BYTE ORDER MARK}" + head + "\n" + ",".join(inputs)
    roster += ",ws-zab-test-a,198.51.100.1,,ok\n,,,,,,,,,,\n"  # and a blank row
    assert ending.inputs(roster, ROSTER_COLUMNS) == [dict(zip(ROSTER_COLUMNS, inputs, strict=True))]
    assert ending.rows_by_slug(roster)[A]["repo_status"] == "ok"


def test_a_spares_address_and_an_earlier_runs_bots():
    spares = {
        "workshop=zab-admin-e2e,zab-role=test/us-east-1/zab-spare-1": {"ip": "198.51.100.9"},
        "workshop=zab-admin-e2e,zab-role=test/us-east-1/x": {"name": "zab-spare-2", "ip": "a"},
        "broken": "not an entry",
    }
    assert ending.spare_ip(spares, "zab-spare-1") == "198.51.100.9"
    assert ending.spare_ip(spares, "zab-spare-2") == "a" and ending.spare_ip(spares, "z") is None
    names = [f"Claude · {TMP}", "Codex · zab-test-tmp-1a", "Claude · zab-test-a",
             "Codex · zab-test-tmp-x", "Workshop admin · e2e"]  # fmt: skip
    assert ending.stale_throwaways(names, TMP) == ["Codex · zab-test-tmp-1a"]


def test_a_plans_records_and_the_instances_running():
    records = [{"plan": "P-2", "event": "plan"}, {"plan": "P-3", "event": "apply_start"},
               {"plan": "P-3", "event": "apply_end", "exit": 0}, {"event": "read"}]  # fmt: skip
    assert sorted(ending.of_plan(records, "P-3")) == ["apply_end", "apply_start"]
    assert ending.of_plan(records, "P-9") == {}
    states = [("pending", A), ("running", B), ("stopped", "zab-spare-1"), ("running", None)]
    found = [{"InstanceId": f"i-{n}", "State": {"Name": state},
              "Tags": [{"Key": "participant", "Value": who}] if who else []}
             for n, (state, who) in enumerate(states)]  # fmt: skip
    assert ending.running(found) == ["i-3", A, B]


# ------------------------------------------------------------------- approvals and config


async def test_one_approval_of_two():
    from e2e.admin.test_harness_chat import FakeUser, rendered

    plan = parse_plan(rendered("!stop all", Requirement("destructive", 2, ("teardown", "2"))).text)
    assert plan is not None
    recorded = "✅ P-42: 1 of 2 approvals, from distinct approvers\n`audit #8 · chain cccccccccccc`"
    user = FakeUser({
        plan.approve: [{"content": recorded}],
        "again": [{"content": "❌ you approved P-42 already: it needs another approver"}],
        "last": [{"content": "✅ P-42 approved"}],
    })  # fmt: skip
    t = Talk(user, 5, 31, "e2e-A9")
    assert (await t.endorse(plan))["content"] == recorded
    for text in ("again", "last"):  # refused; or the last approval, which applies it
        with pytest.raises(Refused):
            await t.endorse(plan, text)


def test_the_two_approver_admin_toml_loads_and_differs_only_there():
    ids = fake_ids()
    shape = dict(region="us-east-1", ami="ami-0123456789abcdef0",
                 groups={rigs.E2E.name: "sg-0123456789abcdef0",
                         rigs.E2E2.name: "sg-0123456789abcdef1"})  # fmt: skip
    rig, end = admin_toml.render(ids, **shape), admin_toml.ending(ids, **shape)
    assert rig == test_toml()
    before, after = parse_admin_config(rig), parse_admin_config(end)
    assert (after.destructive_approvals, after.plan_ttl_minutes) == (2, 15)
    e2e, e2e2 = after.workshops
    assert after.approvers(e2e) == {ids.tester, ids.tester2} == after.approvers(e2e2)
    assert e2e.organizers == {ids.tester2} and e2e2.organizers == {ids.tester2}
    same = dataclasses.replace(after, destructive_approvals=1, plan_ttl_minutes=2,
                               workshops=(dataclasses.replace(e2e, organizers=frozenset()),
                                          e2e2))  # fmt: skip
    assert same == before
    alerts = [ids.channels[ws.alerts] for ws in rigs.WORKSHOPS]  # !waker on needs them (#381)
    assert [ws.alerts_channel for ws in after.workshops] == alerts
    pairs = list(zip(rig.splitlines(), end.splitlines(), strict=True))
    assert [new for old, new in pairs if old != new] == [
        "destructive_approvals = 2", "plan_ttl_minutes = 15", f"organizers = [{ids.tester2}]"
    ]  # fmt: skip


# ------------------------------------------------------------------------ !retire (#559)


def test_the_retire_rows_are_slugs_the_grammar_takes():
    """A9's `!retire` rows (6b): slugs the roster and the grammar take, the run's own distinct
    from the throwaway row's, and never a reserved word."""
    for started in (1_761_000_000.0, 4_294_967_295.0):
        for slug in (ending.RETIRE_KEEPER, ending.retiring(started), ending.never(started)):
            assert SLUG_RE.fullmatch(slug) and not SPARE_RE.fullmatch(slug), slug
            assert slug not in RESERVED
        assert (
            len({ending.retiring(started), ending.never(started), ending.throwaway(started)}) == 3
        )


def test_the_retire_table_reads_as_the_product_writes_it():
    """6b reads the result's table with `chat.table` (`cells`): the runner's own `Rows.table`,
    a row retired and a row stopped at its sync, with `export`."""
    from zab.admin.actions import STAGES, Step
    from zab.admin.runner import Ran, Rows

    r1, r2 = ending.RETIRE_KEEPER, ending.retiring(1_761_000_000.0)
    steps = tuple(Step(("x",), row=r, stage=s) for r in (r1, r2) for s in STAGES)
    rows = Rows(dataclasses.replace(ACTION_STUB, steps=steps), {r1: "i-1", r2: "i-2"})
    for step in steps:
        if rows.skip(step):
            continue
        failed = step.row == r2 and step.stage == "final-sync"
        rows.ran(step, Ran("admin/final_sync.py", 1 if failed else 0))
    read = {r["participant"]: r for r in table(f"```text\n{rows.table()}\n```", "participant")}
    assert read[r1] == {"participant": r1, **dict.fromkeys(STAGES, "ok"), "time": "0 s",
                        "retired": "yes"}  # fmt: skip
    assert read[r2]["final-sync"] == "failed: exit 1" and read[r2]["retired"] == "no"
    assert [read[r2][s] for s in STAGES[5:]] == ["not run"] * 4


#: An action of no verb's: only its steps matter to `Rows`.
ACTION_STUB = actions.Action("retire", "zab-admin-e2e", 21, (), (), actions.RETIRE, (), ())


def test_the_retired_rows_line_reads_as_the_product_writes_it():
    """6b's `!status all` check: `planning.left_out`'s line, as the scenario's pattern reads it."""
    r1, r2 = ending.RETIRE_KEEPER, ending.retiring(1_761_000_000.0)
    when = "P-50 at 2026-10-29 14:02 UTC"
    the_fleet = dataclasses.replace(
        fleet({}), rows=(Row(r1), Row(r2)), retired={r1: when, r2: when}
    )
    line = planning.left_out(the_fleet)
    named = re.search(r"^\*Retired, so left out: (.+)\.\*$", f"x\n\n{line}\n", re.M)
    assert named and r1 in named.group(1) and r2 in named.group(1), line


def test_a_torn_down_row_reads_so_and_shared_addresses_are_found():
    """#604: after a teardown, A9 checks each row's `ip` cleared and its `host` kept, and after
    step 6b's launch that no two rows hold one address."""
    assert ending.torn_down({"slug": A, "host": f"ws-{A}", "ip": ""}, A) == ""
    kept = ending.torn_down({"host": f"ws-{A}", "ip": "203.0.113.176"}, A)
    assert kept == f"{A} keeps the ip 203.0.113.176, released with its VM"
    assert "host is ''" in ending.torn_down({"host": "", "ip": ""}, A)
    rows = {A: {"ip": "203.0.113.176"}, "zab-test-r": {"ip": "203.0.113.176"}, B: {"ip": ""}}
    assert ending.shared_ips(rows) == {"203.0.113.176": [A, "zab-test-r"]}
    assert ending.shared_ips({A: {"ip": "198.51.100.1"}, B: {"ip": ""}}) == {}


@pytest.mark.parametrize("ip", ["", "198.51.100.9"])
def test_all_leaves_out_the_retired_rows_as_a9_checks_it(ip):
    """The combined run on #606 (Oct 23): step 6b's `!stop all` passed `--all`, every retired
    row's ip cleared by its teardown, and the old check refused any `--all`. `leaves_out`
    accepts the product's commands with a retired row's ip cleared (`--all`) or kept (named
    rows), and refuses `--all` while a retired row keeps an ip, or a command naming one."""
    here = fleet({}).workshop.aws.region
    rows = (Row(A, "198.51.100.1", here), Row(B, "", here, f"ws-{B}"),
            Row("zab-test-r", ip, here, "ws-zab-test-r"))  # fmt: skip
    retired = dataclasses.replace(fleet({}, rows), retired={"zab-test-r": "P-9 at 12:00 UTC"})
    roster = {r.slug: {"ip": r.ip, "host": r.host} for r in rows}
    for message in ("!stop all", "!start all"):
        act = action(message, retired)
        shown = " ".join([*act.targets, *act.steps[0].argv])
        assert ending.leaves_out(shown, ["zab-test-r"], roster) == "", shown
        assert (" --all" in shown) == (not ip), shown
    kept = {**roster, "zab-test-r": {"ip": "198.51.100.9"}}
    said = ending.leaves_out("power.py --stop --all", ["zab-test-r"], kept)
    assert said == "it passes --all, which takes zab-test-r: retired, with an ip still"
    assert ending.leaves_out("--stop --only zab-test-r", ["zab-test-r"], roster) == (
        "it names a retired row"
    )


def test_the_launch_refusal_a9_matches_as_the_product_renders_it():
    """The `!launch` of a retired row that A9 step 6b sends: its roster keeps its `host` once
    the teardown cleared the `ip` (#604), and the refusal says so in the words A9 checks."""
    retired = dataclasses.replace(
        fleet({}), rows=(Row("zab-test-r", host="ws-zab-test-r"),),
        retired={"zab-test-r": "P-9 at 2026-10-23 12:00 UTC"},
    )  # fmt: skip
    with pytest.raises(ActionError) as refused:
        action("!launch zab-test-r", retired)
    said = str(refused.value)
    assert "the roster keeps its host or ip" in said
    assert "clears its host and ip in roster.csv and reactivates its bots" in said
