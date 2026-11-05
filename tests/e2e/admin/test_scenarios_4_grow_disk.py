"""A21, `!grow-disk` on the rig (docs/ADMIN.md §7; #553): a fake participant's root volume grown
by `GROWN_GB` through `Workshop admin · e2e`, as ORGANIZERS §4.1 has an organizer send it, on a
running VM and on a stopped one. The file system's size is read over SSH from the server, apart
from what the bot says: `df -Pk /` as `zab-admin`, over the fleet key and the workshop's pinned
host key, as the bot's scripts connect (`DF_ON_VM`).

- **Refusals, which change nothing:** a size over the cap, which the grammar refuses, and one
  smaller than the volume, which `grow_disk.py`'s dry run refuses (`refusing: … GB already`).
  The volume's own size is no refusal on a running VM: it grows the file system alone (#563).
- **A running VM,** `zab-test-b`: its volume's size from EC2, then `!grow-disk zab-test-b
  <size + 2>GB`, a change for one approver with no phrase, whose dry run names the volume, both
  sizes, the cost a month and the disk's use now. Applied: EC2 shows the new size, the result
  ends with the root file system's new size, and `df` shows it about `GROWN_GB` larger than
  before. A second request at once is refused at plan time: EBS takes one modification of a
  volume every 6 hours. This is VERIFY V17's first half: `GROW` online, on the workshop image.
  Then the new size again, within those 6 hours, as after an interrupted grow (#563): its plan
  says it grows the file system alone, at no cost; applied, no `ModifyVolume`, `growpart`
  finds nothing to grow, and EC2 and `df` keep their sizes.
- **A stopped VM,** `zab-test-a`, V17's other half: its file system's size read while it runs,
  then it is stopped and grown (the plan and the result say that its next boot grows it), and
  started. Cloud-init's `growpart` and `resizefs` grow it at that boot, and `df` shows it about
  `GROWN_GB` larger: read again for up to `RESIZE_S` after SSH answers, since `resizefs` may
  still be running then (`grown_from`). The VM is stopped again whatever happened, never by a
  step that could skip and hide a failure (`stop_again`).
- **The scope,** which A21 takes only the allowed side of: A13 probes `ModifyVolume` of the
  server's volume and of the #263 VM's, refused, beside a participant's.
- **A second run within 6 hours** on the same VMs (`--scenarios A21` again): the plan is refused
  for the 6-hour limit, and the part skips, naming it.
- **The fleet as A9 finds it:** `zab-test-a` stopped, `zab-test-b` running, as A20 leaves them;
  A21 runs between the two (by file name).

The GB added cost $0.08 a month each, until A9 terminates the VMs and their volumes
(`DeleteOnTermination`): cents. It needs the rig's role rendered from the commit under test
(`Volume`, and `Read`'s two volume calls: ADMIN §5), which the setup puts on it.
"""

from __future__ import annotations

import csv
import io
import ipaddress
import re
import time
from typing import Any

import pytest

from e2e.admin.conftest import requires
from e2e.admin.harness.chat import Refused, Talk
from e2e.admin.harness.rig import E2E
from e2e.admin.harness.settle import until
from e2e.admin.test_scenarios_2_bot import refusal, talk, vm_state, vm_states, wait_state
from e2e.admin.test_scenarios_3_late_joiner import room_for
from e2e.admin.test_scenarios_8_the_end import WS

A, B = "zab-test-a", "zab-test-b"
GROWN_GB = 2
#: At least this much of the GB added shows in `df`'s size: ext4 keeps some of each for its own
#: metadata (inode tables, the journal's share), a few percent.
SHOWN_KIB = int(GROWN_GB * 2**20 * 0.9)
#: How long the VM's SSH may take to answer after EC2 says `running` (a boot), and how long
#: more its file system may take to show its new size (cloud-init's `resizefs` may still run).
SSH_S, RESIZE_S = 300.0, 120.0
#: The 6-hour refusal of `grow_disk.py`'s dry run (EBS's limit).
SIX_HOURS = "and EBS allows one modification every 6 hours: request again from"
#: As `zab-admin` on the server, the workshop and an address as `$1` and `$2`: the root file
#: system's `df -Pk /` data line on that VM, over the fleet key (the default identity in
#: zab-admin's HOME) and the workshop's pinned host keys, with nothing forwarded.
DF_ON_VM = r"""set -eu
case $1 in zab-admin-e2e) ;; *) exit 2 ;; esac
exec /usr/bin/ssh -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=yes \
  -o "UserKnownHostsFile=/var/lib/zab-admin/ws/$1/out/known_hosts" \
  -o GlobalKnownHostsFile=/dev/null -o ForwardAgent=no -o ForwardX11=no \
  -o ClearAllForwardings=yes "ubuntu@$2" -- 'LC_ALL=C df -Pk / | tail -n 1'
"""


def ip_of(server: Any, slug: str) -> str:
    """The row's `ip`, from the server's roster: the address its scripts connect to."""
    rows = csv.DictReader(io.StringIO(server.read(f"{WS}/roster.csv")))
    found = next((row.get("ip") or "" for row in rows if row.get("slug") == slug), "")
    return str(ipaddress.ip_address(found.strip()))


def root_kib(server: Any, ip: str) -> int | None:
    """The root file system's size in KiB, `df -Pk /`'s, or None when SSH doesn't answer."""
    done = server.as_admin("/bin/bash", "-c", DF_ON_VM, "bash", E2E.name, ip, timeout=120)
    parts = done.stdout.split()
    return int(parts[1]) if done.ok and len(parts) >= 6 and parts[1].isdigit() else None


async def root_size(server: Any, slug: str) -> int:
    """`root_kib` of the row's VM, again until its SSH answers (it may just have booted)."""
    ip = ip_of(server, slug)
    return await until(lambda: root_kib(server, ip), lambda kib: kib is not None,
                       what=f"{slug}'s df over SSH", within=SSH_S, every=15)  # fmt: skip


async def grown_from(server: Any, slug: str, before: int) -> int:
    """`root_kib` of the row's VM, again until it is `SHOWN_KIB` above `before`: after a boot,
    SSH may answer before cloud-init's `resizefs` is done (the review of #561, N3)."""
    ip = ip_of(server, slug)
    return await until(lambda: root_kib(server, ip),
                       lambda kib: kib is not None and kib - before >= SHOWN_KIB,
                       what=f"{slug}'s df at least {SHOWN_KIB} KiB above {before} KiB",
                       within=SSH_S + RESIZE_S, every=15)  # fmt: skip


async def stop_again(t: Talk, aws: Any, slug: str) -> None:
    """The row's VM stopped, as A9 finds it, with no skip: unlike `room_for`, it never turns a
    failure of the part into a skip (the review of #561, N2)."""
    if vm_state(aws, slug) == "pending":
        await wait_state(aws, slug, "running", within=300)  # a start ends first
    if vm_state(aws, slug) == "running":
        await t.run(f"!stop {slug}", phrase=False)
    await wait_state(aws, slug, "stopped")


def volume_of(aws: Any, slug: str) -> dict[str, Any]:
    """The row's VM's root volume, as EC2 describes it now."""
    inst = vm_states(aws)[slug]
    root = inst.get("RootDeviceName")
    vid = next(m["Ebs"]["VolumeId"] for m in inst["BlockDeviceMappings"]
               if m.get("DeviceName") == root)  # fmt: skip
    (volume,) = aws.aws.ec2("describe_volumes", VolumeIds=[vid])["Volumes"]
    return volume


async def sized(aws: Any, slug: str, gb: int) -> None:
    """Until EC2 shows the row's root volume at `gb` (a read may lag the change: `settle`)."""
    await until(lambda: volume_of(aws, slug).get("Size"), lambda size: size == gb,
                what=f"{slug}'s volume at {gb} GB", within=120, every=10)  # fmt: skip


async def grow_plan(t: Talk, slug: str, gb: int) -> Any:
    """`!grow-disk slug <gb>GB`'s plan; the part skips when the volume was grown less than 6
    hours ago, by an earlier run on the same VMs."""
    try:
        return await t.request(f"!grow-disk {slug} {gb}GB")
    except Refused as refused:
        said = await refusal(t, refused)
        if SIX_HOURS in said:
            pytest.skip(f"{slug}'s volume was grown less than 6 hours ago (an earlier run)")
        raise


def dry_run_names(plan: Any, slug: str, size: int, gb: int, state: str) -> None:
    """The plan as ORGANIZERS §2.2 has it: one approver, no phrase; the volume, its sizes and
    the cost a month."""
    assert plan.targets == [slug], f"{plan.id}'s targets: {plan.targets}"
    assert not plan.phrase, f"{plan.id} asks for the phrase {plan.phrase!r}"
    cost = f"+{gb - size} GB: about ${(gb - size) * 0.08:,.2f} a month more, at $0.08 per GB-month"
    line = rf"{slug} i-[0-9a-f]+ \({state}\): volume vol-[0-9a-f]+ \S+ {size} GB → {gb} GB, "
    assert re.search(line + re.escape(cost), plan.text), plan.text[:1500]


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A21")
@requires("launched")
async def test_a21_a_size_that_doesnt_grow_is_refused(scenario, tester, rigged, aws, nonce):
    t = talk(tester, rigged, E2E.name, f"e2e-A21-refused-{nonce}")
    await room_for(t, aws, running=(B,), beside=(A,))
    capped = await t.ask(f"!grow-disk {B} 1001GB", r"^❌")
    assert "a size like 300GB, at most 1000GB" in capped["content"], capped["content"][:300]
    size = int(volume_of(aws, B)["Size"])
    with pytest.raises(Refused) as refused:  # the volume's own size grows its file system (#563)
        await t.request(f"!grow-disk {B} {size - 1}GB")
    said = await refusal(t, refused.value)
    assert f"is {size} GB already: ask for more (EBS only grows)" in said, said[:500]
    assert int(volume_of(aws, B)["Size"]) == size, "a refused plan changed the volume"


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A21")
@requires("launched")
async def test_a21_a_running_vm_grows_online(scenario, tester, rigged, server, aws, nonce):
    t = talk(tester, rigged, E2E.name, f"e2e-A21-running-{nonce}")
    await room_for(t, aws, running=(B,), beside=(A,))
    size = int(volume_of(aws, B)["Size"])
    before = await root_size(server, B)
    plan = await grow_plan(t, B, size + GROWN_GB)
    dry_run_names(plan, B, size, size + GROWN_GB, "running")
    assert f"{B}: root file system now: " in plan.text, plan.text[:1500]
    started = time.monotonic()
    _, text = await t.approve(plan)
    scenario.timing("!grow-disk of a running VM, approval to result", time.monotonic() - started)
    assert text.startswith(f"✅ **{plan.id}**"), text.split("\n")[0]
    assert re.search(rf"{B}: \S+ is (optimizing|completed) after \d+ s", text), text[-1500:]
    assert f"{B}: root file system now: " in text.rsplit("growpart", 1)[-1], text[-1500:]
    await sized(aws, B, size + GROWN_GB)
    after = await grown_from(server, B, before)
    scenario.notes.append(f"{B}: {size} → {size + GROWN_GB} GB; df {before} → {after} KiB")
    with pytest.raises(Refused) as again:
        await t.request(f"!grow-disk {B} {size + 2 * GROWN_GB}GB")
    said = await refusal(t, again.value)
    assert SIX_HOURS in said, f"a second grow within 6 hours: {said[:500]}"
    # #563: the volume's own size, within the 6 hours, grows the file system alone, as after
    # an interrupted grow: no ModifyVolume, and GROW finds nothing left to grow here
    plan = await t.request(f"!grow-disk {B} {size + GROWN_GB}GB")
    assert f"already {size + GROWN_GB} GB: its file system alone" in plan.text, plan.text[:1500]
    assert "would grow its file system over SSH (no cost)" in plan.text, plan.text[:1500]
    _, text = await t.approve(plan)
    assert text.startswith(f"✅ **{plan.id}**"), text.split("\n")[0]
    assert f"{B}: ModifyVolume vol-" not in text, text[-1500:]  # the apply's own line, if any
    assert re.search(rf"{B}: growpart \S+ \d+: unchanged", text), text[-1500:]
    assert re.search(rf"{B}: \S+ on \S+ fills it already: nothing to resize", text), text[-1500:]
    assert f"{B}: root file system now: " in text, text[-1500:]
    assert int(volume_of(aws, B)["Size"]) == size + GROWN_GB, "the file system alone grew EBS"
    assert await root_size(server, B) >= after, "the file system alone shrank df"
    scenario.notes.append(f"{B}: the same size again grew its file system alone (unchanged)")


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A21")
@requires("launched")
async def test_a21_a_stopped_vm_grows_at_its_next_boot(
    scenario, tester, rigged, server, aws, nonce
):
    """Its size is read while it runs; it is left stopped, as A9 finds it."""
    t = talk(tester, rigged, E2E.name, f"e2e-A21-stopped-{nonce}")
    await room_for(t, aws, running=(A,), beside=(B,))
    size = int(volume_of(aws, A)["Size"])
    before = await root_size(server, A)
    await room_for(t, aws, running=(), stopped=(A,), beside=(B,))
    try:
        plan = await grow_plan(t, A, size + GROWN_GB)
        dry_run_names(plan, A, size, size + GROWN_GB, "stopped")
        assert "its file system grows at its next boot (cloud-init)" in plan.text, plan.text[:1500]
        _, text = await t.approve(plan)
        assert text.startswith(f"✅ **{plan.id}**"), text.split("\n")[0]
        grown = f"the VM is stopped: its file system grows to {size + GROWN_GB} GB at its next boot"
        assert grown in text, text[-1500:]
        await sized(aws, A, size + GROWN_GB)
        assert vm_states(aws)[A]["State"]["Name"] == "stopped", "the apply started the VM"
        started = time.monotonic()
        await room_for(t, aws, running=(A,), beside=(B,))
        after = await grown_from(server, A, before)
        scenario.timing("the stopped VM's start to its df", time.monotonic() - started)
        scenario.notes.append(f"{A}: {size} → {size + GROWN_GB} GB; df {before} → {after} KiB")
    finally:  # as A9 finds it
        await stop_again(t, aws, A)
