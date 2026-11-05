"""Scenarios 3, 4 and 10 of issue #263 (the watchdog, a Zulip outage, the waker; 10's
follow-ups after the stop, #683 and #713) and 14 (a turn begun while the bridge starts).

3, 4 and 10 freeze the bridge, cut the VM off Zulip or stop the VM, and take 3 to 15 minutes
each, so they run in the `slow` group, once per batch (`run.py --scenarios slow`), never
alongside the fast ones on the same VM. 14 stops only the bridge, for a few seconds, and takes
about 25 s: it runs in the `fast` group, so every run checks recovery (#305).
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import time
from urllib.parse import urlsplit

import pytest

from e2e.harness import flows, installed
from e2e.harness.cleanup import BRIDGE_STOPPED, UNBLOCK, VM_STOPPED, ZULIP_BLOCKED, undo
from e2e.harness.flows import ANSWER_S, DM_STATUS_RE, Chat, Turn, answer, echo, topic_name
from e2e.harness.zulip_user import dm_narrow, mention, topic_narrow

pytestmark = pytest.mark.e2e
INTERRUPTED = "⏹ interrupted by bridge restart; send a message to continue"
#: A turn's status showing its running `sleep`: at the first look after it started, a minute
#: into the turn at the earliest (#754; `flows.WORKING_S`), so each `sleep` here runs well past
#: that.
WORKING_SLEEP = r"^⏳ \*\*Working\*\*.*sleep"
STARTING = r"Starting your VM"
#: The waker's ⏰ on a channel message that waited for a wake.
ALARM = "23f0"
#: What recovery shows when it ends a turn it takes for one that died with the bridge (SPEC
#: §7.8.1): Claude's restart notice, Codex's "The stored turn is no longer active.", and an
#: interrupted turn's status heading (`zab.core.presentation`), as nobody stops these turns
STALE = r"[Ii]nterrupted|no longer active|⏹ \*\*Stopped\*\*"

#: Cut the VM off Zulip (scenario 4): reject TCP to every address the site resolves to, so open
#: connections reset too. Rules only, in memory: nothing survives a stop (reviewer on #285: no
#: /etc/hosts line). The arguments are the addresses; each rule carries the comment `zab-e2e`,
#: which `harness/cleanup.UNBLOCK` removes. Fixed text: inputs are arguments.
BLOCK = r"""set -eu
for ip in "$@"; do
  case "$ip" in *:*) t=ip6tables ;; *) t=iptables ;; esac
  "$t" -I OUTPUT -d "$ip" -p tcp -m comment --comment zab-e2e -j REJECT --reject-with tcp-reset
done
"""
BLOCK_S = 12 * 60
#: Seconds of clock difference allowed between the development machine and Zulip's message
#: timestamps.
CLOCK_SLACK_S = 5
#: The bridge's own startup once systemd restarts it: the slack in scenario 3's bound (#305).
STARTUP_S = 60


def safety_timer(unit: str, seconds: int) -> list[str]:
    """A transient timer on the VM that runs `UNBLOCK` after `seconds`: the undo when the run
    dies without its own clean-up. `--expand-environment=no`: systemd (≥ 254) would otherwise
    expand `${…}` in the command, and `"${rule[@]}"` would become empty, deleting nothing
    (reviewer on #285)."""
    return ["sudo", "systemd-run", "--expand-environment=no", f"--unit={unit}",
            f"--on-active={seconds}", "/bin/bash", "-c", UNBLOCK]  # fmt: skip


def restart_limits(vm) -> tuple[float, float]:
    """(bound, target) of scenario 3's restart, in seconds, from the installed unit (#305): the
    watchdog fires at most `WatchdogSec` after the bridge's last ping, the frozen bridge can't
    act on its SIGABRT, so systemd waits out `TimeoutAbortSec` (`TimeoutStopSec` unless the unit
    sets it) before the SIGKILL, then `RestartSec`, then the bridge starts (`STARTUP_S`): about
    395 s for the shipped unit. The target, recorded but never a failure, is `WatchdogSec`."""
    spans = vm.unit_spans("WatchdogUSec", "TimeoutAbortUSec", "RestartUSec")
    assert 0 < spans["WatchdogUSec"] < float("inf"), f"no watchdog on the bridge's unit: {spans}"
    assert max(spans.values()) < float("inf"), f"an unbounded span: {spans}"
    return sum(spans.values()) + STARTUP_S, spans["WatchdogUSec"]


def restarts(vm) -> tuple[int, str]:
    unit = vm.unit()
    return int(unit["NRestarts"]), unit["MainPID"]


@pytest.mark.scenario(3)
async def test_watchdog(chat: Chat, vm, scenario, nonce):
    """SIGSTOP the bridge mid-turn: systemd restarts it (NRestarts + 1) within the bound its
    unit sets (`restart_limits`, about 6.5 minutes; 361 s was seen, #305), the topic says the
    turn was interrupted, and a follow-up continues the same conversation."""
    topic, code = topic_name(3, nonce), f"lily-{nonce}"
    turn = await chat.say(
        "claude", topic, f"Remember the codeword `{code}`. Run `sleep 300`, then reply `DONE`."
    )
    await chat.reply(turn, WORKING_SLEEP, within=flows.WORKING_S)
    bound, target = restart_limits(vm)
    count, pid = restarts(vm)
    vm.signal(int(pid), "STOP")
    stopped, unit = time.monotonic(), {}
    try:
        while time.monotonic() - stopped < bound + 60:
            await asyncio.sleep(10)
            unit = vm.unit()
            if unit["MainPID"] not in ("0", pid) and unit["ActiveState"] == "active":
                break
    finally:
        if vm.unit()["MainPID"] == pid:
            vm.signal(int(pid), "CONT")  # never leave the bridge frozen
    took = time.monotonic() - stopped
    scenario.timing("restart after SIGSTOP", took, high=bound)
    scenario.timing("restart after SIGSTOP, within WatchdogSec", took, high=target, target=True)
    assert unit.get("MainPID") not in (None, "0", pid), "the bridge was not restarted"
    assert int(unit["NRestarts"]) == count + 1, f"NRestarts {count} → {unit['NRestarts']}"
    await chat.reply(turn, re.escape(INTERRUPTED), within=300)
    follow = await chat.say(
        "claude",
        topic,
        "What was the codeword? Reply with exactly the codeword.",
        mention_bot=False,
    )
    _, took = await chat.reply(follow, answer(code))
    scenario.timing("follow-up answer after the restart", took)


@pytest.mark.scenario(4)
async def test_zulip_outage(chat: Chat, vm, scenario, nonce):
    """Zulip blocked for 12 minutes during a Claude turn: no restart, the turn's answer appears
    after the block, and a message sent during the block is answered afterwards."""
    host = urlsplit(chat.user.client.site).hostname or ""
    assert re.fullmatch(r"[A-Za-z0-9.-]+", host), f"odd Zulip host {host!r}"
    lines = vm.run(["getent", "ahosts", host]).stdout.splitlines()
    ips = sorted({str(ipaddress.ip_address(line.split()[0])) for line in lines if line.strip()})
    token, during = f"OUTAGE-{nonce}", f"DURING-{nonce}"
    turn = await chat.say(
        "claude", topic_name(4, nonce), f"Run `sleep 120`, then reply with exactly `{token}`."
    )
    await chat.reply(turn, WORKING_SLEEP, within=flows.WORKING_S)
    before = restarts(vm)
    safety = f"zab-e2e-unblock-{nonce}"  # undoes the block on the VM even if this run dies
    vm.run(safety_timer(safety, BLOCK_S + 180))
    try:
        vm.script(BLOCK, *ips, sudo=True)  # inside: a block that half went in is undone too
        said = await chat.say("claude", topic_name(4, nonce, "during"), echo(during))
        await asyncio.sleep(BLOCK_S)
    finally:  # retried; if it still fails, the safety timer is left to undo it (#517)
        await undo(ZULIP_BLOCKED, lambda: vm.script(UNBLOCK, sudo=True))
        vm.run(["sudo", "systemctl", "stop", f"{safety}.timer"], check=False)
    unblocked_at, unblocked = time.time(), time.monotonic()
    reply, _ = await chat.reply(turn, answer(token), within=600)
    scenario.timing("turn's answer after the block", time.monotonic() - unblocked)
    late, _ = await chat.reply(said, answer(during), within=600)
    scenario.timing("message sent during the block answered", time.monotonic() - unblocked)
    # the scenario proves its own outage: nothing got through while the VM was blocked
    for what, msg in (("the turn's answer", reply), ("the during-block answer", late)):
        assert msg["timestamp"] >= unblocked_at - CLOCK_SLACK_S, f"{what} was posted mid-block"
    assert restarts(vm) == before, f"the bridge restarted: {before} → {restarts(vm)}"


@pytest.mark.scenario(10)
async def test_waker(chat: Chat, tester2, ec2, instance_id, scenario):
    """The VM stopped through AWS: zab-tester2 can't wake it; two quick DMs from zab-tester
    wake it once ("⏳ Starting your VM…", then the answers). Target: the answer in 45 s."""
    await stop(ec2, instance_id)
    try:
        await until_stopped(ec2, instance_id)
        claude = chat.bots["claude"]
        refused = await tester2.send_dm(claude, "!status")
        await asyncio.sleep(90)
        assert ec2.state(instance_id) == "stopped", "zab-tester2 woke the VM"
        notices = await tester2.silent(
            dm_narrow(claude), after_id=refused, senders=[claude], seconds=0
        )
        assert not notices, f"zab-tester2 got an answer: {notices[0]['content'][:80]}"
        first = await chat.dm("claude", "!status")
        second = await chat.dm("codex", "!status")
        answer1, took = await chat.reply(first, DM_STATUS_RE, within=600)
        scenario.timing("wake: DM to its answer", took, high=45, target=True)
        answer2, _ = await chat.reply(second, DM_STATUS_RE, within=300)
        starting = [m for t in (first, second) for m in await chat.bot_messages(t)
                    if re.search(STARTING, m["content"])]  # fmt: skip
        assert len(starting) == 1, f"{len(starting)} wake notices for two quick messages"
        # "⏳ Starting your VM…", then the answers (reviewer on #285)
        assert starting[0]["id"] < min(answer1["id"], answer2["id"]), "the notice came last"
    finally:
        await undo(VM_STOPPED, lambda: running_again(ec2, instance_id), attempts=1)


@pytest.mark.scenario(10)
async def test_waker_stream(chat: Chat, ec2, instance_id, scenario, nonce):
    """The VM stopped through AWS: in a new topic, zab-tester mentions one bot, typing `@claude`
    (or `@codex`) as plain text, as without Zulip's typeahead (#713); at once, in a second
    topic, mentions both bots, completed; then follows up in the first topic without a mention
    (SPEC §13.4).
    - The typed mention gets one ⏰, from the bot it names and from no other.
    - The mention of both bots gets one ⏰ from each (#679), never one bot's alone, if it
      reaches the waker before the start request, under a second after the first mention;
      else none, which the report notes: then its check did not run.
    - The follow-up gets a ⏰ from each bot, as the bridge sends a message without a mention
      to each (#713), if it reaches the waker before the start request, about a second after
      the mention; else none, which the report notes. Never one bot's alone.
    - The turn's answer arrives. This needs the bridge's startup recovery fix (#303).
    - No ⏰ comes later, on any of the three.
    The bot is Claude, or Codex when `!vm` says the VM's Claude is not signed in. A Claude
    whose sign-in state `!vm` can't give fails before the VM is stopped (`signed_in`); one
    that is signed in but rate limited fails at the answer."""
    kind = await signed_in(chat, scenario, topic_name(10, nonce, "vm"))
    if kind != "claude":
        scenario.notes.append("Claude not signed in on the VM: the stream wake used Codex")
    token = f"WAKE-{nonce}"
    await stop(ec2, instance_id)  # after a waker poll has dropped the `!vm` DM
    try:
        await until_stopped(ec2, instance_id)
        topic = topic_name(10, nonce, "stream")
        typed = await chat.user.send(chat.stream, topic, f"@{kind} " + echo(token))
        first = Turn(topic_narrow(chat.stream, topic), typed, chat.bots[kind], time.monotonic())
        both = await chat.user.send(
            chat.stream,
            topic_name(10, nonce, "both"),
            " ".join(mention(chat.names[k], chat.bots[k]) for k in sorted(chat.bots))
            + " "
            + echo(f"BOTH-{nonce}"),
        )
        second = await chat.say(kind, topic, "And again: " + echo(token), mention_bot=False)
        bots = set(chat.bots.values())

        async def alarms(message_id: int) -> list[int]:
            return await alarm_clocks(chat, message_id)

        async def first_alarms() -> list[int] | None:
            return await alarms(first.message_id) or None

        # a ⏰ from any of the bots: one from the wrong bot fails at once, not after a timeout
        found = await chat.user.wait_for(first_alarms, within=120, what="a ⏰ on the mention")
        scenario.timing("wake: mention to its ⏰", time.monotonic() - first.sent_at, high=60)
        assert found == [first.bot], f"⏰ on the mention from {found}, not [{first.bot}]"
        await asyncio.sleep(10)  # the other ⏰s, if any, come in the same pass
        each = sorted(await alarms(both))
        assert each in (sorted(bots), []), f"⏰s on the mention of both bots from {each}"
        if not each:
            scenario.notes.append("the mention of both bots reached the waker after the start "
                                  "request: no ⏰, by design (SPEC §13.4); its check did not "
                                  "run")  # fmt: skip
        follow = sorted(await alarms(second.message_id))
        assert follow in (sorted(bots), []), f"⏰s on the follow-up from {follow}"
        if not follow:
            scenario.notes.append("the follow-up reached the waker after the start request: "
                                  "no ⏰, by design (SPEC §13.4)")  # fmt: skip
        _, took = await chat.reply(first, answer(token), within=600)
        scenario.timing("wake: mention to its answer", took, high=120, target=True)
        assert await alarms(first.message_id) == [first.bot], "a later ⏰ on the mention"
        assert sorted(await alarms(second.message_id)) == follow, "a later ⏰ on the follow-up"
        assert sorted(await alarms(both)) == each, "a later ⏰ on the mention of both bots"
    finally:
        await undo(VM_STOPPED, lambda: running_again(ec2, instance_id), attempts=1)


@pytest.mark.scenario(10)
async def test_waker_follow_ups(chat: Chat, ec2, instance_id, scenario, nonce):
    """Follow-ups without a mention after the stop (#683, #713). With the VM up, zab-tester
    mentions Claude in one topic and both bots in another, and each mentioned bot answers. Once
    the VM is stopped through AWS, zab-tester writes in both topics at once, without a mention,
    and in a new topic, without one either (#687).
    - Each of the three gets one ⏰ from each bot, whatever its topic's earlier messages
      addressed (SPEC §13.4.2, #713: the waker reads no history). The message that woke the VM
      gets its ⏰s; the others, if they reached the waker after the start request, none, which
      the report notes: then their check did not run.
    - The bridge agrees: both bots answer each, Codex in a new session in Claude's topic.
    - No ⏰ comes later.
    Both agents must be signed in on the VM; Claude's is checked (`signed_in`), or the part is
    skipped."""
    if await signed_in(chat, scenario, topic_name(10, nonce, "vm")) != "claude":
        pytest.skip("Claude not signed in on the VM: the follow-ups need its answers")
    codex, claude = chat.bots["codex"], chat.bots["claude"]
    alone, pair = topic_name(10, nonce, "claude"), topic_name(10, nonce, "both")
    fresh = topic_name(10, nonce, "new")
    said = await chat.say("claude", alone, echo(f"ALONE-{nonce}"))
    addresses = " ".join(mention(chat.names[k], chat.bots[k]) for k in sorted(chat.bots))
    both = await chat.user.send(chat.stream, pair, f"{addresses} {echo(f'PAIR-{nonce}')}")
    await chat.reply(said, answer(f"ALONE-{nonce}"))
    for bot in (codex, claude):
        await chat.user.wait_reply(topic_narrow(chat.stream, pair), after_id=both, senders=[bot],
                                   pattern=answer(f"PAIR-{nonce}"), within=ANSWER_S)  # fmt: skip
    await stop(ec2, instance_id)  # after a waker poll has dropped what it saw meanwhile
    try:
        await until_stopped(ec2, instance_id)
        again = "And again: " + echo(f"AGAIN-{nonce}")
        first, second, third = await asyncio.gather(  # as close together as the waker allows
            chat.say("claude", alone, again, mention_bot=False),
            chat.say("claude", pair, again, mention_bot=False),
            chat.say("claude", fresh, again, mention_bot=False),
        )
        both_bots = sorted([codex, claude])
        wanted = {turn.message_id: both_bots for turn in (first, second, third)}
        where = {first.message_id: "Claude's topic", second.message_id: "both bots' topic",
                 third.message_id: "a new topic"}  # fmt: skip

        async def found() -> dict[int, list[int]] | None:
            now = {mid: sorted(await alarm_clocks(chat, mid)) for mid in wanted}
            return now if any(now.values()) else None

        # a ⏰ from any bot; one from the wrong bot fails after the pass, not after a timeout
        await chat.user.wait_for(found, within=120, what="a ⏰ on a follow-up")
        sent = min(turn.sent_at for turn in (first, second, third))
        scenario.timing("wake: follow-up to its ⏰", time.monotonic() - sent, high=60)
        await asyncio.sleep(10)  # the other ⏰s, if any, come in the same pass
        each = await found()
        assert each is not None, "the ⏰s on the follow-ups went away"
        for mid, bots in wanted.items():
            assert each[mid] in (bots, []), f"⏰s in {where[mid]} from {each[mid]}, not {bots}"
            if not each[mid]:
                scenario.notes.append(f"the follow-up in {where[mid]} reached the waker after "
                                      "the start request: no ⏰, by design (SPEC §13.4); its "
                                      "check did not run")  # fmt: skip
        _, took = await chat.reply(first, answer(f"AGAIN-{nonce}"), within=600)
        scenario.timing("wake: follow-up to Claude's answer", took, high=120, target=True)
        for turn in (first, second, third):
            for bot in (codex, claude):
                await chat.user.wait_reply(
                    turn.narrow, after_id=turn.message_id, senders=[bot],
                    pattern=answer(f"AGAIN-{nonce}"), within=300,
                )  # fmt: skip
        assert await found() == each, "a later ⏰ on a follow-up"
    finally:
        await undo(VM_STOPPED, lambda: running_again(ec2, instance_id), attempts=1)


async def alarm_clocks(chat: Chat, message_id: int) -> list[int]:
    """The VM's bots that reacted ⏰ to the message."""
    bots = set(chat.bots.values())
    reactions = (await chat.user.message(message_id)).get("reactions", [])
    return [r["user_id"] for r in reactions
            if r.get("emoji_code") == ALARM and r.get("user_id") in bots]  # fmt: skip


#: Seconds that cover one waker state poll (every 30 s) and its pass.
WAKER_POLL_S = 40


async def stop(ec2, instance_id: str) -> None:
    """Stop the VM once a waker poll has passed. The waker keeps an owner message it saw while
    the VM was up until its next poll, and wakes the VM if that poll shows it stopping
    (SPEC §13.4): `signed_in`'s `!vm`, or the previous scenario's last message."""
    await asyncio.sleep(WAKER_POLL_S)
    ec2.stop(instance_id)


async def until_stopped(ec2, instance_id: str) -> None:
    """Wait until the VM is stopped and the waker has seen it so."""
    ec2.wait_state(instance_id, "stopped", timeout=600)
    await asyncio.sleep(WAKER_POLL_S)
    assert ec2.state(instance_id) == "stopped", "the VM woke before the scenario's messages"


async def signed_in(chat: Chat, scenario, topic: str) -> str:
    """`claude`, or `codex` when `!vm` says the VM's Claude is not signed in (#409). Not by
    `!status`'s "sign in via DM: !login" hint: the bridge leaves it out when the check fails or
    takes over 2 s, and its absence read as signed in. `!vm` then says "sign-in status
    unavailable" (`zab.core.vm.sign_in`): asked again, and if it stays so, the scenario fails."""
    for ask in range(1, SIGN_IN_ASKS + 1):
        state, line = await vm_sign_in(chat, topic, within=300)
        if state is not None:
            if ask > 1:
                scenario.notes.append(f"Claude's sign-in status unavailable until `!vm` {ask}")
            return "claude" if state else "codex"
        if ask < SIGN_IN_ASKS:
            await asyncio.sleep(SIGN_IN_RETRY_S)
    pytest.fail(f"Claude's sign-in state unknown after {SIGN_IN_ASKS} `!vm`: {line}", pytrace=False)


async def running_again(ec2, instance_id: str) -> None:
    """A failed wake must not leave the VM stopped for the scenarios after this one."""
    if ec2.state(instance_id) != "running":
        if ec2.state(instance_id) in ("stopping", "pending"):
            await asyncio.sleep(60)
        if ec2.state(instance_id) == "stopped":
            ec2.start(instance_id)
        ec2.wait_state(instance_id, "running", timeout=600)


def claude_sign_in(report: str) -> bool | None:
    """Claude's state in `!vm`'s status line (`zab.core.vm.sign_in_text`, or the older
    `bridge … · Claude signed in · …`): True when signed in, False when not, None unknown."""
    for line in report.splitlines():
        for part in line.split(" · "):
            if "Claude" in part:
                if part.endswith("not signed in"):
                    return False
                return True if part.endswith("signed in") else None
    return None


#: The bridge's `!vm` reply, by its sign-in part (`zab.core.vm.sign_in`): never the sentinel's
#: reply to a DM (`zab.tools.sentinel`), which has none.
VM_SIGN_IN_RE = r"signed in|sign-in status"
#: `signed_in` asks `!vm` up to this many times, this many seconds apart, while Claude's
#: sign-in status is unavailable: a check over 2 s on a VM just woken may be quicker next time.
SIGN_IN_ASKS, SIGN_IN_RETRY_S = 3, 15.0


async def vm_sign_in(chat: Chat, topic: str, *, within: float) -> tuple[bool | None, str]:
    """Claude's state in the bridge's `!vm` (`claude_sign_in`), and the reply's status line.
    Asked of the Codex bot in `topic`: in a DM, where `zab sentinel` answers `!vm` once it is
    installed (#204), the reply has no sign-in."""
    turn = await chat.say("codex", topic, "!vm")
    reply, _ = await chat.reply(turn, VM_SIGN_IN_RE, within=within)
    text = reply["content"]
    line = next((ln for ln in text.splitlines() if re.search(VM_SIGN_IN_RE, ln)), text[:200])
    return claude_sign_in(text), line


@pytest.mark.scenario(14)
async def test_startup_race(chat: Chat, vm, scenario, nonce):
    """Stop the bridge, mention each bot in a new topic, start it: each answers the prompt, and
    recovery ends neither turn (Drew's VM on 2026-10-14: a turn begun during startup was ended
    "⏹ interrupted by bridge restart" and its answer dropped; SPEC §7.8.1). When the test VM's
    Claude is not signed in, Codex alone is checked and the scenario reports a skip."""
    claude = True
    if installed.has(vm, installed.HAS_VM):
        state, _ = await vm_sign_in(chat, topic_name(14, nonce, "vm"), within=120)
        claude = state is not False
    kinds = ("claude", "codex") if claude else ("codex",)
    tokens = {kind: f"RACE-{kind}-{nonce}" for kind in kinds}
    vm.unit_action("stop")
    try:
        turns = {kind: await chat.say(kind, topic_name(14, nonce, kind), echo(tokens[kind]))
                 for kind in kinds}  # fmt: skip
        await asyncio.sleep(5)
    finally:
        await undo(BRIDGE_STOPPED, lambda: vm.unit_action("start"))
    started = time.monotonic()
    for kind in kinds:
        # the answer, or what recovery posts instead of it, whichever comes first
        reply, _ = await chat.reply(turns[kind], f"{answer(tokens[kind])}|{STALE}")
        assert not re.search(STALE, reply["content"]), f"{kind}: {reply['content'][:120]!r}"
        scenario.timing(f"{kind}: answered, after the start", time.monotonic() - started)
    for kind in kinds:
        stale = [m["content"][:120] for m in await chat.bot_messages(turns[kind])
                 if re.search(STALE, m["content"])]  # fmt: skip
        assert not stale, f"{kind}: recovery ended the turn: {stale[0]!r}"
    if not claude:
        pytest.skip("Codex passed; Claude not checked: it is not signed in on the test VM")
