"""docs/ORGANIZERS.md quotes the product (#389, and the review of #390).

The runbook is for organizers who will type what it shows and match what the bot answers. So:

- **Commands.** Each code span that starts with `!` parses with the bot's own grammar
  (`zab.admin.grammar.parse`), against a roster of the slugs the examples use, and the command
  reference lists every usage of `grammar.SPECS`, word for word.
- **Answers.** The bot's answers are rendered by the product, then compared, whole, with the
  runbook's code spans (`Quotes`): a conversation with the real bot, planner and runner
  (`botkit`, `Runner`, scripts faked as `test_admin_runner` fakes them); the plans of
  `push_config.py` and `power.py`, from the scripts' own dry runs (`test_admin_plan_fit`); and
  the runner's progress and result lines and the 5-minute guard's texts, from their own
  functions. Plan IDs, hashes, mentions and the audit footer are normalized on both sides; in a
  span, `…` and a `<placeholder>` stand for any text.
- **Both ways.** Every rendered answer the runbook should quote matches a span; every span that
  quotes an answer (it starts with ✅, ❌, ⚠, …) matches a rendered answer, except those in
  `UNPINNED`, each with the reason.
"""

import asyncio
import calendar
import itertools
import json
import re
import sys
import time
import zipfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from zab.admin import runner as runner_mod
from zab.admin.actions import PROTECT_SETTLE, Ending, Protection, protect_applying
from zab.admin.approvals import DESTRUCTIVE_VERBS, PHRASE_ABOVE_HALF
from zab.admin.bot import ACK
from zab.admin.fleet import REINSTALL, TOUCH
from zab.admin.grammar import _GROUPS, LATER, SPECS, Command, Context, Roster, parse
from zab.admin.planning import STATUS_COLUMNS, Planning, left_out
from zab.admin.plans import APPLYING, PENDING, PLANNING, REFUSED, Plan, Requirement
from zab.admin.runner import TIMEOUT, TIMEOUTS, Ran, Runner
from zab.admin.wakerdown import down_line, down_text, up_text
from zab.waker.service import STARTING

from .botkit import (
    CODEX_BOB,
    DREW,
    HARVARD,
    HARVARD_CH,
    OLGA,
    OSCAR,
    PAT,
    TEST,
    TEST_CH,
    incoming,
    make_site,
    until,
)
from .test_admin_plan_fit import POWER, SRC_CONFIG, Ec2, ScriptRunner, fleet_files, plan_of
from .test_admin_runner import RETAGGED, adopt_spare, calls, fake

DOC = Path(__file__).resolve().parents[2] / "docs" / "ORGANIZERS.md"
BOT = "Workshop admin · harvard-2026"
PRODUCTION = re.compile(r"^v\d+\.\d+\.\d+$", re.ASCII)  # admin.toml's release = "final"
ROSTER = Roster(("alice", "bob", "carol", "dave"), ("zab-spare-1", "zab-spare-2"))
CTX = Context(901, BOT, PRODUCTION, ROSTER)
UPLOAD = " [roster.csv](/user_uploads/2/ab/xyzXYZ_-12/roster.csv)"
#: Commands of the participants' own bots, which the runbook names.
PARTICIPANT = frozenset({"!login", "!tools", "!vm", "!restart", "!restart codex"})
#: A command named by its verb alone, as prose does: `!teardown`, `!roster import`, `!waker on`;
#: and `!` itself.
NAMES = frozenset(
    ["!", *("!" + name for name in (*SPECS, *_GROUPS, *LATER))]
    + [f"!{verb} {on}" for verb in ("protect", "waker") for on in ("on", "off")]
)
_RUN = re.compile(r"`+")
#: What starts a quoted answer of the bot.
MARKS = ("✅", "❌", "⚠", "⏳", "⏱", "⌛", "⏹", "🚫", "busy:")
#: Quoted answers that no rendering here reaches, and why.
UNPINNED = {
    "⚠ **P-42**: the encrypted backup after it failed: …. The apply is unaffected; a deployer "
    "should look (docs/ADMIN.md §1.7).": "needs an apply that changes ws/; test_admin_backup",
}


def spans(text: str) -> list[str]:
    """The inline code spans of `text` (CommonMark: a run of n backticks to the next run of
    exactly n), outside fenced blocks, with a table cell's `\\|` read as `|`."""
    out: list[str] = []
    prose = "\n".join(line for line in _unfenced(text.split("\n")) if line is not None)
    pos = 0
    while (opening := _RUN.search(prose, pos)) is not None:
        n = len(opening.group())
        closing = next(
            (m for m in _RUN.finditer(prose, opening.end()) if len(m.group()) == n), None
        )
        if closing is None:
            pos = opening.end()
            continue
        body = prose[opening.end() : closing.start()].replace("\n", " ")
        if len(body) > 2 and body.startswith(" ") and body.endswith(" "):
            body = body[1:-1]
        out.append(body.replace("\\|", "|"))
        pos = closing.end()
    return out


def _unfenced(lines: list[str]) -> list[str | None]:
    """The lines outside fenced code blocks; None for those inside (and the fences)."""
    inside, out = False, list[str | None]()
    for line in lines:
        if line.lstrip().startswith("```"):
            inside = not inside
            out.append(None)
        else:
            out.append(None if inside else line)
    return out


TEXT = DOC.read_text(encoding="utf-8")
SPANS = spans(TEXT)
USAGES = {spec.usage for spec in SPECS.values()}


# ---------------------------------------------------------------------------------- commands


def test_the_runbooks_nothing_to_change_answers_are_the_bots() -> None:
    """#708: each `☑` answer the runbook quotes is `planning.unchanged_title`'s, for its plan
    and its command as the grammar normalizes it."""
    from zab.admin.planning import unchanged_title
    from zab.admin.plans import UNCHANGED
    from zab.admin.runner import NOTHING_TO_CHANGE

    quoted = [s for s in SPANS if s.startswith("☑ ")]
    assert len(quoted) >= 4, quoted
    for span in quoted:
        found = re.fullmatch(r"☑ \*\*P-(\d+)\*\* · `([^`]+)`: .*", span)
        assert found, span
        plan = Plan(int(found[1]), "harvard-2026", HARVARD, OLGA, found[2], UNCHANGED, 0.0,
                    None, (), "", None, None, None, "", None)  # fmt: skip
        assert span == unchanged_title(plan, NOTHING_TO_CHANGE), span
        upload = UPLOAD if found[2] == "!roster import" else ""
        parsed = parse(found[2] + upload, CTX)
        assert isinstance(parsed, Command) and parsed.text() == found[2], span


def test_the_doc_has_commands_to_check() -> None:
    assert len([s for s in SPANS if s.startswith("!")]) > 100


def test_every_command_in_the_runbook_parses_with_the_bots_grammar() -> None:
    bad = []
    for span in SPANS:
        if span.startswith(f"@**{BOT}** !"):
            command = span
        elif span.startswith("!") and span not in USAGES | NAMES | PARTICIPANT:
            command = span + UPLOAD if span.startswith("!roster import") else span
        else:
            continue
        result = parse(command, CTX)
        if not isinstance(result, Command):
            bad.append(f"{span!r}: {getattr(result, 'reason', getattr(result, 'why', ''))}")
    assert not bad, "\n".join(bad)


def test_the_command_reference_lists_every_usage_word_for_word() -> None:
    missing = sorted(USAGES - set(SPANS))
    assert not missing, missing


def test_the_rules_the_runbook_states_are_the_bots() -> None:
    """The phrase and two-approver classes (§1.2), the time limits (§2.4), the wait after a
    `!protect` (§5.4) and the plans that make a row's sync forgotten (§5.1)."""
    assert PHRASE_ABOVE_HALF == {"stop", "rollout", "idle-stop", "uninstall"}
    assert "| `!stop`, `!rollout`, `!idle-stop` or `!uninstall` of more than half the" in TEXT
    assert DESTRUCTIVE_VERBS == {"teardown", "deactivate-bots", "retire"}
    assert (
        "| `!retire`, `!teardown` or `!deactivate-bots` | 2 different people, or 1 global admin |"
        in TEXT
    )
    assert (TIMEOUT, TIMEOUTS) == (900, {"launch": 3600} | dict.fromkeys(
        ("rollout", "final-sync", "export", "teardown"), 1800))  # fmt: skip
    assert (
        "The limits are 60 minutes for `!launch`; 30 minutes for `!rollout`, `!final-sync`, "
        "`!export` and `!teardown`; for `!retire`, 60 minutes, or 20 minutes a row above three, "
        "and 55 minutes more a row with `export` (§5.3); 15 minutes for anything else." in TEXT
    )
    from zab.admin.actions import EXPORT_ROW_S, RETIRE_FLOOR_S, RETIRE_ROW_S

    assert (RETIRE_FLOOR_S, RETIRE_ROW_S, EXPORT_ROW_S) == (3600, 1200, 3300)  # the review of #566
    assert "The apply stops at 60 minutes, or 20 minutes a row above three, and 55" in TEXT
    assert PROTECT_SETTLE == 300 and "for 5 minutes:" in TEXT
    assert TOUCH == {"launch", "adopt-spare", "start", "rollout", "final-sync", "retire"}
    assert REINSTALL == {"launch", "adopt-spare", "rollout"}
    assert (
        "- `!launch`, `!adopt-spare`, `!start`, `!rollout`, `!final-sync` or `!retire` makes the "
        "bot forget the row's sync;\n- `!launch`, `!adopt-spare` and `!rollout`, which "
        "bring" in TEXT
    )
    assert all(column in SPANS for column in STATUS_COLUMNS)  # the plan's status table (§2.2)


def status_examples() -> list[str]:
    """§3.6's `!status alice` and `!status all`, rendered by the product (#439) from rows of
    status.py's own (`fleet_json`): alice runs a Claude turn, bob's allowlist waits for his
    Zulip ID, carol is stopped (EC2's state alone), dave's memory and disk are under strain and
    his sentinel is down (#588)."""
    from zab.admin.planning import status_reply
    from zab.admin.scrub import Scrubber

    from .test_admin_plan_fit import STATUS

    def vm(slug, bridge, logins, **kw):
        row = STATUS.VmStatus(slug=slug, host="198.51.100.1", bridge=bridge, zab_version="0.2.15")
        row.codex_login, row.claude_login = logins
        row.sentinel = "ok"  # #588
        row.idle_stop = {"enabled": True, "active": True, "minutes": 60, "last_verdict": None}
        row.running_turns, row.active_sessions = [], 0
        row.metrics = {"load1": 0.4, "cpus": 8, "mem_total_kib": 32_594_412,
                       "mem_available_kib": 29_557_000, "disk_percent": 23}  # fmt: skip
        for key, value in kw.items():
            setattr(row, key, value)
        return row

    at = calendar.timegm((2026, 10, 27, 14, 5, 0))
    turn = {"bot": "claude", "backend": "claude", "started_at": "2026-10-27T13:58:00Z"}
    verdict = "2026-10-27T14:00:00Z stay up: zab active 3 min ago"
    alice = vm(
        "alice",
        "active",
        ("ok", "no"),
        last_activity=float(at),
        running_turns=[turn],
        idle_stop={"enabled": True, "active": True, "minutes": 60, "last_verdict": verdict},
    )
    failure = (
        "codex:allowlist: no allowed user yet: no Zulip user with the allowed e-mail "
        "visible to this bot (not joined yet, or the address is hidden from members)"
    )
    bob = vm(
        "bob",
        "active, doctor FAIL",
        ("ok", "ok"),
        doctor_failures=[failure],
        last_error=STATUS.first_line(failure, 80),
    )
    carol = STATUS.VmStatus(slug="carol", host="198.51.100.3", bridge="stopped")
    full = "/dev/root 203056560 178000000 15600000 92% /"  # 15 GB left (#552)
    dave = vm("dave", "active, sign-in pending", ("no", "no"), mem="29/31G !",
              disk=STATUS.load_columns(full)["disk"], sentinel="down")  # fmt: skip
    dave.metrics = {**dave.metrics, "mem_available_kib": 2_000_000,
                    **STATUS.load_numbers(full)}  # fmt: skip
    return [status_reply(json.dumps(STATUS.fleet_json(rows)), Scrubber(), limit=10000).text
            for rows in ([alice], [alice, bob, carol, dave])]  # fmt: skip


def test_the_status_examples_are_the_bots() -> None:
    """§3.6 shows `!status`'s answers as Zulip does: each line of each, the fences aside."""
    lines = TEXT.split("\n")
    for answer in status_examples():
        missing = [
            x for x in answer.split("\n") if x and not x.startswith("```") and x not in lines
        ]
        assert not missing, "\n".join(missing)


# ----------------------------------------------------------------------------------- quotes


def normalize(text: str) -> str:
    """A plan's ID, its hash and a silent mention as Zulip shows it (the name alone)."""
    text = re.sub(r"@_\*\*([^|*]+)\|\d+\*\*", r"\1", text)
    text = re.sub(r"\bP-\d+\b", "P-n", text)
    text = re.sub(r"(?<![0-9a-f])[0-9a-f]{12}(?![0-9a-f])", "<hash12>", text)
    return re.sub(r"(!approve P-n )[0-9a-f]{6}\b", r"\1<hash6>", text)


def pattern(span: str) -> re.Pattern[str]:
    """A span as a pattern: `…` and a `<placeholder>` (not the hashes) stand for any text."""
    parts = re.split(r"(…|<[a-z ]+>)", normalize(span))
    return re.compile("".join(".+?" if i % 2 else re.escape(p) for i, p in enumerate(parts)))


PATTERNS = [(span, pattern(span)) for span in SPANS]


#: A span quotes a line only with this many characters besides its wildcards: `<n>` alone, or
#: `**P-42** · …`, would quote anything.
LITERAL = 12


def quoted(line: str) -> str | None:
    """The span that quotes `line` whole, or None."""
    text = normalize(line)
    return next((span for span, p in PATTERNS if literal(span) and p.fullmatch(text)), None)


def literal(span: str) -> bool:
    return len(re.sub(r"…|<[a-z ]+>", "", normalize(span))) >= LITERAL


def lines(posts: list[str]) -> list[str]:
    """Each post's lines, its audit footer left out."""
    footer = re.compile(r"`audit #\d+ · chain [0-9a-f]{12}`")
    return [x for post in posts for x in post.split("\n") if x and not footer.fullmatch(x)]


#: Where the plans start: the doc's examples are P-42.
FLOOR = 41
#: The doc's plan time: posted at 15:41 UTC, it expires at 15:51 (§2.2's header).
POSTED = calendar.timegm((2026, 10, 28, 15, 41, 0))
VMS = {"alice": "i-1", "bob": "i-2"}


@pytest.fixture
def run_site(tmp_path, monkeypatch):
    """botkit's server with the real `Runner` and faked scripts, as `test_admin_runner`'s."""
    site = make_site(tmp_path)
    layout = replace(site.paths.layout, release=str(tmp_path / "release"), python=sys.executable)
    site.paths = replace(site.paths, layout=layout)
    for name, value in (("PROGRESS", 0.05), ("GRACE", 0.3), ("STOP_GRACE", 0.3), ("DRAIN", 1.0)):
        monkeypatch.setattr(runner_mod, name, value)
    wait = "while os.path.exists({!r}): time.sleep(0.02)\n"
    long = (
        'if DRY and "--all" in sys.argv:\n    print("\\n".join(f"line {n}" for n in range(45)))\n'
    )
    fake(site, "aws/power.py", wait.format(str(tmp_path / "hold-power")) + long
         + 'print("would stop")')  # fmt: skip
    answer = {"schema": 1, "vms": [], "summary": {}, "ec2_error": None}
    fake(site, "admin/status.py", wait.format(str(tmp_path / "hold-status"))
         + f"print(json.dumps({answer!r}))")  # fmt: skip
    fake(site, "admin/zulip_provision.py", 'print("would deactivate")')
    fake(site, "aws/teardown.py", 'print("terminate i-2 ws-bob (running, participant bob)")')
    (tmp_path / "state" / "runs" / f"P-{FLOOR}").mkdir(parents=True)
    site.clock.now = float(POSTED)
    return site


async def start(site):
    srv = site.server()
    srv.planner = Planning(Runner(srv, vms=lambda fleet, names: {n: VMS.get(n) for n in names}))
    await srv.start()
    return srv


async def settle(srv):
    while srv.planner.tasks:
        await asyncio.gather(*list(srv.planner.tasks), return_exceptions=True)


def _write(path: Path, text: str) -> None:
    path.write_text(text)


def _read(path: Path) -> str:
    return path.read_text()


def _hold(path: Path, on: bool) -> None:
    """A fake script waits while `path` exists (`run_site`)."""
    if on:
        path.touch()
    else:
        path.unlink()


def _wheel(site) -> None:
    """`test_admin_plan_fit`'s wheel of v0.2.15, all that a dry run reads of the release."""
    wheels = Path(site.paths.layout.wheels)
    wheels.mkdir()
    with zipfile.ZipFile(wheels / "zulip_agent_bridge-0.2.15-py3-none-any.whl", "w") as whl:
        whl.write(SRC_CONFIG, "zab/config.py")


async def converse(site) -> list[str]:
    """§1.3, §2.1 to §2.5 and §5.3's answers, from the real bot, planner and runner."""
    async with asyncio.timeout(120):  # a fake script that waits never hangs the suite
        return await _converse(site)


async def _converse(site) -> list[str]:
    roster = site.tmp / "ws" / "harvard-2026" / "roster.csv"
    spares = site.tmp / "ws" / "harvard-2026" / "out" / "spares.json"
    srv = await start(site)
    ids = itertools.count(10**6)  # after every post of the fake realm
    bots = {HARVARD: next(b for b in srv.bots if b.user_id == HARVARD)}
    bots[TEST] = next(b for b in srv.bots if b.user_id == TEST)

    async def say(text, sender=OLGA, bot=HARVARD, stream=HARVARD_CH, wait=True, **kw):
        await bots[bot].on_message(incoming(sender, text, stream=stream, mid=next(ids), **kw))
        if wait:
            await settle(srv)

    def plan(n):
        return srv.store.get(n)

    try:
        whole = _read(roster)
        _write(roster, whole.split("\n", 1)[0] + "\n")
        await say("!roster show")
        _write(roster, whole)
        for text in ("!help", "!plans", "!console", "!status", "!status all\n!stop all",
                     "!status erin", "!rebind", "!roster import", "!retire all",
                     "!roster import [roster.txt](/user_uploads/2/ab/roster.txt)"):  # fmt: skip
            await say(text)
        await say("!plans", stream=None, to=(HARVARD,))
        await say("`!stop bob`")  # §2.1: a command sent as code (#546)
        glued = "@**Workshop admin · harvard-2026**!status all"  # §2.1: no space (#650)
        await say(glued, flags=("mentioned",))
        await say("!status all", sender=PAT)
        await say("hello", sender=CODEX_BOB)
        site.realm.subscribers[HARVARD_CH].add(PAT)
        site.realm.privacy[HARVARD_CH] = "public"  # §1.2: the check that it is private (#347)
        await srv.membership(bots[HARVARD])
        site.realm.subscribers[HARVARD_CH].discard(PAT)
        del site.realm.privacy[HARVARD_CH]
        _write(spares, "{")
        await say("!status all")
        spare = {"harvard-2026/us-west-2/zab-spare-1": {"ip": "198.51.100.9"}}
        _write(spares, json.dumps(spare))
        _hold(site.tmp / "hold-status", True)
        await say("!status alice", wait=False)
        await until(lambda: "harvard-2026" in srv.planner.reading)
        await say("!status bob", wait=False)
        _hold(site.tmp / "hold-status", False)
        await settle(srv)
        # P-42, a change; P-43, above half the fleet; P-44, destructive
        await say("!stop bob")
        assert plan(42).state == PENDING
        h42 = plan(42).hash[:6]
        await say("!approve P-42 000000", sender=OSCAR)
        await say(f"!approve P-42 {h42} stop 1", sender=OSCAR)
        await say("!stop all")
        await say(f"!approve P-43 {plan(43).hash[:6]}")
        await say("!deactivate-bots alice")
        h44 = plan(44).hash[:6]
        await say(f"!approve P-44 {h44} deactivate-bots 1")
        await say(f"!approve P-44 {h44} deactivate-bots 1")
        await say("!reject P-44", sender=OSCAR)
        await say(f"!approve P-44 {h44} deactivate-bots 1", sender=OSCAR)
        await say(f"!approve P-42 {h42}", sender=DREW, bot=TEST, stream=TEST_CH)
        # refused: P-45 to P-48
        for text in ("!rollout v0.2.15 alice", "!adopt-spare carol zab-spare-1",
                     "!waker on alice", "!teardown bob"):  # fmt: skip
            await say(text)
        # STALE: P-42 after a roster edit; P-49 after its row is renamed
        edited = whole.replace("carol,Carol", "carol,Caroline")
        _write(roster, edited)
        await say(f"!approve P-42 {h42}")
        await say("!stop bob")
        _write(roster, edited.replace("bob,Bob", "robert,Bob"))
        await say(f"!approve P-49 {plan(49).hash[:6]}")
        _write(roster, whole)
        # P-50, still being prepared; then busy behind P-43 applying
        _hold(site.tmp / "hold-power", True)
        await say("!stop alice", wait=False)
        await until(lambda: plan(50) is not None and plan(50).state == PLANNING)
        await say("!approve P-50 000000", sender=OSCAR, wait=False)
        _hold(site.tmp / "hold-power", False)
        await settle(srv)
        srv.store.move(43, (PENDING,), APPLYING)
        await say(f"!approve P-50 {plan(50).hash[:6]}")
        srv.store.move(43, (APPLYING,), PENDING)
        # expired: an approval after the TTL, then the tick
        site.clock.now += srv.config.plan_ttl_minutes * 60
        await say(f"!approve P-50 {plan(50).hash[:6]}")
        await srv.planner.tick()
        # P-51 (its rows' heading), completed by a global admin's approval alone (#558); P-52,
        # by two organizers'; each then STALE, the roster edited since, so nothing applies
        await say("!teardown bob unsynced")
        await say("!deactivate-bots bob")
        h51, h52 = plan(51).hash[:6], plan(52).hash[:6]
        _write(roster, edited)
        await say(f"!approve P-51 {h51} teardown 1 unsynced", sender=DREW)
        await say(f"!approve P-52 {h52} deactivate-bots 1")
        await say(f"!approve P-52 {h52} deactivate-bots 1", sender=OSCAR)
        _write(roster, whole)
        # interrupted: P-53 applying when the bots restart
        await say("!stop alice")
        srv.store.move(53, (PENDING,), APPLYING)
    finally:
        await srv.close()
    srv = await start(site)
    await srv.close()
    return [post["content"] for by in (HARVARD, TEST) for post in site.realm.posts(by)]


#: The lines of each plan's dry run that §2.2 and §3.5 quote, by their start: each command must
#: print exactly these, so that a renamed prefix fails here rather than leave nothing to compare.
DRY = {
    "!rollout v0.2.15 alice": ("multi_party:", "claude steer:", "dry-run alice (198.51.100.1): "),
    "!uninstall alice": ("dry-run alice (198.51.100.1): ",),
    "!idle-stop 60 alice": ("dry-run alice (198.51.100.1): ",),
    "!idle-stop 0 alice": ("dry-run alice (198.51.100.1): ",),
    "!stop bob": ("bob i-", "would stop "),
}


async def dry_runs(site) -> list[str]:
    """The dry-run lines of §2.2's table and list, from push_config.py and power.py."""
    out = []
    for text, starts in DRY.items():
        _, message, _ = await plan_of(
            site, ScriptRunner(), text, sender=OLGA, bot=HARVARD, stream=HARVARD_CH
        )
        found = [x for x in message.split("\n") if x.startswith(starts)]
        assert [next(s for s in starts if x.startswith(s)) for x in found] == list(starts), (
            text,
            found,
        )
        out += found
    return out


class Said(Runner):
    """The runner's own progress and result lines (§2.4), posted nowhere."""

    def __init__(self) -> None:
        self.posted: list[str] = []

    async def _post(self, job, text):
        self.posted.append(text)

    async def _attach(self, bot, name, scrubbed):
        return f"[{name}](/user_uploads/x)"


async def results() -> list[str]:
    """§2.4: `!rollout v0.2.15 all`'s progress, success, failure, timeout and cut output."""
    plan = Plan(42, "harvard-2026", HARVARD, OLGA, "!rollout v0.2.15 all", APPLYING, 0.0,
                Requirement("above-half", 1, ("rollout", "2")), (), "0" * 64, None, None,
                None, "", None)  # fmt: skip
    action = SimpleNamespace(steps=(1, 2), applied=(1, 2), verb="rollout", timeout=None)
    job = SimpleNamespace(plan=plan, bot=None, action=action)
    said, push = Said(), "admin/push_config.py"
    outcomes = (
        ("done", [Ran(push, 0), Ran("admin/status.py", 0)], 245, "ok\n" * 100),
        ("failed", [Ran(push, 1)], 1, "x\n"),
        ("timed out", [Ran(push, -15, ended="timeout")], 1, "x\n"),
    )
    for outcome, ran, ago, output in outcomes:
        await said._result(job, plan, outcome, ran, output, time.monotonic() - ago, None)
    progress = Runner._progress(said, job, [Ran(push)], "")
    heads = [post.split("\n", 1)[0] for post in said.posted]
    cut = [x for post in said.posted for x in post.split("\n") if x.startswith("*The last")]
    assert len(cut) == 1, said.posted  # the note under the cut output, never left out unseen
    return [progress, *heads, *cut]


def guard(rendered: list[str]) -> list[str]:
    """§5.4: the 5-minute guard's refusal and its STALE, in the bot's own frames (a refused plan
    and a plan that no longer builds, from the conversation), and its wait while applying."""
    at = calendar.timegm((2026, 11, 1, 14, 48, 0))
    why = Protection("P-49", "protect", float(at)).wait(float(at + 60))
    refused = next(x for x in rendered if re.match(r"❌ P-\d+ refused: no wheel", x))
    stale = next(x for x in rendered if " is stale: it no longer builds: " in x)
    head, tail = re.fullmatch(r"(❌ P-\d+ refused: ).*(; nothing was done)", refused).groups()
    one, two = re.fullmatch(r"(❌ P-\d+ is stale: it no longer builds: ).*(\. Request again\.)",
                            stale).groups()  # fmt: skip
    applying = protect_applying("P-49", "protect")
    return [head + why + tail, one + why + two, head + applying + tail]


def retire_results() -> list[str]:
    """§5.3: `!retire alice bob`'s result headlines, every row through and bob stopped at his
    sync, in the runner's own words (`Runner._rows`)."""
    from zab.admin.actions import STAGES, Step
    from zab.admin.runner import Rows

    out = []
    for failing, unsynced in (("", False), ("bob", False), ("bob", True)):
        steps = tuple(Step(("x",), row=r, stage=s, tolerated=unsynced and s == "final-sync")
                      for r in ("alice", "bob") for s in STAGES)  # fmt: skip
        rows = Rows(SimpleNamespace(applied=steps), {"alice": "i-1", "bob": "i-2"})
        for step in steps:
            if not rows.skip(step):
                failed = step.row == failing and step.stage == "final-sync"
                rows.ran(step, Ran("admin/final_sync.py", 1 if failed else 0))
        outcome = "failed" if rows.stopped else "done"
        mark, text = Runner._rows(rows, outcome, time.monotonic() - 125, ("", ""))
        out.append(f"{mark} **P-50** · `!retire alice bob{' unsynced' * unsynced}`: {text}")
    return out


def retired_line() -> str:
    """§5.1: the line under `!status all` that names a row a `!retire` ended (#559, #651)."""
    from .test_admin_actions import FLEET

    return left_out(replace(FLEET, retired={"alice": "P-50 at 2026-10-29 14:02 UTC"}))


def vms_stale(rendered: list[str]) -> str:
    """§5.5: a `!retire` gone STALE because a row's VM appeared (#559), in the bot's own frame
    (P-42's STALE, from the conversation) with `Runner._diff`'s words."""
    frame = next(x for x in rendered if "; since the plan: roster.csv changed)" in x)
    base = {"release": "v1", "steps": "s", "inputs": {}, "dry_run": "d"}
    then, now = {**base, "vms": {}}, {**base, "vms": {"dave": "i-4"}}
    return frame.replace("roster.csv changed", "; ".join(Runner._diff(then, now)))


def ends_stale(rendered: list[str]) -> str:
    """§5.5: a teardown with `unsynced` gone STALE because a row's state changed (#376), in the
    bot's own frame (P-42's STALE through its hash, from the conversation) with `Runner._diff`'s
    words for the row: `alice` synced after its uninstall, then `!start`ed."""
    frame = next(x for x in rendered if "; since the plan: roster.csv changed)" in x)
    base = {"release": "v1", "steps": "s", "inputs": {}, "vms": {}}
    then, now = ({**base, "ends": {"alice": f}} for f in ([True] * 3, [True, False, False]))
    return frame.replace("roster.csv changed", "; ".join(Runner._diff(then, now)))


#: The quoted parts of the conversation's answers that aren't one line of their own: refusals'
#: reasons, which the runbook quotes alone (§1.1, §3.5, §4.2).
REASON = re.compile(r"❌ P-\d+ refused: (.*); nothing was done")


async def test_the_bots_answers_are_quoted_whole_both_ways(run_site, tmp_path_factory, monkeypatch):
    rendered = lines(await converse(run_site))
    fit = make_site(tmp_path_factory.mktemp("fit"))
    _wheel(fit)
    monkeypatch.setattr(POWER, "Ec2", Ec2)
    fleet_files(fit, "harvard-2026", ["alice", "bob"], "us-west-2")
    plans = await dry_runs(fit)
    endings = [
        Ending(uninstalled="P-41 at 2026-11-01 14:02 UTC", synced="P-43 at 2026-11-01 14:20 UTC",
               counts=True, after="P-41 at 2026-11-01 14:02 UTC").line("alice"),
        Ending(uninstalled="P-41 at 2026-11-01 14:02 UTC").line("bob"),
        Ending(synced="P-43 at 2026-11-01 14:20 UTC").line("alice"),
    ]  # fmt: skip
    answers = [x for x in rendered if x.startswith(MARKS)]
    expected = {
        # the conversation's other lines the runbook quotes (§1.3, §2.1, §2.2, §2.3)
        "**harvard-2026's bot** takes:",
        "No plan is pending on this bot.",
        "The roster has no participants yet: `!roster import`, with the CSV attached.",
        "`erin` is in zab-test: ask @_**Workshop admin · zab-test|902**",
        "**Status** of the targets, read for this plan (shown, not hashed):",
    }
    assert expected <= set(rendered), sorted(expected - set(rendered))
    header = next(x for x in rendered if x.startswith("**P-42** · harvard-2026 · `!stop bob`"))
    plan_line = next(x for x in rendered if x.startswith("**Plan** ") and "P-42`" in x)
    approvals = [x for x in rendered if re.match(r"P-(42|43|44) `", x)]
    assert len(approvals) == 3
    # a phrase plan's approval message (#542, P-43 and P-44): its last paragraph too
    approvals += [x for x in rendered if re.fullmatch(r"It takes .*`!reject P-4[34]`\.", x)]
    assert len(approvals) == 5
    alone = ("no wheel for ", "no !provision zulip or !rollout ", "no alerts_channel ")
    reasons = [
        m[1] for x in rendered if (m := REASON.fullmatch(x)) and any(k in m[1] for k in alone)
    ]
    assert len(reasons) == len(alone), reasons
    framed = [x for x in rendered if x.startswith(("*Cut to fit: ", "**Participant rows** "))]
    framed += [x for x in rendered if x.startswith("── ") and x.endswith(" exited 0")][:1]
    assert len(framed) == 3, framed
    for frame in ("── <script>", "── <script> exited 0"):  # §2.2's frame, for one script
        assert frame in SPANS and frame.replace("<script>", "aws/power.py") in rendered, frame
    must = [*answers, *expected, header, plan_line, *approvals, *reasons, *plans, *endings, *framed,
            *await results(), *guard(rendered), ends_stale(rendered), STARTING, *waker_down(),
            *retire_results(), vms_stale(rendered), retired_line()]  # fmt: skip
    unquoted = [x for x in must if quoted(x) is None]
    assert not unquoted, "rendered by the product, not quoted whole by the runbook:\n" + "\n".join(
        unquoted
    )
    every = [normalize(x) for x in must]
    stale = [
        span for span, p in PATTERNS
        if span.startswith(MARKS) and len(span) > 6 and span not in UNPINNED
        and not any(p.fullmatch(x) for x in every)
    ]  # fmt: skip
    assert not stale, "quoted by the runbook, rendered by nothing here:\n" + "\n".join(stale)
    assert all(span in SPANS for span in UNPINNED)


def waker_down() -> list[str]:
    """The waker down (#717): `!status`'s line, and the alert and its "back" in fleet-alerts."""
    since, now = 1_800_000_000.0, 1_800_003_600.0
    return [down_line(since), down_text("harvard-2026", since), up_text(since, now)]


# ------------------------------------------------------------------- the acknowledgement (#466)


#: The organization's `max_message_length` (DECISIONS, the 2026-09-27 row).
MAX_MESSAGE = 10000
#: Where §3.8 item 6 splits the quick-start in two messages: before this heading.
QUICKSTART_SPLIT = "## Commands (type them in the topic)"


def test_the_quickstart_goes_in_two_messages_that_fit() -> None:
    """§3.8 item 6 posts `docs/quickstart.md` as two messages, split before its commands'
    heading as its `awk` lines split it: each part must fit in one Zulip message."""
    lines = (DOC.parent / "quickstart.md").read_text(encoding="utf-8").splitlines(keepends=True)
    cuts = [i for i, line in enumerate(lines) if line.startswith("## Commands ")]
    assert [lines[i].rstrip("\n") for i in cuts] == [QUICKSTART_SPLIT]
    parts = "".join(lines[: cuts[0]]), "".join(lines[cuts[0] :])
    assert all(0 < len(part) < MAX_MESSAGE for part in parts), [len(p) for p in parts]
    item = section("3.8")
    assert f"`{QUICKSTART_SPLIT}`" in item
    assert "awk '/^## Commands /{exit} {print}' quickstart.md > ~/quickstart-1.md" in item
    assert "awk '/^## Commands /{p=1} p' quickstart.md > ~/quickstart-2.md" in item
    assert "not in `#announcements`, which reaches every workshop in the organization" in item


def test_the_runbook_names_the_bots_acknowledgement() -> None:
    """§2.1, §2.5, §6.1 and §8.1 name the reaction that the bot adds to a command it took, as
    the bot adds it (`test_admin_ack` pins when)."""
    eyes = chr(int(ACK["emoji_code"], 16))
    assert f"- **{eyes} means the bot took it.** The bot adds a {eyes} reaction" in section("2.1")
    assert f"nothing you sent was seen,** and it has no {eyes}." in section("2.5")
    assert f"| `{eyes}`, a reaction on your command | the bot took it," in section("6.1")
    assert f"1. **Look for the bot's {eyes} on it** (§2.1)." in section("8.1")


# ------------------------------------------------------------------ a spare not running (#455)


def section(number: str) -> str:
    """§`number` of the runbook (`3.7`), from its heading to the next one."""
    head = re.search(rf"^### {re.escape(number)} .*$", TEXT, re.MULTILINE)
    assert head is not None, number
    rest = TEXT[head.end() :]
    after = re.search(r"^#{2,3} ", rest, re.MULTILINE)
    return rest[: after.start() if after else len(rest)]


def quotes(number: str, line: str) -> bool:
    """Whether a span of §`number` quotes `line` whole."""
    text = normalize(line)
    return any(literal(s) and pattern(s).fullmatch(text) for s in spans(section(number)))


async def test_a_spare_that_isnt_running_is_refused_as_quoted(run_site):
    """§3.7 quotes `!adopt-spare`'s refusal of a stopping spare (#715), and that of a running
    spare whose SSH doesn't answer yet (#455): the bot's answers, from the real bot, planner and
    runner, with `adopt_spare.py`'s own dry run against a fake EC2. The plan is refused, and
    nothing ran but the dry run. §3.7 also quotes the dry run's line for a stopped spare, which
    is started for the adoption, not refused."""
    site = run_site
    _wheel(site)  # the fleet's release: v0.2.15
    srv = await start(site)
    bot, answers, ids = srv.bots[0], {}, itertools.count(10**6)
    assert bot.user_id == HARVARD
    try:
        state = adopt_spare(site, srv)
        for case, spare in (("stopping", {"state": "stopping", "ssh": True}),
                            ("no ssh", {"state": "running", "ssh": False})):  # fmt: skip
            _write(state, json.dumps(spare))
            text = "!adopt-spare carol zab-spare-1"
            await bot.on_message(incoming(OLGA, text, stream=HARVARD_CH, mid=next(ids)))
            await settle(srv)
            answers[case] = lines([site.realm.posts(HARVARD)[-1]["content"]])
        assert [srv.store.get(n).state for n in (42, 43)] == [REFUSED, REFUSED]
    finally:
        await srv.close()
    ran = calls(site, "aws/adopt_spare.py")
    assert len(ran) == 2 and all(call["dry"] for call in ran), ran
    assert not any(RETAGGED in x for said in answers.values() for x in said)
    head = "❌ P-n refused: the dry run failed: aws/adopt_spare.py exited 1; nothing was done"
    for said in answers.values():
        assert normalize(said[0]) == head and quoted(said[0]) is not None, said
    refusing = {case: next(x for x in said if x.startswith("refusing: "))
                for case, said in answers.items()}  # fmt: skip
    assert "zab-spare-1 is stopping: request again" in refusing["stopping"], refusing
    assert "198.51.100.9:22" in refusing["no ssh"], refusing
    for case, where in (("stopping", ("3.7",)), ("no ssh", ("3.7",))):
        missing = [number for number in where if not quotes(number, refusing[case])]
        assert not missing, f"not quoted in §{', §'.join(missing)}: {refusing[case]!r}"
    # a stopped spare: the plan's line (`adopt_spare.started`, its test pins the text), not a
    # refusal (#715)
    for number in ("3.7", "4.1"):
        assert quotes(number, "zab-spare-1: stopped, started for the adoption"), number
