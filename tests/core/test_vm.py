import asyncio
import re
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_bridge import bridge as bridge
from test_bridge import user_message

from zab import __version__
from zab.core import types as t
from zab.core import vm
from zab.core.commands import DM_HELP, HELP
from zab.core.types import Check
from zab.tools import vmstat

pytestmark = pytest.mark.integration
#: The real collector's `!usage` line starts with the CPUs in use (#441), or says it can't tell
VM_CPU = r"(^|\n\n)VM: (\d+\.\d of \d+ CPUs?|CPU unavailable) · "


def stat() -> vmstat.VmStat:
    return vmstat.VmStat(
        host="ws-alice",
        cpus=8,
        load=(1.4, 1.0, 0.9),
        mem_total=31 * 1024**3,
        mem_available=21 * 1024**3,
        uptime=7300.0,
        disks=(vmstat.Disk("/", 97 * 1024**3, 41 * 1024**3, 51 * 1024**3),),
        heaviest=(vmstat.Proc(1, "lean", 8 * 1024**3, 95.0, 60.0),),
        unavailable=(),
        cpu=0.18,
    )


@pytest.mark.parametrize("dm,existing", [(True, False), (False, False), (False, True)])
async def test_vm_routes_without_starting_turn_and_hides_channel_processes(
    bridge, monkeypatch, dm, existing
):
    b, fake, backend = bridge
    backend.kind = "codex"
    backend.doctor.return_value = [Check(name="codex:login", ok=True)]
    if existing:
        await user_message(b, fake, "@**Test Bot** start")
    calls = backend.start_turn.await_count
    collect = AsyncMock(return_value=stat())
    monkeypatch.setattr(vm, "collect", collect)
    if dm:
        await b.handle(
            {
                "type": "message",
                "message": {
                    "id": 999,
                    "sender_id": 20,
                    "type": "private",
                    "content": "!vm",
                    "display_recipient": [{"id": 1}, {"id": 20}],
                },
            }
        )
    else:
        await user_message(b, fake, "@**Test Bot** !vm")
    assert collect.await_args.kwargs == {"top": 3 if dm else 0}
    replies = [m["content"] for m in fake.messages.values() if "**ws-alice**" in m["content"]]
    assert len(replies) == 1 and "Codex signed in" in replies[0]
    assert ("`lean`" in replies[0]) is dm  # command lines only in a DM
    lines = replies[0].splitlines()
    assert lines[:5] == [
        "**ws-alice**",
        "🟩🟩⬜⬜⬜⬜⬜⬜⬜⬜ 1.4 of 8 CPUs",
        "🟩🟩🟩⬜⬜⬜⬜⬜⬜⬜ 32% of 31 GB memory",
        "🟩🟩🟩🟩🟩⬜⬜⬜⬜⬜ 45% of 97 GB disk",
        "",
    ]
    turns = "1 turn running" if existing else "no turns running"
    assert lines[5] == f"{turns} · up 2 hours · Codex signed in · zab {__version__}"
    assert backend.start_turn.await_count == calls


def fake_bridge(*backends, running=0):
    """A `BotBridge` stand-in for `vm.report`: one bridge per backend."""
    bridges = [
        SimpleNamespace(backend=backend, actors={i: SimpleNamespace(turn=i < running)})
        for i, backend in enumerate(backends)
    ]
    first = bridges[0]
    first.config = SimpleNamespace(projects=[SimpleNamespace(path="/work")])
    first.all_bridges = bridges
    return first


def backend(kind, *, ok=None, fails=False):
    name = "codex:login" if kind == "codex" else "claude account main"
    checks = [] if ok is None else [Check(name=name, ok=ok)]
    return SimpleNamespace(
        kind=kind,
        doctor=AsyncMock(side_effect=RuntimeError() if fails else None, return_value=checks),
    )


@pytest.mark.parametrize(
    ("states", "text"),
    [
        ([("Codex", "signed in"), ("Claude", "signed in")], "Codex and Claude signed in"),
        (
            [("Codex", "signed in"), ("Claude", "not signed in")],
            "Codex signed in · Claude not signed in",
        ),
        ([("Claude", "not signed in")], "Claude not signed in"),
        (
            [("Codex", "sign-in status unavailable"), ("Claude", "sign-in status unavailable")],
            "Codex and Claude sign-in status unavailable",
        ),
    ],
)
def test_sign_in_is_merged_when_the_agents_agree(states, text):
    assert vm.sign_in_text(states) == text


def answering(seconds: float, state: bool = False) -> SimpleNamespace:
    """A backend whose `signed_in` hook answers `state` after `seconds`."""

    async def signed_in():
        await asyncio.sleep(seconds)
        return state

    return SimpleNamespace(signed_in=signed_in)


@pytest.mark.parametrize("wait", [0.05, None])
async def test_signed_in_gives_up_after_the_wait_it_is_given(monkeypatch, wait):
    """#805 (the review of #788): `vm.signed_in` stops waiting for its hook after `wait`, or
    after `SIGNED_IN_WAIT_S` without one. A hook that never answers is then unknown (None),
    long before `SIGNED_IN_OWN_WAIT_S`, the long wait, which a stretched `wait` would reach.
    In real time, with a margin of 100 times: `asyncio.timeout` runs on the event loop's
    clock, which the tests' fake clocks don't drive."""
    monkeypatch.setattr(vm, "SIGNED_IN_WAIT_S", 0.05)
    assert vm.SIGNED_IN_OWN_WAIT_S > 5  # the default, 15 s
    assert await asyncio.wait_for(vm.signed_in(answering(30), wait), 5) is None


async def test_signed_in_waits_its_whole_wait(monkeypatch):
    """...and not less: a hook that answers after 0.1 s, within a wait of 10 s, is heard,
    though `SIGNED_IN_WAIT_S` is shorter."""
    monkeypatch.setattr(vm, "SIGNED_IN_WAIT_S", 0.01)
    assert await vm.signed_in(answering(0.1, True), 10) is True


@pytest.mark.parametrize(
    ("running", "text"), [(0, "no turns running"), (1, "1 turn running"), (2, "2 turns running")]
)
async def test_report_status_line_is_turns_uptime_sign_in_version(monkeypatch, running, text):
    monkeypatch.setattr(vm, "collect", AsyncMock(return_value=stat()))
    both = fake_bridge(backend("codex", ok=True), backend("claude", ok=True), running=running)
    reply = await vm.report(both, dm=False)
    assert reply.endswith(
        f"\n\n{text} · up 2 hours · Codex and Claude signed in · zab {__version__}"
    )
    assert vm.collect.await_args.args == (("/", "/work"),)
    split = fake_bridge(backend("codex", ok=True), backend("claude", ok=False))
    assert "· Codex signed in · Claude not signed in ·" in await vm.report(split, dm=False)
    unknown = fake_bridge(backend("codex", fails=True), backend("claude"))
    assert "· Codex and Claude sign-in status unavailable ·" in await vm.report(unknown, dm=False)


async def test_a_failed_collection_still_reports_the_bridge(monkeypatch):
    monkeypatch.setattr(vm, "collect", AsyncMock(side_effect=OSError("boom")))
    reply = await vm.report(fake_bridge(backend("codex", ok=True)), dm=True)
    assert reply == (
        "VM statistics unavailable; try !vm again.\n"
        f"no turns running · Codex signed in · zab {__version__}"
    )


async def test_nonowner_vm_gets_no_statistics(bridge, monkeypatch):
    b, fake, _ = bridge
    collect = AsyncMock()
    monkeypatch.setattr(vm, "collect", collect)
    await user_message(b, fake, "@**Test Bot** !vm", sender=21)
    collect.assert_not_awaited()


async def test_usage_includes_vm_even_without_backend_data(bridge, monkeypatch):
    from zab.core.usage import usage_reply

    b, fake, backend = bridge
    await user_message(b, fake, "@**Test Bot** start")
    backend.usage.return_value = None
    summary = AsyncMock(return_value="VM: 0.4 of 8 CPUs · memory 10% · disk 41%")
    monkeypatch.setattr(vm, "summary", summary)
    actor = b.actors["native"]
    reply = await usage_reply(actor)
    assert "Usage unavailable" in reply
    assert reply.endswith("\n\nVM: 0.4 of 8 CPUs · memory 10% · disk 41%")
    assert summary.await_args.args == (("/", actor.row["workspace"]),)
    monkeypatch.setattr(vm, "summary", AsyncMock(side_effect=OSError))
    assert (await usage_reply(actor)).endswith("\n\nVM: statistics unavailable")
    assert "!vm" in HELP and "`!vm`" in DM_HELP.split("\n")[0]


async def test_usage_does_not_wait_for_the_vm_sample(bridge, monkeypatch):
    """`!usage` samples the CPU for a quarter second, not `!vm`'s second, and does so while
    the backend reports: here the backend answers only once the VM collection has started, and
    the collection goes on only once the backend has been asked, so either sequential order
    times out (reviewer on #279)."""
    from zab.core.usage import usage_reply

    monkeypatch.setattr(vm, "SAMPLE_S", 1.0)  # the real values, not the conftest's zeros
    monkeypatch.setattr(vm, "USAGE_SAMPLE_S", 0.25)
    b, fake, backend = bridge
    await user_message(b, fake, "@**Test Bot** start")
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    entered = threading.Event()
    samples = []
    real = vmstat.collect

    def spy(*args, **kwargs):  # in the worker thread
        samples.append(kwargs["sample"])
        loop.call_soon_threadsafe(started.set)
        assert entered.wait(5)  # times out if the VM line came before the usage call
        return real(*args, **kwargs)

    async def usage(native_id):
        entered.set()
        await asyncio.wait_for(started.wait(), 5)  # times out if the VM line came after us
        return t.Usage(context_tokens=0)

    monkeypatch.setattr(vmstat, "collect", spy)
    backend.kind = "codex"
    backend.usage.side_effect = usage
    began = time.monotonic()
    reply = await usage_reply(b.actors["native"])
    assert time.monotonic() - began < vm.SAMPLE_S
    assert samples == [0.25]
    assert "Context: 0 tokens" in reply and re.search(VM_CPU, reply)


async def test_real_vmstat_import_collection_and_summary(tmp_path):
    # The real collector, not a mocked one (the conftest turns its CPU sample off).
    text = await vm.summary(("/",))
    assert re.match(VM_CPU, text) and " · memory " in text and " · disk " in text
    assert (await vm.collect(("/",), top=0)).heaviest == ()
    real = fake_bridge(backend("codex", ok=True))
    real.config.projects = [SimpleNamespace(path=str(tmp_path / "gone"))]
    channel = (await vm.report(real, dm=False)).splitlines()
    assert re.fullmatch(r"\S+ \d+\.\d of \d+ CPUs?", channel[1])  # in CPUs (#441)
    assert "unavailable" not in "".join(channel[1:4])
    assert channel[4] == f"disk at `{tmp_path}/gone`: unavailable"
    assert channel[-1].endswith(f"zab {__version__}")
