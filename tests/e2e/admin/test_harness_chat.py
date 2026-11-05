"""Reading the admin bots, offline (issue #356): the parsers against messages that the bot's own
code renders (`zab.admin.planning`), and the request-approve-result flow against a fake user."""

from __future__ import annotations

import json
import re

import pytest

from e2e.admin.harness.chat import (
    ACK_EVERY,
    ACK_S,
    Refused,
    Talk,
    Unchanged,
    heads,
    parse_plan,
    result_of,
    shows,
    status_rows,
    unchanged_of,
    whole,
    widget_replies,
)
from zab.admin import planning
from zab.admin.actions import TARGETS, Action, Step
from zab.admin.audit import Head
from zab.admin.plans import Plan, Requirement
from zab.admin.scrub import Scrubber

DIGEST = "3f9a2c" + "ab" * 29
#: What the runner posts when an approved plan's hash changed (`Runner._apply`, #354).
STALE = (
    "❌ P-42 is stale (hash now 0123456789ab; since the plan: roster.csv changed): nothing was "
    "done. Request again."
)


def status_answer(names) -> str:
    """`status.py --json` as the plan's status read prints it: zab-test-b has a turn running,
    and the VMs after the first two run a zab that can't say (null)."""
    vms = [{"slug": name, "bridge": "active", "zab_version": "0.2.4rc1",
            "idle_stop": {"enabled": False, "active": False, "minutes": None},
            "running_turns": [{"backend": "codex"}] if name == "zab-test-b" else [] if n < 2
            else None} for n, name in enumerate(names)]  # fmt: skip
    return json.dumps({"schema": 1, "vms": vms, "summary": {}}, indent=2) + "\n"


def rendered(command: str, needs: Requirement, *, limit: int = 10000, n: int = 2):
    names = ("zab-test-a", "zab-test-b", *(f"zab-test-{i:02d}" for i in range(n - 2)))
    argv = ("/opt/zab-admin/current/.venv/bin/python", "-E", "-s", "power.py", "--stop")
    argv += tuple(w for name in names for w in ("--only", name))
    action = Action("stop", "zab-admin-e2e", 21, ("all",), (Step(argv, (*argv, "--dry-run")),),
                    TARGETS, names, (), status=("status.py", "--json"))  # fmt: skip
    plan = Plan(42, "zab-admin-e2e", 21, 11, command, "planning", 0.0, None, (), "", None, None,
                None, "", None)  # fmt: skip
    read = planning.StatusRead.of(status_answer(names), Scrubber())
    return planning.plan_message(plan, action, DIGEST, needs, requester="@_**Zab Tester|11**",
                                 expires=0.0, output="zab-test-a: would stop\n",
                                 head=Head(7, "c" * 64), limit=limit, status=read)  # fmt: skip


def test_a_plan_as_the_bot_renders_it():
    message = rendered("!stop all", Requirement("above-half", 1, ("stop", "2")))
    plan = parse_plan(message.text, 99)
    assert plan is not None and message.full is None
    assert (plan.id, plan.workshop, plan.command) == ("P-42", "zab-admin-e2e", "!stop all")
    assert plan.approve == "!approve P-42 3f9a2c stop 2" and plan.phrase == "stop 2"
    assert plan.hash6 == "3f9a2c" and plan.message_id == 99
    assert plan.targets == ["zab-test-a", "zab-test-b"]
    assert len(plan.commands) == 1 and plan.commands[0].endswith(
        "--only zab-test-a --only zab-test-b"
    )
    assert status_rows(plan.text) == {
        "zab-test-a": {"state": "active", "zab": "0.2.4rc1", "idle stop": "off",
                       "running turns": "0"},
        "zab-test-b": {"state": "active", "zab": "0.2.4rc1", "idle stop": "off",
                       "running turns": "1 codex"},
    }  # fmt: skip
    assert heads(plan.text) == [(7, "c" * 12)]


def test_a_plan_without_a_phrase_and_one_cut_to_fit():
    plan = parse_plan(rendered("!stop zab-test-b", Requirement("change")).text)
    assert plan is not None and plan.phrase == "" and plan.approve == "!approve P-42 3f9a2c"
    message = rendered("!stop all", Requirement("change"), limit=1500, n=60)
    assert message.full is not None  # cut: the bot attaches the whole plan
    cut = parse_plan(message.text)
    assert cut is not None and 0 < len(cut.targets) < 60  # the message's own list, cut
    assert not any("…" in t or "more" in t for t in cut.targets)  # `, … and 51 more` dropped
    whole_plan = parse_plan(message.text + "\n" + message.full)
    assert whole_plan is not None and whole_plan.commands[0].endswith("--only zab-test-57")
    assert len(whole_plan.targets) == 60 and whole_plan.targets[-1] == "zab-test-57"
    assert len(status_rows(message.text)) < 60  # the message's table, cut
    rows = status_rows(whole_plan.text)  # completed by the attachment's
    assert len(rows) == 60 and rows["zab-test-57"]["running turns"] == "?"


def test_a_plan_whose_approval_names_another_plan_is_none():
    text = rendered("!stop all", Requirement("change")).text
    assert parse_plan(text.replace("!approve P-42 ", "!approve P-41 ")) is None


def test_approval_messages_and_results():
    text, widget = planning.approval_message(42, DIGEST, Requirement("change"), "!stop zab-test-b")
    assert widget["extra_data"]["choices"][0]["reply"] == "!approve P-42 3f9a2c"
    assert parse_plan(text) is None  # the approval message is not the plan
    assert result_of("✅ **P-42** · `!launch all`: done in 3 min 2 s, every step exited 0") == (
        "✅",
        "P-42",
    )
    assert result_of("⏱ **P-7** · `!rollout v1.0.0 all`: timed out after 30 min") == ("⏱", "P-7")
    assert result_of("✅ P-42 approved") is None


class FakeUser:
    """A TestUser whose bot answers from a script: `answers[text]` is what follows a post."""

    def __init__(self, answers):
        self.answers, self.posted, self.ids = answers, [], 100
        self.client = FakeClient()

    async def send(self, stream, topic, content):
        self.ids += 1
        self.posted.append(content)
        self.last = (self.ids, content)
        return self.ids

    async def wait_reply(self, narrow, *, after_id, senders, pattern, within):
        for message in self.answers.get(self.last[1], []):
            if re.search(pattern, message["content"], re.S):
                return {"id": after_id + 1, "sender_id": 5, **message}
        raise TimeoutError(f"no reply matching {pattern!r}")


class FakeClient:
    async def request(self, method, endpoint, **params):
        assert endpoint == "user_uploads/2/ab/P-42-plan.txt"
        return {"url": "/user_uploads/temporary/xyz"}

    async def download(self, url, limit):
        assert url == "/user_uploads/temporary/xyz"
        return b"the whole plan", "text/plain"


def test_a_retire_waits_for_its_own_deadline():
    """Codex on #566: a `!retire`'s apply deadline grows with its rows and `export`, as
    `actions._build_retire` sets it; the others keep their verb's."""
    from e2e.admin.harness.chat import POST_MARGIN_S, apply_s
    from e2e.admin.harness.chat import Plan as Posted
    from zab.admin.actions import EXPORT_ROW_S, RETIRE_FLOOR_S, RETIRE_ROW_S

    def plan(command):
        return Posted("P-1", "w", command, "!approve P-1 3f9a2c", "3f9a2c", "")

    assert apply_s(plan("!retire a b export")) == (
        max(RETIRE_FLOOR_S, 2 * RETIRE_ROW_S) + 2 * EXPORT_ROW_S + POST_MARGIN_S
    )
    assert apply_s(plan("!retire a")) == RETIRE_FLOOR_S + POST_MARGIN_S
    rows = " ".join(f"r{n}" for n in range(8))
    assert apply_s(plan(f"!retire {rows} unsynced")) == 8 * RETIRE_ROW_S + POST_MARGIN_S
    assert apply_s(plan("!teardown a")) == 1900.0 and apply_s(plan("!stop a")) == 1000.0


async def test_request_approve_and_result():
    plan_text = rendered("!stop zab-test-b", Requirement("change")).text
    approval, _ = planning.approval_message(42, DIGEST, Requirement("change"), "!stop zab-test-b")
    user = FakeUser({
        "!stop zab-test-b": [{"content": plan_text}, {"content": approval}],
        "!approve P-42 3f9a2c": [
            {"content": "✅ P-42 approved\n`audit #8 · chain cccccccccccc`"},
            {"content": "✅ **P-42** · `!stop zab-test-b`: done in 12 s, every step exited 0"},
        ],
        "!stop all": [{"content": "❌ P-43 refused: the dry run failed; nothing was done"}],
        "!approve P-42 3f9a2c stale": [
            {"content": "✅ P-42 approved"},
            {"content": STALE},
        ],
        "!approve P-42 3f9a2c busy": [
            {"content": "busy: P-41 is applying. Nothing was recorded: send the approval again "
                        "once it is done, before P-42 expires."},
            {"content": "✅ **P-42** · `!stop zab-test-b`: done in 12 s"},
        ],
        "!approve P-42 3f9a2c failed": [
            {"content": "✅ P-42 approved"},
            {"content": "❌ **P-42** · `!stop zab-test-b`: failed at step 1 of 1: power.py "
                        "exited 1. Nothing after it ran"},
        ],
    })  # fmt: skip
    t = Talk(user, 5, 31, "e2e-A7")
    plan, text = await t.run("!stop zab-test-b")
    assert plan.id == "P-42" and text.startswith("✅ **P-42**")
    assert plan.answered == "✅ P-42 approved"  # its first line, without the footer
    with pytest.raises(Refused, match="dry run failed"):
        await t.request("!stop all")
    _, stale = await t.approve(plan, "!approve P-42 3f9a2c stale")
    assert stale == STALE
    with pytest.raises(Refused, match="busy"):  # never taken for an approval
        await t.approve(plan, "!approve P-42 3f9a2c busy")
    user.answers["!approve P-42 3f9a2c"] = user.answers["!approve P-42 3f9a2c failed"]
    with pytest.raises(AssertionError, match="failed at step 1"):  # run() wants every step done
        await t.run("!stop zab-test-b")


#: The bot's answer to a `!roster sync` with nothing to change (#708), as `Planning._unchanged`
#: posts it: its title, the dry run's block, the footer.
NOTHING_ANSWER = "\n".join([
    planning.unchanged_title(
        Plan(76, "zab-admin-e2e2", 22, 12, "!roster sync", "unchanged", 0.0, None, (), "", None,
             None, None, "", None),
        "nothing to change, as its dry run says",
    ),
    "```text",
    "── admin/roster_sync.py",
    "roster sync: 0 added, 0 renamed, 0 removed, 0 left the channel, 0 excluded, 1 unchanged",
    "nothing to change: every subscriber has a row, and every row is theirs",
    "── admin/roster_sync.py exited 3",
    "```",
    "`audit #9 · chain cccccccccccc`",
])  # fmt: skip


def test_nothing_to_change_as_the_bot_renders_it():
    assert NOTHING_ANSWER.startswith(
        "☑ **P-76** · `!roster sync`: nothing to change, as its dry run says; no plan to approve\n"
    )
    assert unchanged_of(NOTHING_ANSWER) == "P-76"
    assert heads(NOTHING_ANSWER) == [(9, "c" * 12)]  # A12's `audit verify --head` takes it too
    assert parse_plan(NOTHING_ANSWER) is None and result_of(NOTHING_ANSWER) is None
    refused = "❌ P-76 refused: nothing to change, as its dry run says; nothing was done"
    assert unchanged_of(refused) is None  # before #708
    assert unchanged_of("✅ **P-76** · `!roster sync`: done in 3 s, every step exited 0") is None


async def test_nothing_to_change_is_neither_a_plan_nor_a_refusal():
    """`Talk.unchanged` wants `☑`; `Talk.request` raises `Unchanged` on it, never `Refused`, so a
    scenario that wants a plan or a refusal fails on it."""
    plan_text = rendered("!stop zab-test-b", Requirement("change")).text
    approval, _ = planning.approval_message(42, DIGEST, Requirement("change"), "!stop zab-test-b")
    user = FakeUser({
        "!roster sync": [{"content": NOTHING_ANSWER}],
        "!stop zab-test-b": [{"content": plan_text}, {"content": approval}],
        "!stop all": [{"content": "❌ P-43 refused: the dry run failed; nothing was done"}],
    })  # fmt: skip
    t = Talk(user, 5, 31, "e2e-A22")
    assert await t.unchanged("!roster sync") == ("P-76", NOTHING_ANSWER)
    with pytest.raises(Unchanged, match="nothing to change") as caught:
        await t.request("!roster sync")
    assert not isinstance(caught.value, Refused)
    for command, shown in (("!stop zab-test-b", r"\*\*P-42\*\*"), ("!stop all", "❌ P-43")):
        with pytest.raises(AssertionError, match=f"not answered as nothing to change: {shown}"):
            await t.unchanged(command)


async def test_a_phrase_plan_is_requested_and_approved_from_its_text():
    """#542: a plan with a phrase has an approval message with no widget, whose text shows the
    line to send by itself; `request` finds it as before, and checks that it shows the plan's
    approve command; an approval message that shows another is an AssertionError."""
    needs = Requirement("above-half", 1, ("stop", "2"))
    plan_text = rendered("!stop all", needs).text
    approval, widget = planning.approval_message(42, DIGEST, needs, "!stop all")
    assert widget is None and "\n\n```text\n!approve P-42 3f9a2c stop 2\n```\n\n" in approval
    assert shows(approval, "!approve P-42 3f9a2c stop 2")
    assert not shows(approval, "!approve P-42 3f9a2c")  # the exact line, not a prefix of it
    assert not shows(approval, "!approve P-42 3f9a2c stop")
    user = FakeUser({
        "!stop all": [{"content": plan_text}, {"content": approval}],
        "!approve P-42 3f9a2c stop 2": [
            {"content": "✅ P-42 approved"},
            {"content": "✅ **P-42** · `!stop all`: done in 12 s, every step exited 0"},
        ],
    })  # fmt: skip
    t = Talk(user, 5, 31, "e2e-A7")
    plan, text = await t.run("!stop all")
    assert plan.phrase == "stop 2" and plan.approval_id > 0 and text.startswith("✅ **P-42**")
    assert user.posted[-1] == "!approve P-42 3f9a2c stop 2"
    other = approval.replace("3f9a2c stop 2", "3f9a2c stop 3")
    user.answers["!stop all"] = [{"content": plan_text}, {"content": other}]
    with pytest.raises(AssertionError, match="doesn't show `!approve P-42 3f9a2c stop 2`"):
        await t.request("!stop all")


async def test_approval_reads_the_message_with_its_widget():
    """`Talk.approval`: the approval message as GET /messages lists it, submessages included
    (A1 reads both buttons there, A9 none)."""
    _, widget = planning.approval_message(42, DIGEST, Requirement("change"), "!stop zab-test-b")
    sub = {"msg_type": "widget", "content": json.dumps(widget)}
    listed = [{"id": 11, "content": "the plan"}, {"id": 12, "content": "", "submessages": [sub]}]

    class Lister:
        async def after(self, narrow, after_id):
            return [m for m in listed if m["id"] > after_id]

    plan = parse_plan(rendered("!stop zab-test-b", Requirement("change")).text, 11)
    assert plan is not None
    plan.approval_id = 12
    t = Talk(Lister(), 5, 31, "e2e-A1")
    assert widget_replies(await t.approval(plan)) == ["!approve P-42 3f9a2c", "!reject P-42"]
    plan.approval_id = 13
    with pytest.raises(AssertionError, match="not found"):
        await t.approval(plan)


async def test_request_waits_for_the_approval_message():
    """An approval before the bot's approval message is refused (`early`, §2.5): the plan is
    only pending once that message is there (reviewer on #368)."""
    plan_text = rendered("!stop zab-test-b", Requirement("change")).text
    t = Talk(FakeUser({"!stop zab-test-b": [{"content": plan_text}]}), 5, 31, "e2e-A7")
    with pytest.raises(TimeoutError, match="approve"):
        await t.request("!stop zab-test-b")


def test_a_status_answer_as_the_bot_renders_it():
    """#439: `!status`'s table, by slug, and one VM's last error; cut, the attachment's whole."""
    from e2e.admin.harness.chat import STATUS_ANSWER, status_of

    def answer(vms, limit=10000):
        doc = json.dumps({"schema": 1, "vms": vms, "summary": {}}, indent=2)
        return planning.status_reply(doc, Scrubber(), limit=limit)

    error = "ssh: connect to host 198.51.100.9 port 22: Connection timed out"
    one = answer([{"slug": "zab-test-b", "bridge": "unreachable", "last_error": error}]).text
    assert re.search(STATUS_ANSWER, one, re.S)
    unknown = {"zab": "?", "idle stop": "?", "running turns": "?"}
    assert status_of(one) == {
        "zab-test-b": {"state": "unreachable", **unknown, "last error": error}
    }
    vms = [{"slug": f"zab-test-{n:02d}", "bridge": "stopped", "last_error": error}
           for n in range(50)]  # fmt: skip
    cut = answer(vms, limit=4000)  # the line cap cuts it, not the room (#764)
    assert cut.full is not None and len(status_of(cut.text)) == planning.STATUS_LINES - 1
    rows = status_of(cut.text + "\n" + cut.full)
    assert len(rows) == 50 and rows["zab-test-49"] == {"state": "stopped", **unknown}


async def test_whole_fetches_the_attachment():
    user = FakeUser({})
    link = "[P-42-plan.txt](/user_uploads/2/ab/P-42-plan.txt)"
    message = {"content": f"*Cut to fit. The whole plan: {link}.*"}
    assert (await whole(user, message)).endswith("\nthe whole plan")


class Reacting:
    """A TestUser whose message 7 gets the bot's 👀 at its third look, and another user's ⚠
    from the start; on a fake clock, which `sleep` moves."""

    def __init__(self):
        self.now, self.looks = 0.0, 0

    def clock(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds

    async def retrying(self, call, *, until, what):
        return await call()

    async def message(self, message_id):
        self.looks += 1
        reactions = [{"emoji_code": "26a0", "user_id": 11}]
        if message_id == 7 and self.looks >= 3:
            reactions.append({"emoji_code": "1f440", "user_id": 5})
        return {"id": message_id, "reactions": reactions}


async def test_the_bots_acknowledgement_is_timed_from_the_send():
    """#466: `acknowledged` polls the message every half second for the bot's own 👀, and times
    out naming it; `reacted` lists only the bot's reactions."""
    user = Reacting()
    t = Talk(user, 5, 31, "e2e-A1")
    assert await t.acknowledged(7) == 2 * ACK_EVERY and user.looks == 3
    assert await t.reacted(7) == ["1f440"] and await t.reacted(8) == []
    with pytest.raises(TimeoutError, match="no 👀 from the bot on 8 within 5 s"):
        await t.acknowledged(8)
    assert ACK_S <= user.now - 2 * ACK_EVERY < ACK_S + ACK_EVERY
