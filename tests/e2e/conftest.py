"""Fixtures of the end-to-end suite (issue #263) and its report.

Scenarios are test functions marked `e2e` and `scenario(N)`. `ZAB_E2E_SCENARIOS` (`all`, `fast`,
`slow`, or numbers such as `1,2,5-7`) selects them; `ZAB_E2E_REPORT_JSON` names the file the
results go to (`run.py` sets both). The fixtures below open nothing until a scenario asks for
them, and only under `ZAB_LIVE=e2e` (the root conftest skips `e2e` tests otherwise).

A scenario whose clean-up failed (`harness.cleanup.Leftover`) adds a run problem to the report,
naming what it left on the VM; when the later scenarios can't run in that state, the run stops
after it (pytest's `Session.shouldstop`, #517) and the scenarios not run say why.

A part in which OpenAI refused a Codex request with a 429 runs once more after a wait, and its
row names that external cause (`harness.refusal`, #842). So does a part that failed on the rig's
side (`harness.rig_failure`, #877); if it fails so again, it has no result (a skip), not a FAIL.
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

from e2e.harness import config, refusal, report
from e2e.harness.cleanup import Leftover
from e2e.harness.recorder import Recorder
from e2e.harness.rig_failure import RigFailure
from e2e.harness.vm import say

_RECORDER = Recorder()
#: The refused parts this run may still run again (`harness.refusal`, #842)
_RETRIES = refusal.Budget()
#: The parts failed on the rig's side this run may still run again (`harness.rig_failure`, #877)
_RIG_RETRIES = refusal.Budget()


def _number(item: pytest.Item) -> int | None:
    marker = item.get_closest_marker("scenario")
    return int(marker.args[0]) if marker else None


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    spec = os.environ.get("ZAB_E2E_SCENARIOS")
    parts: dict[int, list[str]] = {}  # every part of every scenario, before the selection
    for item in items:
        if (number := _number(item)) is not None:
            parts.setdefault(number, []).append(item.name)
    if spec:
        wanted = set(report.select(spec))
        # a part of a fast scenario that stops the VM (`@pytest.mark.slow`, scenario 8's cold
        # import) runs with the slow group, never in a fast run (codex on #286)
        slow = bool({p.strip() for p in spec.split(",")} & {"slow", "all"})
        keep, drop = [], []
        for item in items:
            number = _number(item)
            if number is None:
                keep.append(item)
            elif item.get_closest_marker("slow"):
                (keep if slow else drop).append(item)
            else:
                (keep if number in wanted else drop).append(item)
        if drop:
            config.hook.pytest_deselected(items=drop)
            items[:] = keep
    # a selected scenario's report row lists all its parts: one left out stays visible
    for number in {n for item in items if (n := _number(item)) is not None}:
        _RECORDER.expect(number, parts[number])


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item):
    """A part in which OpenAI refused a Codex request with a 429 runs once more, after a wait,
    with a fresh nonce, while the run's `_RETRIES` last; its row names the cause, and a second
    refusal fails it (`harness.refusal`, #842). A part that failed on the rig's side runs once
    more the same way, while `_RIG_RETRIES` last; failed so again, or with the budget spent, it
    is skipped: no result, as the bridge wasn't seen to fail (`harness.rig_failure`, #877)."""
    number = _number(item)
    if number is None or not item.get_closest_marker("e2e"):
        return (yield)
    row = _RECORDER.row(number)
    kept, noted = len(row.timings), len(row.notes)
    try:
        return (yield)
    except (refusal.Refused, RigFailure) as first:
        funcargs = getattr(item, "funcargs", {})

        def renew() -> None:
            if "nonce" in funcargs:
                funcargs["nonce"] = new_nonce()

        budget = _RIG_RETRIES if isinstance(first, RigFailure) else _RETRIES
        if budget.left > 0:
            say(f"{item.name}: {first}; running it again in {first.wait_s:.0f} s")
        try:
            refusal.retry(item.name, first, row, kept=kept, noted=noted, budget=budget,
                          run=item.runtest, renew=renew)  # fmt: skip
        except RigFailure as last:
            pytest.skip(f"no result: {last}")
        return None


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    rep = yield
    number = _number(item)
    if number is not None and item.get_closest_marker("e2e"):
        record(_RECORDER, number, item.name, rep)
        if call.excinfo is not None and isinstance(call.excinfo.value, Leftover):
            left_behind(_RECORDER, item.session, number, call.excinfo.value)
    return rep


def record(recorder: Recorder, number: int, name: str, rep: pytest.TestReport) -> None:
    """Fold one phase's report into the scenario's row: the call; a setup that did not pass
    (a skip, a missing input); a teardown that failed (a cleanup that raised, codex on #282)."""
    if not (rep.when == "call" or (rep.when == "setup" and not rep.passed) or rep.failed):
        return
    reason = ""
    if rep.skipped and isinstance(rep.longrepr, tuple):
        reason = str(rep.longrepr[2]).removeprefix("Skipped: ")
    elif rep.failed:
        # the exception and its message (`ExceptionRepr.reprcrash`), not only the traceback's
        # last line, which names the file and the exception's type (#517)
        crash = getattr(getattr(rep.longrepr, "reprcrash", None), "message", "") or ""
        if crash:
            line = crash.strip().splitlines()[:1]
        else:
            line = (getattr(rep, "longreprtext", "") or "").strip().splitlines()[-1:]
        reason = (f"{rep.when}: " if rep.when != "call" else "") + (line or [""])[0][:300]
    recorder.finish(number, name, rep.outcome, rep.duration, reason)


def left_behind(recorder: Recorder, session: pytest.Session, number: int, left: Leftover) -> None:
    """Scenario `number`'s clean-up failed (#517): a run problem naming what it left. When the
    later scenarios can't run in that state, the run stops once this scenario's teardown is
    done (`shouldstop`, which pytest checks between tests), and pytest exits 2. The row keeps
    the scenario's own failure that came before (`Leftover.after`)."""
    if left.after:
        row = recorder.row(number)
        row.reason = f"{row.reason}; after the scenario failed: {left.after}"
    if left.stops:
        recorder.stop(f"the run stopped after scenario {number}: {left}",
                      why=f"the run stopped after scenario {number}: {left.what}")  # fmt: skip
        session.shouldstop = f"scenario {number}'s clean-up failed: {left.what}"
    else:
        recorder.problems.append(f"scenario {number}'s clean-up failed: {left}")


def noted(recorder: Recorder, number: int | None) -> Callable[[str], None]:
    """Where an SSH connect failure's line goes (`Vm.call`, #517 item 4): stderr, and scenario
    `number`'s notes, so that the report shows each one, in a passed scenario's row too."""

    def log(line: str) -> None:
        say(line)
        if number is not None:
            recorder.row(number).notes.append(line)

    return log


def pytest_sessionfinish(session: pytest.Session) -> None:
    target = os.environ.get("ZAB_E2E_REPORT_JSON")
    if target and (_RECORDER.rows or _RECORDER.problems):
        _RECORDER.report().write(Path(target))


# -------------------------------------------------------------------------------- fixtures


@pytest.fixture
def scenario(request: pytest.FixtureRequest) -> report.ScenarioResult:
    """The scenario's report row: `scenario.timing(…)`, `scenario.notes.append(…)`."""
    number = _number(request.node)
    assert number is not None, "an e2e test needs @pytest.mark.scenario(N)"
    return _RECORDER.row(number)


@pytest.fixture(scope="session")
def rig() -> config.RigConfig:
    cfg = config.load()
    missing = cfg.missing()
    if missing:
        pytest.fail("the rig's inputs are missing: " + "; ".join(missing), pytrace=False)
    return cfg


@pytest.fixture(scope="session")
def ec2(rig: config.RigConfig):
    from e2e.harness.aws import TestEc2

    return TestEc2(rig.region, dict(rig.scope))


@pytest.fixture(scope="session")
def instance_id(rig: config.RigConfig, ec2) -> str:
    return str(ec2.find({"participant": rig.slug})["InstanceId"])


@pytest.fixture
def vm(request: pytest.FixtureRequest, rig: config.RigConfig, ec2, instance_id: str):
    """SSH to the test VM at its current address (it keeps its Elastic IP across stops); its
    connect failures in the scenario's notes (`noted`)."""
    from e2e.harness.vm import Vm

    log = noted(_RECORDER, _number(request.node))
    return Vm(ec2.public_ip(instance_id), key=rig.ssh_key, known_hosts=rig.known_hosts, log=log)


async def _user(path: Path, rig: config.RigConfig) -> AsyncIterator:
    """A test user who may post only in the rig's channels (the dev org's `zab-dev*` channels and
    the test participant's) and DM only the rig's bots. A channel it can't see stays closed."""
    from e2e.harness.zulip_user import TestUser
    from zab.zulip.client import ZulipError

    user = TestUser.from_zuliprc(path)
    try:
        channels = []
        for name in (rig.channel, rig.shared_channel):
            try:
                channels.append(await user.stream_id(name))
            except ZulipError:
                pass
        bots = [await user.bot_id(name) for name in rig.bots.values()]
        user.restrict(channels=channels, bots=bots)
        yield user
    finally:
        await user.close()


@pytest.fixture
async def tester(rig: config.RigConfig) -> AsyncIterator:
    """`zab-tester`: the participant, on both bots' allowlists."""
    async for user in _user(rig.tester_zuliprc, rig):
        yield user


@pytest.fixture
async def tester2(rig: config.RigConfig) -> AsyncIterator:
    """`zab-tester2`: in the shared channel, on no allowlist (the negative scenarios)."""
    async for user in _user(rig.tester2_zuliprc, rig):
        yield user


@pytest.fixture
async def bots(rig: config.RigConfig, tester) -> dict[str, int]:
    """Backend kind → the bot's Zulip user id."""
    return {kind: await tester.bot_id(name) for kind, name in rig.bots.items()}


@pytest.fixture
async def stream(rig: config.RigConfig, tester) -> int:
    """The participant's channel: zab-tester is its only human, so (as in every participant's
    sandbox) a follow-up needs no mention. Another human there would make it multi-party."""
    sid = await tester.stream_id(rig.channel)
    humans = await tester.humans(sid)
    if humans != {(await tester.me())["user_id"]}:
        pytest.fail(
            f"#{rig.channel} must have zab-tester as its only human (it has {len(humans)}): "
            "with another, follow-ups without a mention are ignored (multi-party, #35). Put "
            f"zab-tester2 in #{rig.shared_channel} (ZAB_E2E_SHARED_CHANNEL) instead.",
            pytrace=False,
        )
    return sid


@pytest.fixture
async def shared_stream(rig: config.RigConfig, tester) -> int:
    """The channel both testers are in, with the bots (scenario 13)."""
    return await tester.stream_id(rig.shared_channel)


#: This run's reset of the VM's rule for a message without a mention to `all` (#878): whether
#: it succeeded, once tried (`flows.reset_rule`).
_RULE_RESET: list[bool] = []


@pytest.fixture
async def chat(rig: config.RigConfig, tester, bots, stream):
    """`zab-tester` talking to the two bots: `harness/flows.Chat`. Its first use in a run sets
    the VM's rule for a message without a mention to `all`, the default the scenarios expect:
    a run killed in scenario 1's part under `last` (#878) can't leave it to the next run. It is
    tried once: after a failure, each later use fails at once, rather than wait again."""
    from e2e.harness.flows import Chat, reset_rule

    chat = Chat(tester, bots, rig.bots, stream)
    await reset_rule(chat, _RULE_RESET)
    return chat


def new_nonce() -> str:
    return f"{time.strftime('%H%M%S')}-{os.urandom(3).hex()}"


@pytest.fixture
def nonce() -> str:
    """A token unique to this test, for topics and the words the agents are asked to echo; a
    part run again after a refusal (`pytest_runtest_call`) gets another."""
    return new_nonce()
