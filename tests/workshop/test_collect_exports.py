"""`workshop/admin/collect_exports.py` (workshop/admin/README.md §7.2; ADMIN §3, `!export`).

A fake VM stands for `export.sh` and the archive's fetch: the archives land 0600 in a 0700
directory, only re-packed (#374), and a VM can't hand over a credential, a non-archive, an
archive past the limit, or fill the server's disk. `Bounded`, which runs every process, is tested
against real local processes. The check and re-pack themselves are `test_archive_check.py`'s.
"""

from __future__ import annotations

import io
import os
import pathlib
import signal
import stat
import subprocess
import sys
import tarfile

import admin_common as ac
import collect_exports as ce
import pytest

pytestmark = pytest.mark.usefixtures("checker")  # the re-pack's socket

ROSTER = """slug,email,repo,ip,repo_status
alice,alice@example.org,alice,198.51.100.1,ok
bob,bob@example.org,bob,198.51.100.2,ok
carol,carol@example.org,carol,,
dave,dave@example.org,dave,198.51.100.4,
"""
SAFE = ["work/alice/README.md", "work/alice/.git/HEAD", ".claude/projects/x.jsonl"]


def archive(*names: str) -> bytes:
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as tar:
        for name in names:
            info = tarfile.TarInfo(name)
            info.size = 3
            tar.addfile(info, io.BytesIO(b"abc"))
    return data.getvalue()


class FakeVms:
    """`ssh` to the VMs, as `Bounded` runs it: `export.sh` (from stdin), then `sudo cat` of the
    archive into `dest`."""

    def __init__(self, archives: dict[str, bytes] | None = None, failing: set[str] = frozenset()):
        self.archives = archives or {}
        self.failing = failing
        self.calls: list[tuple[list[str], bytes | None]] = []

    def __call__(
        self, argv, *, timeout_s, stdin=None, dest=None, limit=ce.OUTPUT_CAP,
        room=lambda: True, env=None, cwd=None,
    ):  # fmt: skip
        self.calls.append((argv, stdin))
        host = argv[-3].removeprefix("ubuntu@")
        if argv[-1] == ce.EXPORT_CMD:
            if host in self.failing:
                return ac.RunResult(1, "", "export.sh: nothing to export under /home/agent\n")
            return ac.RunResult(0, "wrote /home/agent/export-x.tar.gz (1K)\n", "")
        data = self.archives.get(host, archive(*SAFE))
        if len(data) > limit:
            return ac.RunResult(-1, "", f"larger than {limit} bytes: not kept")
        if not room():
            return ac.RunResult(-1, "", "the disk is nearly full: not kept")
        dest.write(data)
        return ac.RunResult(0, "", "")


@pytest.fixture
def roster(tmp_path):
    path = tmp_path / "roster.csv"
    path.write_text(ROSTER)
    return path


def run(roster, tmp_path, vms, *only, dry_run=False, limit=1 << 20, reserve=0):
    rows = ac.select_rows(ac.load_roster(str(roster)), list(only) or None)
    out = tmp_path / "out" / "exports"
    if not dry_run:
        ce.export_dir(out)
    return out, [
        ce.collect(
            row,
            vms,
            out_dir=out,
            script="#!/bin/bash\necho export\n",
            known_hosts=str(tmp_path / "known_hosts"),
            limit=limit,
            reserve=reserve,
            dry_run=dry_run,
        )
        for row in rows
    ]


def mode(path: pathlib.Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_archives_are_private_and_checked(roster, tmp_path):
    vms = FakeVms()
    (tmp_path / "out").mkdir(mode=0o755)
    out, results = run(roster, tmp_path, vms)
    assert [(r.slug, r.note, r.problems) for r in results] == [
        ("alice", "ok", []),
        ("bob", "ok", []),
        ("carol", "skipped: not provisioned, nothing to export", []),
        ("dave", "skipped: not provisioned, nothing to export", []),
    ]
    assert mode(out) == 0o700
    assert sorted(p.name for p in out.iterdir()) == ["export-alice.tar.gz", "export-bob.tar.gz"]
    for path in out.iterdir():
        assert mode(path) == 0o600
        with tarfile.open(path) as tar:
            assert tar.getnames() == SAFE


def test_the_commands_are_fixed_and_pinned(roster, tmp_path):
    vms = FakeVms()
    run(roster, tmp_path, vms, "alice")
    (export, script), (fetch, none) = vms.calls
    kh = f'UserKnownHostsFile="{tmp_path / "known_hosts"}"'
    for argv in (export, fetch):
        assert argv[0] == "ssh" and kh in argv and "StrictHostKeyChecking=yes" in argv
        assert argv[-3:-1] == ["ubuntu@198.51.100.1", "--"]
    assert export[-1] == "cd / && sudo -u agent -H bash -s"  # the script comes on stdin
    assert script == b"#!/bin/bash\necho export\n" and none is None
    assert fetch[-1] == "sudo -u agent cat -- /home/agent/export-alice.tar.gz"  # never root


@pytest.mark.parametrize(
    "name",
    [
        ".codex/auth.json",
        "./.claude/.credentials.json",
        "work/alice/notes/claude.zuliprc",
        ".config/zab/config.toml",
        ".config/zab",
    ],
)
def test_an_archive_that_names_a_credential_is_deleted(roster, tmp_path, name):
    vms = FakeVms({"198.51.100.1": archive(*SAFE, name)})
    out, results = run(roster, tmp_path, vms, "alice")
    assert results[0].problems == [f"it names a credential file ({name}): deleted"]
    assert list(out.iterdir()) == []  # neither the archive nor its temporary file


def names(path: pathlib.Path) -> list[str]:
    with tarfile.open(path) as tar:
        return tar.getnames()


def test_a_failed_rerun_keeps_the_archive_collected_before(roster, tmp_path):
    out, _ = run(roster, tmp_path, FakeVms(), "alice")
    before = (out / "export-alice.tar.gz").read_bytes()
    for bad in (archive(".codex/auth.json"), b"junk"):
        _, results = run(roster, tmp_path, FakeVms({"198.51.100.1": bad}), "alice")
        assert results[0].problems
        assert (out / "export-alice.tar.gz").read_bytes() == before
    newer = archive(*SAFE, "work/alice/new.md")
    run(roster, tmp_path, FakeVms({"198.51.100.1": newer}), "alice")
    assert names(out / "export-alice.tar.gz") == [*SAFE, "work/alice/new.md"]


def test_only_the_re_packed_archive_is_stored(roster, tmp_path):
    """#374: the participant's own bytes are never kept, only what the re-pack wrote; what it
    leaves out, and the copies it makes, show in the table."""
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
        for name, kind, target in (
            ("work/a.md", tarfile.REGTYPE, ""),
            ("work/same.md", tarfile.LNKTYPE, "work/a.md"),
            ("work/latest", tarfile.SYMTYPE, "a.md"),
        ):
            info = tarfile.TarInfo(name)
            info.type, info.linkname, info.uname, info.mode = kind, target, "agent", 0o600
            info.size = 3 * (kind == tarfile.REGTYPE)
            tar.addfile(info, io.BytesIO(b"abc"))
    sent = data.getvalue()
    out, results = run(roster, tmp_path, FakeVms({"198.51.100.1": sent}), "alice")
    assert results[0].note == "ok: 1 symlink(s) listed in SYMLINKS.txt; 1 hard link(s) copied"
    assert sorted(p.name for p in out.iterdir()) == ["export-alice.tar.gz"]
    stored = out / "export-alice.tar.gz"
    assert stored.read_bytes() != sent
    with tarfile.open(stored) as tar:
        members = tar.getmembers()
        assert [(m.name, m.type, m.mode, m.uname) for m in members] == [
            ("work/a.md", tarfile.REGTYPE, 0o644, ""),
            ("work/same.md", tarfile.REGTYPE, 0o644, ""),
            ("SYMLINKS.txt", tarfile.REGTYPE, 0o644, ""),
        ]
        assert tar.extractfile("work/same.md").read() == b"abc"


def test_a_stale_temporary_file_or_a_planted_link_is_not_followed(roster, tmp_path):
    out = ce.export_dir(tmp_path / "out" / "exports")
    target = tmp_path / "elsewhere"
    target.write_text("keep me")
    (out / ".export-alice.tar.gz.part").symlink_to(target)
    (out / ".export-alice.tar.gz.new").symlink_to(target)
    _, results = run(roster, tmp_path, FakeVms(), "alice")
    assert results[0].note == "ok" and target.read_text() == "keep me"
    assert not (out / "export-alice.tar.gz").is_symlink()


def test_export_failures_fetch_nothing(roster, tmp_path):
    vms = FakeVms(failing={"198.51.100.2"})
    out, results = run(roster, tmp_path, vms, "bob")
    assert results[0].problems == [
        "export.sh failed: export.sh: nothing to export under /home/agent"
    ]
    assert len(vms.calls) == 1 and list(out.iterdir()) == []


def test_limits_on_size_and_disk(roster, tmp_path):
    vms = FakeVms()
    _, results = run(roster, tmp_path, vms, "alice", limit=10)
    assert results[0].problems == ["fetch failed: larger than 10 bytes: not kept"]
    _, results = run(roster, tmp_path, vms, "alice", reserve=1 << 62)
    assert results[0].problems == ["fetch failed: the disk is nearly full: not kept"]


def test_a_row_without_a_vm_is_a_problem(tmp_path):
    path = tmp_path / "roster.csv"
    path.write_text("slug,email,repo,ip,repo_status\neve,eve@example.org,eve,,ok\n")
    _, results = run(path, tmp_path, FakeVms())
    assert results[0].problems[0].startswith("no IP address for 'eve'")


def test_the_dry_run_connects_to_nothing(roster, tmp_path):
    vms = FakeVms()
    out, results = run(roster, tmp_path, vms, "alice", "bob", dry_run=True)
    assert vms.calls == [] and not out.exists()
    assert [r.note for r in results] == ["would export and fetch"] * 2


def test_main_selects_reports_and_fails_on_a_problem(roster, tmp_path, capsys):
    vms = FakeVms({"198.51.100.2": b"junk"})
    out = tmp_path / "exports"
    args = ["--roster", str(roster), "--known-hosts", str(tmp_path / "kh"), "--out-dir", str(out)]
    assert ce.main([*args, "--only", "alice"], ssh=vms) == 0
    assert "no problems" in capsys.readouterr().out
    assert ce.main(args, ssh=vms) == 1
    printed = capsys.readouterr().out
    assert "1 VM(s) with problems" in printed and "2 row(s) not provisioned" in printed
    assert ce.main([*args, "--only", "eve"], ssh=vms) == 1  # not in the roster
    assert ce.main([*args, "--dry-run"], ssh=vms) == 0
    assert ce.main([*args, "--only", "alice", "--max-expanded-mb", "1"], ssh=vms) == 0
    for bad in ("--max-mb", "--max-expanded-mb", "--parallel"):
        with pytest.raises(SystemExit):
            ce.main([*args, bad, "0"], ssh=vms)


# ------------------------------------------------------------------------------ Bounded


def bounded(code: str, tmp_path: pathlib.Path, *, to_file: bool = True, **kwargs):
    """`code` run by `Bounded`, its stdout to a file (as the fetch) or kept (as export.sh)."""
    path = tmp_path / "fetched"
    kwargs.setdefault("timeout_s", 10.0)
    with path.open("wb") as dest:
        got = ce.Bounded()([sys.executable, "-c", code], dest=dest if to_file else None, **kwargs)
    return got, path.read_bytes()


BIG = "import sys; sys.stdout.buffer.write(b'y' * 3_000_000)"


def test_bounded_streams_stdout_to_the_file(tmp_path):
    got, data = bounded(BIG, tmp_path, limit=4 << 20)
    assert got.ok and data == b"y" * 3_000_000


def test_bounded_stops_at_the_limit_the_disk_and_the_time(tmp_path):
    got, data = bounded(BIG, tmp_path, limit=1 << 20)
    assert (got.exit_code, got.stderr) == (-1, "larger than 1 MB: not kept")
    assert len(data) <= 1 << 20
    got, _ = bounded(BIG, tmp_path, limit=4 << 20, room=lambda: False)
    assert got.stderr == "the disk is nearly full: not kept"
    got, _ = bounded("import time; time.sleep(30)", tmp_path, timeout_s=0.3)
    assert got.stderr == "timed out after 0.3 s"


def test_bounded_keeps_only_the_head_of_stderr_and_stops_a_flood(tmp_path):
    """Codex rounds 1 and 3: a VM's output is read as it comes and only its head kept; a flood
    of it, before or while stdout comes, neither blocks nor fills memory or a disk."""
    some = "import sys; sys.stderr.write('e' * 100_000); print('done'); sys.exit(255)"
    got, data = bounded(some, tmp_path)
    assert (got.exit_code, data, got.stderr) == (255, b"done\n", "e" * ce.KEPT)
    flood = "import sys\nwhile True: sys.stderr.buffer.write(b'e' * 65536)\n"
    got, _ = bounded(flood, tmp_path)
    assert (got.exit_code, got.stderr) == (-1, "its stderr passed 1024 KB")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["fetched"]


def test_bounded_caps_kept_stdout(tmp_path):
    """The export.sh call: stdout isn't a file, so its head is kept and a flood stopped."""
    got, _ = bounded("print('wrote it')", tmp_path, to_file=False)
    assert got.ok and got.stdout == "wrote it\n"
    got, _ = bounded("import sys; sys.stdout.write('x' * 9000)", tmp_path, to_file=False)
    assert got.ok and got.stdout == "x" * ce.KEPT
    flood = "import sys\nwhile True: sys.stdout.buffer.write(b'y' * 65536)\n"
    got, _ = bounded(flood, tmp_path, to_file=False)
    assert (got.exit_code, got.stderr) == (-1, "printed over 1 MB")


def test_bounded_feeds_stdin_without_blocking(tmp_path):
    echo = "import sys; data = sys.stdin.buffer.read(); print(len(data))"
    got, _ = bounded(echo, tmp_path, to_file=False, stdin=b"s" * 5_000_000)
    assert got.ok and got.stdout == "5000000\n"
    got, _ = bounded("import sys; sys.exit(3)", tmp_path, stdin=b"s" * 5_000_000)
    assert got.exit_code == 3  # it never read: no deadlock, no BrokenPipeError


def test_bounded_reports_the_commands_failure(tmp_path):
    code = "import sys; sys.stderr.write('No such file'); sys.exit(1)"
    got, _ = bounded(code, tmp_path)
    assert (got.exit_code, got.stderr) == (1, "No such file")
    got = ce.Bounded()(["/nonexistent/ssh"], timeout_s=1)
    assert got.exit_code == -1 and "cannot run" in got.stderr


# ------------------------------------------------- the review of #360: one run, its own bytes


def main_args(roster, tmp_path, *more):
    out = tmp_path / "exports"
    return out, ["--roster", str(roster), "--known-hosts", str(tmp_path / "kh"),
                 "--out-dir", str(out), *more]  # fmt: skip


def test_one_run_at_a_time_on_a_directory(roster, tmp_path, capsys):
    out, args = main_args(roster, tmp_path, "--only", "alice")
    with ce.exports_lock(ce.export_dir(out)):
        assert ce.main(args, ssh=FakeVms()) == 1
    assert "another collect_exports.py run is using" in capsys.readouterr().err
    assert ce.main(args, ssh=FakeVms()) == 0  # released with its run
    assert (out / "export-alice.tar.gz").exists()


def test_a_killed_runs_temporary_files_go_at_the_next_run(roster, tmp_path):
    """A run killed by SIGKILL (the runner's, at its deadline) runs no `finally`: its lock goes
    with its process, and the next run deletes its `.part` files first."""
    out, args = main_args(roster, tmp_path, "--only", "alice")
    out = ce.export_dir(out)
    code = (
        "import pathlib, sys, time\n"
        f"sys.path.insert(0, {str(pathlib.Path(ce.__file__).parent)!r})\n"
        "import collect_exports as ce\n"
        f"out = pathlib.Path({str(out)!r})\n"
        "with ce.exports_lock(out):\n"
        "    for slug in ('alice', 'bob'):\n"
        "        (out / f'.export-{slug}.tar.gz.part').write_bytes(b'unchecked')\n"
        "    (out / '.export-bob.tar.gz.new').write_bytes(b'half written')\n"
        "    print('ready', flush=True)\n"
        "    time.sleep(60)\n"
    )
    child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE)  # noqa: S603
    assert child.stdout is not None and child.stdout.readline() == b"ready\n"
    with pytest.raises(ValueError, match=r"another collect_exports\.py run"):
        with ce.exports_lock(out):
            pass
    os.kill(child.pid, signal.SIGKILL)
    child.wait()
    child.stdout.close()
    assert sorted(p.name for p in out.glob(".export-*")) == [
        ".export-alice.tar.gz.part", ".export-bob.tar.gz.new", ".export-bob.tar.gz.part",
    ]  # fmt: skip
    assert ce.main(args, ssh=FakeVms()) == 0
    assert list(out.glob(".export-*")) == []  # bob's too, though this run didn't name him
    assert (out / "export-alice.tar.gz").exists()


def test_only_the_bytes_written_by_the_re_pack_are_kept(roster, tmp_path, monkeypatch):
    """The new archive is renamed into place only if its temporary name still is the file the
    re-pack wrote: bytes swapped in afterwards are never kept."""
    real = ce.repack_file

    def repack_then_swap(archive, dest, **kwargs):
        verdict = real(archive, dest, **kwargs)
        new = next((tmp_path / "out" / "exports").glob(".export-*.new"))
        swapped = new.with_name("swapped")
        swapped.write_bytes(b"unchecked bytes")
        os.replace(swapped, new)
        return verdict

    monkeypatch.setattr(ce, "repack_file", repack_then_swap)
    out, results = run(roster, tmp_path, FakeVms(), "alice")
    assert results[0].problems == ["its temporary file was replaced: not kept"]
    assert list(out.iterdir()) == []


def test_the_re_pack_reads_the_file_it_was_handed(roster, tmp_path, monkeypatch):
    """`repack_file` gets the open file the fetch wrote, never a path to reopen, and the open
    file it writes to."""
    seen = []
    real = ce.repack_file

    def spy(archive, dest, **kwargs):
        seen.append((archive, dest))
        return real(archive, dest, **kwargs)

    monkeypatch.setattr(ce, "repack_file", spy)
    _, results = run(roster, tmp_path, FakeVms(), "alice")
    assert results[0].problems == [] and len(seen) == 1
    for file in seen[0]:
        assert not isinstance(file, (str, pathlib.Path)) and file.closed
