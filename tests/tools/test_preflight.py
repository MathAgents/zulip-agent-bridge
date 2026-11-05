"""`zab preflight` (SPEC §13.1): scripted pass, fail and timeout outcomes; the JSON report."""

from __future__ import annotations

import json
import os
from collections import namedtuple

import pytest

from zab.tools import preflight as pf
from zab.tools.common import RunResult

from .fakes import FakeRunner, fail, ok, ran_tool

Usage = namedtuple("Usage", "total used free")


def all_green(home: str) -> FakeRunner:
    return FakeRunner(
        ok(["codex", "login", "status"], "Logged in using ChatGPT\n"),
        ran_tool(["codex", "exec"]),
        ran_tool(["claude", "-p"]),
        ok(["git"]),
        ok(["gp", "--version-short"], "2.17.1\n"),
        ok([os.path.join(home, "venv", "bin", "python")], "2.2.0\n"),
        ok(["python3"], "2.2.0\n"),
        ok(["sage", "--version"], "SageMath version 10.5\n"),
    )


def make_repo(tmp_path):
    repo = tmp_path / "work" / "thesis"
    (repo / ".git").mkdir(parents=True)
    return str(repo)


def test_cypari2_probe_needs_no_dunder_version(tmp_path):
    """cypari2 has no `__version__` (2.2.4): the real probe, run by this interpreter against a
    stub package, must exit 0 and print the metadata version, or `installed` without metadata
    (codex round 2 on PR #19)."""
    import subprocess
    import sys

    site = tmp_path / "site"
    (site / "cypari2").mkdir(parents=True)
    (site / "cypari2" / "__init__.py").write_text("")  # no __version__, like the real package
    env = {**os.environ, "PYTHONPATH": str(site)}
    res = subprocess.run(  # noqa: S603 - this interpreter, a fixed snippet
        [sys.executable, "-c", pf.CYPARI2_PROBE], env=env, capture_output=True, text=True
    )
    assert res.returncode == 0 and res.stdout.strip() == "installed", res.stderr
    (site / "cypari2-9.9.dist-info").mkdir()
    (site / "cypari2-9.9.dist-info" / "METADATA").write_text("Name: cypari2\nVersion: 9.9\n")
    res = subprocess.run(  # noqa: S603
        [sys.executable, "-c", pf.CYPARI2_PROBE], env=env, capture_output=True, text=True
    )
    assert res.returncode == 0 and res.stdout.strip() == "9.9"
    # a missing package still fails by exit code
    res = subprocess.run(  # noqa: S603
        [sys.executable, "-c", pf.CYPARI2_PROBE],
        env={**os.environ, "PYTHONPATH": str(tmp_path / "empty")},
        capture_output=True,
        text=True,
    )
    assert res.returncode != 0


def test_cypari2_check_prefers_the_venv_and_judges_by_exit_code(tmp_path):
    home = str(tmp_path)
    venv_python = os.path.join(home, "venv", "bin", "python")
    os.makedirs(os.path.dirname(venv_python))
    open(venv_python, "w").close()
    run = FakeRunner(
        ok(["gp"], "2.17.1\n"),
        lambda argv: (
            RunResult(0, "2.2.4\n", "some harmless warning on stderr\n")
            if argv[0] == venv_python
            else None
        ),
        fail(["python3"], "must not be used when the venv exists"),
        fail(["sage"]),
    )
    checks = {c.name: c for c in pf.check_math_tools(run, home)}
    assert checks["cypari2"].ok and checks["cypari2"].detail == f"2.2.4 ({venv_python})"
    assert run.calls[1]["argv"] == [venv_python, "-c", pf.CYPARI2_PROBE]
    assert not any(c["argv"][0] == "python3" for c in run.calls)


def test_all_green(tmp_path, monkeypatch):
    home = str(tmp_path)
    repo = make_repo(tmp_path)
    monkeypatch.setattr(pf.shutil, "disk_usage", lambda p: Usage(1, 1, 50e9))
    checks = pf.run_all(all_green(home), home=home, repo_dir=repo, host="box")
    assert [c.name for c in checks] == [
        "codex login",
        "codex smoke",
        "claude smoke",
        "git push",
        "gp",
        "cypari2",
        "sage",
        "disk space",
    ]
    assert pf.all_ok(checks) and not any(c.warning for c in checks)
    assert next(c for c in checks if c.name == "disk space").detail == "50.0 GB free"


def test_smoke_prompts_scrub_keys_and_use_cheap_model(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-nope")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok")
    run = all_green(str(tmp_path))
    pf.check_claude_smoke(run, str(tmp_path))
    pf.check_codex_smoke(run, str(tmp_path))
    for call in run.calls:
        assert "ANTHROPIC_API_KEY" not in call["env"] and "ANTHROPIC_AUTH_TOKEN" not in call["env"]
        assert call["timeout_s"] == pf.SMOKE_TIMEOUT_S and call["cwd"] == str(tmp_path)
    claude, codex = run.calls[0]["argv"], run.calls[1]["argv"]
    assert claude[:4] == ["claude", "-p", "--model", "haiku"]
    paths = []
    for argv in (claude, codex):
        path = argv[-1].split("cat ", 1)[1].split()[0]
        assert argv[-1] == pf.SMOKE_PROMPT.format(path=path)
        assert os.path.basename(path).startswith("zab-preflight-")
        assert not os.path.exists(path)  # cleaned up
        paths.append(path)
    assert claude[4] == f"--allowedTools=Bash(cat {paths[0]})"


def test_smoke_fails_when_the_agent_cannot_run_the_command(tmp_path):
    """AMI v1: Codex answered every prompt but could not spawn its tool runner."""
    run = FakeRunner(
        ok(["codex", "exec"], "I could not run the command: failed to spawn code-mode host\n"),
        ran_tool(["claude", "-p"], before="Output: "),
    )
    codex = pf.check_codex_smoke(run, str(tmp_path))
    claude = pf.check_claude_smoke(run, str(tmp_path))
    assert not codex.ok and "did not run the shell command" in codex.detail
    assert claude.ok and not claude.warning


def test_failures_do_not_abort_the_others(tmp_path, monkeypatch):
    home = str(tmp_path)
    monkeypatch.setattr(pf.shutil, "disk_usage", lambda p: Usage(1, 1, 5e9))
    run = FakeRunner(
        fail(["codex", "login"], "Not logged in", 1),
        lambda argv: (
            RunResult(-1, "", "timed out after 120 s: codex")
            if argv[:2] == ["codex", "exec"]
            else None
        ),
        ok(["claude", "-p"], "Sure! Here is my answer.\n"),  # made up, no tool run
        ok(["gp"], "2.17.1\n"),
        fail(["python3"], "ModuleNotFoundError: No module named 'cypari2'"),
        fail(["sage"], "not found: sage", -1),
    )
    checks = {c.name: c for c in pf.run_all(run, home=home, repo_dir=None, host="box")}
    assert not checks["codex login"].ok and "Not logged in" in checks["codex login"].detail
    assert not checks["codex smoke"].ok and "timed out" in checks["codex smoke"].detail
    assert not checks["claude smoke"].ok and "did not run" in checks["claude smoke"].detail
    assert not checks["git push"].ok and "zab setup-repo" in checks["git push"].detail
    assert checks["gp"].ok
    assert not checks["cypari2"].ok and "cypari2" in checks["cypari2"].detail
    assert checks["sage"].ok and checks["sage"].warning
    assert not checks["disk space"].ok and "5.0 GB" in checks["disk space"].detail
    assert not pf.all_ok(checks.values())


def test_a_crashing_check_becomes_a_failed_check(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(pf, "check_codex_login", boom)
    checks = pf.run_all(all_green(str(tmp_path)), home=str(tmp_path), repo_dir=None, host="box")
    first = checks[0]
    assert first.name == "codex login" and not first.ok and "kaboom" in first.detail
    assert len(checks) == 8


def test_details_never_carry_tokens(tmp_path):
    run = FakeRunner(
        ok(["codex", "login", "status"], "token sk-proj-abcdefghijklmnopqrstuvwxyz0123\n")
    )
    check = pf.check_codex_login(run)
    assert "sk-proj" not in check.detail and "[redacted]" in check.detail


def test_git_push_needs_a_checkout(tmp_path):
    check = pf.check_git_push(FakeRunner(), str(tmp_path / "nope"), "box")
    assert not check.ok and "not a git checkout" in check.detail


def test_participant_repo_dir(tmp_path):
    f = tmp_path / "participant.json"
    f.write_text('{"github": "alice", "repo": "alice/thesis"}')
    assert pf.participant_repo_dir("/home/agent", str(f)) == "/home/agent/work/thesis"
    f.write_text('{"github": "alice", "repo": "thesis.git"}')
    assert pf.participant_repo_dir("/home/agent", str(f)) == "/home/agent/work/thesis"
    f.write_text("{}")
    assert pf.participant_repo_dir("/home/agent", str(f)) is None
    assert pf.participant_repo_dir("/home/agent", str(tmp_path / "missing")) is None


def test_main_writes_report_and_json(tmp_path, monkeypatch, capsys):
    home = str(tmp_path)
    repo = make_repo(tmp_path)
    monkeypatch.setattr(pf.shutil, "disk_usage", lambda p: Usage(1, 1, 50e9))
    rc = pf.main(
        ["--json", "--repo", repo], run=all_green(home), home=home, participant_file="/nonexistent"
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["version"] and out["host"] and out["time"].endswith("+00:00")
    assert {c["name"] for c in out["checks"]} >= {"codex login", "claude smoke", "git push"}
    assert all(set(c) == {"name", "ok", "detail", "warning"} for c in out["checks"])
    report = tmp_path / ".local" / "state" / "zab" / "preflight.json"
    assert json.loads(report.read_text()) == out


def test_main_table_and_exit_code_on_failure(tmp_path, monkeypatch, capsys):
    home = str(tmp_path)
    monkeypatch.setattr(pf.shutil, "disk_usage", lambda p: Usage(1, 1, 50e9))
    run = FakeRunner(
        fail(["codex"], "nope"),
        ran_tool(["claude"]),
        ok(["gp"]),
        ok(["python3"]),
        fail(["sage"]),
    )
    rc = pf.main(
        ["--state-dir", str(tmp_path / "st")], run=run, home=home, participant_file="/nonexistent"
    )
    assert rc == 1
    out = capsys.readouterr().out
    assert "FAIL codex login" in out and "OK   claude smoke" in out and "WARN sage" in out
    assert "post this in your workshop's channel" in out
    assert (tmp_path / "st" / "preflight.json").exists()


@pytest.mark.parametrize(
    "code,stdout,expected_ok",
    [
        (0, "zab-0123abcd\n", True),
        (0, "The output is `zab-0123abcd`.", True),
        (0, "pong", False),
        (1, "zab-0123abcd", False),
    ],
)
def test_smoke_verdicts(code, stdout, expected_ok):
    check = pf._smoke_verdict("x", RunResult(code, stdout, "boom"), "zab-0123abcd")
    assert (check.ok, check.warning) == (expected_ok, False)


def test_find_repo_dir_prefers_what_setup_repo_recorded(tmp_path):
    """The roster's `repo` may not match what the participant created in the homework."""
    from zab.tools import setup_repo as sr

    home = str(tmp_path)
    roster = tmp_path / "participant.json"
    roster.write_text('{"github": "alice", "repo": "alice/planned-name"}')
    actual = tmp_path / "work" / "actual-name"
    (actual / ".git").mkdir(parents=True)
    # Nothing recorded, roster repo not checked out: the only checkout in ~/work.
    assert pf.find_repo_dir(home, str(roster)) == str(actual)
    # A second checkout makes that ambiguous; the roster path is reported instead.
    (tmp_path / "work" / "other" / ".git").mkdir(parents=True)
    assert pf.find_repo_dir(home, str(roster)) == str(tmp_path / "work" / "planned-name")
    # The roster's repository wins once it is checked out ...
    (tmp_path / "work" / "planned-name" / ".git").mkdir(parents=True)
    assert pf.find_repo_dir(home, str(roster)) == str(tmp_path / "work" / "planned-name")
    # ... and what setup-repo recorded wins over everything.
    sr.record_checkout(home, str(actual))
    assert pf.find_repo_dir(home, str(roster)) == str(actual)
    # A recorded checkout that was deleted is ignored.
    sr.record_checkout(home, str(tmp_path / "gone"))
    assert pf.find_repo_dir(home, str(roster)) == str(tmp_path / "work" / "planned-name")


def test_find_repo_dir_without_any_checkout(tmp_path):
    assert pf.find_repo_dir(str(tmp_path), "/nonexistent") is None
