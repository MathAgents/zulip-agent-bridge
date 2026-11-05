"""`lean-project` (issue #205): one read-only Mathlib per VM, linked into every project."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "workshop" / "ami" / "files" / "bin" / "lean-project"
TOOLCHAIN = "leanprover/lean4:v4.34.1"
MATHLIB = "d13f23b723b8a846827a245b89c10fc7d3f11612"
LAKEFILE = """name = "workspace"
version = "0.1.0"
keywords = ["math"]
defaultTargets = ["Workspace"]

[leanOptions]
pp.unicode.fun = true

[[require]]
name = "mathlib"
scope = "leanprover-community"
rev = "v4.34.1"

[[lean_lib]]
name = "Workspace"
"""


def load():
    loader = SourceFileLoader("lean_project", str(SCRIPT))
    spec = importlib.util.spec_from_loader("lean_project", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


lp = load()


UPSTREAM = "https://github.com/leanprover-community/"


def manifest(name: str = "workspace", rev: str = MATHLIB, url: str = UPSTREAM) -> dict:
    return {
        "version": "1.2.0",
        "packagesDir": ".lake/packages",
        "name": name,
        "lakeDir": ".lake",
        "packages": [
            {
                "name": "mathlib",
                "rev": rev,
                "inputRev": "v4.34.1",
                "type": "git",
                "url": url + "mathlib4",
                "subDir": None,
            },
            {
                "name": "batteries",
                "rev": "f2effa3d",
                "inputRev": "main",
                "type": "git",
                "url": UPSTREAM + "batteries",
                "subDir": None,
            },
        ],
    }


def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "home" / "lean" / "workspace"
    (ws / ".lake" / "packages" / "mathlib" / ".lake" / "build" / "bin").mkdir(parents=True)
    (ws / "lean-toolchain").write_text(TOOLCHAIN + "\n")
    (ws / "lakefile.toml").write_text(LAKEFILE)
    (ws / "lake-manifest.json").write_text(json.dumps(manifest()))
    olean = ws / ".lake" / "packages" / "mathlib" / ".lake" / "build" / "Mathlib.olean"
    olean.write_bytes(b"olean")
    cache = ws / ".lake" / "packages" / "mathlib" / ".lake" / "build" / "bin" / "cache"
    cache.write_bytes(b"#!binary")
    cache.chmod(0o755)
    return ws


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "opt" / "lean"
    monkeypatch.setattr(lp, "ROOT", r)
    yield r
    for dirpath, _, _ in os.walk(tmp_path):  # let pytest delete the read-only copies
        os.chmod(dirpath, 0o700)


@pytest.fixture
def shared(tmp_path, root):
    ws = workspace(tmp_path)
    lp.install(ws)
    return ws


def writable(path: Path) -> bool:
    return bool(path.lstat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def test_install_moves_the_packages_makes_them_read_only_and_links_them_back(tmp_path, root):
    ws = workspace(tmp_path)
    olean = ws / ".lake" / "packages" / "mathlib" / ".lake" / "build" / "Mathlib.olean"
    inode = olean.stat().st_ino
    out = lp.install(ws)
    packages = root / "packages" / "v4.34.1"
    moved = packages / "mathlib" / ".lake" / "build" / "Mathlib.olean"
    assert "moved" in out and moved.stat().st_ino == inode  # a rename, nothing copied
    assert (ws / ".lake" / "packages").is_symlink()
    assert os.path.realpath(ws / ".lake" / "packages") == str(packages)
    assert not writable(moved) and not writable(packages) and not writable(moved.parent)
    assert (packages / "mathlib/.lake/build/bin/cache").stat().st_mode & stat.S_IXOTH
    template = root / "template" / "v4.34.1"
    text = (template / "lakefile.toml").read_text()
    assert 'name = "@NAME@"' in text and '"@LIB@"' in text and "Workspace" not in text
    assert json.loads((template / "lake-manifest.json").read_text())["name"] == "@NAME@"
    assert (template / "lean-toolchain").read_text().strip() == TOOLCHAIN
    assert lp.install(ws).startswith("already shared")  # idempotent, and rewrites the template


def test_uninstall_puts_the_packages_back_with_their_write_bits(tmp_path, root):
    """Review on #259: a rollout whose check fails right after the move undoes it."""
    ws = workspace(tmp_path)
    packages = ws / ".lake" / "packages"
    for name, mode in (("git-object", 0o444), ("shared-group", 0o664), ("cache", 0o600)):
        (packages / "mathlib" / name).write_text("x")
        (packages / "mathlib" / name).chmod(mode)

    def modes() -> dict[str, int]:
        return {
            str(p.relative_to(packages)): p.lstat().st_mode
            for p in [packages, *packages.rglob("*")]
        }

    before = modes()
    lp.install(ws)
    record = root / "packages" / "v4.34.1.write-bits.json"
    assert json.loads(record.read_text())["paths"]  # the commonest bits once, the others by path
    assert lp.install(ws).startswith("already shared")  # a rerun keeps the first record
    assert "back to" in lp.uninstall(ws)
    assert packages.is_dir() and not packages.is_symlink() and modes() == before
    assert not (root / "packages" / "v4.34.1").exists() and not record.exists()
    assert not (root / "template" / "v4.34.1").exists() and lp.installed() == []
    with pytest.raises(lp.Refused, match="nothing to undo"):
        lp.uninstall(ws)


def test_uninstall_without_the_record_changes_nothing(tmp_path, root):
    ws = workspace(tmp_path)
    lp.install(ws)
    (root / "packages" / "v4.34.1.write-bits.json").unlink()
    assert lp.main(["--uninstall", str(ws)]) == 1
    assert (ws / ".lake" / "packages").is_symlink()
    assert not writable(root / "packages" / "v4.34.1" / "mathlib")


@pytest.mark.parametrize("step", ["read_only", "write_template"])
def test_an_install_that_fails_after_the_move_puts_everything_back(
    tmp_path, root, monkeypatch, step
):
    """Review on #262: the record of the write bits was written only at the end, so a failure
    after the rename left nothing to undo with. It is written before the rename now, and a
    failure after it restores the workspace: write bits, link, template and record."""
    ws = workspace(tmp_path)
    packages = ws / ".lake" / "packages"
    (packages / "mathlib" / "git-object").write_text("x")
    (packages / "mathlib" / "git-object").chmod(0o444)
    before = {
        str(p.relative_to(packages)): p.lstat().st_mode for p in [packages, *packages.rglob("*")]
    }
    real = getattr(lp, step)

    def fail_after(*args, **kw):  # the step does its work, then something goes wrong
        real(*args, **kw)
        raise OSError("disk full")

    monkeypatch.setattr(lp, step, fail_after)
    with pytest.raises(OSError, match="disk full"):
        lp.install(ws)
    assert packages.is_dir() and not packages.is_symlink()
    assert {
        str(p.relative_to(packages)): p.lstat().st_mode for p in [packages, *packages.rglob("*")]
    } == before
    assert sorted(p.name for p in (root / "packages").iterdir()) == []  # no copy, no record
    assert not (root / "template" / "v4.34.1").exists()


def test_an_install_the_template_can_not_come_from_changes_nothing(tmp_path, root):
    """The template is made from the workspace's lakefile before anything moves."""
    ws = workspace(tmp_path)
    (ws / "lakefile.toml").write_text('name = "workspace"\n')  # not `lake new … math`'s
    with pytest.raises(lp.Refused, match="isn't the `lake new … math` one"):
        lp.install(ws)
    assert (ws / ".lake" / "packages").is_dir() and not (ws / ".lake" / "packages").is_symlink()
    assert not (root / "packages").exists()


def test_install_refuses_rather_than_copying_across_filesystems(tmp_path, root, monkeypatch):
    ws = workspace(tmp_path)
    monkeypatch.setattr(lp, "device", lambda p: 1 if "home" in str(p) else 2)
    with pytest.raises(lp.Refused, match="different filesystems"):
        lp.install(ws)
    assert (ws / ".lake" / "packages").is_dir() and not (ws / ".lake" / "packages").is_symlink()


def test_a_new_project_is_linked_and_never_downloads(tmp_path, shared, root):
    project = tmp_path / "repo" / "research" / "fermat-little"
    out = lp.new(project, None)
    assert "fermat-little" in out and "nothing to download" in out
    assert 'name = "fermat-little"' in (project / "lakefile.toml").read_text()
    assert '"FermatLittle"' in (project / "lakefile.toml").read_text()
    assert json.loads((project / "lake-manifest.json").read_text())["name"] == "fermat-little"
    assert (project / "FermatLittle.lean").read_text() == "import FermatLittle.Basic\n"
    assert (project / "FermatLittle" / "Basic.lean").read_text() == "import Mathlib.Tactic\n"
    assert (project / ".gitignore").read_text() == "/.lake\n"
    packages = project / ".lake" / "packages"
    assert packages.is_symlink() and os.path.realpath(packages) == str(
        root / "packages" / "v4.34.1"
    )
    assert writable(project / "lakefile.toml")  # the project is the agent's, not read-only
    with pytest.raises(lp.Refused, match="already a Lean project"):
        lp.new(project, None)


def test_a_new_project_keeps_the_participants_files(tmp_path, shared):
    project = tmp_path / "repo"
    project.mkdir()
    (project / ".gitignore").write_text("*.pdf\n")
    (project / "README.md").write_text("mine")
    lp.new(project, "Line 9")
    assert (project / ".gitignore").read_text() == "*.pdf\n/.lake\n"
    assert (project / "README.md").read_text() == "mine"
    assert (project / "Line9.lean").exists()


def committed(at: Path, rev: str = MATHLIB, url: str = UPSTREAM) -> Path:
    """A project as a repository holds it: config files, no `.lake`."""
    at.mkdir(parents=True)
    (at / "lean-toolchain").write_text(TOOLCHAIN + "\n")
    (at / "lakefile.toml").write_text(LAKEFILE.replace("workspace", "proof"))
    (at / "lake-manifest.json").write_text(json.dumps(manifest("proof", rev, url)))
    return at


def test_a_fork_at_the_same_commit_is_linked_another_directory_is_not(tmp_path, shared):
    """Codex round 4 on #258 compared URLs too, but Lake v4.34.1 compares them only when a
    checkout isn't at the manifest's revision, so a fork at the same commit loads the shared
    files (review on #262; the real-Lake test below builds one). Another kind or directory
    of a package is still refused, and the note says what differs."""
    fork = committed(tmp_path / "fork", url="https://github.com/someone/")
    assert lp.repair(fork).startswith("linked")
    project = tmp_path / "new"
    project.mkdir()
    (project / "lake-manifest.json").write_text((fork / "lake-manifest.json").read_text())
    assert "nothing to download" in lp.new(project, None)
    data = manifest("proof")
    data["packages"][0]["subDir"] = "src"  # another directory of the same repository
    other = committed(tmp_path / "other")
    (other / "lake-manifest.json").write_text(json.dumps(data))
    note = lp.repair(other)
    assert note.startswith("not linked") and "another source for mathlib" in note
    (project / "elsewhere").mkdir()
    (project / "elsewhere" / "lake-manifest.json").write_text(json.dumps(data))
    with pytest.raises(lp.Refused, match=r"already has lake-manifest\.json for other pins"):
        lp.new(project / "elsewhere", None)


def test_repair_links_a_committed_project_on_the_shared_pins(tmp_path, shared, root):
    project = committed(tmp_path / "wt" / "research" / "lean")
    assert lp.repair(project).startswith("linked")
    assert lp.repair(project) == "already linked to the shared Mathlib"


def test_repair_leaves_other_pins_and_private_copies_alone(tmp_path, shared):
    other = committed(tmp_path / "other", rev="5ed2965256430c3649e86755f9576b54eca72435")
    note = lp.repair(other)
    assert note.startswith("not linked") and "5ed29652" in note and "d13f23b7" in note
    assert "ask the participant" in note and "GB free" in note
    assert not (other / ".lake").exists()
    private = committed(tmp_path / "private")
    (private / ".lake" / "packages" / "mathlib").mkdir(parents=True)
    assert "its own copy" in lp.repair(private)
    assert not (private / ".lake" / "packages").is_symlink()
    newer = committed(tmp_path / "newer")
    (newer / "lean-toolchain").write_text("leanprover/lean4:v4.35.0\n")
    assert lp.repair(newer).startswith("not linked")
    no_manifest = committed(tmp_path / "bare")
    (no_manifest / "lake-manifest.json").unlink()
    assert "no lake-manifest.json" in lp.repair(no_manifest)


def test_repair_all_finds_projects_three_levels_deep_and_skips_tool_directories(
    tmp_path, shared, capsys
):
    wt = tmp_path / "wt"
    a = committed(wt / "research" / "fermat" / "lean")
    b = committed(wt / "lean")
    committed(wt / "a" / "b" / "c" / "too-deep")
    committed(wt / ".zab" / "worktrees" / "x")
    committed(wt / ".lake" / "packages" / "y")
    assert lp.projects(wt) == [b, a]
    assert lp.main(["--repair-all", str(wt)]) == 0
    out = capsys.readouterr().out
    assert out.count("linked to the shared Mathlib") == 2 and "too-deep" not in out


def test_repair_all_never_fails_the_caller(tmp_path, shared, capsys):
    broken = committed(tmp_path / "wt" / "broken")
    (broken / "lake-manifest.json").write_text("{not json")
    assert lp.main(["--repair-all", str(tmp_path / "wt")]) == 0
    assert "skipped" in capsys.readouterr().out


def test_without_a_shared_copy_new_refuses_and_repair_explains(tmp_path, root, capsys):
    assert lp.main([str(tmp_path / "p")]) == 1
    assert "no shared Mathlib is installed" in capsys.readouterr().err
    project = committed(tmp_path / "q")
    assert "not installed" in lp.repair(project)


def test_check_times_a_mathlib_file_in_a_scratch_project(tmp_path, shared, monkeypatch):
    seen = []
    pinned = {p["name"]: p["rev"] for p in manifest()["packages"]}

    def fake_run(argv, **kw):
        if argv[0] == "git":  # each package at its pin (real git: the tests further down)
            return subprocess.CompletedProcess(argv, 0, pinned[Path(argv[2]).name] + "\n", "")
        seen.append((argv, kw["cwd"]))
        assert (Path(kw["cwd"]) / ".lake" / "packages").is_symlink()
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(lp.subprocess, "run", fake_run)
    assert lp.check(False, 30).startswith("Mathlib.Tactic file checked in")
    assert seen[0][0] == ["lake", "env", "lean", "Check.lean"]
    assert not Path(seen[0][1]).exists()  # cleaned up
    assert (shared / ".lake" / "packages").is_symlink()  # and the shared copy is intact
    assert (lp.Shared("v4.34.1").packages / "mathlib").is_dir()
    monkeypatch.setattr(
        lp.subprocess,
        "run",
        lambda argv, **kw: (
            fake_run(argv, **kw)
            if argv[0] == "git"
            else subprocess.CompletedProcess(argv, 1, "", "boom")
        ),
    )
    with pytest.raises(lp.Refused, match="lake env lean failed: boom"):
        lp.check(False, None)


def test_the_script_runs_with_the_system_python():
    out = subprocess.run(  # noqa: S603 - a fixed argument list
        [sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, check=True
    ).stdout
    assert "--repair-all" in out and "--install" in out
    assert os.access(SCRIPT, os.X_OK)


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("lean-toolchain", "leanprover/lean4:v4.35.0\n"),
        (
            "lake-manifest.json",
            json.dumps(manifest(rev="5ed2965256430c3649e86755f9576b54eca72435")),
        ),
        ("lake-manifest.json", "{not json"),
    ],
)
def test_a_new_project_refuses_other_pins_already_there(tmp_path, shared, name, text):
    """Codex round 1 on #258: the shared packages can't serve another toolchain or manifest,
    so nothing is written and nothing is linked."""
    project = tmp_path / "repo"
    project.mkdir()
    (project / name).write_text(text)
    with pytest.raises(lp.Refused, match=f"already has {name} for other pins"):
        lp.new(project, None)
    assert sorted(p.name for p in project.iterdir()) == [name]


def test_a_new_project_accepts_matching_config_already_there(tmp_path, shared):
    project = tmp_path / "repo"
    project.mkdir()
    (project / "lean-toolchain").write_text(TOOLCHAIN + "\n")
    lp.new(project, None)
    assert (project / ".lake" / "packages").is_symlink()


def test_a_new_project_refuses_a_manifest_that_puts_packages_elsewhere(tmp_path, shared):
    """Codex round 2 on #258: same pins, but Lake would use `vendor`, not the link."""
    project = tmp_path / "repo"
    project.mkdir()
    (project / "lake-manifest.json").write_text(json.dumps(manifest() | {"packagesDir": "vendor"}))
    with pytest.raises(lp.Refused, match=r"already has lake-manifest\.json for other pins"):
        lp.new(project, None)
    assert sorted(p.name for p in project.iterdir()) == ["lake-manifest.json"]


def test_a_new_project_refuses_packages_that_are_not_the_shared_copy(tmp_path, shared, root):
    """Codex round 2 on #258: an existing .lake/packages (even empty) is never left in place
    while reporting the shared Mathlib; one that already links to it is fine."""
    project = tmp_path / "repo"
    (project / ".lake" / "packages").mkdir(parents=True)
    with pytest.raises(lp.Refused, match="isn't the shared Mathlib"):
        lp.new(project, None)
    assert sorted(p.name for p in project.iterdir()) == [".lake"]
    (project / ".lake" / "packages").rmdir()
    (project / ".lake" / "packages").symlink_to(root / "packages" / "v4.34.1")
    assert "nothing to download" in lp.new(project, None)


@pytest.mark.parametrize("text", ["null", '{"packages": null}', '{"packages": [7]}', "[]"])
def test_a_manifest_that_is_not_one_skips_that_project_only(tmp_path, shared, capsys, text):
    """Codex round 3 on #258: valid JSON of the wrong shape never stops --repair-all, and
    --repair says so instead of a traceback."""
    wt = tmp_path / "wt"
    broken = committed(wt / "a-broken")
    (broken / "lake-manifest.json").write_text(text)
    good = committed(wt / "b-good")
    assert lp.main(["--repair-all", str(wt)]) == 0
    out = capsys.readouterr().out
    assert "a-broken: skipped (lake-manifest.json isn't a Lake manifest)" in out
    assert (good / ".lake" / "packages").is_symlink()
    assert lp.main(["--repair", str(broken)]) == 1
    assert "isn't a Lake manifest" in capsys.readouterr().err


def private(project: Path) -> Path:
    """A private Mathlib copy, as `lake exe cache get` leaves it."""
    olean = project / ".lake" / "packages" / "mathlib" / ".lake" / "build" / "M.olean"
    olean.parent.mkdir(parents=True)
    olean.write_bytes(b"x" * 100_000)
    (olean.parent / "readonly.olean").write_bytes(b"y")
    (olean.parent / "readonly.olean").chmod(0o444)
    return project / ".lake" / "packages"


PINNED = {"mathlib": MATHLIB, "batteries": "f2effa3d"}


@pytest.fixture
def git_state(monkeypatch):
    """What `git` says about each package checkout: clean at its pin unless a test changes it."""
    state = {"head": dict(PINNED), "status": {}, "ahead": {}, "shared": None}
    checked: list[Path] = []
    state["checked"] = checked

    def shared_check(data, packages):  # git in the shared copy (real git: the tests above)
        checked.append(packages)
        return state["shared"]

    monkeypatch.setattr(lp, "git_problem", shared_check)

    def fake(repo, *args, trust=False):
        name = Path(repo).name
        if args[:2] == ("rev-parse", "--verify"):
            return None  # no stash
        if args[0] == "rev-parse":
            return state["head"].get(name)
        if args[0] == "status":
            return state["status"].get(name, "")
        if args[0] == "rev-list":
            return state["ahead"].get(name, "0")
        if args[0] == "for-each-ref":
            return ""  # no tags
        raise AssertionError(args)

    monkeypatch.setattr(lp, "git_out", fake)
    return state


def reclaim_tree(tmp_path: Path) -> tuple[Path, dict[str, Path]]:
    work = tmp_path / "work"
    repo = work / "thesis"
    trees = {
        "main": committed(repo / "lean"),
        "topic": committed(repo / ".zab" / "worktrees" / "line-9-abc123" / "research" / "lean"),
        "other": committed(
            repo / ".zab" / "worktrees" / "old-def456" / "lean",
            rev="5ed2965256430c3649e86755f9576b54eca72435",
        ),
        "linked": committed(repo / ".zab" / "worktrees" / "new-0a1b2c" / "lean"),
    }
    for name in ("main", "topic", "other"):
        private(trees[name])
    lp.repair(trees["linked"])
    return work, trees


def fake_proc(tmp_path: Path, entries: dict[int, tuple[str, list[str]]]) -> Path:
    proc = tmp_path / "proc"
    for pid, (cwd, argv) in entries.items():
        d = proc / str(pid)
        d.mkdir(parents=True)
        (d / "cmdline").write_bytes("\0".join(argv).encode() + b"\0")
        (d / "cwd").symlink_to(cwd)
    (proc / "self").mkdir(parents=True, exist_ok=True)
    return proc


def test_reclaim_reports_first_and_changes_nothing(tmp_path, shared, git_state):
    work, trees = reclaim_tree(tmp_path)
    lines = lp.reclaim(work, apply=False, proc=fake_proc(tmp_path, {}))
    assert sum("would be freed" in line for line in lines) == 2  # main checkout and topic
    assert any(str(trees["other"]) in line and "other pins: kept" in line for line in lines)
    assert not any(str(trees["linked"]) in line for line in lines)
    assert lines[-1].startswith("total: 0.0 GB to free (a dry run")
    for name in ("main", "topic", "other"):
        assert not (trees[name] / ".lake" / "packages").is_symlink()


def test_reclaim_apply_replaces_private_copies_with_the_link(tmp_path, shared, git_state, root):
    work, trees = reclaim_tree(tmp_path)
    lines = lp.reclaim(work, apply=True, proc=fake_proc(tmp_path, {}))
    assert sum("freed" in line for line in lines[:-1]) == 2 and lines[-1].endswith("GB freed")
    for name in ("main", "topic"):
        packages = trees[name] / ".lake" / "packages"
        assert packages.is_symlink() and os.path.realpath(packages) == str(
            root / "packages" / "v4.34.1"
        )
        assert [p.name for p in (trees[name] / ".lake").iterdir()] == ["packages"]  # no trash
    assert not (trees["other"] / ".lake" / "packages").is_symlink()  # other pins kept


def test_reclaim_checks_each_shared_copy_once_before_linking(tmp_path, shared, git_state, root):
    work, trees = reclaim_tree(tmp_path)
    git_state["shared"] = "git refuses the shared mathlib as this user: fatal: dubious ownership"
    lines = lp.reclaim(work, apply=True, proc=fake_proc(tmp_path, {}))
    assert git_state["checked"] == [root / "packages" / "v4.34.1"]  # once, not per project
    assert sum("kept: the shared copy failed its git check" in line for line in lines) == 2
    for name in ("main", "topic", "other"):
        assert not (trees[name] / ".lake" / "packages").is_symlink()
    git_state["shared"] = None
    lines = lp.reclaim(work, apply=True, proc=fake_proc(tmp_path, {}))
    assert sum(": freed" in line for line in lines) == 2


def test_reclaim_deletes_unreadable_directories_and_one_failure_stops_nothing(
    tmp_path, shared, git_state, root, monkeypatch
):
    """Review on #262: rmtree's error hook raised TypeError on a directory without read
    permission, the run ended in a traceback, and the lines of copies already freed were lost.
    Now such directories are deleted, and a project that fails is reported on its own line."""
    work, trees = reclaim_tree(tmp_path)
    locked = trees["main"] / ".lake" / "packages" / "mathlib" / ".lake" / "locked"
    (locked / "deep").mkdir(parents=True)
    (locked / "deep" / "f").write_text("x")
    (locked / "deep").chmod(0o000)
    locked.chmod(0o500)
    (trees["main"] / ".lake" / "packages" / "mathlib").chmod(0o555)
    real = lp.size_bytes
    monkeypatch.setattr(  # the topic's copy breaks in the middle of the run
        lp, "size_bytes", lambda p: 1 / 0 if "line-9" in str(p) else real(p)
    )
    lines = lp.reclaim(work, apply=True, proc=fake_proc(tmp_path, {}))
    assert lines[0].startswith(f"{trees['main']}: freed")
    assert lines[1] == f"{trees['topic']}: skipped (ZeroDivisionError: division by zero)"
    assert [p.name for p in (trees["main"] / ".lake").iterdir()] == ["packages"]  # no trash
    assert not (trees["topic"] / ".lake" / "packages").is_symlink()  # untouched
    assert (root / "packages" / "v4.34.1" / "mathlib").is_dir()  # the shared copy too


def test_reclaim_skips_a_project_lean_is_working_in(tmp_path, shared, git_state):
    work, trees = reclaim_tree(tmp_path)
    proc = fake_proc(
        tmp_path,
        {
            4242: (str(trees["topic"]), ["/home/agent/.elan/toolchains/x/bin/lean", "--server"]),
            4343: ("/", ["lake", "build", str(trees["main"] / "Proof.lean")]),
            4444: (str(trees["topic"]), ["python3", "script.py"]),  # not lake or lean
        },
    )
    lines = lp.reclaim(work, apply=True, proc=proc)
    assert any("skipped: lake or lean is running there (pid 4242)" in line for line in lines)
    assert any("(pid 4343)" in line for line in lines)
    assert not (trees["topic"] / ".lake" / "packages").is_symlink()
    assert not (trees["main"] / ".lake" / "packages").is_symlink()


def test_reclaim_from_the_command_line(tmp_path, shared, git_state, capsys):
    work, _ = reclaim_tree(tmp_path)
    assert lp.main(["--reclaim", str(work)]) == 0
    assert "a dry run" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (
            lambda st: st["status"].update(mathlib=" M Mathlib/Data/Nat.lean"),
            "mathlib has local changes",
        ),
        (lambda st: st["status"].update(mathlib="?? Scratch.lean"), "mathlib has local changes"),
        (lambda st: st["head"].update(mathlib="0" * 40), "mathlib is not at its pinned revision"),
        (lambda st: st["ahead"].update(mathlib="2"), "mathlib has commits of its own"),
        (lambda st: st["head"].pop("mathlib"), "mathlib is not at its pinned revision"),  # no git
    ],
)
def test_reclaim_keeps_a_copy_with_work_in_it(tmp_path, shared, git_state, change, reason):
    """Codex round 1 on #262: pins alone don't prove a private copy is disposable."""
    work, trees = reclaim_tree(tmp_path)
    change(git_state)
    lines = lp.reclaim(work, apply=True, proc=fake_proc(tmp_path, {}))
    assert sum(f"kept: {reason}" in line for line in lines) == 2
    assert not (trees["topic"] / ".lake" / "packages").is_symlink()


def test_reclaim_keeps_a_copy_with_packages_the_manifest_lacks(tmp_path, shared, git_state):
    work, trees = reclaim_tree(tmp_path)
    (trees["topic"] / ".lake" / "packages" / "mine").mkdir()
    lines = lp.reclaim(work, apply=True, proc=fake_proc(tmp_path, {}))
    assert any("kept: packages not in the manifest (mine)" in line for line in lines)
    assert not (trees["topic"] / ".lake" / "packages").is_symlink()


def test_a_relative_path_argument_counts_as_working_there(tmp_path, shared, git_state):
    """Codex round 1 on #262: `lean lean/Proof.lean` run from the project's parent."""
    _, trees = reclaim_tree(tmp_path)
    parent = trees["topic"].parent
    proc = fake_proc(tmp_path, {77: (str(parent), ["lean", "lean/Proof.lean"])})
    assert lp.live_lean(trees["topic"], proc) == [77]
    assert lp.live_lean(trees["main"], proc) == []


def test_untracked_files_count_even_when_git_is_told_to_hide_them(tmp_path, monkeypatch):
    """Codex round 2 on #262, on a real checkout: `status.showUntrackedFiles=no` must not hide
    an uncommitted Scratch.lean from the check before a deletion."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    (tmp_path / "gitconfig").write_text("[status]\n\tshowUntrackedFiles = no\n")
    checkout = tmp_path / "packages" / "mathlib"
    checkout.mkdir(parents=True)

    def git(*args):
        subprocess.run(  # noqa: S603 - a fixed argument list
            ["git", "-C", str(checkout), "-c", "user.name=t", "-c", "user.email=t@t", *args],  # noqa: S607
            check=True,
            capture_output=True,
        )

    git("init", "-q")
    (checkout / "Mathlib.lean").write_text("import Mathlib.Tactic\n")
    git("add", "Mathlib.lean")
    git("commit", "-q", "-m", "pinned")
    head = lp.git_out(checkout, "rev-parse", "HEAD")
    data = {"packages": [{"name": "mathlib", "rev": head}]}
    assert lp.unsaved_work(tmp_path / "packages", data) is None  # clean at its pin
    (checkout / "Scratch.lean").write_text("-- work in progress\n")
    assert lp.git_out(checkout, "status", "--porcelain") == ""  # what git alone would say
    assert lp.unsaved_work(tmp_path / "packages", data) == "mathlib has local changes"


def test_a_commit_kept_only_by_a_local_tag_is_still_found(tmp_path, monkeypatch):
    """Codex round 3 on #262, on a real checkout: a commit made there, tagged, and left behind
    by moving HEAD back to the pin is in HEAD's reflog, which the check reads. (`--tags` itself
    would keep every clean Lake copy: their upstream tags point at commits on no remote branch,
    43 of them in mathlib v4.34.1.)"""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    checkout = tmp_path / "packages" / "mathlib"
    checkout.mkdir(parents=True)

    def git(*args):
        subprocess.run(  # noqa: S603 - a fixed argument list
            ["git", "-C", str(checkout), "-c", "user.name=t", "-c", "user.email=t@t", *args],  # noqa: S607
            check=True,
            capture_output=True,
        )

    git("init", "-q")
    git("commit", "-q", "--allow-empty", "-m", "pinned")
    pin = lp.git_out(checkout, "rev-parse", "HEAD")
    data = {"packages": [{"name": "mathlib", "rev": pin}]}
    git("checkout", "-q", "--detach")
    git("commit", "-q", "--allow-empty", "-m", "my proof")
    git("tag", "mine")
    git("checkout", "-q", "--detach", pin)
    assert lp.git_out(checkout, "rev-parse", "HEAD") == pin
    assert lp.unsaved_work(tmp_path / "packages", data) == "mathlib has commits of its own"


def test_a_tag_of_ones_own_counts_after_the_reflog_expires(tmp_path, monkeypatch):
    """Codex round 4 on #262, on real clones: a tagged commit of one's own whose reflog entry
    has expired is still found, by comparing tags with the clean shared clone. An upstream tag
    on no remote branch (mathlib has 43) is in both clones, so it doesn't count."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))

    def git(where, *args):
        out = subprocess.run(  # noqa: S603 - a fixed argument list
            ["git", "-C", str(where), "-c", "user.name=t", "-c", "user.email=t@t", *args],  # noqa: S607
            check=True,
            capture_output=True,
            text=True,
        )
        return out.stdout.strip()

    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git(upstream, "init", "-q", "-b", "master")
    git(upstream, "commit", "-q", "--allow-empty", "-m", "pinned")
    git(upstream, "checkout", "-q", "-b", "nightly")
    git(upstream, "commit", "-q", "--allow-empty", "-m", "nightly")
    git(upstream, "tag", "nightly-2026-10-01")
    git(upstream, "checkout", "-q", "master")
    git(upstream, "branch", "-q", "-D", "nightly")  # the tag is now on no branch
    for clone in ("shared", "private"):
        (tmp_path / clone).mkdir()
        git(tmp_path / clone, "clone", "-q", str(upstream), "mathlib")
    mine = tmp_path / "private" / "mathlib"
    pin = git(mine, "rev-parse", "HEAD")
    data = {"packages": [{"name": "mathlib", "rev": pin}]}
    assert lp.unsaved_work(tmp_path / "private", data, tmp_path / "shared") is None
    git(mine, "checkout", "-q", "--detach")
    git(mine, "commit", "-q", "--allow-empty", "-m", "my proof")
    git(mine, "tag", "mine")
    git(mine, "checkout", "-q", "master")
    git(mine, "reflog", "expire", "--expire=now", "--all")
    assert (
        lp.git_out(mine, "rev-list", "--count", "--branches", "--reflog", "--not", "--remotes", pin)
        == "0"
    )
    assert lp.unsaved_work(tmp_path / "private", data, tmp_path / "shared") == (
        "mathlib has a tag of its own (mine)"
    )


def test_the_root_owned_shared_clone_is_read_as_a_safe_directory(tmp_path, monkeypatch):
    """Codex round 5 on #262: as agent, git refuses a repository root owns unless it's named
    in safe.directory; only the shared clone's command gets that, for its own path."""
    seen = []

    def run(argv, **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(lp.subprocess, "run", run)
    lp.tags(tmp_path / "shared" / "mathlib", trust=True)
    lp.tags(tmp_path / "private" / "mathlib")
    real = os.path.realpath(tmp_path / "shared" / "mathlib")
    assert seen[0][:3] == ["git", "-c", f"safe.directory={real}"]
    assert "safe.directory" not in " ".join(seen[1])


def test_a_stash_counts_after_the_reflog_expires(tmp_path, monkeypatch):
    """Codex round 7 on #262, on a real checkout: `git stash` then an expired reflog leaves
    HEAD at the pin and the tree clean, but refs/stash still holds the changes."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    checkout = tmp_path / "packages" / "mathlib"
    checkout.mkdir(parents=True)

    def git(*args):
        subprocess.run(  # noqa: S603 - a fixed argument list
            ["git", "-C", str(checkout), "-c", "user.name=t", "-c", "user.email=t@t", *args],  # noqa: S607
            check=True,
            capture_output=True,
        )

    git("init", "-q")
    (checkout / "Mathlib.lean").write_text("import Mathlib.Tactic\n")
    git("add", "Mathlib.lean")
    git("commit", "-q", "-m", "pinned")
    pin = lp.git_out(checkout, "rev-parse", "HEAD")
    data = {"packages": [{"name": "mathlib", "rev": pin}]}
    (checkout / "Mathlib.lean").write_text("-- my lemma\n")
    git("stash", "push", "-q")
    git("reflog", "expire", "--expire=now", "--all")
    assert lp.git_out(checkout, "status", "--porcelain", "--untracked-files=all") == ""
    assert lp.unsaved_work(tmp_path / "packages", data, None) == "mathlib has stashed changes"


def test_a_project_named_lean_gets_a_library_that_leaves_leans_modules_alone(tmp_path, shared):
    """A library named Lean (from `lean-project lean`, the instructions' example) took over
    Lean's own modules: with the real Mathlib, `lake build` failed on "bad import
    'Lean.Elab.Command'" (checked on the development machine)."""
    lp.new(tmp_path / "research" / "lean", None)
    text = (tmp_path / "research" / "lean" / "lakefile.toml").read_text()
    assert 'name = "lean"' in text and '"LeanProject"' in text
    assert [lp.lib_name(n) for n in ("mathlib", "std", "lean4", "my-lean")] == [
        "MathlibProject",
        "StdProject",
        "Lean4",
        "MyLean",
    ]


def test_tags_sort_as_versions(tmp_path, root):
    """Review on #258: `v4.9.0` sorted after `v4.34.1` as a string, and new() takes the last."""
    for tag in ("v4.9.0", "v4.34.1", "v4.34.1-rc2", "v4.10.0"):
        (root / "packages" / tag).mkdir(parents=True)
        (root / "template" / tag).mkdir(parents=True)
        (root / "template" / tag / "lake-manifest.json").write_text("{}")
    assert [s.tag for s in lp.installed()] == ["v4.9.0", "v4.10.0", "v4.34.1-rc2", "v4.34.1"]
    assert sorted(["rc10", "rc2"], key=lambda t: lp.version_key(f"v1.0.0-{t}")) == ["rc2", "rc10"]


# ------------------------------------------- ownership, with real git (review on #258)
#
# Lake runs git in every dependency whenever it loads a project, and git refuses a repository
# that belongs to another user ("dubious ownership"). The tests can't chown, so they record
# the ownership --install gives as root, and let git see another owner through its own test
# switch, GIT_TEST_ASSUME_DIFFERENT_OWNER=1, exactly when the record says so.


def git(cwd: Path, *args: str) -> str:
    argv = ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args]
    run = subprocess.run(argv, cwd=cwd, check=True, capture_output=True, text=True)  # noqa: S603
    return run.stdout.strip()


@pytest.fixture
def plain_git(monkeypatch):
    """git without the developer's or the system's config (a global `safe.directory = *`)."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("GIT_TEST_ASSUME_DIFFERENT_OWNER", raising=False)


def git_workspace(tmp_path: Path) -> Path:
    """The workspace with its packages as real git checkouts, pinned by the manifest."""
    ws = workspace(tmp_path)
    data = manifest()
    for package in data["packages"]:
        repo = ws / ".lake" / "packages" / package["name"]
        repo.mkdir(parents=True, exist_ok=True)
        git(repo, "init", "-q")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "--allow-empty", "-m", package["name"])
        package["rev"] = git(repo, "rev-parse", "HEAD")
    (ws / "lake-manifest.json").write_text(json.dumps(data))
    return ws


def lake_faked(monkeypatch) -> list[list[str]]:
    """lake and comparator-run succeed without running; git really runs."""
    real, ran = subprocess.run, []

    def run(argv, **kw):
        if argv[0] == "git":
            return real(argv, **kw)
        ran.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(lp.subprocess, "run", run)
    return ran


needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


@needs_git
def test_installed_as_root_the_packages_stay_usable_by_the_workspace_owner(
    tmp_path, root, plain_git, monkeypatch
):
    ws = git_workspace(tmp_path)
    me = os.stat(ws)
    owners: dict[str, tuple[int, int]] = {}
    with monkeypatch.context() as m:
        m.setattr(lp.os, "geteuid", lambda: 0)
        m.setattr(lp.os, "lchown", lambda p, uid, gid: owners.__setitem__(str(p), (uid, gid)))
        lp.install(ws)
    packages, template = root / "packages" / "v4.34.1", root / "template" / "v4.34.1"

    def under(top: Path) -> set[tuple[int, int]]:
        return {o for p, o in owners.items() if Path(p).is_relative_to(top)}

    if under(packages) != {(me.st_uid, me.st_gid)}:  # git sees what agent would see
        monkeypatch.setenv("GIT_TEST_ASSUME_DIFFERENT_OWNER", "1")
    ran = lake_faked(monkeypatch)
    assert lp.check(False, None).startswith("Mathlib.Tactic file checked in")
    assert ran == [["lake", "env", "lean", "Check.lean"]]
    assert under(packages) == {(me.st_uid, me.st_gid)} and under(template) == {(0, 0)}
    assert owners[str(ws / ".lake" / "packages")] == (me.st_uid, me.st_gid)  # the link too


@needs_git
def test_check_stops_where_git_would_refuse_the_shared_packages(
    tmp_path, root, plain_git, monkeypatch
):
    lp.install(git_workspace(tmp_path))
    ran = lake_faked(monkeypatch)
    monkeypatch.setenv("GIT_TEST_ASSUME_DIFFERENT_OWNER", "1")  # as if someone else owned them
    with pytest.raises(lp.Refused, match=r"git refuses the shared mathlib.*dubious ownership"):
        lp.check(False, None)
    assert ran == []  # Lake never ran


@needs_git
def test_reclaim_links_nothing_to_a_shared_copy_git_refuses(tmp_path, root, plain_git, monkeypatch):
    """Review on #262: `--reclaim --apply` checks the shared copy with git once, as the
    calling user, before linking anything to it; a refused copy keeps every private one."""
    lp.install(git_workspace(tmp_path))
    template = root / "template" / "v4.34.1"
    project = tmp_path / "work" / "thesis" / "lean"
    project.mkdir(parents=True)
    for f in ("lean-toolchain", "lakefile.toml", "lake-manifest.json"):
        (project / f).write_text((template / f).read_text())
    private(project)
    monkeypatch.setenv("GIT_TEST_ASSUME_DIFFERENT_OWNER", "1")  # as if someone else owned it
    lines = lp.reclaim(tmp_path / "work", apply=True, proc=fake_proc(tmp_path, {}))
    assert lines[0].startswith(f"{root / 'packages' / 'v4.34.1'}: not used: git refuses")
    assert lines[1] == f"{project}: kept: the shared copy failed its git check"
    assert lines[-1] == "total: 0.0 GB freed"
    assert not (project / ".lake" / "packages").is_symlink()


TOOLCHAINS = Path(os.environ.get("ELAN_HOME", Path.home() / ".elan")) / "toolchains"


@pytest.mark.skipif(
    shutil.which("lake") is None
    or shutil.which("git") is None
    or not (TOOLCHAINS / "leanprover--lean4---v4.34.1").is_dir(),
    reason="needs Lake with Lean v4.34.1 already installed (never downloads it)",
)
def test_real_lake_builds_a_new_project_through_the_shared_packages(tmp_path, root, plain_git):
    """The #246 VERIFY with Lake itself and a small stand-in for Mathlib: a project made by
    lean-project builds against the installed shared copy and writes nothing in it. Seen as
    another user's, the copy makes git refuse and Lake fail, as on a VM before the fix."""
    dep = tmp_path / "mathlib-src"
    (dep / "Mathlib").mkdir(parents=True)
    (dep / "lakefile.toml").write_text('name = "mathlib"\n\n[[lean_lib]]\nname = "Mathlib"\n')
    (dep / "lean-toolchain").write_text(TOOLCHAIN + "\n")
    (dep / "Mathlib.lean").write_text("import Mathlib.Tactic\n")
    (dep / "Mathlib" / "Tactic.lean").write_text("def two : Nat := 2\n")
    git(dep, "init", "-q")
    git(dep, "add", "-A")
    git(dep, "commit", "-q", "-m", "stand-in")
    ws = tmp_path / "home" / "lean" / "workspace"
    ws.mkdir(parents=True)
    (ws / "lean-toolchain").write_text(TOOLCHAIN + "\n")
    (ws / "lakefile.toml").write_text(
        f'name = "workspace"\ndefaultTargets = ["Workspace"]\n\n'
        f'[[require]]\nname = "mathlib"\ngit = "{dep}"\n'
        f'rev = "{git(dep, "rev-parse", "HEAD")}"\n\n[[lean_lib]]\nname = "Workspace"\n'
    )
    (ws / "Workspace.lean").write_text("import Mathlib.Tactic\n")

    def lake(cwd: Path, *args: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(  # noqa: S603
            ["lake", *args],  # noqa: S607
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=600,
            env={**os.environ, **env},
        )

    assert lake(ws, "update").returncode == 0
    assert lake(ws, "build").returncode == 0
    lp.install(ws)
    shared = root / "packages" / "v4.34.1"
    before = {str(p): p.lstat().st_mtime_ns for p in shared.rglob("*")}
    project = tmp_path / "topic" / "lean"
    lp.new(project, None)
    (project / "Use.lean").write_text("import Mathlib.Tactic\n\n#eval two + 2\n")
    built = lake(project, "build")
    assert built.returncode == 0, built.stderr + built.stdout
    used = lake(project, "env", "lean", "Use.lean")
    assert used.returncode == 0 and "4" in used.stdout, used.stderr + used.stdout
    refused = lake(project, "env", "lean", "Use.lean", GIT_TEST_ASSUME_DIFFERENT_OWNER="1")
    assert refused.returncode != 0 and "permission denied" in refused.stderr + refused.stdout
    # a fork at the same commit: Lake compares no URL then, and loads the shared files
    fork = tmp_path / "topic" / "fork"
    fork.mkdir()
    for f in ("lean-toolchain", "lakefile.toml", "Use.lean"):
        (fork / f).write_text((project / f).read_text())
    data = json.loads((project / "lake-manifest.json").read_text())
    data["packages"][0]["url"] = "https://example.invalid/someone/mathlib4"
    (fork / "lake-manifest.json").write_text(json.dumps(data))
    assert lp.repair(fork).startswith("linked")
    used = lake(fork, "env", "lean", "Use.lean")
    assert used.returncode == 0 and "4" in used.stdout, used.stderr + used.stdout
    assert before == {str(p): p.lstat().st_mtime_ns for p in shared.rglob("*")}  # untouched
    assert lp.git_problem(lp.manifest(project), project / ".lake" / "packages") is None
