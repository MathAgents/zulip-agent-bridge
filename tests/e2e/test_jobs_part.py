"""Scenario 18's helpers (#655), offline: the job names and prompts, the bridge's words as SPEC
§7.14 gives them, the probes of the installed zab, and scenario 11's part 2's verdicts."""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from e2e import test_idle_stop as s11
from e2e import test_scenarios_jobs as s18
from e2e.harness import installed, report

#: What the bridge posts, as #940, #941 and #942 write it (SPEC §7.14), and the scenario's
#: patterns for them
END_POST = (
    "🔧 **Bridge** (not Claude): @**Alice|7** background job `rank-search` finished: exit 0 "
    "after 2 h 13 min. Claude will report on it. (job 3f9c2a7b1d4e)"
)
CUT_OFF = (
    "⚠️ **Bridge** (not Claude): @**Alice|7** background job `rank-search` was cut off when the "
    "VM stopped. (job 3f9c2a7b1d4e)"
)
REPORT = (
    "[zab: a background job ended] `rank-search`, started 2026-11-21 14:02 in this topic "
    "(`python3 -u search.py`), finished: exit 0 at <time:2026-11-21T16:15:00Z> after 2 h 13 min."
)
STOPPED = "⏹ Stopped background job `rank-search` after 41 min."
KEEPS = (
    "Background job `rank-search` keeps running: `!stop rank-search` stops it, and `!stop jobs` "
    "stops all of this topic's jobs."
)
STATUS = (
    "⚙️ Job `rank-search` · running since <time:2026-11-21T14:02:00Z> · log "
    "`/home/agent/.local/state/zab/jobs/3f9c2a7b1d4e/output.log`"
)


def test_the_names_are_job_names_and_the_values_come_from_the_nonce():
    nonce = "121748-a1b2c3"
    for part in ("cl", "co", "b", "c", "d", "e0", "e5", "f"):
        assert re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", s18.name_of(part, nonce))
    with pytest.raises(ValueError):
        s18.name_of("X", nonce)
    assert s18.value_of(nonce) == 0xA1B2C3 * 7919 != s18.value_of("121748-a1b2c4")


def test_the_prompt_gives_the_exact_command():
    command = s18.computation(180, 42, "S18A-x")
    assert command == "python3 -c 'import time; time.sleep(180); print(42)' S18A-x"
    prompt = s18.start_prompt("S18A-x", "s18cl-x", command, "STARTED-S18A-x", "Then report.")
    assert f"`zab job start --name s18cl-x -- {command}`" in prompt
    assert prompt.endswith("with exactly `STARTED-S18A-x` and nothing else. Then report.")
    quiet = s18.start_prompt("m", "n", "true", "W", flags="--no-report")
    assert "`zab job start --name n --no-report -- true`" in quiet


def test_the_patterns_match_the_bridges_words():
    assert re.search(s18.ended("rank-search"), END_POST) and s18.TAG.search(END_POST)
    assert s18.TAG.search(CUT_OFF) and not s18.TAG.search(STOPPED)
    assert REPORT.startswith(s18.REPORT_PROMPT)
    notes = (
        "[zab: background jobs]\n[zab: a background job ended] `rank-search`, started "
        "2026-11-21 14:02 in this topic (`python3 -u search.py`), was cut off when the VM "
        "stopped; zab doesn't restart it.\n[end of background jobs]\n\nWhat did it find?"
    )
    assert s18.noted(notes, "was cut off when the VM stopped")
    assert not s18.noted(notes, "What did it find?")
    assert not s18.noted("was cut off when the VM stopped", "was cut off when the VM stopped")
    assert not re.search(s18.ended("rank"), END_POST)
    assert re.search(s18.ended("rank-search", "was cut off when the VM stopped"), CUT_OFF)
    assert re.search(r"⏹ Stopped background job `rank-search`", STOPPED)
    name = re.escape("rank-search")
    assert re.search(rf"Background job `{name}` keeps running: `!stop {name}`", KEEPS)
    assert re.search(rf"⚙️ Job `{name}` · running since", STATUS)
    assert re.search(r"\b1 jobs? running", "1 turn running · 1 job running · up 2 hours")
    assert s18.job_dir("log: " + STATUS.split("`")[-2]).endswith("/jobs/3f9c2a7b1d4e")
    with pytest.raises(AssertionError, match="no log path"):
        s18.job_dir("started")


class Vm:
    def __init__(self, answers: dict[str, tuple[int, str]]) -> None:
        self.answers, self.calls = answers, []

    def as_agent(self, *argv: str) -> list[str]:
        return ["sudo", "-u", "agent", *argv]

    def run(self, argv, check=True, timeout=120):
        self.calls.append(argv)
        for needle, (code, out) in self.answers.items():
            if needle in " ".join(argv):
                return SimpleNamespace(ok=code == 0, returncode=code, stdout=out, stderr="")
        return SimpleNamespace(ok=True, returncode=0, stdout="", stderr="")


def test_the_parts_skip_where_the_build_lacks_what_they_test(monkeypatch):
    asked = []

    def has(vm, snippet):
        asked.append(snippet)
        return snippet != installed.JOB_REPORTS

    monkeypatch.setattr(installed, "has", has)
    with_cli = Vm({"start --help": (0, "usage: zab job start [--no-report] --name NAME")})
    assert "--no-report" in s18.needs_jobs(with_cli, "watcher", "commands")
    assert asked == [installed.JOB_WATCHER, installed.JOB_STOP_NAME]
    asked.clear()
    s18.needs_jobs(with_cli, "status")
    assert asked == [installed.JOB_STATUS]
    with pytest.raises(pytest.skip.Exception, match="no report turn"):
        s18.needs_jobs(with_cli, "watcher", "reports")
    with pytest.raises(pytest.skip.Exception, match="no memory limit"):
        s18.needs_jobs(with_cli, "limits")  # no slice: `MemoryMax` unread
    limited = Vm({"start --help": (0, "usage"), "zab-jobs.slice": (0, "MemoryMax=24000000000\n")})
    assert s18.needs_jobs(limited, "limits") == "usage"
    loose = Vm({"start --help": (0, "usage"), "zab-jobs.slice": (0, "MemoryMax=infinity\n")})
    assert not s18.slice_limited(loose)
    with pytest.raises(pytest.skip.Exception, match="no `zab job`"):
        s18.needs_jobs(Vm({"start --help": (2, "invalid choice: 'job'")}), "watcher")
    assert installed.JOB_HELP == ["/home/agent/.local/bin/zab", "job", "start", "--help"]


@pytest.mark.parametrize(
    "snippet",
    [installed.JOB_WATCHER, installed.JOB_REPORTS, installed.JOB_STOP_NAME, installed.JOB_STATUS],
)
def test_the_job_probes_run_against_this_build(snippet, capsys):
    """Each probe runs against this checkout and prints `True` or `False`, as `installed.has`
    reads it; which, depends on which of #655's parts are merged."""
    exec(snippet)  # noqa: S102 - a fixed snippet of `harness/installed.py`
    assert capsys.readouterr().out.strip() in ("True", "False")


def test_the_units_and_the_memory_are_read_from_the_vm():
    lines = "zab-job-3f9c2a7b1d4e.service loaded active running x\n\n"
    assert s18.units(Vm({"list-units": (0, lines)})) == ["zab-job-3f9c2a7b1d4e.service"]
    meminfo = "MemTotal:       32000000 kB\nMemFree: 1 kB\n"
    assert s18.mem_total(Vm({"meminfo": (0, meminfo)})) == 32000000 * 1024
    unit = "zab-job-3f9c2a7b1d4e.service"
    shown = Vm({"ActiveEnterTimestamp": (0, "ActiveEnterTimestamp=@1731000000\n")})
    assert s18.started_at(shown, unit) == 1731000000.0
    assert shown.calls[-1][-3:] == [unit, "--property=ActiveEnterTimestamp", "--timestamp=unix"]
    assert (
        s18.started_at(Vm({"ActiveEnterTimestamp": (0, "ActiveEnterTimestamp=\n")}), unit) is None
    )


def test_scenario_18_is_listed_and_its_vm_stopping_part_is_slow():
    assert report.SCENARIOS[18] == ("Background jobs (#655)", "fast")
    assert "slow" in [m.name for m in s18.test_job_lost_at_boot.pytestmark]


def test_scenario_11_reads_a_jobs_hold_from_idle_stops_log():
    """Its verdict, as `idle_stop.py` words it (workshop/admin, #655)."""
    log = (
        "2026-11-07T12:00:00Z stay up: up only 3 min; logged in 1 min ago\n"
        "2026-11-07T12:11:00Z stay up: 1 background job running\n"
        "2026-11-07T12:13:00Z stay up: a background job ran 0 min ago\n"
        "2026-11-07T12:24:00Z power off: idle for 10 min (no turn, no job, no login, load 0.01)\n"
    )
    import datetime as dt

    def at(clock: str) -> float:
        return dt.datetime.fromisoformat(f"2026-11-07T{clock}+00:00").timestamp()

    assert s11.job_verdicts(log, at("12:05:00"), at("12:20:00")) == [log.splitlines()[1]]
    assert s11.job_verdicts(log, at("12:12:00"), at("12:20:00")) == []
    # the timeout after its end (#970's Codex round 1), in the words of idle stop's rule
    after = "a background job ran "
    assert f'f"{after}{{' in (s11.admin_dir() / "idle_stop.py").read_text()
    assert s11.job_verdicts(log, at("12:05:00"), at("12:30:00"), after) == [log.splitlines()[2]]
    assert s11.job_verdicts(log, at("12:14:00"), at("12:30:00"), after) == []
    assert s11.JOB_HELD.startswith("stay up: 1 background job running")
    assert s11.JOB_S > s11.HIGH_S  # only the job can hold the VM past the timeout


class Shell:
    """The VM's answers by argv, recorded: `find` lists the records, `cat` reads them, `ps`
    lists no marked process."""

    def __init__(self, records: dict[str, str]) -> None:
        self.records, self.calls = records, []

    def as_agent(self, *argv: str) -> list[str]:
        return ["sudo", "-u", "agent", "-H", "env", *argv]

    def run(self, argv, check=True, timeout=120):
        self.calls.append(list(argv))
        out = ""
        if "find" in argv:
            out = "\n".join(self.records) + "\n"
        elif "cat" in argv:
            out = self.records[argv[-1]]
        return SimpleNamespace(ok=True, returncode=0, stdout=out, stderr="")


def test_the_clean_up_stops_the_parts_jobs_by_their_records():
    """#935's review, B5: over SSH `zab job stop NAME` has no `ZAB_SESSION` and finds nothing.
    The clean-up finds the part's jobs by its nonce in their records, stops each with its own
    session's `zab job stop` (no end post, no report turn), then its unit by id; a record
    that isn't a job's, or a session that isn't one, is left alone or stopped by unit only."""
    nonce = "121748-a1b2c3"
    base = s18.JOBS_DIR
    vm = Shell({
        f"{base}/3f9c2a7b1d4e/job.json": f'{{"id": "3f9c2a7b1d4e", "name": "s18c-{nonce}", '
                                         '"session": "s-123"}',
        f"{base}/0123456789ab/job.json": '{"id": "0123456789ab", "name": "x", '
                                         '"session": "a b; rm -rf"}',
        f"{base}/ffffffffffff/job.json": "not json",
    })  # fmt: skip
    s18.stop_all(vm, nonce, f"S18C-{nonce}")
    finds = [c for c in vm.calls if "find" in c]
    assert finds and finds[0][-7:] == ["grep", "-l", "-F", "--", nonce, "{}", "+"]
    stops = [c[c.index("env") + 1 :] for c in vm.calls if "stop" in c]
    assert stops == [
        ["ZAB_SESSION=s-123", s18.ZAB, "job", "stop", f"s18c-{nonce}"],
        ["systemctl", "--user", "stop", "zab-job-3f9c2a7b1d4e.service"],
        ["systemctl", "--user", "stop", "zab-job-0123456789ab.service"],
    ]
    assert any(c[:3] == ["ps", "-eo", "pid=,args="] for c in vm.calls)  # `end_marked`


def test_job_help_raises_unless_the_build_has_no_zab_job():
    """#935's review, B6: only argparse's refusal reads as no `zab job`; an SSH failure or a
    timeout raises, never a skip."""
    has = Vm({"start --help": (0, "usage: zab job start")})
    assert installed.job_help(has) == "usage: zab job start"

    class Refusing(Vm):
        def run(self, argv, check=True, timeout=120):
            return SimpleNamespace(ok=False, returncode=self.code, stdout="", stderr=self.err)

    none = Refusing({})
    none.code, none.err = 2, "zab: error: argument command: invalid choice: 'job'"
    assert installed.job_help(none) == ""
    for code, err in ((255, "ssh: connect to host: Connection timed out"), (124, "")):
        down = Refusing({})
        down.code, down.err = code, err
        with pytest.raises(RuntimeError, match=f"exit {code}"):
            installed.job_help(down)


async def test_a_vm_wide_command_mentions_the_bot():
    """#935's review, B1: `!vm` in the Codex topic, unmentioned, is Claude's bot's to answer
    (the VM's first by name, SPEC §4.6); `command(kind=…)` mentions the agent's bot."""
    sent = []

    class User:
        async def send(self, stream, topic, text):
            sent.append(("send", text))
            return 7

        async def wait_reply(self, narrow, **kw):
            return {"id": 8, "after": kw["after_id"]}

    class Chat:
        stream, user = "sandbox", User()

        async def say(self, kind, topic, text):
            sent.append(("say", kind, text))
            return SimpleNamespace(message_id=9)

    turn = SimpleNamespace(bot=42)
    assert (await s18.command(Chat(), turn, "t", "!vm", "x", kind="codex"))["after"] == 9
    assert (await s18.command(Chat(), turn, "t", "!status", "x"))["after"] == 7
    assert sent == [("say", "codex", "!vm"), ("send", "!status")]


def test_part_5_reads_the_limit_and_the_jobs_directory_from_start():
    """#938's review, N1 and N3: the count limit and the job's directory come from what
    `zab job start` prints, whatever the VM's config sets."""
    out = (
        "Started background job `s18e0-x` (id 3f9c2a7b1d4e).\n"
        "Its log: /srv/zab-state/jobs/3f9c2a7b1d4e/output.log\n"
        "Limits: 12 hours for this job; 6 jobs at a time on this machine, all of them together "
        "at most 75% of its memory.\n"
    )
    assert s18.max_running(out) == 6
    assert s18.job_dir(out) == "/srv/zab-state/jobs/3f9c2a7b1d4e"
    with pytest.raises(AssertionError, match="no limit"):
        s18.max_running("Started.")


def test_the_slice_probe_raises_when_it_cant_read():
    """#938's review, N2: a failed read is an error, not a skip."""
    assert s18.slice_limited(Vm({"zab-jobs.slice": (0, "MemoryMax=25769803776\n")}))
    assert not s18.slice_limited(Vm({"zab-jobs.slice": (0, "MemoryMax=infinity\n")}))
    with pytest.raises(RuntimeError, match="exit 255"):
        s18.slice_limited(Vm({"zab-jobs.slice": (255, "")}))


def test_part_6_needs_report_turns_and_brings_the_vm_up_first():
    """#938's review, B1 and B2: its notes-block check needs #942, and its `finally` brings
    the VM up before any clean-up over SSH."""
    import inspect

    source = inspect.getsource(s18.test_job_lost_at_boot)
    assert 'needs_jobs(vm, "watcher", "reports")' in source
    after = source.split("finally:")[1]
    assert after.index("VM_STOPPED") < after.index("clean_up(")


def test_scenario_11_skips_without_the_jobs_idle_stop_rule(tmp_path, monkeypatch):
    """#938's review, B2: the tested build's `idle_stop.py` must count the jobs' units."""
    (tmp_path / "idle_stop.py").write_text('JOBS_ACTIVE = ["zab-job-*.service"]\n')
    monkeypatch.setattr(s11, "admin_dir", lambda: tmp_path)
    assert s11.holds_for_jobs()
    (tmp_path / "idle_stop.py").write_text("ACTIVE = []\n")
    assert not s11.holds_for_jobs()
