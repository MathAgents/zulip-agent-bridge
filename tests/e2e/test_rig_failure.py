"""Failures on the rig's side, offline (#877), on a fake clock with a fake Zulip client: a wait
that timed out says how long the rig's connection to Zulip failed; one whose state showed, by
the message's edit history, while the rig couldn't see it is a rig failure, as is a final state
Zulip served an older version of; a part that failed so runs once more, and a second rig
failure is no result, not a FAIL."""

from __future__ import annotations

import calendar
import re

import httpx
import pytest

from e2e.harness import refusal, report, rig_failure
from e2e.harness.flows import DONE
from e2e.harness.recorder import Recorder
from e2e.harness.refusal import Refused
from e2e.harness.rig_failure import Outage, RigFailure, RigTimeout
from e2e.harness.zulip_user import TestUser, topic_narrow
from e2e.test_recorder import inner_pytest

#: Where the fake clock's 0 is on Zulip's: 08:38:00Z
EPOCH = calendar.timegm((2026, 11, 6, 8, 38, 0))
BOT, TESTER, TURN = 77, 10, 100
NARROW = topic_narrow(5, "e2e-12-btw-claude-x")
WORKING = r"^⏳ \*\*Working\*\*.*sleep"
STARTED = "⏳ **Working** · started <time:2026-11-06T08:38:00Z> · 0 actions · `claude`"
SLEEPING = STARTED.replace("0 actions", "1 actions") + "\n- ✅ `sleep 150`"
FINISHED = "✅ **Done** · 3m 15s · 1 actions · `claude`\n- ✅ `sleep 150`"


class Clock:
    """Fake time: `sleep` moves it."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


class Zulip:
    """The calls a wait makes, answered at once on the fake clock. `GET /messages` serves each
    message as it reads at the time, or as `stale` keeps it (the version Zulip went on
    serving); `GET /messages/{id}/history`, its versions so far, in `shape`. A call while the
    clock is in one of the `down` intervals fails with a ConnectError."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.versions: dict[int, list[tuple[float, str]]] = {}
        self.senders: dict[int, int] = {}
        self.stale: dict[int, int] = {}
        self.down: list[tuple[float, float]] = []
        self.histories = 0
        self.shape = lambda history: {"message_history": history}

    def post(self, *versions: tuple[float, str], sender: int = BOT) -> int:
        """A message (the bot's), its versions (fake-clock time, content) oldest first."""
        mid = TURN + 1 + len(self.versions)
        self.versions[mid], self.senders[mid] = list(versions), sender
        return mid

    def message(self, mid: int) -> dict:
        now = [content for at, content in self.versions[mid] if at <= self.clock.now]
        content = now[min(self.stale.get(mid, len(now)), len(now) - 1)]
        return {"id": mid, "sender_id": self.senders[mid], "content": content, "reactions": []}

    async def request(self, method: str, endpoint: str, **params) -> dict:
        assert method == "GET"
        if any(start <= self.clock.now < end for start, end in self.down):
            raise httpx.ConnectError("connection refused")
        if endpoint.endswith("/history"):
            self.histories += 1
            versions = self.versions[int(endpoint.split("/")[1])]
            shown = [(at, content) for at, content in versions if at <= self.clock.now]
            return self.shape([{"content": c, "timestamp": EPOCH + at} for at, c in shown])
        if endpoint.startswith("messages/"):
            return {"message": self.message(int(endpoint.split("/")[1]))}
        assert int(params["anchor"]) == TURN
        posted = [m for m, versions in self.versions.items() if versions[0][0] <= self.clock.now]
        return {"messages": [self.message(m) for m in posted], "found_newest": True}

    async def close(self) -> None:
        pass


def rig() -> tuple[Clock, Zulip, TestUser]:
    clock = Clock()
    zulip = Zulip(clock)
    user = TestUser(zulip, sleep=clock.sleep, clock=clock, wall=lambda: EPOCH + clock.now)
    return clock, zulip, user


async def wait(user: TestUser, pattern: str = WORKING, within: float = 240) -> dict:
    return await user.wait_reply(NARROW, after_id=TURN, senders=[BOT], pattern=pattern,
                                 within=within)  # fmt: skip


async def outage_in_the_middle(pattern: str = WORKING) -> TimeoutError:
    """A wait for 240 s whose calls fail from 60 s to 185 s: at 60, 66, 78, 102 and 150 s (the
    pause doubles from 6 s), then answered at 210 s; the status never matches. Its error."""
    _, zulip, user = rig()
    zulip.post((5, STARTED), (200, "❌ **Failed** · 3m 15s · 0 actions · `claude`"))
    zulip.down = [(60, 185)]
    with pytest.raises(TimeoutError) as caught:
        await wait(user, pattern)
    return caught.value


async def test_a_timeout_says_how_long_the_rigs_connection_failed():
    """Criterion 1: the seconds lost, of the wait's. The status never showed the state, by its
    edit history too: the bridge's failure, not a rig failure."""
    error = await outage_in_the_middle()
    assert not isinstance(error, RigFailure)
    assert str(error) == (
        "the rig's Zulip connection failed for 150 s of the 240 s wait (5 errors, the last "
        f"ConnectError); no reply matching {WORKING!r} within 240 s"
    )


async def test_a_wait_through_an_outage_still_finds_the_status():
    clock, zulip, user = rig()
    zulip.post((5, STARTED), (62, SLEEPING), (195, FINISHED))
    zulip.down = [(60, 100)]
    assert (await wait(user))["content"] == SLEEPING
    assert clock.now == 102 and zulip.histories == 0  # the history only after a timeout


async def test_a_state_shown_while_the_connection_failed_is_the_rigs():
    """Criterion 2, the run of Nov 6 08:06Z: the status read `⏳ **Working** … sleep` from 100 s
    to 195 s, while the rig's calls failed from 60 s until 10 s past the deadline. Its edit
    history, read once the connection is back, says so."""
    clock, zulip, user = rig()
    status = zulip.post((5, STARTED), (100, SLEEPING), (195, FINISHED))
    zulip.down = [(60, 250)]
    with pytest.raises(RigTimeout) as caught:
        await wait(user)
    assert str(caught.value.line) == (
        f"message {status} matched from 08:39:40Z to 08:41:15Z by its edit history, but the rig "
        "never saw it: the rig's Zulip connection failed for 180 s of the 240 s wait (7 errors, "
        f"the last ConnectError), still at its end; no reply matching {WORKING!r} within 240 s"
    )
    assert isinstance(caught.value, TimeoutError) and clock.now == 258  # back at 258 s


async def test_a_state_zulip_did_not_serve_is_the_rigs():
    """Criterion 2, the run of Nov 6 12:24Z: no connection error, but `GET /messages` kept
    serving the status's first version."""
    _, zulip, user = rig()
    status = zulip.post((5, STARTED), (100, SLEEPING), (195, FINISHED))
    zulip.stale[status] = 0
    with pytest.raises(RigTimeout) as caught:
        await wait(user)
    assert caught.value.line.startswith(
        f"message {status} matched from 08:39:40Z to 08:41:15Z by its edit history, but the rig "
        "never saw it: Zulip didn't serve it, with no connection errors; no reply matching"
    )


async def test_the_history_decides_once_it_can_be_read():
    """The connection failed at the deadline, but the history read after it shows no matching
    version before the deadline (the bridge showed it late): the bridge's failure. One never
    read, as the connection never came back, is the rig's: it couldn't look."""
    _, zulip, user = rig()
    zulip.post((5, STARTED), (241, SLEEPING))
    zulip.down = [(200, 250)]
    with pytest.raises(TimeoutError) as caught:
        await wait(user)
    assert not isinstance(caught.value, RigFailure) and zulip.histories == 1
    # failed at 201, 207, 219 and 240 s, the pause cut short by the deadline
    assert str(caught.value).startswith(
        "the rig's Zulip connection failed for 39 s of the 240 s wait (4 errors, the last "
        "ConnectError), still at its end; no reply"
    )
    _, zulip, user = rig()
    zulip.post((5, STARTED), (100, SLEEPING))
    zulip.down = [(60, 10_000)]
    with pytest.raises(RigTimeout, match=r"still at its end, the edit history unread "
                       r"\(ConnectError\): the rig couldn't look; no reply matching"):  # fmt: skip
        await wait(user)


async def test_a_wait_without_a_history_that_could_not_look_is_the_rigs():
    """A reaction has no history: a wait for one whose calls still failed at its end is the
    rig's, as it couldn't look."""
    _, zulip, user = rig()
    mid = zulip.post((0, STARTED))
    zulip.down = [(30, 100)]
    with pytest.raises(RigTimeout) as caught:  # failed at 30, 36, 48 and 60 s
        await user.wait_reaction(mid, emoji_code="21aa", user_id=BOT, within=60)
    assert caught.value.line == (
        "the rig's Zulip connection failed for 30 s of the 60 s wait (4 errors, the last "
        "ConnectError), still at its end: the rig couldn't look; no reaction 21aa within 60 s"
    )


def test_an_outage_counts_up_to_the_waits_end():
    outage = Outage(start=10, within=100)
    outage.failed(20, "ConnectError")
    outage.back(50)
    outage.failed(90, "Zulip E502 (HTTP 502)")
    assert outage.down and outage.text(130).startswith("the rig's Zulip connection failed for 50 s")
    outage.back(140)
    assert outage.text(150) == ("the rig's Zulip connection failed for 50 s of the 100 s wait "
                                "(2 errors, the last Zulip E502 (HTTP 502))")  # fmt: skip
    assert Outage(start=0, within=5).text(9) == ""


async def test_a_final_state_zulip_served_an_older_version_of_is_the_rigs():
    """Criterion 5, message 638119008: `GET /messages` served `⏳ **Working**`, the edit
    history's last entry `✅ **Done**`. Both are in the failure."""
    clock, zulip, user = rig()
    status = zulip.post((5, STARTED), (154, FINISHED))
    clock.now = 600
    await user.settled(await user.message(status), DONE)  # served as it is: no history read
    assert zulip.histories == 0
    zulip.stale[status] = 0
    with pytest.raises(RigFailure) as caught:
        await user.settled(await user.message(status), DONE)
    assert caught.value.line == (
        f"Zulip served message {status} as {STARTED!r} at 08:48:00Z, but its edit history ends "
        f"with {FINISHED!r} from 08:40:34Z"
    )
    stuck = zulip.post((5, STARTED))  # the bridge never finished it: its failure
    with pytest.raises(AssertionError, match=rf"^message {stuck} reads '⏳ \*\*Working\*\*"):
        await user.settled(await user.message(stuck), DONE)
    served = await user.message(status)
    zulip.down = [(600, 10_000)]
    with pytest.raises(AssertionError, match=r"its edit history unread: ConnectError"):
        await user.settled(served, DONE)


@pytest.mark.parametrize("edit_at", [650, 599])
async def test_a_final_edit_made_after_the_read_stays_the_bridges(edit_at):
    """The bridge left the status `⏳ **Working**` after its answer and finished it only after
    the rig read it (650 s, during the history's read), or too close to the read (at 600 s) to
    tell, Zulip's timestamps being whole seconds on another clock: what Zulip served was true,
    so the check fails as the bridge's (#877)."""
    clock, zulip, user = rig()
    status = zulip.post((5, STARTED), (edit_at, FINISHED))
    clock.now = 600
    zulip.stale[status] = 0  # at 599 s, Zulip served the older version, or the edit came after
    read = await user.message(status)
    assert read["content"] == STARTED
    zulip.down = [(600, 700)]  # the history is read at 750 s, after the edit
    with pytest.raises(AssertionError) as caught:
        await user.settled(read, DONE)
    assert not isinstance(caught.value, RigFailure) and zulip.histories == 1
    edited = "08:48:50Z" if edit_at == 650 else "08:47:59Z"
    assert str(caught.value).endswith(f"(its edit history ends with a match from {edited}, not "
                                      "before the read at 08:48:00Z)")  # fmt: skip
    with pytest.raises(AssertionError, match=r"not before the read\)$"):  # a read not stamped
        await user.settled({"id": status, "content": STARTED}, DONE)


def shown(*versions: tuple[float, str]) -> str | None:
    """`rig_failure.shown` for a wait from 0 to 240 s, polling every 3 s."""
    history = [{"content": content, "timestamp": EPOCH + at} for at, content in versions]
    return rig_failure.shown(history, re.compile(WORKING, re.S), since=EPOCH,
                             before=EPOCH + 240, every=3.0)  # fmt: skip


def test_a_version_counts_only_if_it_began_surely_before_the_deadline():
    """Zulip's whole seconds against the rig's clock (`rig_failure.SKEW_S`): a version from
    2 s before the deadline counts, one from 1 s before doesn't."""
    assert shown((5, STARTED), (238, SLEEPING)) == "matched from 08:41:58Z on by its edit history"
    assert shown((5, STARTED), (239, SLEEPING)) is None


def test_a_version_counts_only_if_it_showed_longer_than_the_polls_are_apart():
    """Review B1 of #906: a version that showed for less than the poll interval and `SKEW_S`
    at each end (7 s), between two polls Zulip answered, is no sign the rig missed it; one
    still shown is, and so is a longer one, counted from the wait's start."""
    assert shown((5, STARTED), (100, SLEEPING), (106, FINISHED)) is None
    assert shown((5, STARTED), (100, SLEEPING), (107, FINISHED)) == (
        "matched from 08:39:40Z to 08:39:47Z by its edit history"
    )
    assert shown((-60, SLEEPING), (6, FINISHED)) is None  # 6 s of it in the wait
    assert shown((-60, SLEEPING), (7, FINISHED)) is not None
    later = shown((5, STARTED), (100, SLEEPING), (101, STARTED), (150, SLEEPING), (195, FINISHED))
    assert later == "matched from 08:40:30Z to 08:41:15Z by its edit history"  # a later one


async def test_a_status_shown_between_two_polls_stays_the_bridges():
    """Review B1 of #906: `⏳ **Working** … sleep` for 1 s, then the turn's final status at once
    (a final edit isn't held to the edit gap), with every poll answered: what a `sleep` cut
    short looks like. The bridge's failure, as before #877."""
    _, zulip, user = rig()
    zulip.post((5, STARTED), (100, SLEEPING), (101, FINISHED))
    with pytest.raises(TimeoutError) as caught:
        await wait(user)
    assert not isinstance(caught.value, RigFailure) and zulip.histories == 1
    assert str(caught.value) == f"no reply matching {WORKING!r} within 240 s"


def no_stamps(history: list[dict]) -> dict:
    return {"message_history": [{"content": v["content"]} for v in history]}


async def test_a_history_without_timestamps_leaves_it_to_the_bridge():
    """Review B2a of #906: versions with no `timestamp` are from an unknown time, never before
    a deadline or a read. A status that matched only 1 s after the deadline, and a Done made
    after the read, stay the bridge's failures."""
    for history in (
        [{"content": SLEEPING}],  # and one whose end is unknown
        [{"content": SLEEPING, "timestamp": EPOCH + 100}, {"content": FINISHED}],
    ):
        regex = re.compile(WORKING, re.S)
        assert rig_failure.shown(history, regex, since=EPOCH, before=EPOCH + 240,
                                 every=3.0) is None  # fmt: skip
    _, zulip, user = rig()
    zulip.shape = no_stamps
    zulip.post((5, STARTED), (241, SLEEPING))
    zulip.down = [(200, 245)]  # down at the deadline, back 5 s after it
    with pytest.raises(TimeoutError) as caught:
        await wait(user)
    assert not isinstance(caught.value, RigFailure) and zulip.histories == 1
    clock, zulip, user = rig()
    zulip.shape = no_stamps
    status = zulip.post((5, STARTED), (650, FINISHED))
    clock.now = 600
    read = await user.message(status)
    clock.now = 700
    with pytest.raises(AssertionError) as failed:
        await user.settled(read, DONE)
    assert not isinstance(failed.value, RigFailure)
    assert "a match from an unknown time, not before the read at 08:48:00Z" in str(failed.value)


async def test_a_history_unread_once_zulip_answers_again_leaves_it_to_the_bridge():
    """Review B2b of #906: down at the deadline, back 5 s after it, and the history answered in
    a shape the rig can't read: Zulip was reached, so the rig could look. The bridge's."""
    _, zulip, user = rig()
    zulip.shape = lambda history: {}
    zulip.post((5, STARTED))  # never matches
    zulip.down = [(230, 245)]
    with pytest.raises(TimeoutError) as caught:
        await wait(user)
    assert not isinstance(caught.value, RigFailure)
    assert "still at its end, the edit history unread (KeyError); no reply matching" in str(
        caught.value
    )


async def test_only_the_awaited_senders_messages_are_read_for_what_was_missed():
    """Review N1 of #906: the test user's own prompt quotes what it asks for; its versions are
    never the bridge's."""
    _, zulip, user = rig()
    zulip.post((1, f"Run `sleep 150`; your status will read {WORKING}"), sender=TESTER)
    zulip.post((5, STARTED))
    with pytest.raises(TimeoutError) as caught:
        await wait(user, r"Run `sleep")
    assert not isinstance(caught.value, RigFailure) and zulip.histories == 1


def test_a_rig_failure_runs_the_part_again():
    """Criterion 3, the retry: after `rig_failure.WAIT_S`, with fresh inputs; a second rig
    failure is raised again, marked; a refusal after it is its own (#842 fails it)."""
    row, calls, slept = Recorder().row(12), [], []

    def run(outcome: Exception | None):
        def part() -> None:
            calls.append("run")
            if outcome is not None:
                raise outcome

        return part

    first = RigTimeout("the rig couldn't look")
    refusal.retry("test_btw[claude]", first, row, kept=0, noted=0, budget=refusal.Budget(),
                  run=run(None), renew=lambda: calls.append("renew"),
                  sleep=slept.append)  # fmt: skip
    assert calls == ["renew", "run"] and slept == [30.0]
    assert row.external == [
        "test_btw[claude]: the rig couldn't look; the part ran again after 30 s"
    ]
    with pytest.raises(RigFailure) as caught:
        refusal.retry("p", first, row, kept=0, noted=0, budget=refusal.Budget(),
                      run=run(RigFailure("stale")), sleep=slept.append)  # fmt: skip
    assert caught.value.why == refusal.AGAIN
    assert row.external[-1] == "p: a rig failure again in its second run: stale"
    with pytest.raises(Refused) as refused:
        refusal.retry("q", first, row, kept=0, noted=0, budget=refusal.Budget(),
                      run=run(Refused("429")), sleep=slept.append)  # fmt: skip
    assert refused.value.why == refusal.SECOND and not refused.value.twice
    assert row.external[-1] == "q: refused in its second run: 429"
    runs = len(calls)
    with pytest.raises(RigFailure, match="retries are spent"):  # the hook: no result, at once
        refusal.retry("r", RigFailure("x"), row, kept=0, noted=0, budget=refusal.Budget(0),
                      run=run(None), sleep=slept.append)  # fmt: skip
    assert len(calls) == runs and len(slept) == 3


INNER = """
import asyncio

import pytest
from e2e.harness import refusal, rig_failure
from e2e.harness.flows import answer
from e2e.test_rig_failure import outage_in_the_middle

pytestmark = pytest.mark.e2e
refusal.WAIT_S = rig_failure.WAIT_S = 0.0


@pytest.mark.scenario(1)
@pytest.mark.parametrize("n", (1, 2, 3))
def test_basics(n, nonce):  # each refused once: the run's 3 retries for a 429 are spent
    with open(f"refused-{n}", "a") as f:
        f.write(nonce + " ")
    if len(open(f"refused-{n}").read().split()) == 1:
        raise refusal.Refused("429 Too Many Requests")


@pytest.mark.scenario(5)
@pytest.mark.parametrize("kind", ("claude", "codex"))
def test_steering(kind, scenario, nonce):
    with open(f"nonces-{kind}", "a") as f:  # in the run's directory
        f.write(nonce + " ")
    scenario.notes.append(f"{kind}: a note")
    if kind == "claude" and len(open("nonces-claude").read().split()) == 1:
        raise rig_failure.RigTimeout("the rig couldn't look")


@pytest.mark.scenario(12)
def test_btw(scenario):
    raise rig_failure.RigFailure("Zulip served an older version")


@pytest.mark.scenario(13)
def test_negative():
    raise asyncio.run(outage_in_the_middle(answer("DONE-084500-abcdef")))
"""


def test_the_suite_runs_a_rig_failure_again_and_a_second_is_no_result(tmp_path):
    """Through the e2e conftest's hook, in a pytest of its own: 5's Claude part passes on its
    second run, with another nonce, on the rig's budget though 1's refusals spent the 429's
    (review N1 of #906); 12's part fails on the rig's side twice: no result, not a FAIL. 13's
    timeout, the bridge's, fails, and its row says how long the connection failed, ahead of
    the long pattern of an answer."""
    done = inner_pytest(tmp_path, INNER)
    assert done.returncode == 1, done.stdout[-2000:] + done.stderr[-2000:]
    nonces = (tmp_path / "nonces-claude").read_text().split()
    assert len(nonces) == 2 and nonces[0] != nonces[1]
    ran = report.Report.read(tmp_path / "r.json")
    rows = {r.number: r for r in ran.results}
    assert rows[1].complete and len(rows[1].external) == 3
    assert rows[5].effective == "passed" and rows[5].complete
    ran_again = "test_steering[claude]: the rig couldn't look; the part ran again after 0 s"
    assert rows[5].external == [ran_again]
    assert rows[5].notes == ["first run: claude: a note", "claude: a note", "codex: a note"]
    assert rows[12].effective == "skipped" and rows[12].skipped_parts == ["test_btw"]
    assert rows[12].reason == (
        "test_btw: no result: a rig failure, not the bridge's (#877), in the part's second run "
        "too: Zulip served an older version"
    )
    again = "test_btw: a rig failure again in its second run: Zulip served an older version"
    assert rows[12].external[1] == again
    assert report.Report(results=[rows[5], rows[12]]).verdict == "NO-RESULT"
    assert rows[13].effective == "failed" and rows[13].reason.startswith(
        "test_negative: TimeoutError: the rig's Zulip connection failed for 150 s of the 240 s "
        "wait (5 errors, the last ConnectError); no reply matching"
    )
    assert "within 240 s" not in rows[13].reason  # the line was cut in the answer's pattern
    summary = ran.markdown()
    assert "| 5 | Steering | ✅ pass · ⚠️ external |" in summary
    assert "| 12 | zab history (#248), !fork (#202) and !btw (#716) | ⏭ skip · ⚠️ external |" in (
        summary
    )
    assert f"external cause: {ran_again}" in summary
    assert re.search(r"- \*\*12\. .*\*\* \(skipped: test_btw\): test_btw: no result", summary)
