"""Scenario 18 of the end-to-end suite: background jobs that outlive a turn (#655, SPEC §7.14).

1. **A job and its report, each agent** (`test_job_report[claude|codex]`). Asked to start a
   3-minute computation as a background job, the agent starts it with `zab job start` and ends
   its turn within 90 s. Meanwhile the topic's `!status` lists the job, its `!vm` counts it, and
   a message is answered by a turn of its own within 60 s (the availability #655 is for). The
   end post comes within 25 s of the job's end, mentioning the participant and ending with its
   `(job <id>)` tag, and the report turn's answer holds the job's output (for Claude, its
   transcript holds the report prompt, `[zab: a background job ended] …`). No `zab-job-*` unit
   is left.
2. **A job that ends during a turn** (Claude). The end post's prompt is steered into the running
   turn (↪, then ✅ on the end post), whose answer holds the job's output, and no turn follows.
3. **Stopping a job** (Claude). `!stop` with no turn running says the job keeps running;
   `!stop NAME` stops it within 20 s, a `setsid` grandchild of it included, and its reply,
   "⏹ Stopped background job …", is the end post: no report turn starts.
4. **A job across restarts** (Claude). The bridge and the Codex daemon restart while it runs;
   it runs on, and its end is posted once.
5. **The VM's limits**, over SSH as `agent` (Drew on #655: 4 jobs per VM, no limit per topic,
   75% of the memory). With the limit's jobs running (`start` prints it) one more is refused
   (exit 2), and a job that
   allocates past the jobs' share of the memory ends `oom-kill`, the bridge's main process
   unchanged.
6. **A job cut off by a VM stop** (slow). The VM stops and starts while a job runs: the topic
   gets "was cut off when the VM stopped", ending with its `(job <id>)` tag, no report turn
   starts, and the next turn's prompt holds the note between `[zab: background jobs]` and
   `[end of background jobs]` (Claude's transcript).

Scenario 11's part 2 (`test_idle_stop.py`) checks that a running job holds the VM up. Not here
yet: the 12-hour cap (`[jobs] max_hours`), which needs a way to set it for a test on the VM; the
cap of 3 report turns in a row is tested offline only, against the fake backends.

Each part skips when the installed zab has no `zab job` (`installed.job_help`), or lacks a later
part it needs (`NEEDS`: the watcher's end posts, report turns, `!stop NAME`, the jobs' lines
in `!status` and `!vm`, the jobs' memory limit), each asked of the installed build. On a build
without report turns (Q4's fallback, J4), parts 1 and 2 skip, so a `--require rc` run of it
fails scenario 18 unless that is accepted for the release candidate. Each part's clean-up sends
`!stop` where its turn may run, then stops the jobs whose records name its nonce, each with its
own session's `zab job stop` (no end post, no report turn) and by its unit, and ends its marked
processes, as one `cleanup.undo` step (`stop_all`).

The topic's `!status` and `!stop` read the jobs the watcher has seen, which it records at its
pass, every `PASS_S`: each part waits that long after the job's unit started before sending
them (`after_a_pass`).

Fast but for part 6: about 25 minutes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time

import pytest

from e2e.harness import installed
from e2e.harness.cleanup import VM_STOPPED, undo, vm_up
from e2e.harness.flows import Chat, Turn, answer, topic_name
from e2e.harness.zulip_user import topic_narrow
from e2e.test_scenarios_subagents import claude_entries, end_marked, marked, poll

pytestmark = pytest.mark.e2e
STEER_CODE, READ_CODE = "21aa", "2705"  # ↪ and ✅, as in scenario 17
ZAB = "/home/agent/.local/bin/zab"
CODEX_UNIT = "codex-app-server.service"
#: agent's job units (SPEC §7.14: `zab-job-<id>.service`), still loaded
UNITS = ["systemctl", "--user", "list-units", "--no-legend", "--plain", "zab-job-*.service"]
JOB_S = 180  # part 1's computation
#: The watcher's pass (#940's `PASS_S`), and a margin: the topic's `!status` and `!stop` know a
#: job once a pass has recorded it
PASS_S = 10.0
SEEN_S = PASS_S + 2.0
#: Where agent's jobs keep their records (`<state_dir>/jobs`, the default state directory)
JOBS_DIR = "/home/agent/.local/state/zab/jobs"
ID = re.compile(r"[0-9a-f]{12}")
#: A session id as zab makes them (`ZAB_SESSION`), safe as an `env` assignment
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def name_of(part: str, nonce: str) -> str:
    """A job name the pattern allows (`^[a-z0-9][a-z0-9-]{0,31}$`); a nonce is digits, a dash
    and hex digits."""
    name = f"s18{part}-{nonce}"
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", name):
        raise ValueError(f"not a job name: {name!r}")
    return name


def value_of(nonce: str) -> int:
    """What the job prints: from the nonce, so that no agent knows it without the log."""
    return int(nonce.rsplit("-", 1)[1], 16) * 7919


def computation(seconds: int, value: int, marker: str) -> str:
    """A marked `python3` that sleeps, then prints `value`."""
    return f"python3 -c 'import time; time.sleep({seconds}); print({value})' {marker}"


def ended(name: str, how: str = "finished: exit 0 after") -> str:
    """The end post of job `name` (§7.14 step 2; #940's `JobEnd.line`)."""
    return rf"background job `{re.escape(name)}` {how}"


#: The end post's last words, its job's id (#940's `id_tag`)
TAG = re.compile(r"\(job [0-9a-f]{12}\)$")
#: The report turn's prompt begins so (Appendix A.3; #942's `jobreport.prompt`)
REPORT_PROMPT = "[zab: a background job ended]"
#: The notes that wait, at the head of the next turn's prompt (#942's `notes_block`)
NOTES_OPEN, NOTES_CLOSE = "[zab: background jobs]", "[end of background jobs]"


def noted(text: str, words: str) -> bool:
    """Whether `words` stand in the notes block of a prompt's `text`."""
    _, opened, rest = text.partition(NOTES_OPEN)
    return bool(opened) and words in rest.partition(NOTES_CLOSE)[0]


def start_prompt(
    marker: str, name: str, command: str, started: str, then: str = "", flags: str = ""
) -> str:
    line = " ".join(filter(None, ["zab job start --name", name, flags, "--", command]))
    return (
        f"This is a test of the bridge (`{marker}`). Start this background job with exactly "
        f"this command: `{line}`. Then end your turn at once with exactly `{started}` and "
        f"nothing else. {then}"
    ).strip()


def units(vm) -> list[str]:
    out = vm.run(vm.as_agent(*UNITS), check=False).stdout
    return [line.split()[0] for line in out.splitlines() if line.strip()]


def started_at(vm, unit: str) -> float | None:
    """When the job's unit became active, epoch seconds (systemd's wall clock), or None."""
    argv = ["systemctl", "--user", "show", unit, "--property=ActiveEnterTimestamp"]
    out = vm.run(vm.as_agent(*argv, "--timestamp=unix"), check=False).stdout
    found = re.search(r"ActiveEnterTimestamp=@(\d+)", out)
    return float(found[1]) if found else None


def job_cli(vm, *argv: str):
    """`zab job …` as agent, from its home, its user manager reachable."""
    return vm.run(vm.as_agent(ZAB, "job", *argv), check=False, timeout=60)


#: The jobs' slice, as `zab service install` writes it (#655 part 3)
SLICE = ["systemctl", "--user", "show", "zab-jobs.slice", "--property=MemoryMax"]


def slice_limited(vm) -> bool:
    """Whether agent's `zab-jobs.slice` caps the jobs' memory: a `MemoryMax` in bytes. A read
    that fails (SSH, `sudo`) raises: an unknown build is an error, not a skip (#938's review,
    N2); `systemctl show` of a slice that doesn't exist answers, with no limit."""
    res = vm.run(vm.as_agent(*SLICE), check=False, timeout=60)
    if not res.ok:
        raise RuntimeError(f"systemctl show zab-jobs.slice: exit {res.returncode}")
    return bool(re.search(r"^MemoryMax=\d+$", res.stdout, re.M))


#: What a part may need beyond `zab job` itself (#655's later parts), asked of the installed
#: build (`harness/installed.py`) or of the VM, and what a skip says it lacks
NEEDS = {
    "watcher": (lambda vm: installed.has(vm, installed.JOB_WATCHER),
                "posts no job's end (#655 part 4)"),
    "reports": (lambda vm: installed.has(vm, installed.JOB_REPORTS),
                "starts no report turn when a job ends (#655 part 5)"),
    "commands": (lambda vm: installed.has(vm, installed.JOB_STOP_NAME),
                 "has no `!stop NAME` for jobs (#655 part 6a)"),
    "status": (lambda vm: installed.has(vm, installed.JOB_STATUS),
               "shows no jobs in `!status` or `!vm` (#655 part 6b)"),
    "limits": (slice_limited, "puts no memory limit on jobs (#655 part 3)"),
}  # fmt: skip


def needs_jobs(vm, *needs: str) -> str:
    """`zab job start --help`, or a skip where the installed zab lacks what the part tests."""
    text = installed.job_help(vm)
    if not text:
        pytest.skip("the installed zab has no `zab job` (#655 part 2)")
    for need in needs:
        present, lacks = NEEDS[need]
        if not present(vm):
            pytest.skip(f"the installed zab {lacks}")
    return text


def jobs_of(vm, nonce: str) -> list[dict]:
    """The records (`job.json`) of agent's jobs that name `nonce`, in their name or their
    marked command: `find` and `grep -F` as `agent`, argument lists only."""
    argv = vm.as_agent(
        "find", JOBS_DIR, "-mindepth", "2", "-maxdepth", "2", "-name", "job.json",
        "-exec", "grep", "-l", "-F", "--", nonce, "{}", "+",
    )  # fmt: skip
    records = []
    for path in vm.run(argv, check=False, timeout=60).stdout.split():
        text = vm.run(vm.as_agent("cat", "--", path), check=False, timeout=60).stdout
        try:
            record = json.loads(text)
        except ValueError:
            continue
        if isinstance(record, dict) and ID.fullmatch(str(record.get("id", ""))):
            records.append(record)
    return records


def stop_all(vm, nonce: str, marker: str) -> None:
    """Each job of the part stopped, found by its `nonce` in the job records (#935's review,
    B5: over SSH `zab job stop NAME` alone has no `ZAB_SESSION`, and finds none): first with
    its own session's `zab job stop`, which writes `stopped.json`, so that no end post or
    report turn follows; then its unit stopped by id, which a job gone already ignores. Then
    the marked processes ended and checked gone (`end_marked`)."""
    for record in jobs_of(vm, nonce):
        session, name = str(record.get("session") or ""), str(record.get("name") or "")
        if SESSION_ID.fullmatch(session) and name:
            vm.run(vm.as_agent(f"ZAB_SESSION={session}", ZAB, "job", "stop", name),
                   check=False, timeout=60)  # fmt: skip
        unit = f"zab-job-{record['id']}.service"
        vm.run(vm.as_agent("systemctl", "--user", "stop", unit), check=False, timeout=60)
    end_marked(vm, marker)


async def clean_up(chat: Chat | None, vm, topic: str, nonce: str, marker: str, *, stop: bool):
    """`!stop` in the topic if its turn may run; then `stop_all`, as a clean-up step (#517)."""
    if stop and chat is not None:
        with contextlib.suppress(Exception):  # a courtesy: the step below is the clean-up
            await chat.user.send(chat.stream, topic, "!stop")
            await asyncio.sleep(10)
    await undo(
        f"background job {marker} still running on the VM",
        lambda: asyncio.to_thread(stop_all, vm, nonce, marker),
        stops=False,
    )


async def after_a_pass(began: float | None) -> None:
    """Until the watcher has had a pass since the job's unit started (`began`, systemd's wall
    clock), so that the topic's `!status` and `!stop` know the job (#935's review, B2)."""
    wait = SEEN_S if began is None else began + SEEN_S - time.time()
    if wait > 0:
        await asyncio.sleep(wait)


async def command(
    chat: Chat, turn: Turn, topic: str, text: str, pattern: str, *, kind: str | None = None
) -> dict:
    """A `!` command in the topic, and the bot's reply matching `pattern`. With `kind`, it
    mentions that agent's bot: a VM-wide command such as `!vm` is otherwise answered by the
    VM's first bot by name, whatever the topic (SPEC §4.6; #935's review, B1)."""
    if kind is not None:
        mid = (await chat.say(kind, topic, text)).message_id
    else:
        mid = await chat.user.send(chat.stream, topic, text)
    return await chat.user.wait_reply(
        topic_narrow(chat.stream, topic), after_id=mid, senders=[turn.bot], pattern=pattern,
        within=60,
    )  # fmt: skip


async def started_job(chat, kind, topic, marker, name, command_line, then="", flags=""):
    """The turn that starts the job and ends at once, and how long it took."""
    word = f"STARTED-{marker}"
    turn = await chat.say(kind, topic, start_prompt(marker, name, command_line, word, then, flags))
    _, took = await chat.reply(turn, answer(re.escape(word)), within=120)
    return turn, took


# ------------------------------------------------------------------------------ the parts


@pytest.mark.scenario(18)
@pytest.mark.parametrize("kind", ["claude", "codex"])
async def test_job_report(chat: Chat, vm, scenario, nonce, kind):
    """Part 1: the agent ends its turn while its job runs, stays available, and reports the
    job's output in a turn zab starts when the job ends."""
    needs_jobs(vm, "watcher", "reports", "status")
    marker, name, value = f"S18A-{nonce}", name_of(kind[:2], nonce), value_of(nonce)
    topic, avail = topic_name(18, nonce, kind), f"AVAIL-{nonce}"
    then = "When zab tells you that it ended, reply with the number it printed."
    over = False
    try:
        turn, took = await started_job(
            chat, kind, topic, marker, name, computation(JOB_S, value, marker), then
        )
        # the model's time, a target (#922's N1); that the turn ended while its job ran is the
        # check, structural, next
        scenario.timing(f"{kind}: the turn ends, its job running", took, high=90, target=True)
        found = await asyncio.to_thread(units, vm)
        assert len(found) == 1, f"job units after the start: {found}"
        began = await asyncio.to_thread(started_at, vm, found[0])
        await after_a_pass(began)
        await command(chat, turn, topic, "!status", rf"⚙️ Job `{re.escape(name)}` · running since")
        await command(chat, turn, topic, "!vm", r"\b1 jobs? running", kind=kind)
        follow = await chat.say(
            kind, topic, f"Meanwhile, reply with exactly `{avail}`.", mention_bot=False
        )
        _, took = await chat.reply(follow, answer(re.escape(avail)), within=180)
        scenario.timing(
            f"{kind}: a message answered while the job runs", took, high=60, target=True
        )
        still = await asyncio.to_thread(units, vm)
        assert found[0] in still, "the answer came after the job had ended: not meanwhile"
        post = await chat.user.wait_reply(
            turn.narrow, after_id=turn.message_id, senders=[turn.bot], pattern=ended(name),
            within=JOB_S + 180,
        )  # fmt: skip
        # Zulip's time and systemd's, both whole seconds on NTP clocks; without the unit's start,
        # a note: from the prompt it would count the turn's start too (#935's review, N3)
        if began is not None:
            late = post["timestamp"] - began - JOB_S
            scenario.timing(f"{kind}: the end post, after the job's end", late, high=25)
        else:
            scenario.notes.append(f"{kind}: the job unit's start unread: the end post untimed")
        assert "@**" in post["content"], f"the end post mentions no one: {post['content']}"
        assert TAG.search(post["content"]), f"the end post lacks its tag: {post['content']}"
        reply = await chat.user.wait_reply(
            turn.narrow, after_id=post["id"], senders=[turn.bot], pattern=answer(str(value)),
            within=300,
        )  # fmt: skip
        over = bool(reply)
        assert not await asyncio.to_thread(units, vm), "a job unit was left"
        if kind == "claude":  # the report turn's prompt, in the session's transcript
            entries = await asyncio.to_thread(claude_entries, vm, marker)
            prompts = [e for e in entries if e.kind == "user" and REPORT_PROMPT in e.text]
            assert prompts, f"no report prompt ({REPORT_PROMPT}) in Claude's transcript"
    finally:
        await clean_up(chat, vm, topic, nonce, marker, stop=not over)


@pytest.mark.scenario(18)
async def test_job_end_during_a_turn(chat: Chat, vm, scenario, nonce):
    """Part 2: a job's end, during a turn of its session, goes into that turn."""
    needs_jobs(vm, "watcher", "reports")
    marker, name, value = f"S18B-{nonce}", name_of("b", nonce), value_of(nonce)
    topic, slow = topic_name(18, nonce, "during"), f"SLOW-{nonce}"
    over = False
    try:
        first, _ = await started_job(
            chat, "claude", topic, marker, name, computation(60, value, marker)
        )
        turn = await chat.say(
            "claude", topic,
            "Now wait about two minutes: run `for i in $(seq 4); do sleep 10; done` three times, "
            "in three calls, with a short note after each. Then reply with exactly "
            f"`{slow}`, followed by the output of any background job you were told had ended.",
            mention_bot=False,
        )  # fmt: skip
        post = await chat.user.wait_reply(
            turn.narrow, after_id=turn.message_id, senders=[turn.bot], pattern=ended(name),
            within=240,
        )  # fmt: skip
        await chat.user.wait_reaction(
            post["id"], emoji_code=STEER_CODE, user_id=turn.bot, within=60
        )
        sent = time.monotonic()
        await chat.user.wait_reaction(
            post["id"], emoji_code=READ_CODE, user_id=turn.bot, within=120
        )
        scenario.timing("claude: the end taken into the running turn (✅)", time.monotonic() - sent)
        reply, _ = await chat.reply(turn, answer(re.escape(slow)), within=400)
        over = True
        assert str(value) in reply["content"], f"the answer lacks the output: {reply['content']}"
        await asyncio.sleep(30)  # a report turn after it would start within seconds
        statuses = await chat.statuses(first)
        assert len(statuses) == 2, f"{len(statuses)} turns ran: the end must not start one"
    finally:
        await clean_up(chat, vm, topic, nonce, marker, stop=not over)


@pytest.mark.scenario(18)
async def test_job_stop(chat: Chat, vm, scenario, nonce):
    """Part 3: `!stop` leaves the job; `!stop NAME` stops it whole, with no report turn."""
    needs_jobs(vm, "watcher", "commands")
    marker, name, topic = f"S18C-{nonce}", name_of("c", nonce), topic_name(18, nonce, "stop")
    sleep = f'python3 -c "import time; time.sleep(600)" {marker}'
    job = f"bash -c 'setsid {sleep} & {sleep}; wait'"
    try:
        turn, _ = await started_job(chat, "claude", topic, marker, name, job)
        await poll(lambda: len(marked(vm, marker)) == 2, within=60, what="job's two processes")
        found = await asyncio.to_thread(units, vm)
        assert len(found) == 1, f"job units after the start: {found}"
        await after_a_pass(await asyncio.to_thread(started_at, vm, found[0]))
        await command(
            chat, turn, topic, "!stop",
            rf"Background job `{re.escape(name)}` keeps running: `!stop {re.escape(name)}`",
        )  # fmt: skip
        sent = time.monotonic()
        await command(
            chat, turn, topic, f"!stop {name}", rf"⏹ Stopped background job `{re.escape(name)}`"
        )
        await poll(lambda: not marked(vm, marker), within=30, what="end of the job's processes")
        scenario.timing("claude: !stop NAME, the job gone", time.monotonic() - sent, high=20)
        await asyncio.sleep(30)
        assert len(await chat.statuses(turn)) == 1, "a turn started after !stop NAME"
    finally:
        await clean_up(chat, vm, topic, nonce, marker, stop=False)


@pytest.mark.scenario(18)
async def test_job_survives_restarts(chat: Chat, vm, scenario, nonce):
    """Part 4: a job runs on through a bridge restart and a Codex daemon restart, and its end
    is posted once."""
    help_text = needs_jobs(vm, "watcher")
    marker, name, topic = f"S18D-{nonce}", name_of("d", nonce), topic_name(18, nonce, "restart")
    quiet = "--no-report" if "--no-report" in help_text else ""  # no report turn needed
    over = False
    try:
        turn, _ = await started_job(
            chat, "claude", topic, marker, name, computation(150, value_of(nonce), marker),
            flags=quiet,
        )  # fmt: skip
        before = await asyncio.to_thread(units, vm)
        assert len(before) == 1, f"job units: {before}"
        await asyncio.to_thread(vm.unit_action, "restart")
        if vm.unit(CODEX_UNIT).get("ActiveState") == "active":
            await asyncio.to_thread(vm.unit_action, "restart", CODEX_UNIT)
        assert await asyncio.to_thread(units, vm) == before, "the job didn't survive"
        await chat.user.wait_reply(
            turn.narrow, after_id=turn.message_id, senders=[turn.bot], pattern=ended(name),
            within=400,
        )  # fmt: skip
        over = True
        await asyncio.sleep(30)
        posts = [m for m in await chat.bot_messages(turn) if re.search(ended(name), m["content"])]
        assert len(posts) == 1, f"its end was posted {len(posts)} times"
    finally:
        await clean_up(chat, vm, topic, nonce, marker, stop=not over)


def mem_total(vm) -> int:
    """The VM's memory in bytes, from /proc/meminfo."""
    for line in vm.run(["cat", "/proc/meminfo"]).stdout.splitlines():
        if line.startswith("MemTotal:"):
            return int(line.split()[1]) * 1024
    raise RuntimeError("no MemTotal in /proc/meminfo")


def job_dir(output: str) -> str:
    """The job's directory, from the log path `zab job start` prints (SPEC §7.14), whatever
    the state directory (#938's review, N1)."""
    found = re.search(r"(/\S*/jobs/[0-9a-f]{12})/output\.log", output)
    if not found:
        raise AssertionError(f"no log path in zab job start's output: {output[-300:]!r}")
    return found[1]


def max_running(output: str) -> int:
    """The machine's count limit, from `zab job start`'s "Limits: … N jobs at a time" line, as
    the VM's config sets it (#938's review, N3)."""
    found = re.search(r"\b(\d+) jobs at a time", output)
    if not found:
        raise AssertionError(f"no limit in zab job start's output: {output[-300:]!r}")
    return int(found[1])


@pytest.mark.scenario(18)
async def test_job_limits(vm, scenario, nonce):
    """Part 5: the VM's count limit (4 by default, as `start` prints it) refuses one more job; a
    job past the memory share is the one the kernel kills, the bridge untouched."""
    needs_jobs(vm, "limits")
    marker = f"S18E-{nonce}"
    sleep = ["python3", "-c", "import time; time.sleep(300)", marker]
    try:
        first = name_of("e0", nonce)
        res = await asyncio.to_thread(job_cli, vm, "start", "--name", first, "--", *sleep)
        assert res.ok, f"{first}: exit {res.returncode}: {(res.stderr or res.stdout)[-300:]}"
        limit = max_running(res.stdout)
        names = [name_of(f"e{i}", nonce) for i in range(limit + 2)]
        for name in names[1:limit]:
            res = await asyncio.to_thread(job_cli, vm, "start", "--name", name, "--", *sleep)
            assert res.ok, f"{name}: exit {res.returncode}: {(res.stderr or res.stdout)[-300:]}"
        res = await asyncio.to_thread(job_cli, vm, "start", "--name", names[limit], "--", *sleep)
        refused = (res.stderr or res.stdout).strip()
        assert res.returncode == 2 and len(refused.splitlines()) == 1, (res.returncode, refused)
        scenario.notes.append(f"job {limit + 1} of {limit}: {refused[:160]}")
        for name in names[:limit]:
            await asyncio.to_thread(job_cli, vm, "stop", name)
        await poll(lambda: not units(vm), within=60, what=f"end of the {limit} jobs")
        bridge = vm.unit()["MainPID"]
        big = int(mem_total(vm) * 0.85)
        hog = ["python3", "-c", f"b = b'x' * {big}; import time; time.sleep(120)", marker]
        res = await asyncio.to_thread(job_cli, vm, "start", "--name", names[-1], "--", *hog)
        path = f"{job_dir(res.stdout + res.stderr)}/ended.json"
        how = await poll(
            lambda: vm.run(vm.as_agent("cat", "--", path), check=False).stdout,
            within=180, what="the memory hog's ended.json",
        )  # fmt: skip
        assert "oom-kill" in how, f"the memory hog ended {how[:200]!r}, not oom-kill"
        assert vm.unit()["MainPID"] == bridge, "the bridge's main process changed"
    finally:
        await clean_up(None, vm, "", nonce, marker, stop=False)


@pytest.mark.slow
@pytest.mark.scenario(18)
async def test_job_lost_at_boot(chat: Chat, vm, ec2, instance_id, scenario, nonce):
    """Part 6: a VM stop cuts the job off; zab says so, starts no turn, and tells the agent
    at the next one, in its notes block (#942: report turns)."""
    needs_jobs(vm, "watcher", "reports")
    marker, name, topic = f"S18F-{nonce}", name_of("f", nonce), topic_name(18, nonce, "lost")
    cut = ended(name, "was cut off when the VM stopped")
    over = False
    try:
        turn, _ = await started_job(
            chat, "claude", topic, marker, name, computation(900, value_of(nonce), marker)
        )
        ec2.stop(instance_id)
        ec2.wait_state(instance_id, "stopped", timeout=600)
        await vm_up(ec2, instance_id, vm)
        post = await chat.user.wait_reply(
            turn.narrow, after_id=turn.message_id, senders=[turn.bot], pattern=cut, within=600
        )
        await asyncio.sleep(60)
        assert len(await chat.statuses(turn)) == 1, "a turn started for a job cut off"
        word = f"NEXT-{nonce}"
        follow = await chat.say("claude", topic, f"Reply with exactly `{word}`.", mention_bot=False)
        await chat.reply(follow, answer(re.escape(word)), within=300)
        over = True
        entries = await asyncio.to_thread(claude_entries, vm, marker)
        told = [
            e for e in entries
            if e.kind == "user" and noted(e.text, "was cut off when the VM stopped")
        ]  # fmt: skip
        assert told, "the next turn's prompt has no note of the job cut off in its notes block"
        assert TAG.search(post["content"]), f"the cut-off post lacks its tag: {post['content']}"
        scenario.notes.append(f"cut-off post: {post['content'][:160]}")
    finally:  # the VM up for every later scenario first, as each VM-stopping part (#517)
        await undo(VM_STOPPED, lambda: vm_up(ec2, instance_id, vm), attempts=1)
        await clean_up(chat, vm, topic, nonce, marker, stop=not over)
