"""`workshop/admin/apt_repair.py` (#947): an older image's expired Macaulay2 snapshot source,
removed at a rollout so that the VM's `apt-get update` works, and Macaulay2's packages held; and
the rollout's step that runs it (`push_config.apt_step`). `apt-mark` and `dpkg-query` are
stand-ins that log their arguments."""

from __future__ import annotations

import os
import stat

import apt_repair as ar
import pytest

HOST = "snapshot.ppa.launchpadcontent.net"
#: The Oct 19 image's file, as `build.sh`'s step 1d wrote it.
SNAPSHOT = (
    "Types: deb\n"
    f"URIs: https://{HOST}/macaulay2/macaulay2/ubuntu/20261018T000000Z\n"
    "Suites: noble\nComponents: main\nSigned-By: /etc/apt/keyrings/macaulay2-ppa.asc\n"
)
#: The PPA itself, not a snapshot of it: a source someone added on purpose, never removed.
PPA = SNAPSHOT.replace(HOST, "ppa.launchpadcontent.net").replace("/20261018T000000Z", "")
#: An agent's repair by hand: the snapshot's line commented out, the live PPA's beside it (#949's
#: review, B1). apt reads only the PPA, so the file is left as it is, and quietly.
COMMENTED = PPA.replace("URIs:", f"# URIs: https://{HOST}/macaulay2/macaulay2/ubuntu/x\nURIs:")
#: Each with its stanza turned off, as apt reads `Enabled: no`.
OFF_SNAPSHOT, OFF_PPA = "Enabled: no\n" + SNAPSHOT, "Enabled: no\n" + PPA


class Vm:
    """A VM's sources file, and stand-ins for `apt-mark` and `dpkg-query` that log each call.
    `dpkg-query` prints `status`'s abbreviation for a package and exits 0, as the real one does
    for any package dpkg knows, installed (`ii`, `hi`) or not (`un`, `rc`); for the others it
    exits 1, as for a package dpkg has never heard of. By default all five are installed."""

    def __init__(self, tmp_path, status=None, mark_fails=False):
        status = {name: "ii " for name in ar.PACKAGES} if status is None else status
        self.sources = tmp_path / "macaulay2.sources"
        self.log = tmp_path / "calls.log"
        self.log.touch()
        self.apt_mark = self._tool(tmp_path / "apt-mark", "exit 1" if mark_fails else "exit 0")
        cases = "\n".join(f'  {name}) printf "{abbrev}" ;;' for name, abbrev in status.items())
        self.dpkg_query = self._tool(
            tmp_path / "dpkg-query",
            f"for last; do :; done\ncase $last in\n{cases}\n  *) exit 1 ;;\nesac",
        )

    def _tool(self, path, body):
        path.write_text(f'#!/bin/sh\nprintf "%s\\n" "${{0##*/}} $*" >>{self.log}\n{body}\n')
        path.chmod(0o755)
        return str(path)

    def run(self, capsys) -> tuple[int, str]:
        argv = ["--sources", str(self.sources), "--apt-mark", self.apt_mark]
        code = ar.main([*argv, "--dpkg-query", self.dpkg_query])
        return code, capsys.readouterr().out.strip()

    def holds(self) -> list[str]:
        return [line for line in self.log.read_text().splitlines() if line.startswith("apt-mark")]


def removed(vm: Vm, holds: str) -> str:
    return f"apt: removed {vm.sources}, an expired {HOST} source; {holds}"


ALL_HELD = "held macaulay2, macaulay2-common, gfan, msolve, libmsolve3"


def test_the_expired_snapshot_source_goes_and_the_packages_are_held(tmp_path, capsys):
    vm = Vm(tmp_path)
    vm.sources.write_text(SNAPSHOT)
    assert vm.run(capsys) == (0, removed(vm, ALL_HELD)) and not vm.sources.exists()
    assert vm.holds() == ["apt-mark hold macaulay2 macaulay2-common gfan msolve libmsolve3"]
    # a rerun changes nothing: the file is gone, and nothing is held again
    assert vm.run(capsys) == (0, "apt: nothing to repair") and len(vm.holds()) == 1


@pytest.mark.parametrize(
    ("kind", "content"),
    [
        ("a comment beside it", f"# the image's own\n{SNAPSHOT}"),
        ("its field folded and lower-cased, the host in capitals",
         f"Types: deb\nuris:\n https://{HOST.upper()}/macaulay2/macaulay2/ubuntu/x\n"
         "Suites: noble\n"),
        ("a stanza of it turned off beside one on",
         f"{SNAPSHOT}\n{OFF_SNAPSHOT}"),
        ("turned off, then on again after a line of blanks, which ends no stanza",
         f"{OFF_SNAPSHOT} \nEnabled: yes\n"),
    ],
)  # fmt: skip
def test_the_snapshot_source_is_read_as_apt_reads_it(tmp_path, capsys, kind, content):
    vm = Vm(tmp_path)
    vm.sources.write_text(content)
    assert vm.run(capsys) == (0, removed(vm, ALL_HELD)), kind
    assert not vm.sources.exists()


def test_only_the_installed_packages_are_held(tmp_path, capsys):
    """Installed is the status's second letter: `hi`, held already (the retry after a removal
    that failed, say), counts; `rc` and `un`, which dpkg-query answers with exit 0, don't
    (#949's review, N1 and N2). `apt-mark hold` of a held package does nothing."""
    status = {"macaulay2": "ii ", "macaulay2-common": "hi ", "gfan": "ii ", "msolve": "rc "}
    vm = Vm(tmp_path, status=status | {"libmsolve3": "un "})
    vm.sources.write_text(SNAPSHOT)
    assert vm.run(capsys) == (0, removed(vm, "held macaulay2, macaulay2-common, gfan"))
    assert vm.holds() == ["apt-mark hold macaulay2 macaulay2-common gfan"]


@pytest.mark.parametrize("abbrev", [None, "un ", "rc "])
def test_none_installed_holds_nothing(tmp_path, capsys, abbrev):
    vm = Vm(tmp_path, status={} if abbrev is None else dict.fromkeys(ar.PACKAGES, abbrev))
    vm.sources.write_text(SNAPSHOT)
    assert vm.run(capsys) == (0, removed(vm, "none of its packages installed"))
    assert vm.holds() == [] and not vm.sources.exists()


def test_no_source_is_nothing_to_do(tmp_path, capsys):
    vm = Vm(tmp_path)
    assert vm.run(capsys) == (0, "apt: nothing to repair")
    assert vm.log.read_text() == ""  # neither dpkg-query nor apt-mark ran


@pytest.mark.parametrize(
    ("kind", "content"),
    [
        ("the PPA itself", PPA),
        ("another archive", "Types: deb\nURIs: http://example.org/u\n"),
        ("the snapshot's line commented out, the PPA's live", COMMENTED),
        ("another field naming the host", PPA.replace("Suites:", f"X-Note: https://{HOST}/\nSuites:")),
        ("the snapshot's stanza turned off, the PPA's live",
         f"{OFF_SNAPSHOT}\n{PPA}"),
        ("a look-alike host", SNAPSHOT.replace(HOST, f"{HOST}.example.org")),
        ("its stanza turned off as `No`", f"Enabled: No\n{SNAPSHOT}"),
        ("the PPA's stanza after a line of blanks, which joins it to the snapshot's",
         f"{SNAPSHOT} \n{PPA}"),
    ],
)  # fmt: skip
def test_a_file_without_a_live_snapshot_source_is_left_quietly(tmp_path, capsys, kind, content):
    """Only the hosts of the `URIs:` values of the stanzas apt reads count: never a comment,
    another field or a stanza turned off (#949's review, B1)."""
    vm = Vm(tmp_path)
    vm.sources.write_text(content)
    code, line = vm.run(capsys)
    assert (code, line) == (0, f"apt: nothing to repair: {vm.sources} has no live {HOST} source")
    assert vm.sources.read_text() == content and vm.log.read_text() == "", kind


@pytest.mark.parametrize(
    ("kind", "content"),
    [
        ("both in one field", SNAPSHOT.replace("20261018T000000Z", "x https://ppa.launchpadcontent.net/m/u")),
        ("in one field, after a comment line",
         SNAPSHOT.replace("Suites:", "# the live one\n https://ppa.launchpadcontent.net/m/u\nSuites:")),
        ("a stanza of each", f"{SNAPSHOT}\n{PPA}"),
        ("another stanza, turned off", f"{SNAPSHOT}\n{OFF_PPA}"),
        ("a malformed URI beside it", SNAPSHOT.replace("20261018T000000Z", "x http://[::1/u")),
        ("both in one field, a vertical tab between",
         SNAPSHOT.replace("20261018T000000Z", "x\vhttps://ppa.launchpadcontent.net/m/u")),
        ("a stanza of each, CRLF line ends", f"{SNAPSHOT}\n{PPA}".replace("\n", "\r\n")),
    ],
)  # fmt: skip
def test_a_snapshot_source_beside_others_is_kept_and_said(tmp_path, capsys, kind, content):
    """Removing the file would remove what else it names, which someone wrote: it stays, and
    the line, which the rollout notes, says that apt still fails there (#949's review, N7)."""
    vm = Vm(tmp_path)
    vm.sources.write_text(content)
    assert vm.run(capsys) == (
        0,
        f"apt: kept {vm.sources}: it names other sources beside the expired {HOST} one, so apt "
        "still fails there; fix it by hand",
    ), kind
    assert vm.sources.read_bytes() == content.encode() and vm.log.read_text() == ""


def test_a_file_too_large_is_kept_and_said(tmp_path, capsys):
    vm = Vm(tmp_path)
    vm.sources.write_text(SNAPSHOT + "#" * ar.LIMIT)
    assert vm.run(capsys) == (
        0,
        f"apt: kept {vm.sources}: over {ar.LIMIT} bytes, so no sources file of the image's",
    )
    assert vm.sources.exists() and vm.log.read_text() == ""


def test_a_link_a_directory_or_a_fifo_is_kept_and_said(tmp_path, capsys):
    odd = "apt: kept {}: not a regular file, so apt may still fail there; look at it"
    target = tmp_path / "elsewhere.sources"
    target.write_text(SNAPSHOT)
    vm = Vm(tmp_path)
    vm.sources.symlink_to(target)
    assert vm.run(capsys) == (0, odd.format(vm.sources))
    assert vm.sources.is_symlink() and target.read_text() == SNAPSHOT and vm.holds() == []
    vm.sources.unlink()
    vm.sources.mkdir()
    assert vm.run(capsys) == (0, odd.format(vm.sources))
    assert stat.S_ISDIR(vm.sources.stat().st_mode)
    vm.sources.rmdir()
    os.mkfifo(vm.sources)
    assert vm.run(capsys) == (0, odd.format(vm.sources))
    assert stat.S_ISFIFO(vm.sources.lstat().st_mode) and vm.log.read_text() == ""


@pytest.mark.timeout(10)  # a FIFO opened without O_NONBLOCK would wait for a writer
@pytest.mark.parametrize("kind", ["link", "fifo", "directory"])
def test_what_is_swapped_in_after_the_look_is_never_read(tmp_path, capsys, monkeypatch, kind):
    """The file is opened with O_NOFOLLOW | O_NONBLOCK and checked with fstat (#949's review, N4
    and the re-review's N2): the lstat here answers for a regular file elsewhere, as if the
    thing at the path had replaced it in between. A link fails the open (exit 1, kept); a FIFO
    is opened without waiting and a directory without reading, and both are kept and said."""
    target = tmp_path / "elsewhere.sources"
    target.write_text(SNAPSHOT)
    vm = Vm(tmp_path)
    {"link": lambda: vm.sources.symlink_to(target), "fifo": lambda: os.mkfifo(vm.sources),
     "directory": vm.sources.mkdir}[kind]()  # fmt: skip
    real = os.lstat
    monkeypatch.setattr(
        ar.os, "lstat", lambda path, **kw: real(target if str(path) == str(vm.sources) else path)
    )
    code, line = vm.run(capsys)
    monkeypatch.undo()
    if kind == "link":
        assert code == 1 and line.startswith(f"apt: {vm.sources} kept for the next rollout: ")
        assert vm.sources.is_symlink()
    else:
        assert (code, line) == (
            0,
            f"apt: kept {vm.sources}: not a regular file, so apt may still fail there; look at it",
        )
    assert vm.sources.exists() and target.read_text() == SNAPSHOT and vm.holds() == []


def test_a_failed_hold_keeps_the_source_for_the_next_rollout(tmp_path, capsys):
    """#949's Codex round 1: the holds come first; a hold that fails (dpkg's lock, say) keeps
    the source, so the next rollout tries both again, rather than finding nothing to repair
    with the packages unheld. Exit 1, which the rollout records under `apt source: …`."""
    vm = Vm(tmp_path, mark_fails=True)
    vm.sources.write_text(SNAPSHOT)
    code, line = vm.run(capsys)
    assert code == 1 and vm.sources.read_text() == SNAPSHOT
    assert line.startswith(f"apt: hold failed, so {vm.sources} is kept for the next rollout")
    # the next rollout, the lock free: held, then removed
    retry = Vm(tmp_path)
    assert retry.run(capsys) == (0, removed(retry, ALL_HELD)) and not retry.sources.exists()
    assert retry.holds()[-1] == "apt-mark hold macaulay2 macaulay2-common gfan msolve libmsolve3"


@pytest.mark.skipif(os.geteuid() == 0, reason="root removes from a read-only directory")
def test_a_failed_removal_is_a_failure(tmp_path, capsys):
    """After the holds; the next rollout finds the packages held (`hi`) and tries again."""
    (tmp_path / "etc").mkdir()
    vm = Vm(tmp_path / "etc")
    vm.sources.write_text(SNAPSHOT)
    (tmp_path / "etc").chmod(0o555)
    try:
        code, line = vm.run(capsys)
    finally:
        (tmp_path / "etc").chmod(0o755)
    assert (code, line) == (1, f"apt: {vm.sources} not removed: Permission denied")
    assert vm.sources.read_text() == SNAPSHOT and len(vm.holds()) == 1


@pytest.mark.parametrize("missing", ["apt_mark", "dpkg_query"])
def test_an_error_is_one_line_and_keeps_the_source(tmp_path, capsys, missing):
    """A program missing, cut off or failing to start, or the file unreadable, is a line the
    rollout can show, not a traceback (#949's review, N3); all come before the removal."""
    vm = Vm(tmp_path)
    vm.sources.write_text(SNAPSHOT)
    setattr(vm, missing, str(tmp_path / "nowhere"))
    code, line = vm.run(capsys)
    assert code == 1 and "\n" not in line
    assert line.startswith(f"apt: {vm.sources} kept for the next rollout: [Errno 2] ")
    assert vm.sources.read_text() == SNAPSHOT


#: dpkg-query's answer on a malformed status file, as dpkg 1.22.6 (noble's) gives it: exit 2.
FATAL = (
    "echo \"dpkg-query: error: parsing file '/var/lib/dpkg/status' near line 0:\" >&2\n"
    "echo \" field name 'Package' must be followed by colon\" >&2\nexit 2"
)


@pytest.mark.parametrize(
    ("body", "said"),
    [
        (FATAL, "exit 2: dpkg-query: error: parsing file '/var/lib/dpkg/status' near line 0: "
         "field name 'Package' must be followed by colon"),
        ("kill -9 $$", "exit -9: no message"),
    ],
)  # fmt: skip
def test_a_dpkg_query_error_keeps_the_source_unheld_for_the_next_rollout(
    tmp_path, capsys, body, said
):
    """#949's Codex round 2: only exit 1 means a package dpkg doesn't know. A fatal error (2,
    the database unreadable or malformed) or a signal says nothing about what is installed, so
    the run stops there, before any hold or removal, says why, and exits 1; the next rollout
    tries again. Read as "not installed", it would have removed the source with nothing held,
    and no later rollout could have held them."""
    vm = Vm(tmp_path)
    vm.sources.write_text(SNAPSHOT)
    vm.dpkg_query = vm._tool(tmp_path / "dpkg-query-broken", body)
    code, line = vm.run(capsys)
    assert (code, line) == (
        1,
        f"apt: {vm.sources} kept for the next rollout: dpkg-query macaulay2: {said}",
    )
    assert vm.sources.read_text() == SNAPSHOT and vm.holds() == []
    retry = Vm(tmp_path)  # the database repaired
    assert retry.run(capsys) == (0, removed(retry, ALL_HELD)) and not retry.sources.exists()


def test_the_holds_come_before_the_removal(tmp_path, capsys):
    """The stand-in `apt-mark` records whether the source was still there when it ran."""
    vm = Vm(tmp_path)
    vm.sources.write_text(SNAPSHOT)
    seen = tmp_path / "seen"
    vm._tool(tmp_path / "apt-mark", f"test -e {vm.sources} && echo present > {seen}; exit 0")
    assert vm.run(capsys)[0] == 0 and seen.read_text() == "present\n"
    assert not vm.sources.exists()


# --- the rollout's step (push_config.apt_step) --------------------------------------------------


STAGING = "/tmp/zab-push.AbC123"  # noqa: S108 - the fake VM's mktemp answer
SRC = "/etc/apt/sources.list.d/macaulay2.sources"


class Result:
    def __init__(self):
        self.notes, self.failures = [], []


def runner_answering(code, stdout, stderr=""):
    import admin_common as ac

    calls = []

    def run(argv, timeout_s=120.0, **_):
        calls.append(argv)
        return ac.RunResult(code, stdout, stderr)

    return run, calls


@pytest.mark.parametrize(
    ("code", "line", "noted", "failed"),
    [
        (0, f"apt: removed {SRC}, an expired … source; held macaulay2", True, False),
        (0, "apt: nothing to repair", False, False),
        (0, f"apt: nothing to repair: {SRC} has no live … source", False, False),
        (0, f"apt: kept {SRC}: it names other sources beside the expired … one, so apt still "
         "fails there; fix it by hand", True, False),
        (0, f"apt: kept {SRC}: not a regular file, so apt may still fail there; look at it", True,
         False),
        (1, f"apt: hold failed, so {SRC} is kept for the next rollout: E: Could not get lock",
         False, True),
        (1, f"apt: {SRC} not removed: Permission denied", False, True),
    ],
)  # fmt: skip
def test_the_rollout_notes_what_it_did_or_left_and_records_a_failure(code, line, noted, failed):
    """Noted: a removal, and a file kept that apt may still fail on (#949's review, N7)."""
    import push_config as pc

    run, calls = runner_answering(code, line + "\n")
    result = Result()
    assert pc.apt_step(result, run, lambda remote: ["ssh", "vm", "--", remote], STAGING) is (
        code == 0
    )
    assert result.notes == ([line] if noted else [])
    assert result.failures == ([f"apt source: {line}"] if failed else [])
    # as root, the uploaded copy, with the system Python, an argument list quoted whole
    assert calls == [["ssh", "vm", "--", f"sudo /usr/bin/python3 {STAGING}/apt_repair.py"]]


def test_the_image_build_leaves_no_snapshot_source():
    """`build.sh` (#947): the source serves the build alone, removed at its end, after the
    packages are installed and the smoke test has passed, the packages held as a rollout holds
    them on an older image."""
    from pathlib import Path

    build = (Path(__file__).resolve().parents[2] / "workshop" / "ami" / "build.sh").read_text()
    hold = "sudo apt-mark hold macaulay2 macaulay2-common gfan msolve libmsolve3 >/dev/null\n"
    remove = "sudo rm -f /etc/apt/sources.list.d/macaulay2.sources\n"
    assert build.count(hold) == 1 and build.count(remove) == 1
    installed = build.index('$APT install -y --no-install-recommends "${m2_install[@]/#/$m2/}"')
    smoke = build.index('echo "smoke test FAILED"')
    assert installed < smoke < build.index(hold) < build.index(remove)
    assert " ".join(ar.PACKAGES) in hold
