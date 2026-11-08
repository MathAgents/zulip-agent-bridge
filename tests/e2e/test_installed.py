"""Skips decided by what is installed, and the verdict when scenarios skip (reviewer on #286)."""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from e2e import run
from e2e.harness import installed, report
from e2e.harness.vm import Result
from e2e.test_scenarios_features import LINES, OLD, check_vm


class AnswersVm:
    def __init__(self, answers: list[Result]) -> None:
        self.answers, self.calls = answers, []

    def run(self, argv, check=True, timeout=0):
        self.calls.append(argv)
        return self.answers.pop(0)


def test_probes_ask_the_installed_build_and_fail_closed():
    vm = AnswersVm([Result(0, "True\n", ""), Result(0, "0.2.2\n", ""), Result(127, "", "none")])
    assert installed.has(vm, installed.VM_LINES)
    assert vm.calls[0][:7] == ["sudo", "-u", "agent", "-H", "bash", "-c", installed.PROBE]
    assert vm.calls[0][-1] == installed.VM_LINES  # the snippet travels as $1
    assert installed.ask(vm, installed.VERSION) == "0.2.2"
    with pytest.raises(RuntimeError, match="cannot ask"):
        installed.has(vm, installed.HAS_FORK)  # an unknown build is an error, never a skip
    with pytest.raises(RuntimeError, match="unexpected"):
        installed.has(AnswersVm([Result(0, "maybe", "")]), installed.HAS_VM)


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_the_probe_and_seed_scripts_parse():
    from e2e.test_scenarios_features import SEED, SHARED_PROBE

    for script in (installed.PROBE, SEED, SHARED_PROBE):
        subprocess.run(["bash", "-n", "-c", script], check=True)  # noqa: S603, S607
    assert '"$1"' in SEED and "{" not in SEED.replace("${", "")  # inputs are arguments only


#: #281's `!vm` at 7368c97, rendered by its own `vmstat.format_report` (`resources`) with
#: `zab.core.vm`'s status line: a second disk, and every number unreadable
NEW_VM = """**ws-zab-test**
🟩🟩⬜⬜⬜⬜⬜⬜⬜⬜ 18% of 8 CPUs
🟩🟩🟩⬜⬜⬜⬜⬜⬜⬜ 32% of 31 GB memory
🟩🟩🟩🟩🟩⬜⬜⬜⬜⬜ 46% of 97 GB disk
🟩⬜⬜⬜⬜⬜⬜⬜⬜⬜ 6% of 200 GB disk at `/data`

1 turn running · up 2 hours · Codex and Claude signed in · zab 0.2.1
Using the most: `lean` 8.0 GB memory, 95% CPU"""
NEW_UNREADABLE = """**ws-zab-test**
8 CPUs: unavailable
memory: unavailable
disk: unavailable

1 turn running · up 2 hours · Codex and Claude signed in · zab 0.2.1"""
NEW_USAGE = "VM: CPU 18% · memory 32% · disk 46%"
#: main's `!vm` (the one-line format), rendered by its `vmstat.format_report`, with
#: `zab.core.vm.report`'s bridge line, and its `!usage` line
OLD_VM = (
    "**ws-zab-test** · load 0.4 / 0.6 / 0.7 (8 CPUs) · memory 4.0 of 31 GiB in use, 27 available"
    " · swap 0 of 8 GiB · disk 41 of 200 GiB (21%) · up 2 h\n"
    "bridge 0.2.2 · 1 turn running · Claude signed in · Codex signed in"
)
OLD_USAGE = "VM: load 0.4/8 · memory 4/31 GiB · disk 21%"


def test_scenario_6_reads_the_format_of_the_installed_build():
    """Reviewer on #286: #281 now prints a line per resource (7368c97), detected by
    `vmstat.resources`; main keeps the one-line format."""
    import re

    check_vm(NEW_VM, LINES)
    check_vm(NEW_UNREADABLE, LINES)
    check_vm(NEW_VM.replace("**ws-zab-test**\n", ""), LINES)  # the host's line may move
    check_vm(OLD_VM, OLD)
    for text, fmt in ((OLD_VM, LINES), (NEW_VM, OLD), (NEW_VM.replace("18% of", "of"), LINES)):
        with pytest.raises(AssertionError):
            check_vm(text, fmt)
    assert re.search(LINES.usage, NEW_USAGE) and not re.search(LINES.usage, OLD_USAGE)
    assert re.search(OLD.usage, OLD_USAGE) and not re.search(OLD.usage, NEW_USAGE)
    assert re.search(LINES.usage, "VM: CPU unavailable · memory unavailable · disk unavailable")


#: #441's `!vm` in a DM and its `!usage` line, rendered by its `vmstat.format_report`
CPUS_VM = (
    NEW_VM.replace("18% of 8 CPUs", "1.4 of 8 CPUs").replace("95% CPU", "1.0 CPUs")
    + " · `sage -c load('x.sage')` 450 MB memory, 2.1 CPUs"
)
CPUS_USAGE = "VM: 1.4 of 8 CPUs · memory 32% · disk 46%"


def test_scenario_6_checks_cpu_use_in_cpus():
    """#441: a build with `vmstat.cpus_text` states CPU use in CPUs, in a DM's processes too,
    and never as a percentage; the older builds keep their own formats."""
    import re

    from e2e.test_scenarios_features import CPUS, vm_format

    check_vm(CPUS_VM, CPUS, dm=True)
    check_vm(CPUS_VM.split("\nUsing the most")[0], CPUS)  # a channel's reply: no processes
    check_vm(NEW_UNREADABLE, CPUS)
    for text in (NEW_VM, CPUS_VM.replace("2.1 CPUs", "210% CPU"), CPUS_VM.split("\nUsing")[0]):
        with pytest.raises(AssertionError):
            check_vm(text, CPUS, dm=True)
    with pytest.raises(AssertionError):
        check_vm(CPUS_VM, LINES)
    assert re.search(CPUS.usage, CPUS_USAGE) and not re.search(CPUS.usage, NEW_USAGE)
    assert not re.search(LINES.usage, CPUS_USAGE)
    assert re.search(CPUS.usage, "VM: CPU unavailable · memory unavailable · disk unavailable")
    for said, fmt in ((["True", "True"], CPUS), (["True", "False"], LINES), (["False"], OLD)):
        vm = AnswersVm([Result(0, s + "\n", "") for s in said])
        assert vm_format(vm) is fmt and not vm.answers  # asked of the build, as far as needed
    assert [call[-1] for call in vm.calls] == [installed.VM_LINES]
    vm = AnswersVm([Result(0, "True\n", ""), Result(0, "True\n", "")])
    vm_format(vm)
    assert [call[-1] for call in vm.calls] == [installed.VM_LINES, installed.VM_CPUS]


def test_scenario_14_reads_claude_sign_in_from_vm():
    """Scenario 14 leaves Claude out only on an explicit "not signed in" in `!vm`."""
    from e2e.test_scenarios_slow import claude_sign_in

    line = "Codex and Claude signed in"
    assert claude_sign_in(NEW_VM) is True and claude_sign_in(OLD_VM) is True
    assert claude_sign_in(NEW_VM.replace(line, "Codex signed in · Claude not signed in")) is False
    assert claude_sign_in(OLD_VM.replace("Claude signed", "Claude not signed")) is False
    unknown = NEW_VM.replace(line, "Codex signed in · Claude sign-in status unavailable")
    assert claude_sign_in(unknown) is None and claude_sign_in("VM statistics unavailable") is None


class VmChat:
    """A `Chat` whose topic messages are answered, in turn, by `replies`, each as the wait
    would match it."""

    def __init__(self, replies: list[str]) -> None:
        self.replies, self.asked = replies, []

    async def say(self, kind: str, topic: str, text: str, *, mention_bot: bool = True) -> int:
        assert topic == "e2e-10-vm-n" and mention_bot  # a topic: a DM `!vm` is the sentinel's
        self.asked.append((kind, text))
        return len(self.asked)

    async def reply(self, turn: int, pattern: str, *, within: float):
        import re

        text = self.replies.pop(0)
        assert re.search(pattern, text), "the wait would not take this reply"
        return {"content": text}, 0.0


async def test_scenario_10_reads_claude_sign_in_from_vm(monkeypatch):
    """#409: the stream wake picks its bot by `!vm`'s sign-in, never by `!status`'s `!login`
    hint; an unavailable status is asked again, then fails the scenario with its line."""
    import re

    from e2e import test_scenarios_slow as slow

    monkeypatch.setattr(slow, "SIGN_IN_RETRY_S", 0)
    line = "Codex and Claude signed in"
    out = NEW_VM.replace(line, "Codex signed in · Claude not signed in")
    unknown = NEW_VM.replace(line, "Codex signed in · Claude sign-in status unavailable")
    row = report.ScenarioResult(10, "Waker")
    chat = VmChat([NEW_VM])
    topic = "e2e-10-vm-n"
    assert await slow.signed_in(chat, row, topic) == "claude"
    assert chat.asked == [("codex", "!vm")]
    assert await slow.signed_in(VmChat([OLD_VM.replace("Claude signed", "Claude not signed")]),
                                row, topic) == "codex"  # fmt: skip
    assert not row.notes
    chat = VmChat([unknown, out])
    assert await slow.signed_in(chat, row, topic) == "codex" and len(chat.asked) == 2
    assert row.notes == ["Claude's sign-in status unavailable until `!vm` 2"]
    chat = VmChat([unknown] * slow.SIGN_IN_ASKS)
    with pytest.raises(pytest.fail.Exception, match="Claude sign-in status unavailable"):
        await slow.signed_in(chat, row, topic)
    assert len(chat.asked) == slow.SIGN_IN_ASKS
    # the sentinel's `!vm`, a DM's: it has no sign-in to read
    sentinel = "bridge running, last Zulip event 40 seconds ago · up 2 hours · zab 0.2.9"
    assert not re.search(slow.VM_SIGN_IN_RE, sentinel)


WORKSHOP = Path(__file__).resolve().parents[2] / "workshop"


def agents_md(managed: str) -> str:
    """`~/.codex/AGENTS.md` as push_config's `agent_instructions.py` writes it."""
    path = WORKSHOP / "admin" / "agent_instructions.py"
    spec = importlib.util.spec_from_file_location("e2e_agent_instructions", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.merge_agents(None, managed)


SHIPPED = (WORKSHOP / "ami" / "files" / "agent-instructions.md").read_text()
#: The instructions before #448: the bullet that says files render, removed
BEFORE = re.sub(r"^- There, Markdown renders.*?(?=^- )", "", SHIPPED, flags=re.S | re.M)
TOLD = ["the agents' instructions say files render", "the bridge links lines (#L) there"]


def test_the_installed_instructions_say_whether_files_render():
    """#448: the organizers' block of the installed `~/.codex/AGENTS.md`, read as agent, and
    the installed bridge's line anchors; the participant's own section never counts."""
    assert BEFORE != SHIPPED and installed.FILES_RENDER.search(SHIPPED)
    assert not installed.FILES_RENDER.search(BEFORE)
    vm = AnswersVm([Result(0, agents_md(SHIPPED), ""), Result(0, "True\n", "")])
    assert installed.files_render(vm) == TOLD
    assert vm.calls[0] == ["sudo", "-u", "agent", "-H", "cat", "--", installed.INSTRUCTIONS]
    assert vm.calls[1][-1] == installed.LINE_ANCHORS
    block = installed.instructions(AnswersVm([Result(0, agents_md(SHIPPED), "")]))
    assert block.startswith("<!-- Maintained by the workshop organizers")
    assert block.endswith("\n\n" + SHIPPED.strip() + "\n")  # and no personal section
    line = SHIPPED[SHIPPED.index("- There, Markdown") :]
    noted = agents_md(BEFORE) + "\n" + line  # in the participant's section
    for text, anchors, told in (
        (agents_md(BEFORE), "False", []),
        (noted, "False", []),
        ("no markers\n" + line, "False", []),
        (agents_md(BEFORE), "True", TOLD[1:]),
        (agents_md(SHIPPED), "False", TOLD[:1]),
    ):
        vm = AnswersVm([Result(0, text, ""), Result(0, anchors + "\n", "")])
        assert installed.files_render(vm) == told
    with pytest.raises(RuntimeError, match="cannot read"):  # an error, never a skip
        installed.files_render(AnswersVm([Result(1, "", "cat: No such file or directory")]))


class FileChat:
    """A `Chat` whose one turn is answered with `text`."""

    def __init__(self, text: str) -> None:
        self.text = text

    async def say(self, kind: str, topic: str, text: str) -> int:
        return 1

    async def reply(self, turn: int, pattern: str, *, within: float):
        assert re.search(pattern, self.text), "the wait would not take this reply"
        return {"content": self.text}, 0.0


@pytest.mark.parametrize(("text", "anchors", "outcome"), [
    (agents_md(SHIPPED), "True", "fail"),
    (agents_md(SHIPPED), "False", "fail"),
    (agents_md(BEFORE), "True", "fail"),
    (agents_md(BEFORE), "False", "skip"),
], ids=["both", "instructions", "bridge", "neither"])  # fmt: skip
async def test_scenario_9_fails_without_rendering_once_the_build_says_files_render(
    monkeypatch, text, anchors, outcome
):
    """#448: a host without `Vary: Sec-Fetch-Dest` fails scenario 9 when the installed
    instructions say files render or the installed bridge links lines there; it skips only
    when the build predates both."""
    from e2e import test_scenarios_features as features
    from e2e.harness import files

    base, nonce = "https://files.example.org", "n1"
    url = f"{base}/zab-test/{'c' * 40}/notes-{nonce}.md"

    async def old_host(link, files_url, **_):
        assert (link, files_url) == (url, base)
        return files.Page(200, "text/plain", "# notes")  # no Vary: no #256 rendering

    monkeypatch.setattr(files, "fetch", old_host)
    chat = FileChat(f"Created [notes-{nonce}.md]({url}).")
    vm = AnswersVm([Result(0, text, ""), Result(0, anchors + "\n", "")])
    scenario = report.ScenarioResult(9, "File server")
    expected = pytest.fail.Exception if outcome == "fail" else pytest.skip.Exception
    with pytest.raises(expected, match="no #256 rendering") as caught:
        await features.test_file_server(chat, SimpleNamespace(files_url=base), vm, scenario, nonce)
    assert ("(#448)" in str(caught.value)) == (outcome == "fail")


@pytest.mark.parametrize(("fragment", "row", "anchors", "error"), [
    ("#L2", '<tr id="L2">', "True", None),
    ("", '<tr id="L2">', "True", "no file-server link to check-n1.py with #L2"),
    ("#L2", '<tr id="L1">', "True", "no row L2 on the page"),
    ("", "", "False", None),
], ids=["linked", "no-anchor", "no-row", "bridge-predates"])  # fmt: skip
async def test_scenario_9_follows_a_line_link_on_a_rendering_host(
    monkeypatch, fragment, row, anchors, error
):
    """Reviewer on #449: on a rendering host, the answer's `check-….py:2` must link with `#L2`
    to a page with the row `id="L2"`; an installed bridge without #448 is noted instead."""
    from e2e import test_scenarios_features as features
    from e2e.harness import files

    base, nonce, snap = "https://files.example.org", "n1", f"https://files.example.org/t/{'c' * 40}"
    pages = {
        f"{snap}/notes-{nonce}.md": '<link href="katex.css"><pre><span class="k">def</span></pre>',
        f"{snap}/check-{nonce}.py": f'<table class="source hl"><tbody>{row}</tbody></table>',
    }

    async def host(link, files_url, **_):
        if link.endswith("?raw=1"):
            return files.Page(200, "text/plain", "$e^{i\\pi} + 1 = 0$", "Sec-Fetch-Dest")
        return files.Page(200, "text/html", pages[link], "Sec-Fetch-Dest")

    monkeypatch.setattr(files, "fetch", host)
    chat = FileChat(
        f"[notes-{nonce}.md]({snap}/notes-{nonce}.md) and "
        f"[check-{nonce}.py:2]({snap}/check-{nonce}.py{fragment})"
    )
    vm = AnswersVm([Result(0, anchors + "\n", "")])  # only the bridge's probe: the host renders
    scenario = report.ScenarioResult(9, "File server")
    run9 = features.test_file_server(chat, SimpleNamespace(files_url=base), vm, scenario, nonce)
    if error:
        with pytest.raises(AssertionError, match=re.escape(error)):
            await run9
        return
    await run9
    assert vm.calls[0][-1] == installed.LINE_ANCHORS
    note = scenario.notes[-1]
    assert note == (f"line link {snap}/check-{nonce}.py#L2" if anchors == "True"
                    else "line link not checked: the installed bridge predates #448")  # fmt: skip


def test_required_skips_fail_and_other_skips_are_no_result():
    skipped = report.ScenarioResult(9, "File server", "skipped", reason="no #256 rendering")
    passed = report.ScenarioResult(1, "Basics")
    assert report.Report(results=[passed]).verdict == "PASS"
    assert report.Report().verdict == "NO-RESULT"  # nothing passed: no gate
    loose = report.Report(results=[passed, skipped])
    assert loose.verdict == "NO-RESULT" and not loose.passed
    strict = report.Report(results=[passed, skipped], required=[1, 9])
    assert strict.verdict == "FAIL" and strict.missed() == [9]
    text = strict.markdown()
    assert text.startswith("## End-to-end: FAIL") and "not passed: 9" in text
    assert "- **9. File server**: no #256 rendering" in text
    assert report.Report(results=[passed], required=[1, 2]).missed() == [2]  # no row at all
    assert run.required("rc") == list(range(1, 18)) and run.required("") == []
    assert run.required("1,9") == [1, 9]


def test_exit_codes_follow_the_verdict():
    assert run.EXIT == {"PASS": 0, "FAIL": 1, "NO-RESULT": 3}


def test_a_partly_skipped_scenario_is_not_complete():
    """Codex on #286, both reproductions: scenario 12 with history passed and !fork skipped,
    and scenario 8's skipped fast checks merged with a passing cold import, satisfied
    `--require` with a PASS. A skipped part now stays on the row, through recording and
    merging: FAIL when the scenario is required, NO-RESULT otherwise."""
    from e2e.harness.recorder import Recorder

    rec = Recorder()
    rec.finish(12, "test_history", "passed", 30, "")
    rec.finish(12, "test_fork", "skipped", 1, "the installed zab has no !fork (#202)")
    run12 = rec.report()
    assert run12.results[0].outcome == "passed" and run12.results[0].skipped_parts == ["test_fork"]
    assert run12.verdict == "NO-RESULT"
    run12.required = [12]
    assert run12.verdict == "FAIL" and run12.missed() == [12]
    assert "(skipped: test_fork)" in run12.markdown()

    build = {"commit": "abc", "wheel": "w.whl"}
    fast, slow = Recorder(), Recorder()
    fast.finish(8, "test_lean_proof[claude]", "skipped", 1, "no shared Mathlib (#205)")
    slow.finish(8, "test_lean_cold_import", "passed", 300, "")
    merged = report.Report(**build, results=fast.report().results, required=[8])
    merged.merge(report.Report(**build, results=slow.report().results))
    (row,) = merged.results
    assert row.outcome == "passed" and row.skipped_parts == ["test_lean_proof[claude]"]
    assert merged.verdict == "FAIL" and merged.missed() == [8]
    done = Recorder()
    done.finish(12, "test_history", "passed", 30, "")
    done.finish(12, "test_fork", "passed", 40, "")
    assert report.Report(results=done.report().results, required=[12]).verdict == "PASS"


def test_the_shared_mathlib_is_read_from_the_vm(monkeypatch):
    """#258/#259 put it at /opt/lean/packages/<tag>, linked from ~/lean/workspace: the scenario
    asks the VM, and skips (a failure under --require) only when there is none."""
    from e2e.test_scenarios_features import SHARED_PROBE, require_shared_mathlib

    monkeypatch.delenv("ZAB_E2E_LEAN_SHARED", raising=False)
    row = report.ScenarioResult(8, "Lean")
    vm = AnswersVm([Result(0, "/opt/lean/packages/v4.34.1\n", "")])
    assert require_shared_mathlib(vm, row) == "/opt/lean/packages/v4.34.1"
    assert vm.calls[0][-3:] == ["bash", "-c", SHARED_PROBE] and row.notes
    assert vm.calls[0][:4] == ["sudo", "-u", "agent", "-H"]  # ubuntu can't enter /home/agent
    with pytest.raises(pytest.skip.Exception, match="no shared Mathlib"):
        require_shared_mathlib(AnswersVm([Result(1, "", "")]), row)
    monkeypatch.setenv("ZAB_E2E_LEAN_SHARED", "/srv/mathlib")
    vm = AnswersVm([Result(0, "", "")])
    assert require_shared_mathlib(vm, row) == "/srv/mathlib"
    assert vm.calls[0][-3:] == ["test", "-d", "/srv/mathlib"] and vm.calls[0][2] == "agent"


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_the_shared_probe_follows_the_workspace_link(tmp_path):
    from e2e.test_scenarios_features import SHARED_PROBE

    script = SHARED_PROBE.replace("/home/agent/lean/workspace", str(tmp_path / "ws"))
    script = script.replace("/opt/lean/packages", str(tmp_path / "opt"))
    (tmp_path / "ws" / ".lake").mkdir(parents=True)
    (tmp_path / "opt" / "v4.34.1").mkdir(parents=True)
    probe = ["bash", "-c", script]
    assert subprocess.run(probe, check=False).returncode == 1  # noqa: S603 - no link, none
    (tmp_path / "ws" / ".lake" / "packages").symlink_to(tmp_path / "opt" / "v4.34.1")
    done = subprocess.run(probe, capture_output=True, text=True, check=True)  # noqa: S603
    assert done.stdout.strip() == str(tmp_path / "opt" / "v4.34.1")


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_the_seed_commits_on_main_and_pushes_only_the_project(tmp_path):
    """Reviewer on #286: the project goes on the checkout's branch and to the repository before
    the topic starts (so the new worktree has it); a change already staged there stays out."""
    from e2e.test_scenarios_features import SEED, SEED_DIR

    def git(*args, cwd=tmp_path):
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,  # noqa: S603, S607
                              text=True).stdout  # fmt: skip

    git("init", "-q", "--bare", "-b", "main", "origin.git")
    git("clone", "-q", str(tmp_path / "origin.git"), "repo")
    repo = tmp_path / "repo"
    for key, value in (("user.name", "Zab Tester"), ("user.email", "t@example.org")):
        git("config", key, value, cwd=repo)
    (repo / "README.md").write_text("x\n")
    git("add", "README.md", cwd=repo)
    git("commit", "-q", "-m", "init", cwd=repo)
    git("push", "-q", "origin", "HEAD:main", cwd=repo)
    (repo / "notes.txt").write_text("staged, not for the seed\n")
    git("add", "notes.txt", cwd=repo)
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "lakefile.toml").write_text(WORKSPACE_LAKEFILE)
    for name in ("lean-toolchain", "lake-manifest.json"):
        (ws / name).write_text(name)
    script = SEED.replace("/home/agent/lean/workspace", str(ws))
    subprocess.run(["bash", "-c", script, "bash", str(repo), SEED_DIR], check=True)  # noqa: S603, S607
    pushed = git("ls-tree", "-r", "--name-only", "main", cwd=tmp_path / "origin.git").split()
    assert sorted(pushed) == ["README.md", *sorted(f"{SEED_DIR}/{n}" for n in (
        "E2eLean.lean", "lake-manifest.json", "lakefile.toml", "lean-toolchain"))]  # fmt: skip
    assert "notes.txt" in git("diff", "--cached", "--name-only", cwd=repo)  # still only staged
    for name in ("lean-toolchain", "lake-manifest.json"):  # the pins, verbatim
        assert (repo / SEED_DIR / name).read_text() == name


#: What `lake new workspace math` writes (build.sh, Lean v4.34.1), as on the development machine's
#: projects
WORKSPACE_LAKEFILE = """name = "workspace"
version = "0.1.0"
keywords = ["math"]
defaultTargets = ["Workspace"]

[leanOptions]
pp.unicode.fun = true # pretty-prints `fun a ↦ b`
relaxedAutoImplicit = false
weak.linter.mathlibStandardSet = true
maxSynthPendingDepth = 3

[[require]]
name = "mathlib"
scope = "leanprover-community"
rev = "v4.34.1"

[[lean_lib]]
name = "Workspace"
"""


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_the_seeded_project_builds_its_own_file_by_default(tmp_path):
    """Codex on #286: the workspace's lakefile alone made `lake build` build `Workspace`, whose
    sources stay behind. The seed keeps that lakefile (name, options, the Mathlib pin) and
    makes its one file's library the default target."""
    import tomllib

    from e2e.test_scenarios_features import SEED, SEED_DIR

    ws, repo = tmp_path / "ws", tmp_path / "repo"
    ws.mkdir()
    (ws / "lakefile.toml").write_text(WORKSPACE_LAKEFILE)
    for name in ("lean-toolchain", "lake-manifest.json"):
        (ws / name).write_text(name)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)  # noqa: S603, S607
    # stop before the commit: only the files matter here
    script = SEED.replace("/home/agent/lean/workspace", str(ws)).split("git add")[0]
    subprocess.run(["bash", "-c", script, "bash", str(repo), SEED_DIR], check=True)  # noqa: S603, S607
    seeded = tomllib.loads((repo / SEED_DIR / "lakefile.toml").read_text())
    original = tomllib.loads(WORKSPACE_LAKEFILE)
    assert seeded["defaultTargets"] == ["E2eLean"]
    assert [lib["name"] for lib in seeded["lean_lib"]] == ["Workspace", "E2eLean"]
    assert (repo / SEED_DIR / "E2eLean.lean").read_text().startswith("import Mathlib\n")
    for key in ("name", "require", "leanOptions"):  # everything else is the workspace's
        assert seeded[key] == original[key]
    (ws / "lakefile.toml").unlink()  # no workspace lakefile: the seed fails, it doesn't guess
    again = subprocess.run(["bash", "-c", script, "bash", str(repo), "other"], check=False)  # noqa: S603, S607
    assert again.returncode != 0


def test_the_tools_check_matches_the_bridges_reply_to_the_oct9_list():
    """Scenario 6's `!tools` pattern (#475) against the bridge's own reply to the list the
    rollout installs on the test VM; never against the sentence for a missing list."""
    from e2e.test_scenarios_features import TOOLS_REPLY
    from zab.core import software

    listing = (WORKSHOP / "admin" / "tools-nt-agents-2026-v3.json").read_text()
    text = software.render(software.parse(listing), 9800)
    assert re.search(TOOLS_REPLY, text, re.S)
    for sentence in (software.MISSING, software.UNREADABLE, software.EMPTY):
        assert not re.search(TOOLS_REPLY, sentence, re.S)
    probe = installed.HAS_TOOLS.split("; ", 1)[1]
    assert "zab.core.software" in probe and importlib.util.find_spec("zab.core.software")


async def test_scenarios_6_and_15_match_the_sentinels_own_replies(tmp_path):
    """Scenario 15's patterns, and scenario 6's DM `!vm` once the sentinel answers it (#204),
    against what `zab.tools.sentinel` itself posts, with fakes for systemd and the clock; never
    against the bridge's `!vm`, which has the turns and the sign-in."""
    import os

    from e2e import test_scenarios_sentinel as s15
    from e2e.test_scenarios_features import CPUS, SENTINEL_VM
    from zab.commands import service
    from zab.config import BotConfig, BridgeConfig, Config, RenderConfig
    from zab.tools import sentinel, vmstat

    posted: list[str] = []
    clock = [1_800_000_000.0]
    beat = tmp_path / "heartbeat-claude"
    systemd = {"active": "active", "pids": [500, 0, 600]}

    class Client:
        async def request(self, method, endpoint, **params):
            posted.append(params["content"])
            return {}

    async def run(argv, seconds):
        if argv[2] == "is-active":
            return 0, systemd["active"]
        if argv[2] == "show":
            pids = systemd["pids"]
            return 0, str(pids.pop(0) if len(pids) > 1 else pids[0])
        return 0, ""

    def touch(at: float) -> None:
        beat.touch()
        os.utime(beat, (at, at))

    async def sleep(seconds):
        clock[0] += seconds
        touch(clock[0])  # the bridge's listener advances

    stat = vmstat.VmStat(
        host="ws-zab-test", cpus=8, load=(0.4, 0.6, 0.7), mem_total=31 * 1024**3,
        mem_available=21 * 1024**3, uptime=7300.0,
        disks=(vmstat.Disk("/", 97 * 1024**3, 41 * 1024**3, 51 * 1024**3),),
        heaviest=(vmstat.Proc(1, "lean", 8 * 1024**3, 95.0, 60.0),), unavailable=(), cpu=0.18,
    )  # fmt: skip
    bot = BotConfig("claude", "unused", "claude")
    config = Config(tmp_path / "c.toml", BridgeConfig(state_dir=str(tmp_path)), [bot], {}, [],
                    RenderConfig())  # fmt: skip
    s = sentinel.Sentinel(
        config, bot, Client(), sentinel.Shared(clock=lambda: clock[0]), run=run,
        collect=lambda paths: stat, now=lambda: clock[0], sleep=sleep,
        units=lambda: ["codex-app-server.service"], hold=str(tmp_path / "hold"),
    )  # fmt: skip
    touch(clock[0] - 40)
    running = await s.vm_report()
    check_vm(running, SENTINEL_VM, dm=True)
    assert re.search(s15.RUNNING, running) and not re.search(s15.STOPPED, running)
    systemd["active"] = "inactive"
    stopped = await s.vm_report()
    check_vm(stopped, SENTINEL_VM, dm=True)
    assert re.search(s15.STOPPED, stopped) and not re.search(s15.RUNNING, stopped)
    for bridge in (NEW_VM, CPUS_VM):  # the bridge's `!vm`, never taken for the sentinel's
        with pytest.raises(AssertionError):
            check_vm(bridge, SENTINEL_VM, dm=True)
    with pytest.raises(AssertionError):
        check_vm(running, CPUS, dm=True)
    systemd["active"] = "active"
    await s.restart_bridge(1)
    await s.restart_bridge(1)
    clock[0] += 61
    await s.restart_codex(1)
    assert s15.matching(
        posted, s15.RESTARTING, s15.BACK, s15.REFUSED, s15.CODEX_RESTARTING, s15.CODEX_BACK
    ), posted
    assert not s15.matching(posted[:2], s15.RESTARTING, s15.REFUSED)
    # the probe asks the build for the unit that `zab service install` writes
    assert "service" in installed.HAS_SENTINEL_UNIT and service.SENTINEL == s15.SENTINEL_UNIT


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_scenario_15_reads_a_leftover_hold_as_the_sentinel_does(tmp_path):
    """#588 (review on #573): scenario 15 stops at once on an organizers' hold left on the VM,
    read as the sentinel reads it (`held`): its expiry, or an hour from the mtime of one that
    can't be read."""
    import os
    import time

    from e2e import test_scenarios_sentinel as s15
    from zab.tools import sentinel

    assert s15.HOLD == sentinel.HOLD
    hold = tmp_path / "hold"

    def left() -> float:
        argv = ["bash", "-c", s15.HOLD_READ, "bash", str(hold)]
        out = subprocess.run(argv, capture_output=True, text=True, check=True).stdout  # noqa: S603
        return s15.hold_left(out)

    assert left() == 0  # none
    now = time.time()
    for text, mtime in ((f"{now + 600:.0f}\n", now), ("?", now), (f"{now - 5:.0f}", now),
                        ("?", now - 4000), ("", now)):  # fmt: skip
        hold.write_text(text)
        os.utime(hold, (mtime, mtime))
        assert (left() > 0) == sentinel.held(time.time(), str(hold)), (text, mtime - now)
    hold.write_text(str(int(now) + 600))  # whole seconds, as `date +%s` counts them
    assert 500 < left() <= 600
    if os.geteuid():  # root reads it anyway
        hold.chmod(0)
        assert 3500 < left() <= 3600  # unreadable: an hour from its mtime


async def test_scenario_15_asserts_the_doctors_sentinel_checks(tmp_path, monkeypatch):
    """#588: what scenario 15 reads of `zab doctor --json` on a healthy VM, from the doctor's
    own checks and output format: a `<bot>:sentinel` check per bot, OK and no warning."""
    import json
    import os
    from dataclasses import asdict

    from e2e import test_scenarios_sentinel as s15
    from zab.commands import doctor
    from zab.config import BotConfig, BridgeConfig, Config, RenderConfig
    from zab.tools import sentinel

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    units = tmp_path / "systemd" / "user"
    units.mkdir(parents=True)
    for unit in ("zab.service", "zab-sentinel.service"):
        (units / unit).write_text("unit")

    async def active(argv, seconds):
        return 0, "active"

    monkeypatch.setattr(sentinel, "run_command", active)
    bots = [BotConfig("claude", "rc", "claude", [1]), BotConfig("codex", "rc", "codex", [1])]
    config = Config(tmp_path / "c.toml", BridgeConfig(state_dir=str(tmp_path)), bots, {}, [],
                    RenderConfig())  # fmt: skip
    for n, bot in enumerate(bots):
        mark = {"pid": os.getpid(), "boot": sentinel.boot_id(), "bot_id": 100 + n}
        sentinel.mark_path(tmp_path, bot.name).write_text(json.dumps(mark))
    found = await doctor.sentinel_checks(config, {"claude": 100, "codex": 101})
    printed = json.dumps([asdict(c) for c in [doctor.Check(name="git", ok=True), *found]])
    checks = s15.sentinel_checks(printed)  # as `zab doctor --json` prints them
    assert [c["name"] for c in checks] == ["claude:sentinel", "codex:sentinel"]
    assert all(c["ok"] and not c["warning"] for c in checks)
    assert s15.sentinel_checks("Traceback (most recent call last):") == []
    assert s15.sentinel_checks('{"name": "x:sentinel"}') == s15.sentinel_checks("[1, null]") == []
    assert "sentinel_checks" in installed.HAS_SENTINEL_CHECK
