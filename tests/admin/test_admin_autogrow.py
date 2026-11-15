"""Automatic disk growth (#998; ADMIN §3.4, `zab.admin.autogrow`): the bots of `botkit` with the
real `Runner`, a fake `grow_disk.py` in a temporary release tree, and the collector's snapshot as
the test writes it. The trigger at and below the threshold, a read too old or before the last
try, a VM not running, EBS's 6 hours, a growth still above the threshold, the cap, each failure
and its bounded tries, the alerts line, the audit record and a restart."""

import asyncio
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from zab.admin import autogrow
from zab.admin import runner as runner_mod
from zab.admin.autogrow import AUTO, CAP_GB, EVERY, RETRY, THRESHOLD, TOPIC, TRIES
from zab.admin.planning import Planning, growth_busy
from zab.admin.plans import APPLYING, DONE, PENDING, PLANNING
from zab.admin.runner import Runner

from .botkit import GLOBAL, HARVARD, HARVARD_CH, OLGA, audit_text, incoming, make_site

pytestmark = pytest.mark.integration
ALERTS = 44
#: A fake `grow_disk.py`: it logs its argv, then answers the next of `answers.json`'s
#: `[exit, [lines]]`, or crashes when there is none.
FAKE = """\
import json, pathlib, sys
with open({calls!r}, "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
answers = pathlib.Path({answers!r})
queue = json.loads(answers.read_text())
code, lines = queue.pop(0)
answers.write_text(json.dumps(queue))
print("\\n".join(lines))
sys.exit(code)
"""


@pytest.fixture
def site(tmp_path, monkeypatch):
    site = make_site(tmp_path)
    text = site.config_path.read_text()
    site.config_path.write_text(
        text.replace(
            'name = "harvard-2026"\n', f'name = "harvard-2026"\nalerts_channel = {ALERTS}\n', 1
        )
    )
    layout = replace(site.paths.layout, release=str(tmp_path / "release"), python=sys.executable)
    site.paths = replace(site.paths, layout=layout)
    monkeypatch.setattr(runner_mod, "GRACE", 0.3)
    script = tmp_path / "release" / "workshop" / "aws" / "grow_disk.py"
    script.parent.mkdir(parents=True)
    script.write_text(FAKE.format(calls=str(tmp_path / "calls.jsonl"),
                                  answers=str(tmp_path / "answers.json")))  # fmt: skip
    (tmp_path / "answers.json").write_text("[]")
    (Path(site.paths.state) / "console").mkdir()
    return site


async def start(site):
    srv = site.server()
    srv.planner = Planning(Runner(srv, vms=lambda fleet, names: {}))
    await srv.start()
    return srv


@pytest.fixture
async def server(site):
    srv = await start(site)
    yield srv
    await srv.close()


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


SAME = object()


def snapshot(site, used, *, read=None, state="running", launched=None, slug="alice",
             instance="i-1", read_from=SAME, **status):  # fmt: skip
    """The collector's snapshot: alice's VM, `used`% full, read `read` (60 s ago), from that
    VM (`read_from`: another, or None)."""
    now = site.clock.now
    values = {"slug": slug, "bridge": "active", "disk_percent": used,
              "instance": instance if read_from is SAME else read_from,
              "at": now - 60 if read is None else read, "source": "sweep"} | status  # fmt: skip
    vm = {
        "slug": slug,
        "kind": "participant",
        "instance": instance,
        "state": state,
        "launched_at": iso(now - 3600 if launched is None else launched),
        "status": values,
    }
    data = {"schema": 1, "at": now, "workshops": {"harvard-2026": {"vms": [vm]}}}
    (Path(site.paths.state) / "console" / "snapshot.json").write_text(json.dumps(data))


def answer(site, outcome, code=None, **fields):
    """The script's next answer: `AUTO` and its JSON, as grow_disk.py prints it."""
    data = {"outcome": outcome, "instance": "i-1", "from": 80, "to": 120, "fs": "grown",
            "used": 58, "again": site.clock.now + EVERY, "reason": ""} | fields  # fmt: skip
    code = {"grown": 0, "failed": 1}.get(outcome, 3) if code is None else code
    lines = ["alice i-1 (running): volume vol-1 gp3 80 GB → 120 GB", AUTO + json.dumps(data)]
    queue(site, [code, lines])


def queue(site, *items):
    path = site.tmp / "answers.json"
    path.write_text(json.dumps([*json.loads(path.read_text()), *items]))


def calls(site):
    """The fake's calls, whole lines only: one still being written is left out (#1001)."""
    path = site.tmp / "calls.jsonl"
    return [json.loads(line) for line in path.read_text().split("\n")[:-1]] if path.exists() else []


def alerts(site):
    return [p["content"] for p in site.realm.posts(HARVARD) if p.get("topic") == TOPIC]


def records(site):
    rows = [json.loads(line) for line in audit_text(site.audit).splitlines()]
    return [r for r in rows if r["event"] == "disk_grow"]


async def look(server):
    await server.grower.check()
    await asyncio.gather(*list(server.grower.tasks))


async def test_at_the_threshold_it_grows_and_says_so_once(site, server):
    """85% used, by a fresh read since the boot: `!grow-disk`'s script for the row with
    `--auto-cap`, one line in the alerts channel, and a `disk_grow` record."""
    snapshot(site, THRESHOLD - 1)
    await look(server)
    assert calls(site) == [] and alerts(site) == []  # below: nothing
    snapshot(site, THRESHOLD)
    answer(site, "grown")
    await look(server)
    (argv,) = calls(site)
    assert argv[argv.index("--slug") + 1] == "alice" and "--size" not in argv
    assert argv[argv.index("--auto-cap") + 1] == str(CAP_GB) == "400"
    assert argv[argv.index("--region") + 1] == "us-west-2"
    assert argv[argv.index("--tag") + 1] == "workshop=harvard-2026"
    assert argv[-2:] == ["--expect-instance", "i-1"]  # the VM read (Codex round 3 on #1003)
    (line,) = alerts(site)
    assert line == (
        "✅ **alice**'s disk grew from 80 to 120 GB, online: its root file system was 85% used at "
        "07:59 UTC, and is 58% now. About $3.20 a month more."
    )
    assert site.realm.posts(HARVARD)[-1]["to"] == str(ALERTS)
    (record,) = records(site)
    assert (record["workshop"], record["slug"], record["instance"], record["outcome"]) == (
        "harvard-2026", "alice", "i-1", "grown")  # fmt: skip
    assert (record["from_gb"], record["to_gb"], record["read"]["used"]) == (80, 120, 85)
    assert type(record["alert"]) is int
    await look(server)  # the same read again: it says nothing new
    assert len(calls(site)) == 1 and len(alerts(site)) == 1


@pytest.mark.parametrize(
    "case", ["old read", "before its boot", "stopped", "spare", "no alerts channel", "down"]
)
async def test_what_grows_nothing(site, case):
    """A read older than FRESH, or made before the VM's boot; a VM not running, or a spare; a
    workshop without an alerts channel; a read that found it down."""
    now = site.clock.now
    kw = {
        "old read": {"read": now - autogrow.FRESH - 1},
        "before its boot": {"launched": now - 30},
        "stopped": {"state": "stopped"},
        "down": {"bridge": "unreachable"},
    }.get(case, {})
    snapshot(site, 99, **kw)
    if case == "spare":
        data = json.loads((Path(site.paths.state) / "console" / "snapshot.json").read_text())
        data["workshops"]["harvard-2026"]["vms"][0]["kind"] = "spare"
        (Path(site.paths.state) / "console" / "snapshot.json").write_text(json.dumps(data))
    if case == "no alerts channel":
        site.config_path.write_text(site.config_path.read_text().replace(
            f"alerts_channel = {ALERTS}\n", ""))  # fmt: skip
    answer(site, "grown")
    server = await start(site)
    try:
        await look(server)
        assert calls(site) == [] and alerts(site) == [] and records(site) == []
    finally:
        await server.close()


async def test_ebs_six_hours_then_again_and_still_above_says_when(site, server):
    """After a growth, none until EBS allows it (the time the script named), whatever the reads
    say; a growth that leaves it above the threshold says when the next can come."""
    snapshot(site, 99)
    answer(site, "grown", used=90, again=site.clock.now + EVERY)
    await look(server)
    (line,) = alerts(site)
    assert line.endswith("and is 90% now. About $3.20 a month more. Still above 85%: EBS allows "
                         "its next growth from 14:00 UTC.")  # fmt: skip
    site.clock.now += EVERY - 120
    snapshot(site, 95)
    await look(server)
    assert len(calls(site)) == 1  # within EBS's 6 hours
    site.clock.now += 120
    snapshot(site, 95)
    answer(site, "grown", **{"from": 120, "to": 180})
    await look(server)
    assert len(calls(site)) == 2 and "from 120 to 180 GB" in alerts(site)[-1]


async def test_a_change_within_six_hours_waits_and_says_until_when(site, server):
    """Another's `!grow-disk` changed the volume less than 6 hours ago: the script grows the
    file system alone, or says that it waits (`wait`), and the next try is then."""
    snapshot(site, 92)
    answer(site, "wait", fs="unchanged", **{"from": 120, "to": 120}, again=site.clock.now + 3600)
    await look(server)
    (line,) = alerts(site)
    assert line == (
        "⏳ **alice**: its root file system was 92% used at 07:59 UTC, and its volume changed less "
        "than 6 hours ago: EBS allows its next growth from 09:00 UTC, when it grows if it still "
        "needs to. Until then `!grow-disk` can't grow it either: its participant may free space."
    )
    site.clock.now += 1800
    snapshot(site, 93)
    await look(server)
    assert len(calls(site)) == 1
    site.clock.now += 1800
    snapshot(site, 93)
    answer(site, "grown", **{"from": 120, "to": 180})
    await look(server)
    assert len(calls(site)) == 2


async def test_at_the_cap_it_says_so_once_until_the_disk_is_below(site, server):
    snapshot(site, 97)
    answer(site, "capped", **{"from": 400, "to": 400})
    await look(server)
    (line,) = alerts(site)
    assert line == (
        "⚠ **alice**: its root file system was 97% used at 07:59 UTC, and its volume is 400 GB, "
        "the cap of automatic growth: it grows no more on its own. `!grow-disk alice 600GB` "
        "grows it further (ORGANIZERS §4.1)."
    )
    for _ in range(3):
        site.clock.now += EVERY
        snapshot(site, 97)
        await look(server)
    assert len(calls(site)) == 1 and len(alerts(site)) == 1
    snapshot(site, 60)  # cleaned up: a later fill tries again
    await look(server)
    site.clock.now += 600
    snapshot(site, 90)
    answer(site, "capped")
    await look(server)
    assert len(calls(site)) == 2


async def test_failures_are_said_and_tried_three_times_at_most(site, server):
    """Each failure in the alerts channel; a try every RETRY, each on a read made since, TRIES
    in all; then none, until a read below the threshold."""
    reasons = [
        "alice's volume vol-1 lacks zab-scope=mathagents: the admin server's role may grow only a "
        "volume in its scope",
        "ModifyVolume of vol-1 (UnauthorizedOperation): not authorized",
    ]
    snapshot(site, 91)
    answer(site, "failed", reason=reasons[0], fs="")
    await look(server)
    first = alerts(site)[-1]
    assert first.startswith(f"⚠ **alice**'s disk didn't grow (try 1 of {TRIES}): `{reasons[0]}`; ")
    assert first.endswith("The next try comes after 08:30 UTC, if it is still above 85%.")
    await look(server)  # the same read: no try
    site.clock.now += RETRY - 60
    snapshot(site, 91)
    await look(server)  # a new read, but before RETRY
    assert len(calls(site)) == 1
    site.clock.now += 60
    snapshot(site, 91)
    answer(site, "failed", reason=reasons[1], fs="", to=80)
    await look(server)
    assert f"(try 2 of {TRIES}): `{reasons[1]}`" in alerts(site)[-1]
    site.clock.now += RETRY
    snapshot(site, 92)
    answer(site, "failed", reason="its file system was not grown (exit 6)", fs="not grown")
    await look(server)
    last = alerts(site)[-1]
    assert "(try 3 of 3)" in last and "Its volume is 120 GB now; its file system not yet." in last
    assert last.endswith("No more automatic tries: `!grow-disk alice 120GB` (ORGANIZERS §4.1).")
    for _ in range(3):
        site.clock.now += RETRY
        snapshot(site, 95)
        await look(server)
    assert len(calls(site)) == 3 and len(alerts(site)) == 3
    assert [r["failures"] for r in records(site)] == [1, 2, 3]
    snapshot(site, 50)
    await look(server)
    site.clock.now += 60
    snapshot(site, 90)
    answer(site, "grown")
    await look(server)
    assert len(calls(site)) == 4 and alerts(site)[-1].startswith("✅ **alice**'s disk grew")


async def test_a_row_the_bot_cant_build_for_is_a_failure(site, server):
    """`!grow-disk`'s own checks, as at plan time (`actions.auto_grow`): no script runs."""
    snapshot(site, 90, slug="erin")  # zab-test's row, in harvard-2026's snapshot
    await look(server)
    (line,) = alerts(site)
    assert line.startswith("⚠ **erin**'s disk didn't grow (try 1 of 3): ") and calls(site) == []
    assert "not in this workshop's roster" in line


async def test_a_try_with_no_answer_of_its_own_is_a_failure(site, server):
    """A crash (no line), or one cut short by the runner: a failed try, said, never a loop."""
    snapshot(site, 90)
    queue(site, [1, ["Traceback (most recent call last):", "RuntimeError: boom"]])
    await look(server)
    (line,) = alerts(site)
    assert "(try 1 of 3): `aws/grow_disk.py exited 1`; its root file system was 90% used" in line
    (site.tmp / "answers.json").write_text("[]")  # the next one crashes in the fake itself
    site.clock.now += RETRY
    snapshot(site, 90)
    await look(server)
    assert "(try 2 of 3): `aws/grow_disk.py exited 1`" in alerts(site)[-1]


async def test_a_vm_stopped_at_the_try_is_neither_grown_nor_said(site, server):
    snapshot(site, 99)
    answer(site, "stopped", **{"from": None, "to": None})
    await look(server)
    assert alerts(site) == [] and [r["outcome"] for r in records(site)] == ["stopped"]
    await look(server)
    assert len(calls(site)) == 1
    site.clock.now += RETRY
    snapshot(site, 99)
    answer(site, "grown")
    await look(server)
    assert len(calls(site)) == 2


def harvard_bot(server):
    return next(b for b in server.bots if b.user_id == HARVARD)


async def test_a_line_zulip_never_took_is_posted_later_and_the_resize_never_runs_again(
    site, server, monkeypatch
):
    """Codex round 5 on #1003: the cap's line, which says that an organizer must act, lost to
    Zulip (no connection, or a 4xx: `AdminBot.deliver` says it never landed). It is kept in its
    try's record and posted again at each look until it lands, once, then an `alerted` record;
    the script never runs again for it. A restart keeps what is still owed, and nothing more."""
    bot = harvard_bot(server)
    deliver, lost = bot.deliver, []

    async def unreachable(**params):
        lost.append(params["content"])
        return None, True

    monkeypatch.setattr(bot, "deliver", unreachable)
    snapshot(site, 97)
    answer(site, "capped", **{"from": 400, "to": 400})
    await look(server)
    (tried,) = records(site)
    assert tried["unsent"] is True and tried["alert"] is None and tried["text"] == lost[0]
    assert "the cap of automatic growth" in lost[0]
    site.clock.now += 60
    await look(server)  # still unreachable: tried again, in vain, and kept
    assert lost == [lost[0]] * 2 and alerts(site) == [] and len(records(site)) == 1
    monkeypatch.setattr(bot, "deliver", deliver)
    for _ in range(2):  # Zulip is back: posted once
        site.clock.now += 60
        snapshot(site, 97)
        await look(server)
    assert alerts(site) == [lost[0]] and len(calls(site)) == 1
    said = records(site)[-1]
    assert (said["outcome"], said["of"], said["slug"]) == ("alerted", tried["seq"], "alice")
    assert isinstance(said["alert"], int) and server.grower.unsent == {}
    await server.close()
    restarted = await start(site)  # posted and recorded: nothing owed after a restart
    try:
        await look(restarted)
        assert alerts(site) == [lost[0]] and restarted.grower.unsent == {}
    finally:
        await restarted.close()


async def test_a_line_owed_at_a_restart_is_posted_after_it(site, server, monkeypatch):
    """A line Zulip never took, then the bots' restart before Zulip is back: the restarted bots
    post it from its record at their first look, without the script."""
    monkeypatch.setattr(harvard_bot(server), "deliver", lambda **params: _lost())
    snapshot(site, 99)
    answer(site, "capped", **{"from": 400, "to": 400})
    await look(server)
    (tried,) = records(site)
    assert tried["unsent"] is True and alerts(site) == []
    await server.close()
    restarted = await start(site)
    try:
        assert set(restarted.grower.unsent) == {tried["seq"]}
        await look(restarted)
        assert alerts(site) == [tried["text"]] and len(calls(site)) == 1
        assert records(site)[-1]["outcome"] == "alerted" and restarted.grower.unsent == {}
    finally:
        await restarted.close()


async def _lost():
    return None, True


async def test_a_line_that_may_have_landed_is_never_posted_again(site, server, monkeypatch):
    """A 5xx, or an answer lost after the request went out (`AdminBot.deliver`): it may have
    landed, so it isn't kept, and no later look posts it."""

    async def unsure(**params):
        return None, False

    monkeypatch.setattr(harvard_bot(server), "deliver", unsure)
    snapshot(site, 97)
    answer(site, "capped", **{"from": 400, "to": 400})
    await look(server)
    monkeypatch.undo()
    site.clock.now += 60
    await look(server)
    (tried,) = records(site)
    assert "unsent" not in tried and alerts(site) == [] and server.grower.unsent == {}


def owe(server, *seqs):
    """Lines owed in harvard-2026, by their tries' seqs, as `observe` or a lost post leaves them."""
    for seq in seqs:
        line = autogrow.Unsent("harvard-2026", "alice", "i-1", f"⚠ **alice**: line {seq}")
        server.grower.unsent[seq] = line


async def test_owed_lines_go_oldest_first_and_stop_at_the_first_zulip_refuses(
    site, server, monkeypatch
):
    """The review of #1003 (N1): two owed lines, Zulip still refusing: the oldest alone is
    tried, and both stay owed; once Zulip takes them, both are posted, oldest first, each an
    `alerted` record naming its try's."""
    bot, tried = harvard_bot(server), []
    deliver = bot.deliver

    async def refusing(**params):
        tried.append(params["content"])
        return None, True

    owe(server, 42, 7)
    monkeypatch.setattr(bot, "deliver", refusing)
    await server.grower.resend(bot, "harvard-2026", ALERTS)
    assert tried == ["⚠ **alice**: line 7"] and set(server.grower.unsent) == {7, 42}
    monkeypatch.setattr(bot, "deliver", deliver)
    await server.grower.resend(bot, "harvard-2026", ALERTS)
    assert alerts(site) == ["⚠ **alice**: line 7", "⚠ **alice**: line 42"]
    assert [(r["outcome"], r["of"]) for r in records(site)] == [("alerted", 7), ("alerted", 42)]
    assert server.grower.unsent == {}


async def test_an_owed_line_that_may_have_landed_is_not_posted_again(site, server, monkeypatch):
    """The review of #1003 (N1): a re-post that may have landed (a 5xx: `AdminBot.deliver`) is
    an `alerted` record with no message ID, and no later look posts it again."""
    bot, tried = harvard_bot(server), []

    async def unsure(**params):
        tried.append(params["content"])
        return None, False

    owe(server, 7)
    monkeypatch.setattr(bot, "deliver", unsure)
    for _ in range(2):
        await server.grower.resend(bot, "harvard-2026", ALERTS)
    assert tried == ["⚠ **alice**: line 7"] and server.grower.unsent == {}
    (said,) = records(site)
    assert (said["outcome"], said["of"], said["alert"]) == ("alerted", 7, None)


async def test_a_restart_keeps_ebs_six_hours_and_the_failures(site, server):
    """The `disk_grow` records are the memory: after a restart, no growth within EBS's 6 hours,
    and the failures in a row still count."""
    snapshot(site, 99)
    answer(site, "grown", used=90)
    await look(server)
    await server.close()
    site.clock.now += 3600
    restarted = await start(site)
    try:
        snapshot(site, 99)
        await look(restarted)
        assert len(calls(site)) == 1
    finally:
        await restarted.close()
    grower = autogrow.Grower(restarted)
    for n in range(TRIES):
        record = {"event": "disk_grow", "instance": "i-2", "outcome": "failed",
                  "ts": iso(site.clock.now + n)}  # fmt: skip
        grower.observe(record)
    assert grower.vms["i-2"].held and grower.vms["i-2"].failures == TRIES


def test_the_snapshot_a_vm_cant_read_crashes_nothing():
    grower = autogrow.Grower(object())  # type: ignore[arg-type]
    now = 1_800_000_000.0
    for entry in (None, [], {"kind": "participant"}, {"kind": "participant", "state": "running"}):
        assert grower.due(entry, now) is None
    odd = {"kind": "participant", "state": "running", "slug": "a", "instance": "i",
           "status": {"bridge": {"x": 1}, "disk_percent": 99, "at": now,
                      "instance": "i"}}  # fmt: skip
    assert grower.due(odd, now) == ("a", "i", 99.0, now)  # an odd bridge is no read found down
    assert autogrow.result("auto result: {not json") is None
    assert autogrow.result('auto result: {"outcome": ["grown"]}') is None
    shown = autogrow.result('x\nauto result: {"outcome": "grown", "from": 10000, "used": 101}')
    assert shown is not None and (shown["from"], shown["used"]) == (None, None)


async def test_a_plan_applying_holds_it_back(site, server):
    """A plan of the workshop applying, or approved and handed over: no try until it ends."""
    snapshot(site, 99)
    answer(site, "grown")
    server.planner.handed["harvard-2026"] = 7
    await look(server)
    assert calls(site) == []
    del server.planner.handed["harvard-2026"]
    await look(server)
    assert len(calls(site)) == 1


@pytest.mark.parametrize("state", ["approved", "applying"])
async def test_a_plan_of_every_workshop_holds_it_back_until_its_end(site, server, state):
    """A server plan holds every workshop's lock (§2.4 step 5): a rollout or trust fix of every
    workshop (#995), whose parts start and stop VMs and restart bridges, or `!upgrade-server`.
    Approved (handed over) or applying (as `state.db` has it): no try in any workshop, and
    none counted, until it ends; then the next look grows."""
    snapshot(site, 99)
    answer(site, "grown")
    store, planner = server.store, server.planner
    plan = store.begin(workshop="", bot=GLOBAL, requester=OLGA,
                       command="!rollout v0.2.2 every-workshop")  # fmt: skip
    if state == "approved":
        planner.handed[""] = plan.id
    else:
        plan = store.move(plan.id, (PLANNING,), APPLYING, "")
    assert planner.busy("harvard-2026") == plan.id
    await look(server)
    assert calls(site) == [] and records(site) == [] and alerts(site) == []
    assert planner.growing("harvard-2026") is None
    if state == "approved":
        del planner.handed[""]
    else:
        store.move(plan.id, (APPLYING,), DONE, "")
    await look(server)
    assert len(calls(site)) == 1 and records(site)[-1]["outcome"] == "grown"


def test_a_reason_is_quoted_as_code():
    """A failure's reason, which may carry a VM's words, can't mention anyone or format."""
    r = dict(autogrow.FAILED, reason="@**all** `x`")
    line = autogrow.alert("alice", 90, 1_800_000_000.0, r, autogrow.Vm(failures=1))
    assert "(try 1 of 3): `` @**all** `x` ``; its root file system" in line


async def test_a_restart_keeps_the_failures_as_recorded_and_their_clearing(site, server):
    """Codex round 1 on #1003: two failures, a read below the threshold, then one failure are
    one failure in a row, after a restart as before it; three, then a read below, are none."""
    for used in (91, 91, 50, 91):
        snapshot(site, used)
        if used > THRESHOLD:
            answer(site, "failed", reason="refused", fs="")
        await look(server)
        site.clock.now += RETRY
    vm = server.grower.vms["i-1"]
    assert (vm.failures, vm.held) == (1, False)
    assert [r["outcome"] for r in records(site)] == ["failed", "failed", "cleared", "failed"]
    await server.close()
    restarted = await start(site)
    try:
        vm = restarted.grower.vms["i-1"]
        assert (vm.failures, vm.held) == (1, False)
        for used in (91, 91, 40):  # two more: held; then a read below clears it
            snapshot(site, used)
            if used > THRESHOLD:
                answer(site, "failed", reason="refused", fs="")
            await look(restarted)
            site.clock.now += RETRY
        assert (vm.failures, vm.held) == (0, False) and len(calls(site)) == 5
    finally:
        await restarted.close()
    again = await start(site)
    try:
        assert (again.grower.vms["i-1"].failures, again.grower.vms["i-1"].held) == (0, False)
    finally:
        await again.close()


BLOCKING = """\
import json, pathlib, sys, time
with open({calls!r}, "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
while not pathlib.Path({go!r}).exists():
    time.sleep(0.02)
print({line!r})
"""
STOP = 'import sys\nprint("bob: would stop i-2" if "--dry-run" in sys.argv else "bob: stopped")\n'
STATUS = 'print(\'{"schema": 1, "vms": []}\')\n'


def plan_fakes(site):
    """`!stop`'s and `!protect`'s scripts, for plans requested and approved beside a growth."""
    for name, body in (("aws/power.py", STOP), ("admin/status.py", STATUS),
                       ("aws/protect.py", 'print("bob: protected")\n')):  # fmt: skip
        path = Path(site.paths.layout.release) / "workshop" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)


async def settled(server):
    while server.planner.tasks:
        await asyncio.gather(*list(server.planner.tasks), return_exceptions=True)


async def test_a_growth_holds_its_workshop_against_an_approval(site, server):
    """Codex round 1 on #1003: while the script runs, an approval in the workshop (or of a
    server plan) is answered `busy:` and not recorded, and the month-end waits; after it, the
    approval goes through. The review of #1003 (N1): a `!stop` or `!start` of another VM goes
    through meanwhile, and applies beside the growth; one of the VM growing waits for it."""
    plan_fakes(site)
    go = site.tmp / "go"
    line = AUTO + json.dumps({"outcome": "grown", "instance": "i-1", "from": 80, "to": 120,
                              "fs": "grown", "used": 58, "again": None, "reason": ""})  # fmt: skip
    script = Path(site.paths.layout.release) / "workshop" / "aws" / "grow_disk.py"
    script.write_text(BLOCKING.format(calls=str(site.tmp / "calls.jsonl"), go=str(go), line=line))
    bot = next(b for b in server.bots if b.user_id == HARVARD)
    plans = {}
    for mid, text in enumerate(("!protect on bob", "!stop alice", "!stop bob"), start=6001):
        await bot.on_message(incoming(OLGA, text, stream=HARVARD_CH, mid=mid))
        await settled(server)
        ids = [row[0] for row in server.store.db.execute("SELECT id FROM plans")]
        plans[text] = server.store.get(max(ids))
        assert plans[text].state == PENDING, plans[text].note
    snapshot(site, 90)
    await server.grower.check()
    for _ in range(1000):  # until the script runs
        if calls(site):
            break
        await asyncio.sleep(0.01)
    planner = server.planner
    assert planner.growing("harvard-2026") == "alice" and planner.growing("") == "alice"
    assert planner.growing("zab-test") is None

    async def approve(text, mid):
        plan = plans[text]
        command = f"!approve {plan.name} {plan.hash[:6]}"
        await bot.on_message(incoming(OLGA, command, stream=HARVARD_CH, mid=mid))
        await settled(server)
        said = [p["content"] for p in site.realm.posts(HARVARD) if p.get("topic") != TOPIC]
        return said, server.store.get(plan.id).state

    for mid, text in ((6101, "!protect on bob"), (6102, "!stop alice")):  # held back
        said, state = await approve(text, mid)
        assert said[-1] == growth_busy("alice", plans[text].name) and state == PENDING
    said, state = await approve("!stop bob", 6103)  # another VM's stop: beside the growth
    assert state == DONE and planner.growing("harvard-2026") == "alice"
    go.write_text("")
    await asyncio.gather(*list(server.grower.tasks))
    assert planner.growing("harvard-2026") is None and len(alerts(site)) == 1
    for mid, text in ((6104, "!protect on bob"), (6105, "!stop alice")):
        assert (await approve(text, mid))[1] == DONE


async def test_a_plan_approved_while_a_try_waits_for_its_slot_holds_it_back(site, server):
    """Codex round 1 on #1003: a try scheduled while the workshop was free, which gets its slot
    only once a plan is approved there, doesn't run, and isn't counted; the next look grows."""
    snapshot(site, 99)
    answer(site, "grown")
    await server.grower.check()  # its task, not run yet
    server.planner.handed["harvard-2026"] = 7  # an approval, before the task's first step
    await asyncio.gather(*list(server.grower.tasks))
    assert calls(site) == [] and records(site) == [] and alerts(site) == []
    assert server.planner.growing("harvard-2026") is None
    del server.planner.handed["harvard-2026"]
    await look(server)
    assert len(calls(site)) == 1 and records(site)[-1]["outcome"] == "grown"


@pytest.mark.parametrize("change", ["below", "stale", "higher"])
async def test_a_try_that_waited_for_its_slot_reads_the_disk_again(site, server, change):
    """Codex round 2 on #1003: a try queued behind the server's other growths runs only on a
    fresh read still at the threshold or above, which its line then quotes. One the participant
    freed meanwhile, or one gone stale, grows nothing, says nothing and counts nothing: the next
    look decides."""
    slots = server.grower.slots
    for _ in range(autogrow.PARALLEL):  # four growths of other VMs run
        await slots.acquire()
    snapshot(site, 95)
    answer(site, "grown")
    await server.grower.check()  # queued for a slot
    if change == "below":
        snapshot(site, THRESHOLD - 25)  # freed while it waited
    elif change == "stale":
        site.clock.now += autogrow.FRESH + 1  # no read since: the collector down
    else:
        snapshot(site, 97, read=site.clock.now - 10)
    for _ in range(autogrow.PARALLEL):
        slots.release()
    await asyncio.gather(*list(server.grower.tasks))
    if change == "higher":
        assert len(calls(site)) == 1 and "was 97% used at 07:59 UTC" in alerts(site)[0]
        return
    assert calls(site) == [] and records(site) == [] and alerts(site) == []
    assert server.grower.vms["i-1"].failures == 0 and server.planner.growing("harvard-2026") is None
    snapshot(site, 95)  # a fresh read above it again: the next look grows
    await look(server)
    assert len(calls(site)) == 1 and records(site)[-1]["outcome"] == "grown"


async def test_a_row_whose_vm_was_replaced_grows_on_its_new_vms_reading(site, server):
    """Codex round 3 on #1003: the script gets the instance read; one replaced since (a spare
    handed over, the snapshot not read again yet) grows nothing, says nothing and counts
    nothing; the new VM's own reading grows it."""
    snapshot(site, 95)
    answer(site, "replaced", **{"from": None, "to": None}, reason="alice's VM is i-2 now")
    await look(server)
    assert alerts(site) == [] and [r["outcome"] for r in records(site)] == ["replaced"]
    assert server.grower.vms["i-1"].failures == 0
    snapshot(site, 96, instance="i-2")  # the collector's next read: the new VM's
    answer(site, "grown", instance="i-2")
    await look(server)
    argv = calls(site)[-1]
    assert argv[-2:] == ["--expect-instance", "i-2"] and len(alerts(site)) == 1


@pytest.mark.parametrize("read_from", ["i-0old", None])
async def test_a_read_of_another_vm_or_of_none_grows_nothing(site, server, read_from):
    """Codex round 4 on #1003: the snapshot pairs the row's VM now with values read from another
    instance (its VM before a hand-over, the next sweep not come yet), or naming none (a read
    before #998): no try. Its own read grows it."""
    snapshot(site, 99, read_from=read_from)
    answer(site, "grown")
    await look(server)
    assert calls(site) == [] and records(site) == [] and alerts(site) == []
    snapshot(site, 99)  # the next sweep: its own read
    await look(server)
    assert len(calls(site)) == 1 and calls(site)[0][-2:] == ["--expect-instance", "i-1"]


def test_only_a_stop_or_start_of_another_vm_goes_beside_a_growth():
    """The review of #1003 (N1): what a growth of alice's disk holds back, plan by plan."""
    from types import SimpleNamespace

    planner = Planning(Runner(None))  # type: ignore[arg-type]
    planner.growths = {"harvard-2026": {"alice"}}

    def plan(command, *targets, workshop="harvard-2026"):
        return SimpleNamespace(command=command, targets=targets, workshop=workshop)

    for held, command, targets in (
        (None, "!stop bob carol", ("bob", "carol")),
        (None, "!start bob", ("bob",)),
        ("alice", "!stop alice bob", ("alice", "bob")),
        ("alice", "!start all", ("alice", "bob", "carol")),
        ("alice", "!protect on bob", ("bob",)),
        ("alice", "!idle-stop 60 bob", ("bob",)),
    ):
        assert planner.growing("harvard-2026", plan(command, *targets)) == held, command
    assert planner.growing("", plan("!upgrade-server v0.2.20", workshop="")) == "alice"
    assert planner.growing("zab-test", plan("!protect on erin", "erin")) is None
