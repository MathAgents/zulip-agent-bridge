"""Plans and approvals on the admin bots (docs/ADMIN.md §2.4, §2.5; #341): request → plan →
approve, with a fake realm, two workshops and a fake runner, the plan and widget messages, the
refusals, expiry and restarts, `!status` and `!console`."""

import asyncio
import hashlib
import json
import os
import re
import stat
import time
from pathlib import Path

import pytest

from zab.admin import collector, planning
from zab.admin.actions import Live
from zab.admin.approvals import Requirement
from zab.admin.audit import verify
from zab.admin.bot import WINDOW
from zab.admin.planning import (
    MAX_PLAN_ID,
    NoRunner,
    Planning,
    Prepared,
    approval_message,
)
from zab.admin.plans import (
    APPLYING,
    APPROVED,
    EXPIRED,
    INTERRUPTED,
    PENDING,
    PLANNING,
    REFUSED,
    REJECTED,
    STALE,
)
from zab.admin.reads import mention
from zab.admin.signin import SignIns

from .botkit import (
    ADMINS_CH,
    CODEX_BOB,
    DREW,
    GLOBAL,
    HARVARD,
    HARVARD_CH,
    OLGA,
    OSCAR,
    PAT,
    TESS,
    TEST,
    TEST_CH,
    audit_text,
    incoming,
    make_site,
    until,
)

NAME = "Workshop admin · harvard-2026"


class FakeRunner:
    """The runner's side, PR 10's, faked: dry runs, reads, the apply lock, applies."""

    def __init__(self) -> None:
        self.ok, self.output = True, "alice: would stop i-1\nbob: would stop i-2\n"
        self.vms = {"alice": "i-1", "bob": "i-2"}
        self.applying: dict[str, object] = {}
        self.prepared: list = []
        self.applied: list = []

    async def prepare(self, plan, action, fleet):
        self.prepared.append((plan, action))
        live = Live(vms=self.vms, tmpdir=f"/var/lib/zab-admin/tmp/{plan.name}")
        return Prepared(self.ok, self.output, live, "" if self.ok else "the dry run failed")

    async def read(self, action, fleet):
        return json.dumps({"alice": "running", "argv": list(action.steps[0].argv[-2:])})

    def busy(self, workshop):
        return self.applying.get(workshop)

    async def apply(self, job):
        self.applied.append(job)


@pytest.fixture
def site(tmp_path):
    return make_site(tmp_path)


@pytest.fixture
def runner():
    return FakeRunner()


@pytest.fixture
async def server(site, runner):
    srv = site.server(planner=Planning(runner))
    await srv.start()
    yield srv
    await srv.close()


def bot_of(server, user_id):
    return next(b for b in server.bots if b.user_id == user_id)


def posts(site, by=None):
    return site.realm.posts(by)


def records(site, event=None):
    text = audit_text(site.audit)
    rows = [json.loads(line) for line in text.splitlines()]
    return [r for r in rows if event is None or r["event"] == event]


async def settle(server):
    while server.planner.tasks:
        await asyncio.gather(*list(server.planner.tasks), return_exceptions=True)


async def request(server, text, sender=OLGA, bot=HARVARD, stream=HARVARD_CH, mid=5000):
    await bot_of(server, bot).on_message(incoming(sender, text, stream=stream, mid=mid))
    await settle(server)
    return server.store.get(max(_ids(server)))


def _ids(server):
    return [row[0] for row in server.store.db.execute("SELECT id FROM plans")]


LATE = 10**6  # a message ID after every post of the fake realm


# ------------------------------------------------------------------- request → plan → approve


async def test_request_plan_approve(site, server, runner):
    plan = await request(server, "!stop alice")
    assert plan.state == PENDING and plan.workshop == "harvard-2026" and plan.bot == HARVARD
    plan_post, approval_post = posts(site, HARVARD)[-2:]
    text = plan_post["content"]
    assert text.startswith(f"**{plan.name}** · harvard-2026 · `!stop alice` · requested by ")
    assert "**Targets** (1): alice" in text and "aws/power.py" in text and "--stop" in text
    assert "alice: would stop i-1" in text  # the dry run, scrubbed and fenced
    assert f"`{plan.hash[:12]}`" in text and f"!approve {plan.name} {plan.hash[:6]}" in text
    head = verify(site.audit).head
    assert text.endswith(f"`audit #{head.seq} · chain {head.sha256[:12]}`")
    widget = json.loads(approval_post["widget_content"])
    assert widget == {
        "widget_type": "zform",
        "extra_data": {
            "type": "choices",
            "heading": f"{plan.name}: !stop alice",
            "choices": [
                {"type": "multiple_choice", "short_name": "✅ Approve",
                 "long_name": f"Approve {plan.name}",
                 "reply": f"!approve {plan.name} {plan.hash[:6]}"},
                {"type": "multiple_choice", "short_name": "❌ Reject",
                 "long_name": f"Reject {plan.name}", "reply": f"!reject {plan.name}"},
            ],
        },
    }  # fmt: skip
    assert f"`!approve {plan.name} {plan.hash[:6]}`" in approval_post["content"]
    assert [r["event"] for r in records(site)] == ["request", "plan"]
    assert records(site, "plan")[0]["hash"] == plan.hash
    # a click on the bot's own widget: the web client's reply mentions the bot
    click = f"@**{NAME}** !approve {plan.name} {plan.hash[:6]}"
    await bot_of(server, HARVARD).on_message(incoming(OSCAR, click, mid=LATE, flags=("mentioned",)))
    await settle(server)
    assert server.store.get(plan.id).state == APPROVED
    assert posts(site, HARVARD)[-1]["content"].startswith(f"✅ {plan.name} approved")
    (job,) = runner.applied
    assert (job.plan.id, job.stream, job.topic, job.action.targets) == (
        plan.id, HARVARD_CH, "ops", ("alice",),
    )  # fmt: skip
    approval = records(site, "approval")[0]
    assert approval["plan"] == plan.name and approval["sender"]["id"] == OSCAR
    assert approval["code"] == "approved"


async def test_a_global_admin_is_served_by_both_bots_an_organizer_by_their_own(
    site, server, runner
):
    one = await request(server, "!stop alice", sender=DREW)
    two = await request(server, "!stop erin", sender=DREW, bot=TEST, stream=TEST_CH, mid=5001)
    assert (one.state, two.state) == (PENDING, PENDING) and one.id != two.id
    await request(server, "!stop erin", sender=OLGA, bot=TEST, stream=TEST_CH, mid=5002)
    assert len(_ids(server)) == 2  # the organizer of harvard-2026 is a stranger there
    assert posts(site, TEST)[-1]["content"].startswith("🚫 Olga Organizer")
    # Tess approves her workshop's plan (all of its one VM: the phrase); on the other
    # workshop's bot she is a stranger
    test = bot_of(server, TEST)
    approve = f"!approve {two.name} {two.hash[:6]} stop 1"
    await test.on_message(incoming(TESS, approve, stream=TEST_CH, mid=LATE))
    assert server.store.get(two.id).state == APPROVED
    await bot_of(server, HARVARD).on_message(
        incoming(TESS, f"!approve {one.name} {one.hash[:6]}", mid=LATE + 1)
    )
    assert server.store.get(one.id).state == PENDING


async def test_an_approval_on_another_bot_is_refused(site, server):
    plan = await request(server, "!stop alice")
    await bot_of(server, TEST).on_message(
        incoming(DREW, f"!approve {plan.name} {plan.hash[:6]}", stream=TEST_CH, mid=LATE)
    )
    assert server.store.get(plan.id).state == PENDING
    assert "another bot's plan" in posts(site, TEST)[-1]["content"]
    refused = records(site, "refused")[-1]
    assert (refused["code"], refused["plan"], refused["bot"]) == ("other-bot", plan.name, TEST)


def assert_zform_renders(widget: dict) -> None:
    """What Zulip's web client accepts (`web/src/zform_data.ts`, `zform_widget_extra_data_schema`,
    zulip/zulip main, Oct 15): it drops the whole widget, showing only the text, when a choice
    lacks one of these strings, though the server accepts it (DECISIONS, V1)."""
    assert widget["widget_type"] == "zform"
    data = widget["extra_data"]
    assert data["type"] == "choices" and isinstance(data["heading"], str)
    assert data["choices"]
    for choice in data["choices"]:
        for key in ("type", "long_name", "reply", "short_name"):
            assert isinstance(choice.get(key), str), (key, choice)


async def test_every_approval_widget_has_the_shape_the_web_client_renders(site, server):
    plan = await request(server, "!stop alice")
    assert_zform_renders(json.loads(posts(site, HARVARD)[-1]["widget_content"]))
    assert_zform_renders(approval_message(plan, plan.hash, Requirement("change"), "!stop alice")[1])
    for needs in (Requirement("above-half", 1, ("stop", "3")),
                  Requirement("destructive", 2, ("teardown", "3", "unsynced"))):  # fmt: skip
        assert approval_message(plan, plan.hash, needs, "!stop all")[1] is None  # #542


def test_the_approval_messages_as_zulip_shows_them():
    """#542: the web client shows a widget *instead of* the text. Without a phrase, the buttons,
    and the text names the command for clients without widgets; with one, no widget, and the
    text shows the line to send as a one-line code block, whose copy button copies it as plain
    text (review of #544: a copied code span can paste with its backticks), then the approvers
    and how to reject."""
    digest = "3f9a2c" + "ab" * 29
    text, widget = approval_message(21, digest, Requirement("change"), "!stop bob")
    assert text == "P-21 `!stop bob`: approve with `!approve P-21 3f9a2c`, or reject with " \
                   "`!reject P-21`."  # fmt: skip
    assert [c["reply"] for c in widget["extra_data"]["choices"]] == ["!approve P-21 3f9a2c",
                                                                     "!reject P-21"]  # fmt: skip
    head = ("P-21 `!idle-stop 60 all`: approve by sending this plan's approval line, exactly "
            "as it is. A plan like this one has no Approve button. Point at the line below and "
            "click its copy button, at its top right (or type the line), then send it as a new "
            "message.")  # fmt: skip
    text, widget = approval_message(
        21, digest, Requirement("above-half", 1, ("idle-stop", "18")), "!idle-stop 60 all"
    )
    assert widget is None
    assert text == f"{head}\n\n```text\n!approve P-21 3f9a2c idle-stop 18\n```\n\n" \
                   "It takes 1 approver. To reject, send `!reject P-21`."  # fmt: skip
    two = Requirement("destructive", 2, ("teardown", "3", "unsynced"))
    text, widget = approval_message(21, digest, two, "!idle-stop 60 all")
    assert widget is None
    assert text == f"{head}\n\n```text\n!approve P-21 3f9a2c teardown 3 unsynced\n```\n\n" \
                   "It takes 2 distinct approvers, or a global admin alone: each one sends " \
                   "that line. To reject, send `!reject P-21`."  # fmt: skip
    for many in (2, 1):  # a global admin's (#558), production's setting and the rig's
        server = Requirement("server", many, ("new-workshop", "mit-2027"))
        text, widget = approval_message(21, digest, server, "!idle-stop 60 all")
        assert widget is None
        assert text.endswith("```\n\nIt takes 1 global admin. To reject, send `!reject P-21`.")


async def test_the_posted_approval_messages_with_and_without_a_phrase(site, server):
    """#542, as posted: the approval message of a plan with a phrase has no widget, and is the
    message the plan is bound to (an approval before it is `early`); without one, the widget."""
    plain = await request(server, "!stop alice")
    posted = posts(site, HARVARD)[-1]
    assert "widget_content" in posted
    assert f"`!approve {plain.name} {plain.hash[:6]}`" in posted["content"]
    phrased = await request(server, "!stop all", mid=5001)
    posted = posts(site, HARVARD)[-1]
    assert "widget_content" not in posted
    line = f"!approve {phrased.name} {phrased.hash[:6]} stop 3"
    assert f"\n\n```text\n{line}\n```\n\n" in posted["content"]
    assert f"To reject, send `!reject {phrased.name}`." in posted["content"]
    (shown,) = [m for m in site.realm.messages.values() if m["content"] == posted["content"]]
    assert shown["widget"] is None and phrased.approval_message == shown["id"]
    assert phrased.plan_message == shown["id"] - 1  # the plan, right before it


async def test_a_click_on_another_senders_widget_is_ignored(site, server):
    plan = await request(server, "!stop alice")
    _, widget = approval_message(plan, plan.hash, Requirement("change"), "!stop alice")
    assert widget["extra_data"]["choices"][0]["reply"] == f"!approve {plan.name} {plan.hash[:6]}"
    # the participant's bot posts that widget; an organizer's click mentions its sender
    click = f"@**Codex · bob** !approve {plan.name} {plan.hash[:6]}"
    count = len(posts(site))
    await bot_of(server, HARVARD).on_message(incoming(OLGA, click, mid=LATE))
    assert len(posts(site)) == count and server.store.get(plan.id).state == PENDING


async def test_a_plan_above_half_the_fleet_takes_the_phrase(site, server, runner):
    plan = await request(server, "!stop all")
    assert "widget_content" not in posts(site, HARVARD)[-1]  # no button types a phrase (#542)
    assert (
        f"\n```text\n!approve {plan.name} {plan.hash[:6]} stop 3\n```\n"
        in posts(site, HARVARD)[-1]["content"]
    )
    harvard = bot_of(server, HARVARD)
    await harvard.on_message(incoming(OLGA, f"!approve {plan.name} {plan.hash[:6]}", mid=LATE))
    assert server.store.get(plan.id).state == PENDING
    assert "needs the phrase `stop 3`" in posts(site, HARVARD)[-1]["content"]
    await harvard.on_message(
        incoming(OLGA, f"!approve {plan.name} {plan.hash[:6]} stop 3", mid=LATE + 1)
    )
    await settle(server)
    assert server.store.get(plan.id).state == APPROVED and len(runner.applied) == 1


async def test_reject_by_click(site, server, runner):
    plan = await request(server, "!stop alice")
    await bot_of(server, HARVARD).on_message(
        incoming(OSCAR, f"@**{NAME}** !reject {plan.name}", mid=LATE, flags=("mentioned",))
    )
    await settle(server)
    assert server.store.get(plan.id).state == REJECTED and runner.applied == []
    assert records(site, "reject")[0]["code"] == "rejected"


async def test_a_stranger_never_approves(site, server):
    plan = await request(server, "!stop alice")
    await bot_of(server, HARVARD).on_message(
        incoming(PAT, f"!approve {plan.name} {plan.hash[:6]}", mid=LATE)
    )
    await bot_of(server, HARVARD).on_message(
        incoming(CODEX_BOB, f"!approve {plan.name} {plan.hash[:6]}", mid=LATE + 1)
    )
    assert server.store.get(plan.id).state == PENDING
    assert server.approvals.approved_by(plan.id) == ()


async def test_a_deactivated_approver_is_refused(site, server):
    plan = await request(server, "!stop alice")
    site.realm.users[OSCAR]["is_active"] = False
    await bot_of(server, HARVARD).on_message(
        incoming(OSCAR, f"!approve {plan.name} {plan.hash[:6]}", mid=LATE)
    )
    assert server.store.get(plan.id).state == PENDING
    assert records(site, "refused")[-1]["code"] == "inactive"


async def test_busy_is_not_recorded(site, server, runner):
    plan = await request(server, "!stop alice")
    other = await request(server, "!stop bob", mid=5001)
    runner.applying["harvard-2026"] = other
    harvard = bot_of(server, HARVARD)
    await harvard.on_message(incoming(OLGA, f"!approve {plan.name} {plan.hash[:6]}", mid=LATE))
    assert posts(site, HARVARD)[-1]["content"].startswith(f"busy: {other.name} is applying")
    assert server.approvals.approved_by(plan.id) == () and records(site, "approval") == []
    del runner.applying["harvard-2026"]
    await harvard.on_message(incoming(OLGA, f"!approve {plan.name} {plan.hash[:6]}", mid=LATE + 1))
    assert server.store.get(plan.id).state == APPROVED


async def test_a_plan_that_no_longer_builds_is_stale(site, server, runner):
    plan = await request(server, "!stop bob")
    roster = Path(server.config.workshop("harvard-2026").roster)
    _replace(roster, "bob,Bob", "robert,Bob")
    await bot_of(server, HARVARD).on_message(
        incoming(OLGA, f"!approve {plan.name} {plan.hash[:6]}", mid=LATE)
    )
    assert server.store.get(plan.id).state == STALE and runner.applied == []
    assert "stale" in posts(site, HARVARD)[-1]["content"]
    assert records(site, "stale")[0]["plan"] == plan.name


def _replace(path, old, new):
    path.write_text(path.read_text().replace(old, new))


# ------------------------------------------------------------------------------ refusals


async def test_a_failed_dry_run_refuses_the_plan(site, server, runner):
    runner.ok, runner.output = False, "alice: FAILED: no such instance\n"
    plan = await request(server, "!stop alice")
    assert plan.state == REFUSED and plan.note == "the dry run failed"
    text = posts(site, HARVARD)[-1]["content"]
    assert text.startswith(f"❌ {plan.name} refused: the dry run failed")
    assert "no such instance" in text
    assert [r["event"] for r in records(site)] == ["request", "refused"]


async def test_an_action_error_refuses_the_plan_and_its_id_is_audited(site, server, runner):
    plan = await request(server, "!rollout v0.2.2 alice")  # no wheel on this server
    assert plan.state == REFUSED and "no wheel for v0.2.2" in plan.note
    assert runner.prepared == []
    assert records(site, "request")[0]["plan"] == plan.name


async def test_a_refused_plans_id_is_never_handed_out_again(site, runner):
    srv = site.server(planner=Planning(runner))
    await srv.start()
    try:
        first = await request(srv, "!rollout v0.2.2 alice")
    finally:
        await srv.close()
    _remove(site.paths.state_db)  # state.db is not in the backups (§1.7)
    srv = site.server(planner=Planning(runner))
    await srv.start()
    try:
        again = await request(srv, "!stop alice")
    finally:
        await srv.close()
    assert again.id == first.id + 1


def _remove(path):
    Path(path).unlink()


async def test_no_runner_refuses_every_plan(site):
    srv = site.server(planner=Planning(NoRunner()))
    await srv.start()
    try:
        plan = await request(srv, "!stop alice")
        assert plan.state == REFUSED and "no runner" in plan.note
        await bot_of(srv, HARVARD).on_message(incoming(OLGA, "!status all", mid=5001))
        assert "no runner" in posts(site, HARVARD)[-1]["content"]
    finally:
        await srv.close()


# ------------------------------------------------------------------------ time, restarts


async def test_a_pending_plan_expires(site, server):
    plan = await request(server, "!stop alice")
    await server.planner.tick()
    assert server.store.get(plan.id).state == PENDING
    site.clock.now += server.config.plan_ttl_minutes * 60
    await server.planner.tick()
    assert server.store.get(plan.id).state == EXPIRED
    assert posts(site, HARVARD)[-1]["content"].startswith(f"⌛ {plan.name} expired")
    assert records(site, "expired")[0]["plan"] == plan.name


async def test_an_approval_after_the_ttl_expires_the_plan(site, server):
    plan = await request(server, "!stop alice")
    site.clock.now += server.config.plan_ttl_minutes * 60
    await bot_of(server, HARVARD).on_message(
        incoming(OLGA, f"!approve {plan.name} {plan.hash[:6]}", mid=LATE)
    )
    assert server.store.get(plan.id).state == EXPIRED
    assert [r["event"] for r in records(site)][-2:] == ["refused", "expired"]


async def test_a_restart_interrupts_what_was_under_way(site, runner):
    srv = site.server(planner=Planning(runner))
    await srv.start()
    try:
        pending = await request(srv, "!stop alice")
        applying = await request(srv, "!stop bob", mid=5001)
        srv.store.move(applying.id, (PENDING,), APPLYING)
    finally:
        await srv.close()
    srv = site.server(planner=Planning(runner))
    await srv.start()
    try:
        assert srv.store.get(pending.id).state == PENDING  # waits for its approval or its TTL
        assert srv.store.get(applying.id).state == INTERRUPTED
    finally:
        await srv.close()
    assert posts(site, HARVARD)[-1]["content"].startswith(f"⏹ {applying.name} was interrupted")
    assert records(site, "interrupted")[0]["plan"] == applying.name


# ------------------------------------------------------------------------ roster import


async def test_roster_import_downloads_the_upload_to_its_plan(site, server, runner):
    csv = b"slug,first_name,email\nalice,Alice,alice@example.com\n"
    path = site.realm.upload("new-roster.csv", csv)
    plan = await request(server, f"!roster import [new-roster.csv]({path})")
    assert plan.state == PENDING, plan.note
    saved = Path(site.paths.layout.runs) / plan.name / "roster-upload.csv"
    assert saved.read_bytes() == csv and stat.S_IMODE(saved.stat().st_mode) == 0o600
    (_, action) = runner.prepared[0]
    assert str(saved) in action.inputs and "--input" in action.steps[0].argv
    await bot_of(server, HARVARD).on_message(
        incoming(OLGA, f"!approve {plan.name} {plan.hash[:6]}", mid=LATE)
    )
    await settle(server)
    (job,) = runner.applied
    assert job.command.verb == "roster import" and str(saved) in job.action.inputs


async def test_a_csv_too_large_is_refused(site, server, runner):
    path = site.realm.upload("big.csv", b"slug\n" + b"a" * 1_000_001)
    plan = await request(server, f"!roster import [big.csv]({path})")
    assert plan.state == REFUSED and "exceeds the configured size limit" in plan.note
    assert runner.prepared == []
    missing = "/user_uploads/7/ab/99/gone.csv"
    plan = await request(server, f"!roster import [gone.csv]({missing})", mid=5001)
    assert plan.state == REFUSED and plan.note.startswith("the CSV could not be downloaded")


async def test_an_import_of_another_workshops_slug_gets_the_bots_own_hint(site, server, runner):
    """#347 (6191067546, decided): roster_import.py says only which workshop has the slug. The
    bot adds the hint naming that workshop's real bot, at most once per sender every 10
    minutes, server-wide (§2.1), as for a command's slug. A line naming a workshop whose
    roster lacks the slug gets none, and spends nothing."""
    runner.ok = False
    path = site.realm.upload("r.csv", b"slug,first_name,email\nerin,Erin,erin@example.com\n")
    ask = f"`erin` is in zab-test: ask {mention(TEST, 'Workshop admin · zab-test')}"

    async def refused(slug, ws, mid):
        tail = "; a slug is never shared between workshops (ADMIN §2.1)\nnothing written\n"
        runner.output = f"refused: {slug} is in {ws}{tail}"
        plan = await request(server, f"!roster import [r.csv]({path})", mid=mid)
        assert plan.state == REFUSED
        return posts(site, HARVARD)[-1]["content"].split("\n")

    assert ask not in await refused("alice", "zab-test", 5000)  # zab-test's roster lacks alice
    assert ask not in await refused("erin", "elsewhere", 4999)  # only the workshop it names
    lines = await refused("erin", "zab-test", 5001)
    assert lines[-3:-1] == ["```", ask]  # after the output, before the footer
    assert ask not in await refused("erin", "zab-test", 5002)  # within the same 10 minutes
    site.clock.now += WINDOW
    assert ask in await refused("erin", "zab-test", 5003)


async def test_a_hint_that_fails_still_refuses_and_posts(site, server, runner, monkeypatch, caplog):
    """The review of #506: the hint is the refusal's last best-effort step, after the plan is
    refused and recorded. A pointer that raises leaves it REFUSED and its refusal posted,
    without the hint, and logs why by its type."""
    runner.ok = False
    runner.output = "refused: erin is in zab-test; a slug is never shared between workshops\n"

    async def broken(*args, **kwargs):
        raise RuntimeError("roster unreadable")

    monkeypatch.setattr(server, "pointer", broken)
    path = site.realm.upload("r.csv", b"slug,first_name,email\nerin,Erin,erin@example.com\n")
    plan = await request(server, f"!roster import [r.csv]({path})")
    assert plan.state == REFUSED and records(site, "refused")[-1]["plan"] == plan.name
    text = posts(site, HARVARD)[-1]["content"]
    assert text.startswith(f"❌ {plan.name} refused: the dry run failed") and ": ask " not in text
    assert f"{plan.name}: no hint for its taken slug (RuntimeError)" in caplog.text


async def test_a_refusal_of_a_plan_no_longer_planning_spends_no_hint(site, server, runner):
    """#510 (the review of #506): `_refuse` returns early for a plan that left PLANNING
    meanwhile, before the hint, so the sender's hint window stays unspent and nothing is
    posted. A mutant that asks for the hint before that return fails here."""
    runner.ok = False
    runner.output = "refused: erin is in zab-test; a slug is never shared between workshops\n"
    prepare = runner.prepare

    async def moved_meanwhile(plan, action, fleet):
        server.store.move(plan.id, (PLANNING,), REFUSED, "moved meanwhile")
        return await prepare(plan, action, fleet)

    runner.prepare = moved_meanwhile
    path = site.realm.upload("r.csv", b"slug,first_name,email\nerin,Erin,erin@example.com\n")
    before = len(posts(site, HARVARD))
    plan = await request(server, f"!roster import [r.csv]({path})")
    assert (plan.state, plan.note) == (REFUSED, "moved meanwhile")
    assert server.hints.open == {} and len(posts(site, HARVARD)) == before
    assert records(site, "refused") == []


async def test_a_long_dry_run_is_attached(site, server, runner):
    runner.output = "".join(f"line {n}\n" for n in range(100))
    plan = await request(server, "!stop alice")
    text = posts(site, HARVARD)[-2]["content"]
    assert "line 39" in text and "line 40" not in text
    assert "60 lines of the dry run left out" in text
    assert f"[{plan.name}-plan.txt](/user_uploads/" in text
    attached = next(v for k, v in site.realm.uploads.items() if k.endswith("-plan.txt"))
    assert b"line 99" in attached


# ------------------------------------------------------------------------- status, console


async def test_status_reads_through_the_runner(site, server):
    await bot_of(server, HARVARD).on_message(incoming(OLGA, "!status alice"))
    await settle(server)
    text = posts(site, HARVARD)[-1]["content"]
    assert '"alice": "running"' in text and "--only" in text
    assert records(site, "read")[-1]["verb"] == "status"


async def test_a_status_read_spanning_an_apply_never_voids_it(site, server, runner):
    """The review of #494, round 2: `!status` keeps its read at the time it started, so an
    idle-stop apply that begins while the read runs is newer than the read, and the collector
    doesn't sweep the VM on what the read showed."""
    start = site.clock()
    off = {"enabled": False, "active": False, "minutes": None, "last_verdict": None}

    async def read(action, fleet):
        site.clock.now += 120  # a two-minute read; an `!idle-stop 60 alice` starts within it
        vm = {"slug": "alice", "bridge": "active", "idle_stop": off}
        return json.dumps({"schema": 1, "vms": [vm]})

    runner.read = read
    await bot_of(server, HARVARD).on_message(incoming(OLGA, "!status alice"))
    await settle(server)
    kept = json.loads((Path(site.paths.state) / "collector/reads/harvard-2026.json").read_text())
    assert kept["vms"]["alice"]["at"] == start
    paths = collector.Paths(state=site.paths.state, secrets=site.paths.secrets,
                            layout=site.paths.layout)  # fmt: skip
    later = start + 2 * collector.SWEEP_EVERY
    c = collector.Collector(server.config, paths=paths, aws=object(), clock=lambda: later,
                            background=False)  # fmt: skip
    c._feeds()
    c.idle_changed["harvard-2026/alice"] = start + 60  # the apply, during the read
    ws = server.config.workshops[0]
    c.vms[ws.name] = [{"slug": "alice", "kind": "participant", "state": "running",
                       "launched": start - 3600}]  # fmt: skip
    assert c.due(ws, later) == []
    c.idle_changed.clear()  # without the apply, the same read makes alice sweepable
    assert c.due(ws, later) == ["alice"]


async def test_status_feeds_the_collector_and_quotes_its_cost(site, server, runner):
    """§4.1: a `!status` read is the collector's too; §11.2: the answer ends with the cost."""
    answer = {"schema": 1, "vms": [{"slug": "alice", "bridge": "active", "zab_version": "0.2.6"}]}

    async def read(action, fleet):
        return json.dumps(answer)

    runner.read = read
    state = Path(site.paths.state)
    (state / "console").mkdir()
    read = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(site.clock()))
    snapshot = {"at": site.clock(), "workshops": {"harvard-2026": {"ec2": {"at": read}, "cost": {
        "month": 412.4, "projected": 1050.2, "running": 9, "vms": 40}}}}  # fmt: skip
    (state / "console" / "snapshot.json").write_text(json.dumps(snapshot))
    await bot_of(server, HARVARD).on_message(incoming(OLGA, "!status alice"))
    await settle(server)
    text = posts(site, HARVARD)[-1]["content"]
    assert "cost (estimate): $412 this month, $1,050 projected · 9 of 40 running" in text
    kept = json.loads((state / "collector" / "reads" / "harvard-2026.json").read_text())
    alice = kept["vms"]["alice"]
    assert alice["at"] == site.clock() and alice["zab_version"] == "0.2.6"
    assert stat.S_IMODE((state / "collector" / "reads" / "harvard-2026.json").stat().st_mode) == (
        0o600
    )
    site.clock.now += 3600  # an old snapshot: no line
    await bot_of(server, HARVARD).on_message(incoming(OLGA, "!status alice", mid=5001))
    await settle(server)
    assert "cost (estimate)" not in posts(site, HARVARD)[-1]["content"]


def waker_status(site, ready):
    """harvard-2026 opted in, and the waker's status file with its `ready_at` (#717)."""
    config = Path(site.paths.layout.waker) / "config.toml"
    config.parent.mkdir(exist_ok=True)
    config.write_text('[[workshop]]\nname = "harvard-2026"\n[[workshop.bots]]\n'
                      'name = "codex-alice"\ninstance_id = "i-0aaaaaaaaaaaaaaa1"\n')  # fmt: skip
    status = Path(site.paths.waker_status)
    status.parent.mkdir(exist_ok=True)
    entry = {"name": "harvard-2026", "ready_at": ready}
    status.write_text(json.dumps({"written_at": site.clock(), "workshops": [entry]}))


async def test_status_says_the_waker_is_down(site, server, runner):
    """#717: a workshop opted in whose waker hasn't served for minutes (`zab.admin.wakerdown`):
    the answer says since when, after the cost; once it serves again, nothing."""

    async def read(action, fleet):
        return json.dumps({"schema": 1, "vms": [{"slug": "alice", "bridge": "active"}]})

    runner.read = read
    waker_status(site, site.clock() - 3600)
    await bot_of(server, HARVARD).on_message(incoming(OLGA, "!status alice"))
    await settle(server)
    since = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(site.clock() - 3600))
    assert f"⚠ waker down since {since}: " in posts(site, HARVARD)[-1]["content"]
    waker_status(site, site.clock())
    await bot_of(server, HARVARD).on_message(incoming(OLGA, "!status alice", mid=5001))
    await settle(server)
    assert "waker down" not in posts(site, HARVARD)[-1]["content"]


async def test_a_cost_line_that_fails_never_costs_the_answer(site, server, monkeypatch):
    """The review of #494: whatever `cost_line` raises, `!status` answers and records."""

    def broken(*args):
        raise AttributeError("malformed snapshot")

    monkeypatch.setattr(planning, "cost_line", broken)
    await bot_of(server, HARVARD).on_message(incoming(OLGA, "!status alice"))
    await settle(server)
    assert '"alice": "running"' in posts(site, HARVARD)[-1]["content"]
    assert records(site, "read")[-1]["verb"] == "status"


async def test_the_console_link_goes_only_by_dm_to_the_sender(site, runner):
    tokens = Path(site.paths.state) / "console" / "tokens"
    tokens.mkdir(parents=True)
    signins = SignIns(tokens, "https://admin.example.com", clock=site.clock)
    srv = site.server(planner=Planning(runner, console=signins))
    await srv.start()
    try:
        await bot_of(srv, HARVARD).on_message(incoming(OLGA, "!console"))
        dm, note = posts(site, HARVARD)[-2:]
        assert dm["type"] == "private" and json.loads(dm["to"]) == [OLGA]
        token = re.fullmatch(
            r"Your console sign-in link, for one use within 5 minutes: "
            r"https://admin\.example\.com/login#([0-9a-f]{64})",
            dm["content"],
        )
        assert token is not None
        assert note["type"] == "stream" and token[1] not in note["content"]
        await bot_of(srv, GLOBAL).on_message(incoming(DREW, "!console", stream=None, to=(GLOBAL,)))
        assert json.loads(posts(site, GLOBAL)[-1]["to"]) == [DREW]
        count = len(posts(site))
        group = incoming(OLGA, "!console", stream=None, to=(HARVARD, CODEX_BOB))
        await bot_of(srv, HARVARD).on_message(group)  # a group DM is never read (#323)
        assert len(posts(site)) == count
        # the console uses Olga's link (deletes its file): the next tick records her sign-in
        (tokens / hashlib.sha256(token[1].encode()).hexdigest()).unlink()
        await srv.planner.tick()
        login = records(site, "console_login")
        assert [(r["workshop"], r["bot"], r["requester"]) for r in login] == [
            ("harvard-2026", HARVARD, {"id": OLGA, "name": "Olga Organizer"})
        ]
        site.clock.now += 301  # Drew's expires unused: deleted, and never recorded
        await srv.planner.tick()
        assert len(records(site, "console_login")) == 1 and list(tokens.iterdir()) == []
    finally:
        await srv.close()


async def test_no_console_yet(site, server):
    await bot_of(server, GLOBAL).on_message(incoming(DREW, "!console", stream=ADMINS_CH))
    assert "console is not set up" in posts(site, GLOBAL)[-1]["content"]


# ------------------------------------------------------------------------ with the queues


async def test_end_to_end_through_the_queues(site, runner):
    srv = site.server(planner=Planning(runner))
    task = asyncio.create_task(srv.run())
    try:
        await until(lambda: len({q.owner for q in site.realm.queues.values()}) == 3)
        site.realm.say(OLGA, HARVARD_CH, "!stop alice")
        await until(lambda: any("widget_content" in p for p in posts(site)))
        approval = max(m for m, msg in site.realm.messages.items() if msg.get("widget"))
        site.realm.click(OSCAR, approval, "✅ Approve")
        await until(lambda: len(runner.applied) == 1)
        assert srv.store.get(runner.applied[0].plan.id).state == APPROVED
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# --------------------------------------------------------------------- review of #346


async def test_a_typed_plan_id_never_moves_the_floor(site, runner):
    """Review of #346, finding 1: `!approve P-999999999` of no plan is recorded as `named`; after
    a restart the next plan still has an ID commands can name, and it can be approved."""
    srv = site.server(planner=Planning(runner))
    await srv.start()
    try:
        await bot_of(srv, HARVARD).on_message(incoming(OLGA, "!approve P-999999999 abcdef"))
        assert srv.ledger.floor == 0
        assert records(site, "refused")[-1]["named"] == "P-999999999"
        assert "plan" not in records(site, "refused")[-1]
    finally:
        await srv.close()
    srv = site.server(planner=Planning(runner))
    await srv.start()
    try:
        plan = await request(srv, "!stop alice")
        assert plan.id == 1 and plan.state == PENDING
        await bot_of(srv, HARVARD).on_message(
            incoming(OLGA, f"!approve {plan.name} {plan.hash[:6]}", mid=LATE)
        )
        assert srv.store.get(plan.id).state == APPROVED
    finally:
        await srv.close()


def test_the_largest_plan_id_is_the_grammars():
    from zab.admin.grammar import Command, Context, parse

    from .botkit import PRODUCTION_PATTERN

    ctx = Context(GLOBAL, "Workshop admin", PRODUCTION_PATTERN, None)
    assert isinstance(parse(f"!plan P-{MAX_PLAN_ID}", ctx), Command)
    assert not isinstance(parse(f"!plan P-{MAX_PLAN_ID + 1}", ctx), Command)


async def test_no_plan_id_beyond_the_grammars_is_handed_out(site, server, caplog):
    with server.store.db:
        server.store.db.execute(
            "UPDATE sqlite_sequence SET seq = ? WHERE name = 'plans'", (MAX_PLAN_ID,)
        )
    await bot_of(server, HARVARD).on_message(incoming(OLGA, "!stop alice"))
    await settle(server)
    assert _ids(server) == []
    assert "can't make plans" in posts(site, HARVARD)[-1]["content"]
    assert any(r.levelname == "ERROR" for r in caplog.records)


class SlowApply(FakeRunner):
    """A runner that marks its workshop busy only after an await, as PR 10's might."""

    def __init__(self) -> None:
        super().__init__()
        self.go = asyncio.Event()

    async def apply(self, job):
        await self.go.wait()
        self.applying[job.plan.workshop] = job.plan
        self.applied.append(job)
        del self.applying[job.plan.workshop]


async def test_busy_holds_before_the_runner_marks_it(site):
    """Review of #346, finding 2: the plan handed to the runner keeps its workshop busy until
    `apply` returns, whatever the runner does first."""
    runner = SlowApply()
    srv = site.server(planner=Planning(runner))
    await srv.start()
    try:
        one = await request(srv, "!stop alice")
        two = await request(srv, "!stop bob", mid=5001)
        harvard = bot_of(srv, HARVARD)
        await harvard.on_message(incoming(OLGA, f"!approve {one.name} {one.hash[:6]}", mid=LATE))
        await harvard.on_message(
            incoming(OSCAR, f"!approve {two.name} {two.hash[:6]}", mid=LATE + 1)
        )
        assert srv.store.get(two.id).state == PENDING
        assert posts(site, HARVARD)[-1]["content"].startswith(f"busy: {one.name} is applying")
        assert srv.approvals.approved_by(two.id) == ()
        runner.go.set()
        await settle(srv)
        await harvard.on_message(
            incoming(OSCAR, f"!approve {two.name} {two.hash[:6]}", mid=LATE + 2)
        )
        assert srv.store.get(two.id).state == APPROVED
    finally:
        await srv.close()


async def test_a_plan_of_250_rows_keeps_its_essentials(site, server, runner):
    """Review of #346, finding 3: the hash, the approve and reject commands, the chain head
    and the closing fences are never cut; the whole plan is attached."""
    rows = "".join(f"p{n:03d},P,{n},p{n:03d}@example.com,,,\n" for n in range(250))
    roster = Path(server.config.workshop("harvard-2026").roster)
    _append(roster, rows)
    runner.output = "".join(f"p{n:03d}: would launch\n" for n in range(250))
    plan = await request(server, "!launch all")
    assert plan.state == PENDING, plan.note
    text = posts(site, HARVARD)[-2]["content"]
    assert len(text) <= 10000 - 100
    assert f"`!approve {plan.name} {plan.hash[:6]}`" in text and f"`!reject {plan.name}`" in text
    head = records(site, "plan")[-1]
    assert f"`{plan.hash[:12]}`" in text and "· chain " in text.splitlines()[-1]
    assert "*Cut to fit: 210 lines of the dry run left out, and long lines cut." in text
    assert f"The whole plan: [{plan.name}-plan.txt](/user_uploads/" in text
    assert text.count("```") % 2 == 0  # every fence closed
    attached = next(v for k, v in site.realm.uploads.items() if k.endswith("-plan.txt"))
    assert b"p249" in attached and attached.count(b"would launch") == 250
    assert head["hash"] == plan.hash


async def test_a_long_status_is_attached(site, server, runner):
    async def long_read(action, fleet):
        return "".join(f"vm {n}\n" for n in range(100))

    runner.read = long_read
    await bot_of(server, HARVARD).on_message(incoming(OLGA, "!status all"))
    await settle(server)
    text = posts(site, HARVARD)[-1]["content"]
    assert "vm 49" in text and "vm 50" not in text and "[status.txt](/user_uploads/" in text
    attached = next(v for k, v in site.realm.uploads.items() if k.endswith("status.txt"))
    assert b"vm 99" in attached


async def test_the_lookup_is_never_the_cached_one(site, server):
    harvard = bot_of(server, HARVARD)
    plan = await request(server, "!stop alice")
    await server.user(harvard, OSCAR)  # cached while active
    site.realm.users[OSCAR]["is_active"] = False
    await harvard.on_message(incoming(OSCAR, f"!approve {plan.name} {plan.hash[:6]}", mid=LATE))
    assert records(site, "refused")[-1]["code"] == "inactive"


@pytest.mark.parametrize(("missing", "code"), [("is_bot", "bot-sender"), ("is_active", "inactive")])
async def test_an_account_missing_a_field_never_approves(site, server, missing, code):
    plan = await request(server, "!stop alice")
    del site.realm.users[OSCAR][missing]
    await bot_of(server, HARVARD).on_message(
        incoming(OSCAR, f"!approve {plan.name} {plan.hash[:6]}", mid=LATE)
    )
    assert server.store.get(plan.id).state == PENDING
    assert records(site, "refused")[-1]["code"] == code


async def test_an_existing_plan_directory_is_never_written(site, server):
    runs = Path(site.paths.layout.runs)
    _mkdir(runs / "P-1")
    _put(runs / "P-1" / "roster-upload.csv", "earlier")
    path = site.realm.upload("r.csv", b"slug\nalice\n")
    plan = await request(server, f"!roster import [r.csv]({path})")
    assert plan.state == REFUSED and plan.id == 1
    assert _read(runs / "P-1" / "roster-upload.csv") == "earlier"


async def test_a_slow_download_is_cut_off(site, server, monkeypatch):
    monkeypatch.setattr(planning, "DOWNLOAD_SECONDS", 0.05)
    harvard = bot_of(server, HARVARD)

    async def trickle(url, limit):
        await asyncio.sleep(5)

    monkeypatch.setattr(harvard.client, "download", trickle)
    path = site.realm.upload("r.csv", b"slug\nalice\n")
    plan = await request(server, f"!roster import [r.csv]({path})")
    assert plan.state == REFUSED and plan.note == "the CSV could not be downloaded: TimeoutError"


async def test_notices_go_to_the_plans_own_topic(site, runner):
    srv = site.server(planner=Planning(runner))
    await srv.start()
    try:
        await bot_of(srv, HARVARD).on_message(incoming(OLGA, "!stop alice", topic="canary"))
        await settle(srv)
        applying = await request(srv, "!stop bob", mid=5001)
        srv.store.move(applying.id, (PENDING,), APPLYING)
        site.clock.now += srv.config.plan_ttl_minutes * 60
        await srv.planner.tick()
    finally:
        await srv.close()
    expired = posts(site, HARVARD)[-1]
    assert expired["content"].startswith("⌛ P-1 expired")
    assert (expired["to"], expired["topic"]) == (str(HARVARD_CH), "canary")
    srv = site.server(planner=Planning(runner))
    await srv.start()
    await srv.close()
    interrupted = posts(site, HARVARD)[-1]
    assert interrupted["content"].startswith(f"⏹ {applying.name} was interrupted")
    assert (interrupted["to"], interrupted["topic"]) == (str(HARVARD_CH), "ops")


class SlowPrepare(FakeRunner):
    def __init__(self) -> None:
        super().__init__()
        self.go = asyncio.Event()
        self.running = self.most = 0

    async def prepare(self, plan, action, fleet):
        self.running += 1
        self.most = max(self.most, self.running)
        await self.go.wait()
        self.running -= 1
        return await super().prepare(plan, action, fleet)


async def test_a_slow_dry_run_never_holds_the_queue(site):
    """Decided on #346: plans are prepared as their own tasks, at most PREPARING of
    one workshop at once, and other messages are answered meanwhile."""
    runner = SlowPrepare()
    srv = site.server(planner=Planning(runner))
    await srv.start()
    try:
        harvard = bot_of(srv, HARVARD)
        for n, slug in enumerate(("alice", "bob", "carol")):
            await harvard.on_message(incoming(OLGA, f"!stop {slug}", mid=5000 + n))
        await harvard.on_message(incoming(OSCAR, "!help", mid=5100))
        assert "takes:" in posts(site, HARVARD)[-1]["content"]  # answered while they wait
        await until(lambda: runner.running == planning.PREPARING)
        assert runner.most == planning.PREPARING
        runner.go.set()
        await settle(srv)
        assert [srv.store.get(i).state for i in _ids(srv)] == [PENDING] * 3
    finally:
        await srv.close()


def _append(path, text):
    with path.open("a") as f:
        f.write(text)


def _put(path, text):
    path.write_text(text)


def _mkdir(path):
    path.mkdir(parents=True)


def _read(path):
    return path.read_text()


def test_the_status_table_of_status_json():
    """#361: one line per VM, columns two spaces apart and aligned, `?` for unknown; the lines
    the read printed besides its answer follow; text without an answer is shown as it is."""
    claude, codex = {"backend": "claude"}, {"backend": "codex"}
    vms = [
        {"slug": "alice", "bridge": "active", "zab_version": "0.2.3",
         "idle_stop": {"enabled": True, "active": False, "minutes": 15},
         "running_turns": [codex, claude, claude]},
        {"slug": "bob", "bridge": None, "zab_version": None, "idle_stop": None,
         "running_turns": None},
        {"slug": "carol", "bridge": "active,   doctor\nFAIL", "zab_version": "0.2.4rc1",
         "idle_stop": {"enabled": False, "active": False, "minutes": None}, "running_turns": []},
        "not a VM",
        {"slug": "dave", "bridge": "active", "zab_version": "0.2.3",
         "idle_stop": {"enabled": True, "active": True, "minutes": None},
         "running_turns": [codex, "?"]},
        {"slug": "erin", "idle_stop": {"enabled": "yes"}},
    ]  # fmt: skip
    answer = json.dumps({"schema": 1, "vms": vms, "summary": {}}, indent=2)
    view = planning.status_view(f"EC2 lookup failed (x)\n{answer}\n── exited 1\n", str)
    assert [re.split(r" {2,}", line) for line in view] == [
        ["VM", "state", "zab", "idle stop", "running turns"],
        ["alice", "active", "0.2.3", "15 min, inactive", "2 claude, 1 codex"],  # by name
        ["bob", "?", "?", "?", "?"],
        ["carol", "active, doctor FAIL", "0.2.4rc1", "off", "0"],
        ["dave", "active", "0.2.3", "on", "1 ?, 1 codex"],
        ["erin", "?", "?", "?", "?"],
        ["EC2 lookup failed (x)"],
        ["── exited 1"],
    ]
    at = view[0].index("zab")  # aligned
    assert [view[n][at:].split("  ")[0] for n in (1, 2, 3)] == ["0.2.3", "?", "0.2.4rc1"]
    no_list = answer.replace('"vms": [', '"vms": null, "x": [')
    for other in ('{"alice": "running"}\n', answer.replace('"schema": 1', '"schema": 2'), no_list):
        assert planning.status_view(other, str) == other.removesuffix("\n").split("\n")


def test_the_status_tables_work_is_bounded():
    """§2.7 item 5, as the review of #380 asked of `json_text`: at most `STATUS_TRIES` places
    are decoded, `STATUS_ROWS` rows built, `MAX_TURNS` turns read per VM; the rest counted."""
    scrubbed = []

    def scrub(text):
        scrubbed.append(text)
        return text

    vm = {"slug": "a", "running_turns": [{"backend": "claude"}] * (planning.MAX_TURNS + 5)}
    answer = json.dumps({"schema": 1, "vms": [vm] * (planning.STATUS_ROWS + 5)})
    view = planning.status_view(answer + "\n", scrub)
    assert len(view) == planning.STATUS_ROWS + 2 and view[-1] == "… and 5 more VMs"
    assert re.split(r" {2,}", view[1])[-1] == "20 claude, 5 more"
    assert len(scrubbed) == planning.STATUS_ROWS * (1 + planning.MAX_TURNS)  # slug, turns
    decoys = '{"schema": x\n' * planning.STATUS_TRIES  # their tries are spent: shown as text
    assert planning.status_view(decoys + answer, str) == (decoys + answer).split("\n")
    assert planning.status_view(decoys[13:] + answer, str)[0].startswith("VM ")


def test_each_status_value_is_scrubbed_again_before_it_is_cut():
    """status.py's JSON escapes what a VM printed (`\\u200b`), so a scan of the text can't see a
    secret an invisible character splits; decoded, each value is scrubbed again, and only then
    cut to its cell, so that no cut leaves a secret's head. A value that isn't a string is `?`:
    `str` of a list would escape it again."""
    from zab.admin.scrub import Scrubber

    scrub = Scrubber(["S" * 16])
    split = "S" * 8 + "\u200b" + "S" * 8
    vms = [{"slug": "alice", "bridge": split, "zab_version": "0.2.3"},
           {"slug": "bob", "bridge": "x" * 20 + split, "zab_version": [split]}]  # fmt: skip
    text = json.dumps({"schema": 1, "vms": vms}, indent=2)
    assert "\\u200b" in text and scrub(text) == text  # the escaped form hides it
    view = planning.status_view(text, scrub)  # the cells alone, without `json_text`
    assert "S" * 8 not in "\n".join(view)
    assert re.split(r" {2,}", view[1])[1] == "[redacted secret]"
    assert re.split(r" {2,}", view[2])[1:3] == ["x" * 20 + "[redacted s…", "?"]


def _status_plan(n, *, dry, notes=(), limit=10000):
    """`plan_message` of a `!stop` of `n` VMs whose dry run is `dry` and whose status read is
    `status.py`'s answer for them; with `notes` under the targets."""
    from zab.admin.actions import TARGETS, Action, Step
    from zab.admin.audit import Head
    from zab.admin.plans import Plan
    from zab.admin.scrub import Scrubber

    names = tuple(f"vm-{i:03d}" for i in range(n))
    argv = ("/usr/bin/python", "power.py", "--stop", "--all")
    action = Action("stop", "ws", HARVARD, ("all",), (Step(argv, argv),), TARGETS, names, (),
                    status=("status.py", "--json"), notes=tuple(notes))  # fmt: skip
    plan = Plan(7, "ws", HARVARD, OLGA, "!stop all", "planning", 0.0, None, (), "", None,
                None, None, "", None)  # fmt: skip
    vms = [{"slug": s, "bridge": "active", "zab_version": "0.2.3", "running_turns": []}
           for s in names]  # fmt: skip
    read = planning.StatusRead.of(json.dumps({"schema": 1, "vms": vms}, indent=2), Scrubber())
    return planning.plan_message(
        plan, action, "ab" * 32, Requirement("change"), requester="Olga", expires=0.0,
        output=dry, head=Head(3, "c" * 64), limit=limit, status=read,
    )  # fmt: skip


def test_a_status_table_over_50_lines_is_cut_though_the_message_fits():
    message = _status_plan(51, dry="vm-000: would stop\n")
    assert message.full is not None and len(message.text) < 4000  # room enough: the cap cut it
    assert "*Cut to fit: 2 lines of the status left out. " in message.text  # 52 lines, 50 shown
    assert message.full.count("  active  0.2.3") == 51
    assert _status_plan(49, dry="vm-000: would stop\n").full is None  # 50 lines: whole (#764)
    one = _status_plan(50, dry="vm-000: would stop\n").text  # 51 lines: one left out
    assert "*Cut to fit: 1 line of the status left out. " in one  # review of #453


def test_a_plans_dry_run_is_cut_at_40_lines_and_its_status_table_at_50():
    """#764: the status table's cap is 50 lines, the dry run's stays 40; each binds here, in a
    message far below Zulip's limit."""
    from .test_admin_plan_fit import fenced_after

    dry = "".join(f"vm-{i:03d}: would stop\n" for i in range(60))
    message = _status_plan(60, dry=dry)
    assert message.full is not None and len(message.text) < 6000
    shown_dry = fenced_after(message.text, "**Dry run**")
    assert shown_dry == [f"vm-{i:03d}: would stop" for i in range(40)]
    shown_read = fenced_after(message.text, "**Status**")
    assert len(shown_read) == 50 and shown_read[-1].startswith("vm-048  active")
    note = "*Cut to fit: 20 lines of the dry run, 11 lines of the status left out. "
    assert note in message.text
    assert "vm-059: would stop" in message.full and "\nvm-059  active  0.2.3" in message.full


def test_a_long_dry_run_leaves_the_table_only_the_room_that_is_left():
    """The dry run takes its room first, and the table only what remains: never both whole."""
    dry = "".join(f"vm-{i:03d}: {'x' * 200}\n" for i in range(40))  # about 8,400 characters
    message = _status_plan(30, dry=dry)
    assert len(message.text) <= 10000 - planning.MARGIN and message.full is not None
    assert "lines of the status left out" in message.text
    assert message.text.count("x" * 200) > 30  # the dry run kept most of its lines


def test_notes_and_the_status_table_both_shown_both_budgeted_both_attached():
    """#367's notes go under the targets, the table after the dry run; when the message is
    cut, each keeps a share, and the attachment holds both."""
    notes = [
        "**Participant rows** (60), 0 unsynced:",
        *(f"- vm-{i:03d}: synced" for i in range(60)),
    ]
    whole = _status_plan(3, dry="vm-000: would stop\n", notes=notes[:4])
    text = whole.text
    assert whole.full is None
    assert text.index("**Targets**") < text.index("- vm-002: synced") < text.index("**Commands**")
    assert text.index("**Dry run**") < text.index("**Status**") < text.index("vm-002  active")
    cut = _status_plan(60, dry="vm-000: would stop\n" * 20, notes=notes, limit=3000)
    assert cut.full is not None and len(cut.text) <= 3000 - planning.MARGIN
    assert "- vm-000: synced" in cut.text and "vm-000  active" in cut.text
    assert "lines of notes" in cut.text and "lines of the status left out" in cut.text
    assert "- vm-059: synced" in cut.full and "vm-059  active" in cut.full


def test_plan_message_cuts_the_targets_last_of_all_but_keeps_its_essentials():
    from zab.admin.actions import TARGETS, Action, Step
    from zab.admin.audit import Head
    from zab.admin.plans import Plan

    names = tuple(f"participant-{n:04d}" for n in range(3000))
    argv = ("/usr/bin/python", "launch.py", *(w for n in names for w in ("--only", n)))
    action = Action("launch", "ws", HARVARD, ("all",), (Step(argv, argv),), TARGETS, names, ())
    plan = Plan(7, "ws", HARVARD, OLGA, "!launch all", "planning", 0.0, None, (), "", None,
                None, None, "", None)  # fmt: skip
    needs = Requirement("change")
    message = planning.plan_message(
        plan, action, "ab" * 32, needs, requester="Olga", expires=0.0,
        output="ok\n" * 5, head=Head(3, "c" * 64), limit=10000,
    )  # fmt: skip
    text = message.text
    assert len(text) <= 10000 - planning.MARGIN and message.full is not None
    assert "targets left out" in text and "participant-2999" not in text
    assert text.endswith("`!reject P-7`.\n`audit #3 · chain cccccccccccc`")
    assert text.count("```") % 2 == 0 and planning.ATTACH in text
    assert "participant-2999" in message.full


async def test_a_plan_directory_made_before_is_never_used(site, server):
    runs = Path(site.paths.layout.runs)
    _mkdir(runs / "P-1")  # empty, made by someone else
    path = site.realm.upload("r.csv", b"slug\nalice\n")
    plan = await request(server, f"!roster import [r.csv]({path})")
    assert plan.state == REFUSED and not (runs / "P-1" / "roster-upload.csv").exists()


def test_the_upload_is_opened_new_never_through_a_link(tmp_path, monkeypatch):
    opened = []
    real = planning.os.open

    def spy(path, flags, mode=0o777):
        opened.append((flags, mode))
        return real(path, flags, mode)

    monkeypatch.setattr(planning.os, "open", spy)
    planning._save(tmp_path / "runs" / "P-3" / "roster-upload.csv", b"x")
    ((flags, mode),) = opened
    assert flags & os.O_EXCL and flags & os.O_NOFOLLOW and flags & os.O_CREAT and mode == 0o600


class SlowRead(FakeRunner):
    def __init__(self) -> None:
        super().__init__()
        self.go = asyncio.Event()

    async def read(self, action, fleet):
        await self.go.wait()
        return "done"


async def test_a_slow_status_never_holds_the_queue(site):
    runner = SlowRead()
    srv = site.server(planner=Planning(runner))
    await srv.start()
    try:
        harvard = bot_of(srv, HARVARD)
        await harvard.on_message(incoming(OLGA, "!status all", mid=5000))
        await harvard.on_message(incoming(OSCAR, "!help", mid=5001))
        assert "takes:" in posts(site, HARVARD)[-1]["content"]
        runner.go.set()
        await settle(srv)
        assert "done" in posts(site, HARVARD)[-1]["content"]
    finally:
        await srv.close()


async def test_a_command_as_long_as_a_message_still_fits(site, server, runner):
    """Codex on #346: `!stop` naming 300 slugs of 32 characters is about 9,900 characters; the
    plan shortens it in its header and keeps every essential, with the whole command in the
    attachment, and so do the approval message and `!plans`."""
    slugs = [f"{'x' * 28}{n:04d}" for n in range(300)]
    _append(
        Path(server.config.workshop("harvard-2026").roster), "".join(f"{s},,,,,,\n" for s in slugs)
    )
    command = "!stop " + " ".join(slugs)
    assert len(command) > 9800
    plan = await request(server, command)
    assert plan.state == PENDING, plan.note
    text, approval = (p["content"] for p in posts(site, HARVARD)[-2:])
    assert len(text) <= 10000 - planning.MARGIN // 2 and len(approval) < 1000
    assert "(301 words in all)" in text and "the command shortened" in text
    assert f"`!approve {plan.name} {plan.hash[:6]} stop 300`" in text
    assert text.splitlines()[-1].startswith("`audit #")
    attached = next(v for k, v in site.realm.uploads.items() if k.endswith("-plan.txt"))
    assert command.encode() in attached
    await bot_of(server, HARVARD).on_message(incoming(OLGA, "!plans", mid=LATE))
    assert len(posts(site, HARVARD)[-1]["content"]) < 1000


async def test_a_plan_that_would_still_be_too_long_is_refused_never_cut(site, server, monkeypatch):
    """The backstop behind `plan_message`'s sizing: never a plan posted cut by `send`."""
    monkeypatch.setattr(
        planning, "plan_message", lambda *a, **k: planning.PlanMessage("x" * 12000, None)
    )
    plan = await request(server, "!stop alice")
    assert plan.state == REFUSED and plan.note == "the plan doesn't fit in a message"
    assert all(len(p["content"]) < 10000 for p in posts(site))


# --------------------------------------------------------------- re-review of #346


def _failing_audit(server, events):
    real = server.audit

    async def audit(event, **fields):
        if event in events or "*" in events:
            raise OSError("no space left on device")
        return await real(event, **fields)

    return audit


async def test_the_busy_mark_never_outlives_a_failed_approval(site, server, runner, monkeypatch):
    """Finding 1: an approval whose record fails leaves no busy mark behind."""
    one = await request(server, "!stop alice")
    two = await request(server, "!stop bob", mid=5001)
    harvard = bot_of(server, HARVARD)
    with monkeypatch.context() as patch:
        patch.setattr(server, "audit", _failing_audit(server, {"approval"}))
        with pytest.raises(OSError):
            await harvard.on_message(
                incoming(OLGA, f"!approve {one.name} {one.hash[:6]}", mid=LATE)
            )
    assert server.planner.handed == {} and runner.applied == []
    await harvard.on_message(incoming(OLGA, f"!approve {two.name} {two.hash[:6]}", mid=LATE + 1))
    await settle(server)
    assert server.store.get(two.id).state == APPROVED and len(runner.applied) == 1


async def test_a_plan_whose_request_record_fails_is_refused_and_said(site, server, monkeypatch):
    """Finding 2: anything after `store.begin` refuses the plan, and its requester hears."""
    monkeypatch.setattr(server, "audit", _failing_audit(server, {"*"}))
    await bot_of(server, HARVARD).on_message(incoming(OLGA, "!stop alice", topic="ops-2"))
    await settle(server)
    (plan_id,) = _ids(server)
    assert server.store.get(plan_id).state == REFUSED
    post = posts(site, HARVARD)[-1]
    assert post["topic"] == "ops-2"
    assert post["content"] == f"❌ P-{plan_id} refused: planning failed (OSError); nothing was done"


async def test_a_plan_with_no_topic_record_is_refused_in_the_requests_topic(site, server):
    """#347 (the approval of #346): when the plan's topic can't be recorded, the planner still
    refuses it, and says so in the request's own topic (`_refuse`'s `where`), not nowhere."""
    server.store.db.execute(
        "CREATE TRIGGER no_topic BEFORE INSERT ON plan_topics "
        "BEGIN SELECT RAISE(ABORT, 'full'); END"
    )
    await bot_of(server, HARVARD).on_message(incoming(OLGA, "!stop alice", topic="ops-3"))
    await settle(server)
    (plan_id,) = _ids(server)
    assert server.store.get(plan_id).state == REFUSED
    assert server.store.db.execute("SELECT * FROM plan_topics").fetchall() == []
    post = posts(site, HARVARD)[-1]
    assert post["topic"] == "ops-3"
    assert post["content"].startswith(f"❌ P-{plan_id} refused: planning failed (IntegrityError)")


async def test_a_failed_task_is_logged_with_its_traceback(site, server, runner, caplog):
    async def broken(job):
        raise RuntimeError("the runner broke")

    runner.apply = broken
    plan = await request(server, "!stop alice")
    await bot_of(server, HARVARD).on_message(
        incoming(OLGA, f"!approve {plan.name} {plan.hash[:6]}", mid=LATE)
    )
    await settle(server)
    (record,) = [r for r in caplog.records if r.getMessage().startswith("a plan's task failed")]
    assert record.exc_info is not None and "the runner broke" in str(record.exc_info[1])
    assert server.planner.handed == {}  # released when the task ended


async def test_a_failed_status_is_answered(site, server, runner):
    async def broken(action, fleet):
        raise RuntimeError("ssh exploded")

    runner.read = broken
    await bot_of(server, HARVARD).on_message(incoming(OLGA, "!status all"))
    await settle(server)
    text = posts(site, HARVARD)[-1]["content"]
    assert text == "❌ `!status` failed (RuntimeError): nothing was changed"


async def test_each_status_in_turn_runs_after_one_that_failed(site, server, runner):
    """#347 (the approval of #346): the workshop's "under way" mark is cleared however a
    `!status` ends, so the next one runs: after one that failed, and after one answered."""
    real, failed = runner.read, []

    async def read(action, fleet):
        if not failed:
            failed.append(True)
            raise RuntimeError("ssh exploded")
        return await real(action, fleet)

    runner.read = read
    harvard = bot_of(server, HARVARD)
    for n in range(3):
        await harvard.on_message(incoming(OLGA, "!status all", mid=5000 + n))
        await settle(server)
    texts = [p["content"] for p in posts(site, HARVARD)]
    assert texts[0] == "❌ `!status` failed (RuntimeError): nothing was changed"
    assert len(texts) == 3 and not any("under way" in text for text in texts)
    assert server.planner.reading == set()


async def test_a_refused_download_names_no_signed_link(site, server):
    """Finding 3: an HTTP error's text holds the signed URL; only its status is shown."""
    path = site.realm.upload("r.csv", b"slug\nalice\n")
    site.realm.forbidden.add(path.removeprefix("/user_uploads/"))
    plan = await request(server, f"!roster import [r.csv]({path})")
    assert plan.note == "the CSV could not be downloaded: HTTP 403"
    assert not any("temporary" in p["content"] for p in posts(site))
    assert "temporary" not in audit_text(site.audit)


async def test_one_status_per_workshop_at_once(site):
    """Finding 5: `!status` reads every VM over SSH: one per workshop at a time."""
    runner = SlowRead()
    srv = site.server(planner=Planning(runner))
    await srv.start()
    try:
        harvard, test = bot_of(srv, HARVARD), bot_of(srv, TEST)
        await harvard.on_message(incoming(OLGA, "!status all", mid=5000))
        await asyncio.sleep(0)
        await harvard.on_message(incoming(OSCAR, "!status alice", mid=5001))
        await until(lambda: any("under way" in p["content"] for p in posts(site, HARVARD)))
        await test.on_message(incoming(TESS, "!status erin", stream=TEST_CH, mid=5002))
        runner.go.set()
        await settle(srv)
        assert sum("done" in p["content"] for p in posts(site)) == 2  # harvard's and zab-test's
    finally:
        await srv.close()


async def test_an_approval_while_the_plan_is_prepared_says_so(site):
    """The re-review's nit: a PLANNING plan has no approvers yet, so not "not an approver"."""
    runner = SlowPrepare()
    srv = site.server(planner=Planning(runner))
    await srv.start()
    try:
        harvard = bot_of(srv, HARVARD)
        await harvard.on_message(incoming(OLGA, "!stop alice", mid=5000))
        await until(lambda: runner.running == 1)
        await harvard.on_message(incoming(OLGA, "!approve P-1 abcdef", mid=5001))
        assert posts(site, HARVARD)[-1]["content"].startswith("❌ P-1 is still being prepared")
        assert records(site, "refused")[-1]["code"] == "planning"
        assert srv.approvals.approved_by(1) == ()
        runner.go.set()
        await settle(srv)
        assert srv.store.get(1).state == PENDING
    finally:
        await srv.close()


async def test_a_stop_cancels_the_planners_tasks_before_closing_what_they_use(site):
    """Codex round 2 on #346: `close()` stops the planner's tasks first, while the store and the
    clients they use are still open; the next start marks the cut plan INTERRUPTED."""
    runner = SlowPrepare()
    seen = []
    started = asyncio.Event()  # the task must be inside prepare, not merely created (#347)
    srv = site.server(planner=Planning(runner))

    async def prepare(plan, action, fleet):
        started.set()
        try:
            await runner.go.wait()
        except asyncio.CancelledError:
            seen.append(srv.store is not None and not _closed(srv))
            raise
        return Prepared(True)

    runner.prepare = prepare
    await srv.start()
    await bot_of(srv, HARVARD).on_message(incoming(OLGA, "!stop alice", mid=5000))
    await until(lambda: _ids(srv) == [1] and srv.planner.tasks)
    await asyncio.wait_for(started.wait(), 5)
    count = len(posts(site))
    await srv.close()
    assert seen == [True] and srv.planner.tasks == set()
    assert len(posts(site)) == count  # nothing posted against closed clients
    again = site.server(planner=Planning(FakeRunner()))
    await again.start()
    try:
        assert again.store.get(1).state == INTERRUPTED
    finally:
        await again.close()


def _closed(srv):
    return any(b.client.http.is_closed for b in srv.bots)
