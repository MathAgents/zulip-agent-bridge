"""A26, a filling disk grows by itself, online (#998; docs/ADMIN.md §3.4, §7): the bots'
automatic growth on the rig, through `Workshop admin · e2e`, whose admin.toml entry has an
`alerts_channel` (`#zab-dev-alerts`), on `zab-test-b`, launched by A4 from the rig's image
(`ZAB_ADMIN_E2E_AMI`: one built with #1004 has an 80 GiB root) and rolled out by A6.

- **A turn's stand-in.** The rig's VMs have no agent signed in (their rows read `sign-in
  pending`: no agent's credentials are put on the rig), so no agent's turn can run on them.
  What a growth must not interrupt stands in for it: a process on the VM that writes a line
  to its root file system every 2 s, from before the fill to after the growth,
  which must run to its end with no gap (`TURN`, one process throughout); the bridge itself,
  whose `NRestarts` and `MainPID` must not change; the VM, whose boot ID must not change; and
  the participant's bot, `Codex · zab-test-b`, which must answer `zab-tester2`'s `!status` DM
  after the growth.
- **The fill:** over the fleet's SSH from the server, as A6 reaches a VM, `fallocate` in
  `ubuntu`'s home until `df` says about 90% used, past the threshold (`autogrow.THRESHOLD`).
  Then `!status zab-test-b`, which feeds the collector's snapshot at its next pass, rather than
  waiting for its 10-minute sweep.
- **The growth,** within `GROW_S` of that read: in `#zab-dev-alerts`, topic `disk`, from the
  workshop's bot, `✅ **zab-test-b**'s disk grew from <size> to <grown> GB, online: …, and is
  <n>% now. About $… a month more.`, where `<size>` is the volume as launched (EC2's) and
  `<grown>` half of it again, rounded up, at most `autogrow.CAP_GB` (`grow_disk.py --auto-cap`'s
  step); EC2 shows the volume at `<grown>` GB, its modification `optimizing` or `completed`;
  `df` on the VM is larger by about the GB added and below the threshold; the audit log has one
  `disk_grow` record for it, `outcome: grown`, with both sizes and the alert's message ID; and
  nothing restarted (above). The time from the read to the line is in the report.
- **Then a second fill** past the threshold of the new size, and a `!status`: once the snapshot
  shows that read, two of the bots' looks later, no second try (no `disk_grow` record) and no
  new line: EBS allows the volume's next change only 6 hours after this one, and the bots wait
  for it. `!grow-disk zab-test-b <grown + 2>GB` is refused for the same 6 hours.
- **Cleanup,** whatever happens: the fill files and the stand-in's files removed, and the
  stand-in ended. The grown volume stays: A9's teardown, or the run's, deletes it with the VM.

**EBS's 6 hours and A21.** A21 grows both of A4's volumes with `!grow-disk`, and A26 needs a
volume no modification has touched for 6 hours. A26 runs after A21 (by file name), so that
A21's checks never meet a volume A26 grew; in a run with A21, or within 6 hours of an earlier
run's growth of `zab-test-b`, it skips, naming the time. Run it apart from A21: `--scenarios
go,A26`. A6's fill of `zab-test-a`, at 93%, would set the growth off too: A6 holds the
collector for its fill (`test_scenarios_2_bot.collector_held`), so no snapshot shows it.

At most three instances run (§7): the server, `zab-test-b`, and `zab-test-a` beside it.
"""

from __future__ import annotations

import datetime as dt
import itertools
import json
import math
import re
import time
from typing import Any

import pytest

from e2e.admin.conftest import DONE, requires
from e2e.admin.harness.chat import PLAN_S, STATUS_ANSWER, Refused
from e2e.admin.harness.rig import E2E
from e2e.admin.harness.settle import until
from e2e.admin.test_scenarios_2_bot import (
    df_of,
    ip_of,
    last_seq,
    newest_in,
    on_vm,
    records,
    refusal,
    talk,
    vm_states,
)
from e2e.admin.test_scenarios_3_late_joiner import room_for
from e2e.admin.test_scenarios_4_grow_disk import SIX_HOURS, sized, volume_of
from e2e.harness.flows import DM_STATUS_RE
from e2e.harness.zulip_user import TestUser, dm_narrow, topic_narrow
from zab.admin.autogrow import CAP_GB, PERIOD, THRESHOLD, TOPIC
from zab.admin.costs import GP3_GB_MONTH

A, B = "zab-test-a", "zab-test-b"
#: What a fill leaves available, of `df`'s Used + Available: `df` then says about 90%, past the
#: threshold with room for the stand-in's lines and the reads.
LEFT = 0.10
FILLS = ("/home/ubuntu/zab-e2e-a26-fill-1", "/home/ubuntu/zab-e2e-a26-fill-2")
#: The turn's stand-in (module doc): its log and the file that ends it, in `ubuntu`'s home.
TURN_LOG, TURN_STOP = "/home/ubuntu/zab-e2e-a26-turn.log", "/home/ubuntu/zab-e2e-a26-turn.stop"
#: The stand-in's own end, if nothing stops it sooner (a part cut short).
TURN_MAX_S = 2400
#: The longest pause between two of its lines that still counts as running on: 2 s apart.
MAX_GAP_S = 20
#: `$1` its log, `$2` its stop file, `$3` its end in seconds: one process, `start <pid>`, then a
#: time stamp every 2 s, appended to the root file system, then `done <pid>`. No input or output
#: of its own, so that the SSH session that starts it (`setsid -f`) ends at once.
TURN = r"""exec </dev/null >/dev/null 2>&1
end=$(( $(date +%s) + $3 ))
echo "start $$" > "$1"
while [ ! -e "$2" ] && [ "$(date +%s)" -lt "$end" ]; do
  date +%s >> "$1"
  sleep 2
done
echo "done $$" >> "$1"
"""
#: The VM's boot ID and uptime, then the bridge's unit as `agent`'s user manager has it.
MARKS = r"""set -eu
uid=$(id -u agent)
cat /proc/sys/kernel/random/boot_id
cut -d' ' -f1 /proc/uptime
exec sudo -u agent -H env -C /home/agent "XDG_RUNTIME_DIR=/run/user/$uid" \
  "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$uid/bus" \
  systemctl --user show zab.service --property=MainPID --property=NRestarts \
  --property=ActiveState
"""
#: How long the bridge may take to be active after a start (`room_for` may start the VM).
BRIDGE_S = 300
#: From the fill's `!status` answer to the growth's line: the collector's next pass and the
#: bots' next look (`PERIOD` each), then the script within `!grow-disk`'s 15 minutes.
GROW_S = 2 * PERIOD + 15 * 60 + 60
#: How long the snapshot may take to show a `!status` read: the collector's next pass, and some.
SNAPSHOT_S = 180
SNAPSHOT = "/var/lib/zab-admin/console/snapshot.json"
#: EBS's limit, as the bots and `grow_disk.py` keep it.
EBS_EVERY = dt.timedelta(hours=6)
GREW = re.compile(
    rf"^✅ \*\*{re.escape(B)}\*\*'s disk grew from (\d+) to (\d+) GB, online: its root file "
    r"system was ([0-9.]+)% used at \d\d:\d\d UTC, and is (\d+)% now\. About \$([0-9,]+\.\d\d) "
    r"a month more\.$"
)


def grown_size(size: int) -> int:
    """`grow_disk.py --auto-cap`'s step from a volume of `size` GB: half again, rounded up."""
    return min(CAP_GB, math.ceil(size * 3 / 2))


def last_change(aws: Any, volume_id: str) -> dt.datetime | None:
    """When the volume's last modification started, as EC2 keeps it; None for one never
    modified (by a filter: `VolumeIds` makes that an error, as `aws_common` notes)."""
    found = aws.aws.ec2("describe_volumes_modifications",
                        Filters=[{"Name": "volume-id", "Values": [volume_id]}])  # fmt: skip
    return max((m["StartTime"] for m in found["VolumesModifications"] if m.get("StartTime")),
               default=None)  # fmt: skip


def modification(aws: Any, volume_id: str) -> dict[str, Any]:
    found = aws.aws.ec2("describe_volumes_modifications",
                        Filters=[{"Name": "volume-id", "Values": [volume_id]}])  # fmt: skip
    return max(found["VolumesModifications"], key=lambda m: m["StartTime"])


def marks(server: Any, ip: str) -> dict[str, str]:
    """`MARKS` on the VM: `boot_id`, `uptime` (seconds), and the bridge's `MainPID`,
    `NRestarts` and `ActiveState`; {} when it doesn't answer (a VM just started)."""
    done = on_vm(server, ip, "bash", "-c", MARKS, "bash")
    lines = done.stdout.strip().splitlines() if done.ok else []
    if len(lines) < 2:
        return {}
    unit = dict(line.split("=", 1) for line in lines[2:] if "=" in line)
    return {"boot_id": lines[0].strip(), "uptime": lines[1].strip(), **unit}


def vm_now(server: Any, ip: str) -> int:
    done = on_vm(server, ip, "date", "+%s")
    assert done.ok, f"date on {ip}: exit {done.returncode}"
    return int(done.stdout.strip())


def fill(server: Any, ip: str, path: str) -> tuple[int, int, int, int]:
    """`path` allocated until `df` leaves `LEFT` of Used + Available: `df_of` after it."""
    _, used, available, _ = df_of(server, ip)
    kib = max(0, available - int((used + available) * LEFT))
    made = on_vm(server, ip, "fallocate", "-l", f"{kib}KiB", path)
    assert made.ok, f"fallocate: exit {made.returncode}: {made.stderr.strip()[-300:]}"
    return df_of(server, ip)


def turn_log(server: Any, ip: str) -> list[str]:
    done = on_vm(server, ip, "cat", "--", TURN_LOG)
    return done.stdout.splitlines() if done.ok else []


def end_turn(server: Any, ip: str) -> None:
    """The stand-in ended, whatever became of the part: by its PID, from its log's first line,
    unless its last says it is done. Then its files go."""
    lines = turn_log(server, ip)
    if lines and lines[0].startswith("start ") and not lines[-1].startswith("done "):
        pid = lines[0].removeprefix("start ").strip()
        if pid.isdigit():
            on_vm(server, ip, "kill", "--", pid)
    on_vm(server, ip, "rm", "-f", "--", TURN_LOG, TURN_STOP)


def snapshot_read(server: Any, instance: str) -> dict[str, Any]:
    """`zab-test-b`'s status in the console's snapshot, which the bots' growth reads: {} when
    it has none, or one of another instance."""
    data = json.loads(server.read(SNAPSHOT) or "{}")
    vms = data.get("workshops", {}).get(E2E.name, {}).get("vms", [])
    entry = next((vm for vm in vms if vm.get("slug") == B), {})
    status = entry.get("status") or {}
    return status if status.get("instance") == instance else {}


async def disk_lines(user: TestUser, channel: int, bot: int, after: int) -> list[dict[str, Any]]:
    """The workshop bot's posts about `zab-test-b` in its alerts channel, topic `disk`, after
    `after`."""
    found = await user.after(topic_narrow(channel, TOPIC), after)
    return [m for m in found if m["sender_id"] == bot and f"**{B}**" in m["content"]]


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A26")
@requires("launched", "provisioned", "rolled_out")
async def test_a26_a_filling_disk_grows_by_itself_online(
    scenario, tester, tester2, rigged, server, aws, nonce
):
    t = talk(tester, rigged, E2E.name, f"e2e-A26-{nonce}")
    await room_for(t, aws, running=(B,), beside=(A,))
    volume = volume_of(aws, B)
    vid, size = volume["VolumeId"], int(volume["Size"])
    changed = last_change(aws, vid)
    now = dt.datetime.now(dt.UTC)
    if changed is not None and now - changed < EBS_EVERY:
        allowed = f"{changed + EBS_EVERY:%H:%M} UTC"
        pytest.skip(f"{B}'s volume was modified at {changed:%H:%M} UTC, by A21 in this run or "
                    f"an earlier run: EBS allows its next growth from {allowed}. Run A26 "
                    "apart from A21 (`--scenarios go,A26`)")  # fmt: skip
    grown = grown_size(size)
    instance = vm_states(aws)[B]["InstanceId"]
    ip = ip_of(server, B)
    ids = rigged.ids()
    alerts, bot = ids.channels[E2E.alerts], ids.bots[E2E.bot][0]
    codex = DONE["provisioned"][f"Codex · {B}"]
    tester2.restrict(channels=tester2.channels, bots=[*tester2.bots, codex["user_id"]])
    scenario.notes.append(f"{B}: launched with a {size} GB root ({instance}, {vid})")
    before = await until(lambda: marks(server, ip), lambda m: m.get("ActiveState") == "active",
                         what=f"{B}'s bridge active", within=BRIDGE_S, every=10)  # fmt: skip
    try:
        on_vm(server, ip, "rm", "-f", "--", TURN_LOG, TURN_STOP, *FILLS)
        started = on_vm(server, ip, "setsid", "-f", "bash", "-c", TURN, "bash", TURN_LOG,
                        TURN_STOP, str(TURN_MAX_S))  # fmt: skip
        assert started.ok, f"the stand-in didn't start: {started.stderr.strip()[-300:]}"
        await until(lambda: turn_log(server, ip), lambda lines: len(lines) >= 2,
                    what="the stand-in's first lines", within=60, every=3)  # fmt: skip
        seq, last = last_seq(server), await newest_in(tester, alerts)
        df_before = df_of(server, ip)
        filled = fill(server, ip, FILLS[0])
        filled_at = vm_now(server, ip)
        assert filled[3] >= THRESHOLD + 2, f"{B}'s disk at {filled[3]}% after the fill"
        await t.ask(f"!status {B}", STATUS_ANSWER, within=PLAN_S)  # feeds the snapshot
        asked = time.monotonic()

        async def first_line() -> dict[str, Any] | None:
            found = await disk_lines(tester, alerts, bot, last)
            return found[0] if found else None

        line = await tester.wait_for(first_line, within=GROW_S, what=f"{B}'s line in disk")
        scenario.timing("the fill's !status to the growth's line", time.monotonic() - asked)
        grew = GREW.match(line["content"])
        assert grew, f"not the online growth's line: {line['content'][:500]}"
        old, new, was, now_pct = int(grew[1]), int(grew[2]), float(grew[3]), int(grew[4])
        assert (old, new) == (size, grown), f"{old} → {new} GB, not {size} → {grown}"
        assert was >= THRESHOLD and now_pct < THRESHOLD, (was, now_pct)
        assert grew[5] == f"{(grown - size) * GP3_GB_MONTH:,.2f}", grew[0]
        grown_at = vm_now(server, ip)
        await sized(aws, B, grown)
        mod = modification(aws, vid)
        assert mod["ModificationState"] in ("optimizing", "completed"), mod
        assert mod["TargetSize"] == grown, mod
        df_after = df_of(server, ip)
        shown = int((grown - size) * 2**20 * 0.9)  # ext4 keeps a few percent for its metadata
        assert df_after[0] - filled[0] >= shown, (filled, df_after)
        assert df_after[3] < THRESHOLD, f"{B}'s disk at {df_after[3]}% after the growth"
        kept = [r for r in records(server, seq) if r["event"] == "disk_grow"
                and r.get("slug") == B]  # fmt: skip
        assert len(kept) == 1, f"disk_grow records of {B}: {kept}"
        (record,) = kept
        assert record["outcome"] == "grown" and record["instance"] == instance, record
        sizes = (record["from_gb"], record["to_gb"], record["fs"])
        assert sizes == (size, grown, "grown"), record
        assert record["alert"] == line["id"], record
        after = marks(server, ip)
        for key in ("boot_id", "MainPID", "NRestarts", "ActiveState"):  # no restart, no reboot
            was_key, now_key = before.get(key), after.get(key)
            assert now_key == was_key, f"{B}'s {key}: {was_key} → {now_key}"
        assert float(after["uptime"]) > float(before["uptime"]), (before, after)
        dm = await tester2.send_dm(codex["user_id"], "!status")
        await tester2.wait_reply(dm_narrow(codex["user_id"]), after_id=dm,
                                 senders=[codex["user_id"]], pattern=DM_STATUS_RE,
                                 within=180)  # fmt: skip
        on_vm(server, ip, "touch", "--", TURN_STOP)
        lines = await until(lambda: turn_log(server, ip),
                            lambda found: bool(found) and found[-1].startswith("done "),
                            what="the stand-in's end", within=60, every=3)  # fmt: skip
        pid = lines[0].removeprefix("start ")
        assert lines[-1] == f"done {pid}", f"not one process throughout: {lines[0]}, {lines[-1]}"
        stamps = [int(x) for x in lines[1:-1]]
        gaps = [b - a for a, b in itertools.pairwise(stamps)]
        assert stamps[0] <= filled_at and stamps[-1] >= grown_at, (stamps[0], stamps[-1])
        assert max(gaps, default=0) <= MAX_GAP_S, f"the stand-in paused {max(gaps)} s"
        scenario.notes.append(
            f"{B}: {size} → {grown} GB by itself, online, read at {was:g}%, {now_pct}% after; "
            f"df {df_before[0]} → {df_after[0]} KiB; no restart (NRestarts "
            f"{after['NRestarts']}, boot ID unchanged); the stand-in wrote {len(stamps)} lines, "
            f"at most {max(gaps, default=0)} s apart"
        )

        # A second fill, at once: EBS's 6 hours, so no second try, and no line
        refilled = fill(server, ip, FILLS[1])
        assert refilled[3] >= THRESHOLD + 2, f"{B}'s disk at {refilled[3]}% after the refill"
        seq, last = last_seq(server), await newest_in(tester, alerts)
        sent = float(server.run(["date", "+%s"]).stdout.strip())  # the server's clock, as `at`
        await t.ask(f"!status {B}", STATUS_ANSWER, within=PLAN_S)
        await until(lambda: snapshot_read(server, instance),
                    lambda s: float(s.get("at") or 0) >= sent - 5
                    and float(s.get("disk_percent") or 0) >= THRESHOLD,
                    what=f"the snapshot's read of {B} past {THRESHOLD}%",
                    within=SNAPSHOT_S, every=10)  # fmt: skip
        await tester.sleep(2 * PERIOD + 30)  # two of the bots' looks, and some
        assert await disk_lines(tester, alerts, bot, last) == [], "a line within EBS's 6 hours"
        tried = [r for r in records(server, seq) if r["event"] == "disk_grow"
                 and r.get("slug") == B]  # fmt: skip
        assert tried == [], f"a try within EBS's 6 hours: {tried}"
        assert int(volume_of(aws, B)["Size"]) == grown, "the volume changed again"
        with pytest.raises(Refused) as again:
            await t.request(f"!grow-disk {B} {grown + 2}GB")
        said = await refusal(t, again.value)
        assert SIX_HOURS in said, f"!grow-disk within EBS's 6 hours: {said[:500]}"
        scenario.notes.append(f"{B}: refilled to {refilled[3]}%: no try, no line, within 6 hours")
    finally:
        end_turn(server, ip)
        removed = on_vm(server, ip, "rm", "-f", "--", *FILLS)
        left = [p for p in FILLS if on_vm(server, ip, "test", "-e", p).returncode != 1]
        assert removed.ok and not left, (
            f"{left} may remain on {B} ({ip}): rm exit {removed.returncode}; remove by hand"
        )
