"""`workshop/admin/sage_wrapper.py` (#482): the new images' `sage` wrapper in place of the Oct 9
image's link, exactly there, and nowhere else; and the way back."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import stat
import subprocess
import sys

import pytest
import sage_wrapper as sw

ADMIN = pathlib.Path(__file__).resolve().parents[2] / "workshop" / "admin"
#: The wrapper the new images install as ~agent/.local/bin/sage (#480), which the rollout uploads
SHIPPED = ADMIN.parent / "ami" / "files" / "bin" / "sage"
#: What the Oct 9 image's ~agent/.local/bin/sage links to: build.sh at 7c1bb2e (the image's
#: commit; workshop/ami/README.md, "The images") ran
#: `ln -sf ~/miniforge/envs/sage/bin/sage ~/.local/bin/sage` as agent, with HOME=/home/agent
OCT9_TARGET = "/home/agent/miniforge/envs/sage/bin/sage"


class Vm:
    """A VM's layout under `tmp_path`: agent's home, the Sage environment, and the wrapper as
    the rollout uploads it, with the environment's path in place of /home/agent's."""

    def __init__(self, tmp_path: pathlib.Path):
        self.home = tmp_path / "home" / "agent"
        self.bin = self.home / ".local" / "bin"
        self.bin.mkdir(parents=True)
        self.env = self.home / "miniforge" / "envs" / "sage"
        self.program = self.env / "bin" / "sage"
        self.path = self.bin / "sage"
        self.record = self.home / ".local" / "state" / "zab" / "sage-wrapper.json"
        self.wrapper = tmp_path / "staging" / "sage"
        self.wrapper.parent.mkdir()
        text = SHIPPED.read_text().replace("/home/agent/miniforge/envs/sage", str(self.env))
        self.wrapper.write_text(text)

    def with_sage(self) -> Vm:
        """The environment's `sage`: here a script that prints PATH's first entry and its
        arguments, as the wrapper's exec reaches it."""
        self.program.parent.mkdir(parents=True)
        self.program.write_text('#!/bin/sh\necho "${PATH%%:*}" "$@"\n')
        self.program.chmod(0o755)
        return self

    def oct9(self) -> Vm:
        self.with_sage().path.symlink_to(self.program)
        return self

    def new_image(self) -> Vm:
        self.with_sage().path.write_bytes(self.wrapper.read_bytes())
        self.path.chmod(0o755)
        return self

    def install(self) -> str:
        return sw.install(str(self.wrapper), str(self.path), str(self.record))

    def remove(self) -> str:
        return sw.remove(str(self.path), str(self.record))

    def run_sage(self, *args: str) -> str:
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.home)}
        done = subprocess.run(  # noqa: S603 - argument list, test only
            [str(self.path), *args], capture_output=True, text=True, env=env, check=True
        )
        return done.stdout.strip()


@pytest.fixture
def vm(tmp_path) -> Vm:
    return Vm(tmp_path)


def state(path: pathlib.Path) -> tuple[int, int, int, str]:
    st = os.lstat(path)
    return (st.st_ino, st.st_mtime_ns, st.st_mode, os.readlink(path) if path.is_symlink() else "")


def test_the_shipped_wrapper_runs_what_the_oct9_link_ran_and_is_known():
    """Exact detection rests on these: the program the wrapper execs is the Oct 9 link's target,
    and the wrapper's bytes are zab's (a change to files/bin/sage adds its hash to KNOWN, so
    that VMs with the old one still count it as zab's)."""
    data = SHIPPED.read_bytes()
    assert sw.program(data) == OCT9_TARGET
    assert hashlib.sha256(data).hexdigest() in sw.KNOWN
    assert stat.S_IMODE(SHIPPED.stat().st_mode) & 0o111  # as git keeps it
    assert sw.PATH == "/home/agent/.local/bin/sage"  # build.sh's $AGENT_HOME/.local/bin/sage


def test_the_wrapper_of_the_new_images_and_of_this_pr_stays_known():
    """zab-fleet-20261019's wrapper, and every one this rollout installs on Oct 9 VMs (#480's
    6aa2fb64, the only version so far): dropping it would make those VMs `skipped` and their
    lists lose the summary (review of #536). Removing it must be a visible edit here."""
    assert "f94c5cd554ae256ae6564cfb2db3fa59469d218dcfae31afadc8a9bef48465ed" in sw.KNOWN


@pytest.mark.parametrize(
    "env", ["/home/agent/x/../miniforge/envs/sage", "/home/agent/miniforge/envs/sage/", "/a//b"]
)
def test_a_wrapper_whose_environment_isnt_a_normal_path_is_refused(env):
    """The link must be the very string the wrapper execs: a SAGE_ENV default with `..`, `//`
    or a trailing `/` would make that string differ from what normalising it gives."""
    data = SHIPPED.read_text().replace("/home/agent/miniforge/envs/sage", env).encode()
    with pytest.raises(ValueError, match="isn't a normal path"):
        sw.program(data)


def test_the_oct9_layout_gets_the_wrapper_which_puts_the_environment_first(vm):
    vm.oct9()
    assert vm.run_sage("-c", "x") == "/usr/bin -c x"  # the link: PATH as it was
    assert vm.install() == (
        f"sage: installed the wrapper as {vm.path}, in place of its link to {vm.program}"
    )
    assert not vm.path.is_symlink() and vm.path.read_bytes() == vm.wrapper.read_bytes()
    assert stat.S_IMODE(vm.path.stat().st_mode) == 0o755
    # the environment's bin first on PATH, the arguments passed on whole
    assert vm.run_sage("-c", "print(giac('1+1'))") == f"{vm.env}/bin -c print(giac('1+1'))"
    assert json.loads(vm.record.read_text()) == {
        "path": str(vm.path),
        "link": str(vm.program),
        "wrapper": hashlib.sha256(vm.wrapper.read_bytes()).hexdigest(),
    }
    assert sorted(p.name for p in vm.bin.iterdir()) == ["sage"]  # no temporary file left


def test_a_second_install_changes_nothing(vm):
    vm.oct9().install()
    before, record = state(vm.path), vm.record.read_text()
    assert vm.install() == f"sage: unchanged: {vm.path} is the wrapper"
    assert state(vm.path) == before and vm.record.read_text() == record


def test_the_new_images_wrapper_is_left_as_it_is(vm):
    vm.new_image()
    before = state(vm.path)
    assert vm.install() == f"sage: unchanged: {vm.path} is the wrapper"
    assert state(vm.path) == before and not vm.record.exists()


def test_an_earlier_version_of_the_wrapper_or_a_wrong_mode_is_replaced(vm, monkeypatch):
    old = b'#!/bin/sh\nexec "$SAGE_ENV/bin/sage" "$@"\n'
    vm.with_sage().path.write_bytes(old)
    vm.path.chmod(0o755)
    monkeypatch.setitem(sw.KNOWN, hashlib.sha256(old).hexdigest(), "an earlier wrapper")
    assert vm.install() == f"sage: updated the wrapper {vm.path}"
    assert vm.path.read_bytes() == vm.wrapper.read_bytes() and not vm.record.exists()
    vm.path.chmod(0o644)  # not executable: sage wouldn't run
    assert vm.install() == f"sage: updated the wrapper {vm.path}"
    assert stat.S_IMODE(vm.path.stat().st_mode) == 0o755


@pytest.mark.parametrize(
    "layout",
    [
        "no sage",
        "no link, an environment",
        "link elsewhere",
        "link to another environment",
        "dangling link",
        "program not executable",
        "another file",
        "a large file",
        "a directory",
        "a fifo",
        "relative link",
        "link through a symlinked directory",
    ],
)
def test_any_other_layout_is_left_alone_and_said(vm, tmp_path, layout):
    """Never a file that isn't zab's or the image's link: refused with a reason, untouched."""
    other = tmp_path / "usr" / "bin" / "sage"
    other.parent.mkdir(parents=True)
    other.write_text("#!/bin/sh\n")
    other.chmod(0o755)
    expect = "left alone"
    if layout == "no sage":
        expect = f"sage: skipped: no SageMath here (no {vm.path}, no {vm.program})"
    elif layout == "no link, an environment":
        vm.with_sage()
        expect = f"sage: skipped: no {vm.path}, though {vm.program} exists: left alone"
    elif layout == "link elsewhere":
        vm.with_sage().path.symlink_to(other)
        expect = f"sage: skipped: {vm.path} links to {other}, not to {vm.program}: left alone"
    elif layout == "link to another environment":
        env2 = vm.home / "miniforge" / "envs" / "sage2" / "bin" / "sage"
        env2.parent.mkdir(parents=True)
        env2.write_text("#!/bin/sh\n")
        env2.chmod(0o755)
        vm.with_sage().path.symlink_to(env2)
    elif layout == "relative link":  # the image's link is absolute (review of #536)
        target = os.path.relpath(vm.program, vm.bin)
        vm.with_sage().path.symlink_to(target)
        expect = f"sage: skipped: {vm.path} links to {target}, not to {vm.program}: left alone"
    elif layout == "link through a symlinked directory":
        # the review's case A: `~/x/..` reads as ~, but ~/x links elsewhere, so this runs the
        # participant's own Sage, not the environment's
        own = tmp_path / "own"
        (own / "sub").mkdir(parents=True)
        mine = own / "miniforge" / "envs" / "sage" / "bin" / "sage"
        mine.parent.mkdir(parents=True)
        mine.write_text("#!/bin/sh\necho mine\n")
        mine.chmod(0o755)
        (vm.home / "x").symlink_to(own / "sub")
        target = f"{vm.home}/x/../miniforge/envs/sage/bin/sage"
        vm.with_sage().path.symlink_to(target)
        assert os.path.realpath(vm.path) == str(mine)  # where it really leads
        expect = f"sage: skipped: {vm.path} links to {target}, not to {vm.program}"
    elif layout == "dangling link":
        vm.path.symlink_to(vm.program)  # the environment is gone
        expect = f"sage: skipped: {vm.path} links to {vm.program}, not an executable file"
    elif layout == "program not executable":
        vm.oct9().program.chmod(0o644)
        expect = "not an executable file: left alone"
    elif layout == "another file":
        vm.with_sage().path.write_text('#!/bin/sh\nexec /opt/sage/sage "$@"\n')
        vm.path.chmod(0o755)
        expect = f"sage: skipped: {vm.path} is not zab's wrapper, nor the image's link"
    elif layout == "a large file":
        vm.with_sage().path.write_bytes(vm.wrapper.read_bytes() + b"#" * sw.MAX_BYTES)
        expect = "is not zab's wrapper"
    elif layout == "a directory":
        vm.path.mkdir()
        expect = f"sage: skipped: {vm.path} is neither a file nor a link: left alone"
    else:
        os.mkfifo(vm.path)
        expect = "is neither a file nor a link"
    before = state(vm.path) if os.path.lexists(vm.path) else None
    outcome = vm.install()
    assert outcome.startswith("sage: skipped: ") and expect in outcome, outcome
    assert (state(vm.path) if os.path.lexists(vm.path) else None) == before
    assert not vm.record.exists() and other.read_text() == "#!/bin/sh\n"
    assert not [p for p in vm.bin.iterdir() if p.name.startswith(".sage-")]


def test_the_record_is_on_disk_before_the_wrapper_replaces_the_link(vm, monkeypatch):
    """The record file synced, renamed, its directory synced; only then the wrapper written,
    synced and renamed over the link (review of #536: a lost record leaves no way back)."""
    vm.oct9()
    events: list[tuple[str, str]] = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd):
        events.append(("fsync", os.readlink(f"/proc/self/fd/{fd}")))
        real_fsync(fd)

    def replace(src, dst):
        events.append(("replace", str(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(sw.os, "fsync", fsync)
    monkeypatch.setattr(sw.os, "replace", replace)
    vm.install()
    names = [(kind, pathlib.Path(where).name) for kind, where in events]
    assert names[1:3] == [("replace", "sage-wrapper.json"), ("fsync", "zab")]
    assert names[0][0] == "fsync" and names[0][1].startswith(".sage-wrapper-")
    assert names[3][0] == "fsync" and names[3][1].startswith(".sage-")
    assert names[4:] == [("replace", "sage")]


def test_remove_puts_the_images_link_back_as_it_was(vm):
    vm.oct9()
    before = os.readlink(vm.path)
    vm.install()
    assert vm.remove() == f"sage: restored {vm.path} as a link to {before}"
    assert vm.path.is_symlink() and os.readlink(vm.path) == before
    assert vm.run_sage("x") == "/usr/bin x"  # Sage runs as on the Oct 9 image again
    assert not vm.record.exists() and sorted(p.name for p in vm.bin.iterdir()) == ["sage"]
    # and the next rollout installs it again
    assert vm.install().startswith("sage: installed the wrapper as ")


def test_remove_never_touches_an_images_own_wrapper_or_a_changed_file(vm):
    vm.new_image()
    before = state(vm.path)
    assert (
        vm.remove() == f"sage: nothing to restore: no record {vm.record}; {vm.path} left as it is"
    )
    assert state(vm.path) == before
    # replaced after the rollout installed the wrapper: left alone, and the record kept
    vm.path.unlink()
    vm.path.symlink_to(vm.program)
    vm.install()
    vm.path.write_text("#!/bin/sh\necho mine\n")
    assert vm.remove().startswith(f"sage: skipped: {vm.path} is no longer zab's wrapper")
    assert vm.path.read_text() == "#!/bin/sh\necho mine\n" and vm.record.exists()
    hand_edited = {"path": str(vm.path), "link": str(vm.program), "wrapper": ["not", "a", "hash"]}
    nested = "[" * 200_000 + "]" * 200_000  # json.load raises RecursionError on it
    for bad in (
        nested,
        "{",
        "[]",
        json.dumps({"path": "/elsewhere", "link": "x"}),
        json.dumps(hand_edited),
    ):
        vm.record.write_text(bad)
        assert vm.remove().startswith("sage: skipped: ")
        assert vm.path.read_text() == "#!/bin/sh\necho mine\n"


def test_the_script_runs_as_the_rollout_runs_it(vm):
    """By path, as agent runs it from the staging directory: one line, exit 0, whatever it
    found; an error is one line on stderr, exit 1, and nothing changed."""
    vm.oct9()

    def run(*argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - argument list, test only
            [sys.executable, "-I", str(ADMIN / "sage_wrapper.py"), *argv],
            capture_output=True,
            text=True,
            check=False,
        )

    where = ("--path", str(vm.path), "--record", str(vm.record))
    done = run("install", str(vm.wrapper), *where)
    assert done.returncode == 0 and done.stderr == ""
    assert (
        done.stdout.startswith("sage: installed the wrapper as ") and done.stdout.count("\n") == 1
    )
    done = run("install", str(vm.wrapper), *where)
    assert (done.returncode, done.stdout) == (0, f"sage: unchanged: {vm.path} is the wrapper\n")
    done = run("remove", *where)
    assert done.returncode == 0 and done.stdout.startswith("sage: restored ")
    link = state(vm.path)
    for argv in (
        ("install", str(vm.wrapper.parent / "missing"), *where),
        ("install", str(vm.program), *where),  # no SAGE_ENV line: not a wrapper
    ):
        done = run(*argv)
        assert done.returncode == 1 and done.stdout == ""
        assert done.stderr.startswith("sage: failed: ") and done.stderr.count("\n") == 1
        assert state(vm.path) == link and not vm.record.exists()
