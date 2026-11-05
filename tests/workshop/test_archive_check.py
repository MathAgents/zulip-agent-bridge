"""The archive check and re-pack (`workshop/admin/archive_check.py`), as `collect_exports.py` runs
it: in a child process with hard resource limits (workshop/admin/README.md §7.2, step 3; #360, as
decided after Codex rounds 1 to 3; #374, the re-pack).

The limits are the guarantee: whatever a parser does with an archive, the child fails closed
and the archive is refused. The explicit checks give the refusal its reason: a whole gzip
stream, valid headers to the end, no credential's name, bounded metadata (each member's and the
global PAX attributes'), a bounded expansion. Every archive here goes through the re-pack, and
every one it accepts is checked (`safe`): only regular files and directories, under normalized
names, none a credential's, listed alike by GNU tar and tarfile. A normal export of about 1 GiB
of small files, generated, re-packs well within the CPU limit.
"""

from __future__ import annotations

import array
import gzip
import io
import ntpath
import os
import pathlib
import posixpath
import re
import resource
import socket
import struct
import subprocess
import sys
import tarfile
import threading
import time

import archive_check as ac_check
import collect_exports as ce
import pytest

from .checker import SERVE, Checker, me
from .test_collect_exports import SAFE, FakeVms, archive, run

pytestmark = pytest.mark.usefixtures("checker")

#: GNU tar's `--list` quoting (its default, `escape`), undone.
_ESCAPE = re.compile(rb"\\(\\|[abfnrtv]|[0-7]{3})")
_ESCAPES = {b"\\": b"\\", b"a": b"\a", b"b": b"\b", b"f": b"\f", b"n": b"\n", b"r": b"\r"}
_ESCAPES |= {b"t": b"\t", b"v": b"\v"}


def gnu_names(path: pathlib.Path) -> list[str]:
    """The names GNU tar lists, its quoting undone, a directory's trailing slash dropped."""
    listed = subprocess.run(  # noqa: S603
        ["/usr/bin/tar", "--list", "--gzip", f"--file={path}"],
        capture_output=True, check=True, env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"},
    ).stdout  # fmt: skip
    names = []
    for line in listed.split(b"\n")[:-1]:
        name = _ESCAPE.sub(lambda m: _ESCAPES.get(m[1]) or bytes([int(m[1], 8)]), line)
        names.append(name.rstrip(b"/").decode("utf-8", "surrogateescape"))
    return names


def safe(path: pathlib.Path) -> list[tarfile.TarInfo]:
    """The re-packed archive at `path`, as `archive_check.py`'s docstring claims it: only
    regular files and directories, normalized names, none a credential's or at the manifest's
    but the manifest, normalized modes, no owners, no PAX attributes; GNU tar lists the same
    names as tarfile, in the same order. Its members."""
    with tarfile.open(path, encoding="utf-8", errors="surrogateescape") as tar:
        members = tar.getmembers()
        assert tar.pax_headers == {}
    for m in members:
        assert m.type in (tarfile.REGTYPE, tarfile.DIRTYPE), (m.name, m.type)
        assert m.mode == (0o755 if m.isdir() else m.mode) and m.mode in (0o644, 0o755), m.name
        assert (m.uid, m.gid, m.uname, m.gname, m.pax_headers) == (0, 0, "", "", {}), m.name
        assert m.name == posixpath.normpath(m.name) and m.name != ".", m.name
        assert not m.name.startswith("/") and ".." not in m.name.split("/"), m.name
        assert not ac_check.credential(m.name), m.name
        top = ac_check.fold(m.name).split("/")[0]
        lists = [r for r in ac_check.RESERVED if top == ac_check.fold(r)]
        assert not lists or (m.name == lists[0] and m.isreg()), m.name
        windows_safe(m.name)
    assert gnu_names(path) == [m.name for m in members]
    return members


#: What Windows forbids in a name component, trims from one, or reads as a device (#383)
WINDOWS_FORBIDDEN = set('\\:*?"<>|') | {chr(c) for c in range(32)}
#: Windows' device names, written out here rather than taken from the code's own `_DEVICE`
#: (the review of #384), as Microsoft's "Naming Files, Paths, and Namespaces" lists them
DEVICES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
DEVICES |= {f"{kind}{digit}" for kind in ("COM", "LPT") for digit in "0123456789\u00b9\u00b2\u00b3"}


def device(part: str) -> bool:
    """Whether Windows reads `part` as a device: its name before any extension, trailing spaces
    dropped, is one; and, where Python has it (3.13), whether `ntpath.isreserved` says so."""
    reserved = getattr(ntpath, "isreserved", None)
    if reserved is not None and reserved(part) and not part.endswith((".", " ")):
        if not set(part) & WINDOWS_FORBIDDEN:
            return True
    return part.split(".")[0].rstrip(" ").upper() in DEVICES


def windows_safe(name: str) -> None:
    """Each component of `name` is one Windows takes as it is: no separator, drive or stream
    (`\\`, `:`), nothing it forbids or trims, no device name."""
    for part in name.split("/"):
        assert not set(part) & WINDOWS_FORBIDDEN, name
        assert not part.endswith((".", " ")) and not device(part), name
        assert len(part.encode("utf-8", "surrogateescape")) <= 255, name  # NAME_MAX


def repacked(tmp_path: pathlib.Path, data: bytes, **kwargs) -> tuple[str, str, pathlib.Path]:
    """`data` re-packed as `collect` does: (refusal or "", note, where the new archive is)."""
    path, new = tmp_path / "x.tar.gz", tmp_path / "new.tar.gz"
    path.write_bytes(data)
    with path.open("rb") as archive, new.open("w+b") as dest:
        problem, note = ce.repack_file(archive, dest, **kwargs)
    if not problem:
        safe(new)
    return problem, note, new


def check_archive(path: pathlib.Path, **kwargs) -> str:
    """Why the archive at `path` is refused, or "" once its re-pack is checked `safe`."""
    new = path.with_name(path.name + ".new")
    with path.open("rb") as archive, new.open("w+b") as dest:
        problem, _ = ce.repack_file(archive, dest, **kwargs)
    if not problem:
        safe(new)
    return problem


def check(tmp_path: pathlib.Path, data: bytes, **kwargs) -> str:
    return repacked(tmp_path, data, **kwargs)[0]


def frames_of(say) -> bytes:
    """What `say(Frames)` sends, as bytes."""
    sent: list[bytes] = []
    say(ac_check.Frames(sent.append))
    return b"".join(sent)


GOOD = archive(*SAFE)


def plain_tar(*names: str) -> bytes:
    return gzip.decompress(archive(*names))


def corrupt_header(name: str) -> bytes:
    block = bytearray(tarfile.TarInfo(name).tobuf())
    block[148:156] = b"0000000\0"  # a checksum no header has
    return bytes(block)


ONE = plain_tar("work/alice/a.md")  # a header, a data block, then zeros to 10240 bytes


def test_a_good_archive_passes(tmp_path):
    assert check(tmp_path, GOOD) == ""


@pytest.mark.parametrize(
    "data",
    [b"", b"not a tar", GOOD[:-4], GOOD[:-9], GOOD[: len(GOOD) // 2], GOOD[:-8] + b"\0" * 8],
    ids=["empty", "plain", "no length", "cut in the trailer", "half", "bad checksum"],
)
def test_anything_but_a_whole_gzip_stream_is_refused(tmp_path, data):
    assert check(tmp_path, data).startswith("not a whole gzip tar")


@pytest.mark.parametrize(
    "data, why",
    [
        # Codex round 1: tarfile reads a corrupt header after the first member as the end
        (ONE[:1024] + corrupt_header(".codex/auth.json") + ONE[1536:], "malformed"),
        (ONE[:1024] + corrupt_header("work/alice/b.md") + ONE[1536:], "malformed"),
        (ONE + b"x", "malformed"),  # anything but zeros after the marker
        (ONE + plain_tar(".codex/auth.json"), "malformed"),  # a second archive appended
        (ONE[:1024], "no end-of-archive marker"),
        (ONE[:1536], "no end-of-archive marker"),  # one zero block of the two
        (ONE[:600], "not a whole gzip tar"),  # a member's data cut short
    ],
    ids=["bad header", "harmless name", "junk", "appended", "no end", "half end", "data cut"],
)
def test_the_tars_own_end_is_checked(tmp_path, data, why):
    assert check(tmp_path, gzip.compress(data)).startswith(why)


def test_names_are_shown_safely(tmp_path):
    name = "work/" + "x" * 200 + "\n\x1b[31m/.codex/auth.json"
    got = check(tmp_path, archive(name))
    assert got.startswith("it names a credential file (") and got.endswith("): deleted")
    assert "\n" not in got and "\x1b" not in got and len(got) < 200


@pytest.mark.parametrize("fmt", [tarfile.PAX_FORMAT, tarfile.GNU_FORMAT, tarfile.USTAR_FORMAT])
def test_a_long_archive_in_each_format_passes(tmp_path, fmt):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz", format=fmt) as tar:
        for i in range(3000):  # each member's data skipped, never read
            info = tarfile.TarInfo(f"work/alice/{'d' * (i % 120)}/f{i}")
            info.size = i % 700
            tar.addfile(info, io.BytesIO(b"z" * info.size))
    assert check(tmp_path, data.getvalue()) == ""


def test_an_archive_made_by_gnu_tar_passes(tmp_path):
    """What `export.sh` makes: GNU tar's own end-of-archive padding."""
    tree = tmp_path / "work"
    (tree / "sub").mkdir(parents=True)
    (tree / "a.md").write_text("hi")
    (tree / "sub" / "b.bin").write_bytes(os.urandom(30000))
    path = tmp_path / "x.tar.gz"
    subprocess.run(["tar", "czf", str(path), "-C", str(tmp_path), "work"], check=True)  # noqa: S603, S607
    assert check_archive(path) == ""


# ------------------------------------------------------------------------------- the bombs


def header_bomb(path: pathlib.Path, kind: bytes, size: int) -> None:
    info = tarfile.TarInfo("././@LongLink")
    info.type, info.size = kind, size
    with gzip.open(path, "wb") as gz:
        gz.write(info.tobuf(format=tarfile.USTAR_FORMAT))
        for _ in range(size >> 20):
            gz.write(b"30 path=" + b"a" * 21 + b"\n" + b"\0" * ((1 << 20) - 30))
        gz.write(b"\0" * 10240)


def pax_record(key: str, value: bytes) -> bytes:
    body = f" {key}=".encode() + value + b"\n"
    n = len(body) + 1
    while len(str(n)) + len(body) != n:
        n = len(str(n)) + len(body)
    return str(n).encode() + body


def global_bomb(path: pathlib.Path, count: int = 500, size: int = 64 << 10) -> None:
    """Codex round 3: global PAX headers with distinct attributes, between empty files;
    tarfile keeps every global attribute for the rest of the archive."""
    with gzip.open(path, "wb") as gz:
        for i in range(count):
            record = pax_record(f"comment{i}", b"v" * (size - 32))
            g = tarfile.TarInfo("pax_global_header")
            g.type, g.size = tarfile.XGLTYPE, len(record)
            gz.write(g.tobuf(format=tarfile.USTAR_FORMAT) + record + b"\0" * (-len(record) % 512))
            gz.write(tarfile.TarInfo(f"work/empty{i}").tobuf(format=tarfile.USTAR_FORMAT))
        gz.write(b"\0" * 10240)


@pytest.mark.parametrize(
    "kind", [tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK]
)
def test_a_32_mib_header_is_refused(tmp_path, kind):
    """Codex round 2: tarfile reads such a header's data whole; 32 MiB compress to 32 KB."""
    path = tmp_path / "x.tar.gz"
    header_bomb(path, kind, 32 << 20)
    assert path.stat().st_size < 100_000
    got = check_archive(path)
    assert got == "over 64 KB of headers and metadata before one member: deleted"


@pytest.mark.parametrize("how", ["wrapped", "swallowed"])
def test_a_refusal_stands_whatever_tarfile_makes_of_it(tmp_path, monkeypatch, how):
    """Python 3.13.16's tarfile wraps an error of the stream in its own ReadError (CI, #360);
    another version might take it for the archive's end. The stream's reason stands."""
    real = tarfile.TarFile.next

    def next_(self):
        try:
            return real(self)
        except ac_check.Refused as exc:
            if how == "wrapped":
                raise tarfile.ReadError(str(exc)) from None
            return None

    monkeypatch.setattr(tarfile.TarFile, "next", next_)
    path = tmp_path / "x.tar.gz"
    header_bomb(path, tarfile.XHDTYPE, 4 << 20)
    with path.open("rb") as raw:
        got, _ = ac_check.scan(raw, ce.MAX_EXPANDED)
    assert got == "over 64 KB of headers and metadata before one member"


def test_consecutive_extension_headers_count_together(tmp_path):
    """Many PAX headers of 64 KiB before one member: one member's metadata, bounded as one."""
    path = tmp_path / "x.tar.gz"
    with gzip.open(path, "wb") as gz:
        for i in range(40):
            record = pax_record(f"comment{i}", b"v" * 65000)
            x = tarfile.TarInfo("pax_header")
            x.type, x.size = tarfile.XHDTYPE, len(record)
            gz.write(x.tobuf(format=tarfile.USTAR_FORMAT) + record + b"\0" * (-len(record) % 512))
        gz.write(tarfile.TarInfo("work/one").tobuf(format=tarfile.USTAR_FORMAT) + b"\0" * 10240)
    got = check_archive(path)
    assert got == "over 64 KB of headers and metadata before one member: deleted"


def test_global_pax_attributes_are_bounded_in_all(tmp_path):
    path = tmp_path / "x.tar.gz"
    global_bomb(path)  # 64 KiB each: refused at the first, for one member's metadata
    assert path.stat().st_size < 200_000
    assert check_archive(path).startswith("over 64 KB of headers and metadata")
    global_bomb(path, size=4096)  # 4 KiB each: under one member's bound, over the global one
    assert check_archive(path) == "over 1 KB of global PAX attributes: deleted"
    global_bomb(path, count=2, size=256)  # under 1 KiB of them: a tool may add some
    assert check_archive(path) == ""


def checker_cpu(checker: Checker, before: resource.struct_rusage) -> float:
    """The CPU the checker's processes took since `before`, a `RUSAGE_CHILDREN` reading: each
    reaped first, since a child's time counts only once it is waited for. Read before then, a
    bound saw a sixth of the 1 GiB re-pack's CPU, and almost none of the pax test's (#476)."""
    for child in checker.children:
        child.wait(timeout=60)
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    return (after.ru_utime + after.ru_stime) - (before.ru_utime + before.ru_stime)


def test_global_pax_attributes_cost_little_within_their_bound(tmp_path, checker):
    """The review of #360: tarfile copies the global attributes for every later member, so
    their number multiplies the work per member (1 MB of them cost 6.3 ms per member). At the
    1 KiB bound, 150 empty keys: 39 µs per member on the development machine, against 24 µs without.
    The checker takes 1.2 s of CPU here and 3.3 to 7.5 s on CI's runners for these 20,000 members;
    1 MB of attributes would take two minutes here. The bound was 10 s until the review of
    #476 found it read before the checker was reaped, when it saw almost none of its CPU."""
    path = tmp_path / "x.tar.gz"
    keys = b"".join(pax_record(f"k{i}", b"") for i in range(150))
    assert sum(len(f"k{i}") for i in range(150)) < ac_check.GLOBAL_META
    with gzip.open(path, "wb") as gz:
        g = tarfile.TarInfo("pax_global_header")
        g.type, g.size = tarfile.XGLTYPE, len(keys)
        gz.write(g.tobuf(format=tarfile.USTAR_FORMAT) + keys + b"\0" * (-len(keys) % 512))
        for i in range(20_000):
            gz.write(tarfile.TarInfo(f"work/e{i}").tobuf(format=tarfile.USTAR_FORMAT))
        gz.write(b"\0" * 10240)
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    assert check_archive(path) == ""
    cpu = checker_cpu(checker, before)
    assert cpu < 30, f"the checker took {cpu:.2f} s of CPU"


@pytest.mark.parametrize("bomb", ["pax", "global"])
def test_a_bomb_keeps_the_archive_collected_before(roster, tmp_path, bomb):
    out, _ = run(roster, tmp_path, FakeVms(), "alice")
    before = (out / "export-alice.tar.gz").read_bytes()
    path = tmp_path / "bomb.tar.gz"
    header_bomb(path, tarfile.XHDTYPE, 32 << 20) if bomb == "pax" else global_bomb(path)
    vms = FakeVms({"198.51.100.1": path.read_bytes()})
    _, results = run(roster, tmp_path, vms, "alice", limit=1 << 30)
    assert results[0].problems[0].startswith(("over 64 KB", "over 1 KB"))
    assert (out / "export-alice.tar.gz").read_bytes() == before
    assert sorted(p.name for p in out.iterdir()) == ["export-alice.tar.gz"]


@pytest.fixture
def roster(tmp_path):
    path = tmp_path / "roster.csv"
    path.write_text("slug,email,repo,ip,repo_status\nalice,a@example.org,alice,198.51.100.1,ok\n")
    return path


def test_the_check_decompresses_at_most_its_budget(tmp_path):
    """A member's data is skipped, never held; past the budget the archive is refused."""
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as tar:
        info = tarfile.TarInfo("work/alice/zeros.bin")
        info.size = 48 << 20
        tar.addfile(info, io.BytesIO(bytes(info.size)))
    assert check(tmp_path, data.getvalue()) == ""
    assert check(tmp_path, data.getvalue(), budget=16 << 20) == "it expands past 16 MB: deleted"


# -------------------------------------------------------------- the checker and its limits


def test_the_checker_runs_on_the_standard_library_alone():
    """As the unit runs it, `python -I -S -B`, from `/` with no environment: no directory of
    this tree and no site-packages on its path."""
    argv = [sys.executable, *SERVE[:-1], "selfcheck"]
    done = subprocess.run(argv, env={}, cwd="/", capture_output=True, text=True)  # noqa: S603
    assert (done.returncode, done.stdout) == (0, "archive_check: selfcheck ok\n"), done.stderr


def test_the_limits_are_hard_and_hold():
    """`limit()` before the archive is read: an allocation past `MEMORY` fails, no limit can be
    raised again, and nothing can be written."""
    code = (
        "import importlib.util, resource, sys\n"
        f"spec = importlib.util.spec_from_file_location('c', {str(ac_check.__file__)!r})\n"
        "c = importlib.util.module_from_spec(spec); spec.loader.exec_module(c); c.limit()\n"
        "print(resource.getrlimit(resource.RLIMIT_AS), resource.getrlimit(resource.RLIMIT_CPU),"
        " resource.getrlimit(resource.RLIMIT_FSIZE))\n"
        "try:\n    resource.setrlimit(resource.RLIMIT_AS, (1 << 40, 1 << 40))\n"
        "except ValueError:\n    print('raise refused')\n"
        "try:\n    bytearray(c.MEMORY)\nexcept MemoryError:\n    print('memory refused')\n"
    )
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-c", code], env={}, cwd="/", capture_output=True, text=True
    )
    memory, cpu = ac_check.MEMORY, ac_check.CPU_SECONDS
    assert done.stdout.splitlines() == [
        f"({memory}, {memory}) ({cpu}, {cpu}) (0, 0)",
        "raise refused",
        "memory refused",
    ]


@pytest.fixture
def fake(monkeypatch):
    """`fake(code)`: the checker's socket answered by `code`, which has the connection on fd 3."""
    started: list[Checker] = []

    def serve(code: str) -> Checker:
        started.append(Checker([sys.executable, "-c", code]))
        monkeypatch.setattr(ce, "CHECKER_SOCKET", started[-1].path)
        return started[-1]

    yield serve
    for served in started:
        served.close()


def says(answer: bytes) -> str:
    """A checker that answers `answer`, whatever it was asked."""
    return f"import os; os.write(3, {answer!r})"


#: An instance that never answers: past MaxConnections, or dead (the review of #384)
BUSY = "no answer at all: busy, at most 4 archives at once, or it failed; export again"
REFUSED = frames_of(lambda f: f.refuse("no"))
DATA = frames_of(lambda f: (f.write(b"abc"), f.end("")))  # three bytes, then their end
#: A frame of 2 MiB, whole, and an end that counts it: the client must refuse it as it comes,
#: never buffer it (the review of #383).
BIG_FRAME = "b'D' + (2 << 20).to_bytes(4, 'big') + bytes(2 << 20) + b'E\\0\\0\\0\\x082097152 '"
#: Eight frames of 1 MiB and their end: with no limit, the test would keep 8 MiB and fail at
#: once, never fill a disk (the review of #383: an endless writer did, to 337 GB).
PAST_THE_LIMIT = (
    "(b'D' + (1 << 20).to_bytes(4, 'big') + bytes(1 << 20)) * 8 + b'E\\0\\0\\0\\x088388608 '"
)


def writes(expression: str) -> str:
    """A checker that answers `expression`, built in it: argv can't hold MiBs."""
    return f"import socket; socket.socket(fileno=3).sendall({expression})"


@pytest.mark.parametrize(
    "code, why",
    [
        (says(b""), BUSY),
        ("import os; os.kill(os.getpid(), 9)", BUSY),
        ("bytearray(1 << 46)", BUSY),
        (says(DATA[:4]), "a malformed answer"),
        (says(b"ok\n"), "a malformed answer"),
        (says(DATA + b"D"), "a malformed answer"),
        (says(DATA + DATA), "more after its end"),
        (says(DATA.replace(b"3 ", b"4 ")), "its end doesn't count what came"),
        (says(b"D" + (2 << 20).to_bytes(4, "big")), "a malformed answer"),
        (writes(BIG_FRAME), "a malformed answer"),
        (says(b"R\0\0\0\x02\x1b["), "a malformed answer"),
        (says(b"R\0\0\0\0"), "a malformed answer"),
        (says(b"E\0\0\0\x02x "), "its end doesn't count what came"),
        (says(DATA[:8] + b"E\0\0\0\x013 "), "a malformed answer"),
        (writes(PAST_THE_LIMIT), "larger than 1 MB: not kept"),
    ],
    ids=[
        "nothing",
        "killed",
        "memory",
        "a part of a frame",
        "a line",
        "a part of a frame after the end",
        "two ends",
        "a wrong count",
        "an oversized frame",
        "an oversized frame, whole",
        "a control character",
        "an empty refusal",
        "no count",
        "a byte past its end",
        "past the limit",
    ],
)  # fmt: skip
def test_any_failure_of_the_checker_refuses_the_archive(tmp_path, fake, code, why):
    fake(code)
    got, note, _ = repacked(tmp_path, GOOD, limit=1 << 20)
    assert (got, note) == (f"the archive check failed ({why}): deleted", "")


def test_only_the_answer_counts(tmp_path, fake):
    """An instance's exit status reaches nobody: a whole answer is the answer."""
    fake(says(REFUSED) + "; raise SystemExit(3)")
    assert repacked(tmp_path, GOOD)[:2] == ("no: deleted", "")
    fake(says(DATA) + "; raise SystemExit(1)")
    with (tmp_path / "x.tar.gz").open("rb") as archive, (tmp_path / "new").open("w+b") as dest:
        assert ce.repack_file(archive, dest) == ("", "")
    assert (tmp_path / "new").read_bytes() == b"abc"


def test_a_hung_checker_is_given_up_on(tmp_path, fake, monkeypatch):
    monkeypatch.setattr(ce, "CHECK_TIMEOUT", 0.5)
    fake("import time; time.sleep(60)")
    started = time.monotonic()
    assert check(tmp_path, GOOD) == "the archive check failed (timed out after 0.5 s): deleted"
    assert time.monotonic() - started < 10


def test_no_checker_refuses_the_archive(tmp_path, monkeypatch):
    path = tmp_path / "none.sock"
    monkeypatch.setattr(ce, "CHECKER_SOCKET", str(path))
    assert check(tmp_path, GOOD) == (
        f"the archive check failed (no checker on {path}: FileNotFoundError): deleted"
    )


def test_a_checker_that_is_not_roots_is_not_asked(tmp_path, monkeypatch):
    """`zab-archive-check.socket` is systemd's: a socket at its path that some other user made
    (or the bot's own user, outside the tests) is not the sandbox."""
    monkeypatch.setattr(ce, "peer_uid", lambda sock: 4242)
    assert check(tmp_path, GOOD) == (
        f"the archive check failed ({ce.CHECKER_SOCKET} is not root's): deleted"
    )


#: The export the re-pack's CPU is measured against, in the same run (#485).
REFERENCE = 8 << 20
#: A pull request's re-pack may take this many times the CPU of a plain one (#590).
PLAIN = 3


def synthetic_export(path: pathlib.Path, total: int, size: int = 5000) -> int:
    """An export of `total` bytes of small source files, generated, as GNU tar lays it out."""
    block = (b"theorem two : 1 + 1 = 2 := by norm_num\n" * 200)[:size]
    pad = b"\0" * (-size % 512)
    count = total // size
    with path.open("wb") as f, gzip.GzipFile(fileobj=f, mode="wb", compresslevel=1) as gz:
        for i in range(count):
            info = tarfile.TarInfo(f"work/repo/src/dir{i // 1000}/file{i}.lean")
            info.size = size
            gz.write(info.tobuf(format=tarfile.PAX_FORMAT) + block + pad)
        gz.write(b"\0" * 10240)
    return count


def repack_cpu(checker: Checker, path: pathlib.Path, new: pathlib.Path) -> float:
    """The checker's CPU for re-packing the export at `path` into `new`, which it accepts."""
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    with path.open("rb") as archive, new.open("wb") as dest:
        assert ce.repack_file(archive, dest) == ("", "")
    return checker_cpu(checker, before)


def plain_repack_cpu(path: pathlib.Path, new: pathlib.Path) -> float:
    """This process's CPU for the checker's work done plainly, by tarfile and gzip alone: the
    export at `path` read through, then read again and written into `new` at the checker's
    level. A runner's speed moves it as it moves the checker's; no change to the checker does
    (#590)."""
    start = time.process_time()
    with tarfile.open(path, "r:gz") as tar:
        for _ in tar:
            pass
    with (
        tarfile.open(path, "r:gz") as src,
        gzip.open(new, "wb", compresslevel=ac_check.LEVEL) as gz,
        tarfile.open(fileobj=gz, mode="w|", format=tarfile.GNU_FORMAT) as dest,
    ):
        for member in src:
            dest.addfile(member, src.extractfile(member))
    return time.process_time() - start


@pytest.mark.timing
@pytest.mark.parametrize(
    ("total", "count", "samples"),
    [
        pytest.param(1 << 30, 214_748, 1, marks=pytest.mark.slow, id="1-GiB"),
        pytest.param(64 << 20, 13_421, 3, id="64-MiB"),
    ],
)
def test_an_export_of_small_files_re_packs_well_within_the_cpu_limit(
    tmp_path, checker, total, count, samples
):
    """On the development machine (Zen 5): 214,748 files, 1 GiB decompressed, both passes and the
    new archive written in about 14 s of the checker's CPU, against a limit of 600 s; on CI's
    runners 20 to 36 s alone, 35 to 87 s beside another xdist worker. A t4g.small is a few
    times slower than the development machine. The GiB's bound is a quarter of the limit: a
    tenth, until the review of #476 found it read before the checker was reaped and so saw a
    sixth of its CPU.

    Each re-pack is also timed against an 8-MiB export's, re-packed just before and just after
    it: its CPU per byte may not pass theirs. The checker's fixed cost (a process, 0.03 s here)
    keeps the ratio under the sizes': at 64 MiB 6.4 to 6.6 of 8 here and 6.5 to 6.9 on CI's
    runners, at 1 GiB 103 of 128 here and 102 to 111 on CI. A duplicate check against a list
    of the names seen, which fails the GiB here (198 s), makes it 10.6 to 11.9 at 64 MiB, and
    as a Python loop 12.9 to 16, and any quadratic term that would take the GiB past its bound
    here about 10. The best of `samples` counts, for the ratio and for the bound, which before
    #485 saw a single re-pack.

    A pull request re-packs 64 MiB. Its bound was the GiB's pro rata, 9.4 s, until a runner
    read 10.09 s (#590). That runner took 38.8 s for this test, against at most 12.3 s in the
    905 other `timing` jobs of Oct 19 to 23, and was as slow from the job's first test to its
    last (0.69 s for the first, against at most 0.27 s), so neither the best of three nor a
    second measurement would have passed it. The bound is relative now: `PLAIN` times the CPU
    of a plain re-pack (`plain_repack_cpu`), timed in turn, the best of three each. A runner's
    speed moves both; no change to the checker moves the plain one. Their ratio is 1.49 to
    1.62 here, under Python 3.11 to 3.13, alone or beside a busy thread on the same core
    (which raised both CPUs by 1.65 times). So a checker about 1.9 times slower per byte
    fails, on any runner where the ratio is what it is here: with three more scan passes
    (1.88 times) it read 2.98 here, with four (2.17 times) 3.44. The old bound let one about 3
    times slower pass on CI's slowest runners, and 7 times on its fastest (estimated from the
    test's durations). Neither sees a quadratic term, which the ratio against 8 MiB does.

    A push to main re-packs the GiB (`slow`, #473), the export's size: the bound against the
    limit, where it matters, and a ratio that a super-linear term too small to show in 13,421
    members raises in 214,748. CI runs this test in its `timing` job, alone
    (pyproject.toml): beside another xdist worker on the core's second thread, an 8-MiB
    re-pack's CPU fell by 1.7 times while a 64-MiB one's held, and the shipped re-pack read
    9.2 to 11.1."""
    path, new = tmp_path / "big.tar.gz", tmp_path / "new.tar.gz"
    small, small_new = tmp_path / "small.tar.gz", tmp_path / "small-new.tar.gz"
    whole = total == 1 << 30  # the GiB, against the limit; else against the plain re-pack
    assert synthetic_export(path, total) == count
    synthetic_export(small, REFERENCE)
    references = [repack_cpu(checker, small, small_new)]
    cpus: list[float] = []
    plain: list[float] = []
    for _ in range(samples):
        cpus.append(repack_cpu(checker, path, new))
        references.append(repack_cpu(checker, small, small_new))
        if not whole:
            plain.append(plain_repack_cpu(path, tmp_path / "plain.tar.gz"))
    sandwiches = zip(cpus, references[:-1], references[1:], strict=True)
    ratios = [cpu / ((before + after) / 2) for cpu, before, after in sandwiches]
    cpu = min(cpus)
    if whole:
        bound = ac_check.CPU_SECONDS / 4
        assert cpu < bound, f"the checker took {cpu:.2f} s of CPU, against {bound:.2f}"
    else:
        assert cpu < PLAIN * min(plain), f"the checker took {cpus} s of CPU, plainly {plain}"
    assert min(ratios) < total / REFERENCE, f"{ratios} against 8 MiB's {references}"
    assert len(safe(new)) == count


def test_the_credential_check_matches_export_sh():
    """The local check refuses at least what `export.sh` refuses (workshop/admin/README.md §7.2)."""
    text = (pathlib.Path(ce.__file__).with_name("export.sh")).read_text()
    assert "auth\\.json|\\.credentials\\.json|[^/]*\\.zuliprc" in text
    for name in ("auth.json", "a/auth.json", ".credentials.json", "x.zuliprc", "a/.zuliprc"):
        assert ac_check.CREDENTIAL.search(name), name
    for name in ("auth.json.txt", "work/zuliprc.md", ".config/zabx/a", "my-auth.json"):
        assert not ac_check.CREDENTIAL.search(name), name
    assert os.path.basename(ce.remote_archive("alice")) == ce.archive_name("alice")


# ------------------------------------------------------- the review of #360: parser agreement


def header(
    name: str, size: int = 0, kind: bytes = tarfile.REGTYPE, size_field: bytes = b""
) -> bytes:
    """A ustar header block, its size field replaced by `size_field` if given, its checksum
    recomputed so that it reads as a header."""
    info = tarfile.TarInfo(name)
    info.size, info.type = size, kind
    block = bytearray(info.tobuf(format=tarfile.USTAR_FORMAT))
    if size_field:
        block[124:136] = size_field
    block[148:156] = b" " * 8
    block[148:156] = b"%06o\0 " % sum(block)
    return bytes(block)


def pax_header(*records: bytes, kind: bytes = tarfile.XHDTYPE) -> bytes:
    data = b"".join(records)
    return header("././@PaxHeader", len(data), kind) + data + b"\0" * (-len(data) % 512)


def raw_record(key: bytes, value: bytes) -> bytes:
    body = b" " + key + b"=" + value + b"\n"
    n = len(body) + 1
    while len(str(n)) + len(body) != n:
        n = len(str(n)) + len(body)
    return str(n).encode() + body


BLOCK, END = b"\0" * 512, b"\0" * 10240
TOKEN = b"token" + b"\0" * 507  # the decoy credential's one data block
#: The reviewer's four archives (#360): each gzip-wrapped with a valid end-of-archive marker;
#: tarfile reads only harmless names in each, GNU tar 1.35 extracts `.codex/auth.json`.
CRAFTED = {
    # tarfile stops the size at the NUL (0) and reads the next block as a header, `work/safe`,
    # which covers the rest; GNU tar skips the NUL and reads 1024 bytes of data
    "a NUL before the size": header("work/x", size_field=b"\0" + b"0000002000" + b"\0")
    + header("work/safe", 1536)
    + BLOCK
    + header(".codex/auth.json", 5)
    + TOKEN
    + END,
    # Python reads `0o…` in base 8; GNU tar rejects the header and skips to the next one
    "0o in the size": header("work/x", size_field=b"0o000002000\0")
    + header(".codex/auth.json", 5)
    + TOKEN
    + END,
    # tarfile keeps the NUL, so the pattern's `$` doesn't match; GNU tar reads a C string
    "a NUL in a PAX path": pax_header(raw_record(b"path", b".codex/auth.json\0.txt"))
    + header("work/placeholder")
    + END,
    # tarfile takes the last record, `path`; GNU tar's GNU.sparse.name always wins
    "GNU.sparse.name before path": pax_header(
        raw_record(b"GNU.sparse.name", b".codex/auth.json"), raw_record(b"path", b"work/safe")
    )
    + header("work/placeholder", 5)
    + TOKEN
    + END,
}


@pytest.mark.parametrize("name", list(CRAFTED))
def test_the_reviewers_archives_are_refused(tmp_path, name):
    got = check(tmp_path, gzip.compress(CRAFTED[name]))
    assert got.endswith(": deleted") and not got.startswith("the archive check failed"), got


def test_each_crafted_archive_is_refused_by_the_strict_pass_with_its_reason(tmp_path):
    reasons = {}
    for name, data in CRAFTED.items():
        with io.BytesIO(gzip.compress(data)) as raw:
            reasons[name] = ac_check.scan(raw, ce.MAX_EXPANDED)[0]
    assert reasons == {
        "a NUL before the size": "a header's numbers are not in canonical form",
        "0o in the size": "a header's numbers are not in canonical form",
        "a NUL in a PAX path": "a NUL in a member's name (.codex/auth.json?.txt)",
        "GNU.sparse.name before path": "a sparse member, which export.sh never makes (work/safe)",
    }


ODD = [
    "work/plain.md",
    "work/sp ace.md",
    "work/tab\there",
    "work/nl\nhere",
    "work/back\\slash",
    "work/café.md",
    "work/ctl\x01x",
    "work/q\"uote'",
    "work/a?b??c",
    "work/del\x7fx",
    "work/" + "long" * 60 + ".md",
]


@pytest.mark.parametrize("fmt", [tarfile.PAX_FORMAT, tarfile.GNU_FORMAT])
def test_odd_but_honest_names_survive_the_re_pack(tmp_path, fmt):
    """Each name comes out as it went in, or renamed where Windows can't take it (#383), and
    GNU tar's `escape` quoting, undone, lists the new archive as tarfile reads it (`safe`)."""
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz", format=fmt) as tar:
        for name in ODD:
            tar.addfile(tarfile.TarInfo(name), io.BytesIO(b""))
        directory = tarfile.TarInfo("work/dir")
        directory.type = tarfile.DIRTYPE
        tar.addfile(directory)
    problem, _, new = repacked(tmp_path, data.getvalue())
    assert problem == ""
    expected = [ac_check.renamed(name) for name in ODD]
    assert expected[2:5] == ["work/tab%09here", "work/nl%0Ahere", "work/back%5Cslash"]
    names = [m.name for m in safe(new)]
    assert names == [*expected, "work/dir", "RENAMED.txt"]
    with tarfile.open(new) as tar:
        listed = tar.extractfile("RENAMED.txt").read().splitlines()  # type: ignore[union-attr]
    assert listed[3:] == [
        ac_check._escape(old) + b"\t" + new.encode()
        for old, new in zip(ODD, expected, strict=True)
        if old != new
    ]


# ------------------------------------------------- the review of #360: the child's own limits


def limits_of(pid: int) -> dict[str, tuple[str, str]]:
    lines = pathlib.Path(f"/proc/{pid}/limits").read_text().splitlines()[1:]
    return {line[:26].strip(): tuple(line[26:].split()[:2]) for line in lines}


def test_the_checker_runs_limited(tmp_path, checker):
    """Asked to re-pack an archive whose new one fills the socket, and not read from, the
    checker waits on its answer: its limits, read back from /proc then, are `limit()`'s. Without
    it this fails (the review of #360)."""
    path = tmp_path / "x.tar.gz"
    path.write_bytes(tar_of(file_("work/noise.bin", os.urandom(4 << 20))))
    asked, release = threading.Event(), threading.Event()
    results: list[tuple[str, str]] = []

    def room() -> bool:  # the first frame came: stop reading until the test is done
        asked.set()
        return release.wait(30)

    def repack() -> None:
        with path.open("rb") as archive, (tmp_path / "new").open("wb") as dest:
            results.append(ce.repack_file(archive, dest, room=room))

    thread = threading.Thread(target=repack)
    thread.start()
    try:
        assert asked.wait(30)
        limits = limits_of(checker.children[0].pid)
        memory, cpu = str(ac_check.MEMORY), str(ac_check.CPU_SECONDS)
        assert limits["Max address space"] == (memory, memory)
        assert limits["Max cpu time"] == (cpu, cpu)
        assert limits["Max file size"] == ("0", "0")
        assert limits["Max core file size"] == ("0", "0")
        assert limits["Max open files"] == (str(ac_check.OPEN_FILES),) * 2
    finally:
        release.set()
        thread.join(30)
    assert results == [("", "")]


def test_the_checker_limits_itself_before_it_takes_the_connection(monkeypatch):
    """`main` calls `limit()` before it touches the connection, let alone the archive."""
    calls: list[str] = []
    monkeypatch.setattr(ac_check, "limit", lambda: calls.append("limit"))

    def connection(env, pid):
        calls.append("connection")
        raise SystemExit("stop here")

    monkeypatch.setattr(ac_check, "connection", connection)
    with pytest.raises(SystemExit, match="stop here"):
        ac_check.main(["serve", "--peer", "zab-admin"])
    assert calls == ["limit", "connection"]


def test_the_checker_takes_only_systemds_connection():
    """Without `LISTEN_FDS=1` and its own pid in `LISTEN_PID`, there is no connection."""
    for env in ({}, {"LISTEN_FDS": "1", "LISTEN_PID": "1"}, {"LISTEN_FDS": "2", "LISTEN_PID": "7"}):
        with pytest.raises(SystemExit, match="no connection from systemd"):
            ac_check.connection(env, 7)


#: A checker that answers, as its refusal, what it was sent and whether it could truncate it.
SPY = """
import array, fcntl, os, socket
sock = socket.socket(fileno=3)
data, ancillary, flags, _ = sock.recvmsg(64, socket.CMSG_SPACE(8))
fds = array.array("i")
for level, kind, payload in ancillary:
    fds.frombytes(payload)
mode = fcntl.fcntl(fds[0], fcntl.F_GETFL) & os.O_ACCMODE
seen = [data.decode(), len(fds), mode, os.fstat(fds[0]).st_ino]
try:
    os.ftruncate(fds[0], 3)
    seen.append(0)
except OSError as exc:
    seen.append(exc.errno)
text = " ".join(map(str, seen)).encode().replace(b"\\n", b"|")
sock.sendall(b"R" + len(text).to_bytes(4, "big") + text)
"""


def test_the_checker_gets_one_line_and_the_archive_read_only(tmp_path, fake):
    """The request: `repack <budget>` and one descriptor, the very file collect_exports wrote,
    reopened read-only even when held read-write. The checker can't truncate it (the approval
    of #360), so the bytes it reads twice don't change."""
    fake(SPY)
    path = tmp_path / "x.tar.gz"
    path.write_bytes(GOOD)
    with open(path, "r+b") as written, (tmp_path / "new").open("wb") as dest:
        got, _ = ce.repack_file(written, dest, budget=1 << 30)
    line, fds, mode, inode, errno = got.removesuffix(": deleted").split(" ")[1:]
    assert got.startswith("repack ") and line == "1073741824|"
    assert (int(fds), int(mode), int(inode)) == (1, os.O_RDONLY, path.stat().st_ino)
    assert int(errno) in (9, 22) and path.read_bytes() == GOOD  # EBADF or EINVAL


def ask(path: str, data: bytes, fds: list[int]) -> bytes:
    """A raw request to the checker at `path`: its whole answer."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(30)
        sock.connect(path)
        ancillary = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", fds))] if fds else []
        try:
            sock.sendmsg([data], ancillary)
        except (BrokenPipeError, ConnectionResetError):
            pass  # it refused the peer and closed before reading: its answer says why (#387)
        answer = b""
        try:
            while chunk := sock.recv(1 << 16):
                answer += chunk
        except ConnectionResetError:
            pass  # it closed with the request unread
        return answer


def refusal(why: str) -> bytes:
    return frames_of(lambda f: f.refuse(why))


def test_the_checker_answers_only_root_and_its_peers(tmp_path):
    """The socket is root:zab-admin 0660; the checker also checks the caller (`SO_PEERCRED`):
    root and its `--peer` users only."""
    path = tmp_path / "x.tar.gz"
    path.write_bytes(GOOD)
    for peers in (["root"], ["no-such-user"], []):
        served = Checker([sys.executable, *SERVE, *(f"--peer={peer}" for peer in peers)])
        try:
            with path.open("rb") as archive:
                answer = ask(served.path, b"repack 1048576\n", [archive.fileno()])
        finally:
            served.close()
        assert answer == refusal("not a caller this checker answers"), peers
    served = Checker([sys.executable, *SERVE, "--peer", "root", "--peer", me()])
    try:
        with path.open("rb") as archive:
            assert ask(served.path, b"repack 1048576\n", [archive.fileno()])[:1] == b"D"
    finally:
        served.close()


NOT_A_REQUEST = "not a request: one line and one file"
NOT_A_FILE = "the archive didn't come as a file opened read-only"


@pytest.mark.parametrize(
    "line, files, why",
    [
        (b"repack 1048576\n", "none", NOT_A_REQUEST),
        (b"repack 1048576\n", "two", NOT_A_REQUEST),
        (b"repack 0\n", "one", NOT_A_REQUEST),
        (b"repack 1048576", "one", NOT_A_REQUEST),
        (b"repack -1\n", "one", NOT_A_REQUEST),
        (b"check 1048576\n", "one", NOT_A_REQUEST),
        (b"repack 1048576\n", "pipe", NOT_A_FILE),
        (b"repack 1048576\n", "writable", NOT_A_FILE),
    ],
    ids=["no file", "two files", "no budget", "no newline", "negative", "another verb", "a pipe",
         "writable"],
)  # fmt: skip
def test_the_checker_refuses_any_other_request(tmp_path, checker, line, files, why):
    path = tmp_path / "x.tar.gz"
    path.write_bytes(GOOD)
    reader, writer = os.pipe()
    try:
        with path.open("rb") as archive, path.open("r+b") as writable:
            fds = {
                "none": [],
                "one": [archive.fileno()],
                "two": [archive.fileno(), archive.fileno()],
                "pipe": [reader],
                "writable": [writable.fileno()],
            }[files]
            assert ask(checker.path, line, fds) == refusal(why)
    finally:
        os.close(reader)
        os.close(writer)


@pytest.mark.parametrize(
    "name",
    [
        "auth.json/",
        ".config//zab",
        "work/./.credentials.json",
        "work//x.zuliprc//",
        "a/b/../auth.json",
        # the review of #360 at 162c432: one path on a case-insensitive filesystem
        ".codex/AUTH.JSON",
        ".claude/.Credentials.Json",
        ".CONFIG/Zab/bot",
        "work/Alice.ZULIPRC",
    ],
)
def test_a_name_is_matched_as_it_extracts(tmp_path, name):
    """Codex on #360: a regular member `auth.json/` passed both passes, and GNU tar and tarfile
    extract it as `auth.json`. Names are matched as paths normalize them too, and casefolded,
    as a case-insensitive filesystem (a Mac's) extracts them."""
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz", format=tarfile.GNU_FORMAT) as tar:
        info = tarfile.TarInfo(name)
        info.size = 3
        tar.addfile(info, io.BytesIO(b"abc"))
    assert check(tmp_path, data.getvalue()).startswith("it names a credential file")
    for honest in ("work/auth.json.md", "work/.config/zabx", "work/zuliprc/notes"):
        assert not ac_check.credential(honest), honest


# ------------------------------------------------------- the approval of #360: three gaps closed


def linked(*members: tuple[str, bytes, str]) -> bytes:
    """A gzip tar of (name, type, link target) members, regular ones holding b"secret"."""
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz", format=tarfile.GNU_FORMAT) as tar:
        for name, kind, target in members:
            info = tarfile.TarInfo(name)
            info.type, info.linkname = kind, target
            body = b"secret" if kind == tarfile.REGTYPE else b""
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return data.getvalue()


def two_targets() -> bytes:
    """The re-review of #360 at 7d6801c: a hard link `h` whose target tarfile reads from a GNU
    `K` record (`a`) and GNU tar from a PAX `linkpath` (`c`, a symlink to `.config`); GNU tar
    then writes `h/zab/key` to `.config/zab/key`."""

    def member(name: str, kind: bytes, target: str = "", body: bytes = b"") -> bytes:
        info = tarfile.TarInfo(name)
        info.type, info.linkname, info.size = kind, target, len(body)
        info.mode = 0o755 if kind in (tarfile.DIRTYPE, tarfile.LNKTYPE) else 0o644
        return info.tobuf(format=tarfile.USTAR_FORMAT) + body + b"\0" * (-len(body) % 512)

    longlink = tarfile.TarInfo("././@LongLink")
    longlink.type, longlink.size = tarfile.GNUTYPE_LONGLINK, 2
    raw = [
        member(".config", tarfile.DIRTYPE),
        member("c", tarfile.SYMTYPE, ".config"),
        member("a", tarfile.REGTYPE, body=b"a"),
        longlink.tobuf(format=tarfile.GNU_FORMAT) + b"a\0" + b"\0" * 510,
        pax_header(pax_record("linkpath", b"c")),
        member("h", tarfile.LNKTYPE, "zz"),
        member("h/zab/key", tarfile.REGTYPE, body=b"secret"),
    ]
    return gzip.compress(b"".join(raw) + b"\0" * 10240)


DIRS = ((".codex", tarfile.DIRTYPE, ""), ("work", tarfile.DIRTYPE, ""))
THROUGH = {
    # the reviewer's: tarfile's extractall writes the secret to .codex/auth.json, any filter
    "a file over a symlink": linked(
        *DIRS,
        ("work/l", tarfile.SYMTYPE, "../.codex/auth.json"),
        ("work/l", tarfile.REGTYPE, ""),
    ),
    "a file under a symlink": linked(
        *DIRS, ("work/d", tarfile.SYMTYPE, "../.codex"), ("work/d/x", tarfile.REGTYPE, "")
    ),
    "a file over a hard link to a symlink": linked(
        *DIRS,
        ("work/l", tarfile.SYMTYPE, "../.codex/auth.json"),
        ("work/h", tarfile.LNKTYPE, "work/l"),
        ("work/h", tarfile.REGTYPE, ""),
    ),
    # Codex round 5 on #360: extraction resolves `..` through the symlink `work/l`
    "a .. after a symlink": linked(
        (".config", tarfile.DIRTYPE, ""),
        (".config/sub", tarfile.DIRTYPE, ""),
        ("work", tarfile.DIRTYPE, ""),
        ("work/l", tarfile.SYMTYPE, "../.config/sub"),
        ("work/l/../zab/key", tarfile.REGTYPE, ""),
    ),
    "a hard link through ..": linked(
        *DIRS, ("work/a", tarfile.REGTYPE, ""), ("work/h", tarfile.LNKTYPE, "work/x/../a")
    ),
    "the same, through ./": linked(
        *DIRS,
        ("./work/l", tarfile.SYMTYPE, "../.codex/x"),
        ("work//l/", tarfile.REGTYPE, ""),
    ),
    # the review of #360 at 162c432: tarfile's filters strip a leading /, normpath keeps it
    "an absolute symlink, then the file": linked(
        *DIRS,
        ("/work/l", tarfile.SYMTYPE, "../.codex/auth.json"),
        ("work/l", tarfile.REGTYPE, ""),
    ),
    "the symlink, then an absolute file": linked(
        *DIRS,
        ("work/l", tarfile.SYMTYPE, "../.codex/auth.json"),
        ("/work/l", tarfile.REGTYPE, ""),
    ),
    "a hard link to an absolute name": linked(
        *DIRS,
        ("work/l", tarfile.SYMTYPE, "../.codex/auth.json"),
        ("work/h", tarfile.LNKTYPE, "/work/l"),
        ("work/h", tarfile.REGTYPE, ""),
    ),
    # and on a case-insensitive filesystem, `work/L` is `work/l`
    "a file over a symlink, in another case": linked(
        *DIRS,
        ("work/L", tarfile.SYMTYPE, "../.codex/auth.json"),
        ("work/l", tarfile.REGTYPE, ""),
    ),
    "a file under a symlink, in another case": linked(
        *DIRS, ("Work/D", tarfile.SYMTYPE, "../.codex"), ("work/d/x", tarfile.REGTYPE, "")
    ),
    "a hard link to a symlink, in another case": linked(
        *DIRS,
        ("work/l", tarfile.SYMTYPE, "../.codex/auth.json"),
        ("work/h", tarfile.LNKTYPE, "WORK/L"),
        ("work/H", tarfile.REGTYPE, ""),
    ),
    "the same, in another normalization": linked(
        *DIRS,
        ("work/\u00e9", tarfile.SYMTYPE, "../.codex/auth.json"),
        ("work/e\u0301", tarfile.REGTYPE, ""),
    ),
    # Codex round 6 on #360: tarfile links `work/h` to the symlink `work/l`, through `work/a`
    "a hard link through a symlink": linked(
        (".config", tarfile.DIRTYPE, ""),
        ("work", tarfile.DIRTYPE, ""),
        ("work/a", tarfile.SYMTYPE, "."),
        ("work/l", tarfile.SYMTYPE, "../.config"),
        ("work/h", tarfile.LNKTYPE, "work/a/l"),
        ("work/h/zab/key", tarfile.REGTYPE, ""),
    ),
    "the same, in another case": linked(
        (".config", tarfile.DIRTYPE, ""),
        ("work", tarfile.DIRTYPE, ""),
        ("work/A", tarfile.SYMTYPE, "."),
        ("work/l", tarfile.SYMTYPE, "../.config"),
        ("work/h", tarfile.LNKTYPE, "WORK/a/L"),
        ("work/h/zab/key", tarfile.REGTYPE, ""),
    ),
    # the re-review of #360 at 7d6801c: nothing under a hard link, whatever target is read
    "a file under a hard link with two targets": two_targets(),
    "a file under a hard link": linked(
        *DIRS,
        ("work/a", tarfile.REGTYPE, ""),
        ("work/h", tarfile.LNKTYPE, "work/a"),
        ("work/h/x", tarfile.REGTYPE, ""),
    ),
}


@pytest.mark.parametrize("name", list(THROUGH))
def test_a_member_written_through_an_earlier_symlink_is_refused(tmp_path, name):
    got = check(tmp_path, THROUGH[name])
    reasons = (
        "a member written through an earlier symlink or hard link (",
        "a hard link to or through an earlier symlink or hard link (",
        "a `..` in a member's name (",
        "an absolute name or link target (",
    )
    assert got.startswith(reasons), got


@pytest.mark.parametrize(
    "name, shown",
    [
        ("an absolute symlink, then the file", "/work/l"),
        ("the symlink, then an absolute file", "/work/l"),
        ("a hard link to an absolute name", "work/h"),
    ],
)
def test_the_reviewers_absolute_names_are_refused_for_their_slash(tmp_path, name, shown):
    assert check(tmp_path, THROUGH[name]) == f"an absolute name or link target ({shown}): deleted"


@pytest.mark.parametrize(
    "name",
    [
        "a hard link through a symlink",
        "the same, in another case",
        "a file over a hard link to a symlink",
    ],
)
def test_a_hard_link_to_or_through_a_symlink_is_refused_at_the_link(tmp_path, name):
    """Codex round 6 on #360: the rule for names applies to hard link targets too."""
    assert check(tmp_path, THROUGH[name]) == (
        "a hard link to or through an earlier symlink or hard link (work/h): deleted"
    )


def test_a_hard_link_with_two_targets_is_refused_under_it(tmp_path):
    """The readers disagree on `h`'s target (tarfile `a`, GNU tar `c`), and both list the same
    names; the file under `h` is refused, whichever target either reads."""
    path = tmp_path / "x.tar.gz"
    path.write_bytes(two_targets())
    with tarfile.open(path) as tar:
        assert tar.getmember("h").linkname == "a"
    listing = subprocess.run(  # noqa: S603
        ["/usr/bin/tar", "--list", "--verbose", "--gzip", "--file", str(path)],
        capture_output=True,
        text=True,
        check=True,
        env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"},
    ).stdout
    assert any(line.endswith(" h link to c") for line in listing.splitlines()), listing
    assert check_archive(path) == (
        "a member written through an earlier symlink or hard link (h/zab/key): deleted"
    )


def test_honest_symlinks_and_hard_links_pass(tmp_path):
    """What GNU tar makes of a tree: links, each named once, nothing written through them."""
    data = linked(
        *DIRS,
        ("work/a.md", tarfile.REGTYPE, ""),
        ("work/latest", tarfile.SYMTYPE, "a.md"),
        ("work/same.md", tarfile.LNKTYPE, "work/a.md"),
        ("work/linkdir", tarfile.SYMTYPE, "."),
        ("work/b.md", tarfile.REGTYPE, ""),
    )
    assert check(tmp_path, data) == ""
    tree = tmp_path / "tree" / "work"
    tree.mkdir(parents=True)
    (tree / "a.md").write_text("a")
    (tree / "l").symlink_to("a.md")
    os.link(tree / "a.md", tree / "h.md")
    path = tmp_path / "gnu.tar.gz"
    subprocess.run(["tar", "czf", str(path), "-C", str(tree.parent), "work"], check=True)  # noqa: S603, S607
    assert check_archive(path) == ""


def test_honest_archives_of_an_absolute_path_pass(tmp_path):
    """GNU tar's `--create` and tarfile's `add` strip the leading `/` of each name and hard
    link target; a symlink keeps its absolute target, which is allowed. Names that differ
    only in case pass when no symlink is among them."""
    tree = tmp_path / "tree" / "work"
    tree.mkdir(parents=True)
    (tree / "a.md").write_text("a")
    (tree / "A.md").write_text("A")
    (tree / "l").symlink_to("a.md")
    (tree / "abs").symlink_to("/etc/hostname")
    os.link(tree / "a.md", tree / "h.md")
    gnu = tmp_path / "gnu.tar.gz"
    argv = ["tar", "--create", "--gzip", "--file", str(gnu), str(tree)]
    subprocess.run(argv, check=True, stderr=subprocess.DEVNULL)  # noqa: S603
    python = tmp_path / "python.tar.gz"
    with tarfile.open(python, "w:gz") as tar:
        tar.add(str(tree))
    for path in (gnu, python):
        with tarfile.open(path) as tar:
            members = tar.getmembers()
        assert not any(
            m.name.startswith("/") or m.linkname.startswith("/") for m in members if not m.issym()
        )
        assert any(m.islnk() for m in members)
        assert any(m.issym() and m.linkname == "/etc/hostname" for m in members)
        assert check_archive(path) == "", path.name


def time_t_overflow() -> bytes:
    """The reviewer's: a canonical base-256 mtime past `time_t`. tarfile reads it, and GNU tar
    lists the same name, then exits 2: only the exit check refuses it."""
    block = bytearray(tarfile.TarInfo("work/a").tobuf(format=tarfile.USTAR_FORMAT))
    block[136:148] = b"\x80" + b"\xff" * 11
    block[148:156] = b" " * 8
    block[148:156] = b"%06o\0 " % sum(block)
    return gzip.compress(bytes(block) + b"\0" * 10240)


def test_an_mtime_past_time_t_is_clamped(tmp_path):
    """The reviewer's archive, which GNU tar listed and then exited 2 on: the new archive's
    mtime is the largest octal one, which GNU tar lists cleanly (`safe`)."""
    problem, _, new = repacked(tmp_path, time_t_overflow())
    assert problem == ""
    assert [(m.name, m.mtime) for m in safe(new)] == [("work/a", ac_check.MTIME)]


def test_codex_round_5s_archive_is_refused_for_its_dotdot(tmp_path):
    got = check(tmp_path, THROUGH["a .. after a symlink"])
    assert got == "a `..` in a member's name (work/l/../zab/key): deleted"


# ------------------------------------------------------------------- #374: the re-pack itself


def tar_of(*members: tuple[str, bytes, str, bytes], fmt: int = tarfile.GNU_FORMAT) -> bytes:
    """A gzip tar of (name, type, link target, data) members, mode 0600, owned by `agent`."""
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz", format=fmt, errors="surrogateescape") as tar:
        for name, kind, target, body in members:
            info = tarfile.TarInfo(name)
            info.type, info.linkname, info.size = kind, target, len(body)
            info.mode, info.uid, info.uname, info.mtime = 0o600, 1001, "agent", 1_700_000_000
            tar.addfile(info, io.BytesIO(body))
    return data.getvalue()


def file_(name: str, body: bytes = b"x") -> tuple[str, bytes, str, bytes]:
    return (name, tarfile.REGTYPE, "", body)


def link(name: str, target: str) -> tuple[str, bytes, str, bytes]:
    return (name, tarfile.LNKTYPE, target, b"")


def contents(path: pathlib.Path) -> list[tuple[str, bytes | None]]:
    """Each member of the archive at `path`, `safe`: a file's data, or None for a directory."""
    safe(path)
    with tarfile.open(path, errors="surrogateescape") as tar:
        return [(m.name, tar.extractfile(m).read() if m.isreg() else None) for m in tar]  # type: ignore[union-attr]


def tree(root: pathlib.Path) -> dict[str, bytes | None]:
    """What an extraction made under `root`: only files and directories, or it fails."""
    found: dict[str, bytes | None] = {}
    for path in sorted(root.rglob("*")):
        assert not path.is_symlink() and (path.is_file() or path.is_dir()), path
        found[str(path.relative_to(root))] = path.read_bytes() if path.is_file() else None
    return found


def test_what_gnu_tar_makes_of_a_tree_comes_back_as_files_and_directories(tmp_path):
    """A tree with every kind of member `export.sh` meets, archived by GNU tar as it does:
    directories and files keep their names and data; the hard link is a copy; the symlinks are
    listed in the manifest, escaped; the FIFO is left out; modes and owners are normalized.
    GNU tar and tarfile each extract the new archive into exactly those files."""
    work = tmp_path / "home" / "work"
    (work / "sub").mkdir(parents=True)
    (work / "a.md").write_bytes(b"alpha")
    (work / "run.sh").write_bytes(b"#!/bin/sh\n")
    (work / "run.sh").chmod(0o700)
    (work / "sub" / "b.bin").write_bytes(os.urandom(70000))
    os.link(work / "a.md", work / "sub" / "same.md")
    (work / "latest").symlink_to("a.md")
    (work / "tab\there").symlink_to("/etc/host\nname")
    os.mkfifo(work / "pipe")
    source = tmp_path / "gnu.tar.gz"
    argv = ["tar", "czf", str(source), "-C", str(work.parent), "work"]
    subprocess.run(argv, check=True)  # noqa: S603
    problem, note, new = repacked(tmp_path, source.read_bytes())
    assert (problem, note) == (
        "",
        "2 symlink(s) listed in SYMLINKS.txt; 1 hard link(s) copied; 1 other member(s) left out",
    )
    with tarfile.open(new) as tar:
        modes = {m.name: m.mode for m in tar.getmembers()}
    assert modes["work/run.sh"] == modes["work"] == modes["work/sub"] == 0o755
    assert modes["work/a.md"] == modes["work/sub/same.md"] == modes["work/sub/b.bin"] == 0o644
    want = {
        "work": None,
        "work/a.md": b"alpha",
        "work/run.sh": b"#!/bin/sh\n",
        "work/sub": None,
        "work/sub/b.bin": (work / "sub" / "b.bin").read_bytes(),
        "work/sub/same.md": b"alpha",
    }
    got = dict(contents(new))
    manifest = got.pop("SYMLINKS.txt") or b""
    assert manifest.startswith(ac_check.MANIFEST_HEAD)
    assert sorted(manifest[len(ac_check.MANIFEST_HEAD) :].splitlines()) == [
        b"work/latest\ta.md",
        b"work/tab\\011here\t/etc/host\\012name",
    ]  # in the order GNU tar met them
    assert got == want
    want["SYMLINKS.txt"] = manifest
    by_gnu, by_tarfile = tmp_path / "gnu", tmp_path / "tarfile"
    by_gnu.mkdir()
    subprocess.run(["tar", "xzf", str(new), "-C", str(by_gnu)], check=True)  # noqa: S603, S607
    trusted = {"filter": "fully_trusted"} if hasattr(tarfile, "fully_trusted_filter") else {}
    with tarfile.open(new) as tar:
        tar.extractall(by_tarfile, **trusted)  # noqa: S202 - the claim is about this
    assert tree(by_gnu) == tree(by_tarfile) == want


def test_hard_links_are_copies_in_any_order(tmp_path):
    """Each link is a copy of the file its target names when it comes, read again from the
    archive: forward, backward, and after the name is written again. (A link to a link is
    refused: #360's rule puts nothing at or under an earlier link.)"""
    data = tar_of(
        file_("work/a", b"A" * 3000),
        file_("work/b", b"B" * 5000),
        link("work/to-b", "work/b"),
        link("work/to-a", "./work/a"),
        file_("work/a", b"newer A"),
        link("work/to-new-a", "work/a"),
        link("work/to-b-again", "work//b"),
    )
    problem, note, new = repacked(tmp_path, data)
    assert (problem, note) == ("", "4 hard link(s) copied")
    assert contents(new) == [
        ("work/a", b"A" * 3000),
        ("work/b", b"B" * 5000),
        ("work/to-b", b"B" * 5000),
        ("work/to-a", b"A" * 3000),
        ("work/a", b"newer A"),
        ("work/to-new-a", b"newer A"),
        ("work/to-b-again", b"B" * 5000),
    ]
    chain = tar_of(file_("work/b"), link("work/l", "work/b"), link("work/m", "work/l"))
    got = check(tmp_path, chain)
    assert got == "a hard link to or through an earlier symlink or hard link (work/m): deleted"


DIRECTORY = ("work/d", tarfile.DIRTYPE, "", b"")


@pytest.mark.parametrize(
    "members, shown",
    [
        ((link("work/l", "work/nothing"),), "work/l"),
        ((link("work/early", "work/late"), file_("work/late")), "work/early"),
        ((file_("work/d"), DIRECTORY, link("work/y", "work/d")), "work/y"),
        ((DIRECTORY, link("work/y", "work/d")), "work/y"),
    ],
    ids=["no such file", "a later file", "a file, then a directory", "a directory"],
)
def test_a_hard_link_to_no_earlier_file_is_refused(tmp_path, members, shown):
    assert check(tmp_path, tar_of(*members)) == f"a hard link to no earlier file ({shown}): deleted"


def test_the_copies_are_bounded(tmp_path):
    """A hard link costs the archive's bytes again: the new archive is bounded by the budget,
    and so is what the copies decompress, rewinds included (a copy before the last one starts
    the second reader again)."""
    big = file_("work/big", bytes(8 << 20))
    data = tar_of(big, *(link(f"work/l{i}", "work/big") for i in range(3)))
    got = check(tmp_path, data, budget=20 << 20)
    assert got == "the re-packed archive expands past 20 MB: deleted"
    to_and_fro = [link(f"work/l{i}", "work/s" if i % 2 else "work/t") for i in range(16)]
    data = tar_of(file_("work/t"), file_("work/pad", bytes(10 << 20)), file_("work/s"), *to_and_fro)
    assert (
        check(tmp_path, data, budget=64 << 20) == "its hard links take over 64 MB to copy: deleted"
    )
    assert check(tmp_path, data, budget=256 << 20) == ""


@pytest.mark.parametrize(
    "name",
    [
        *("SYMLINKS.txt", "./SYMLINKS.txt", "symlinks.TXT", "SYMLINKS.txt/x", "Symlinks.txt/"),
        *("RENAMED.txt", "renamed.TXT/x"),
    ],
)
def test_no_member_takes_a_lists_name(tmp_path, name):
    got = check(tmp_path, tar_of(file_(name)))
    which = "RENAMED.txt" if "renamed" in name.lower() else "SYMLINKS.txt"
    assert got == f"a member at {which}, a list's name ({name}): deleted"
    assert check(tmp_path, tar_of(file_(f"work/{name}"))) == ""


def test_the_top_directory_is_left_out_and_nothing_else_is_named_dot(tmp_path):
    """GNU tar's `--create .` names the top `./`: the new archive leaves it out."""
    top = ("./", tarfile.DIRTYPE, "", b"")
    problem, _, new = repacked(tmp_path, tar_of(top, file_("./work/a")))
    assert problem == "" and contents(new) == [("work/a", b"x")]
    got = check(tmp_path, tar_of(file_("./")))
    assert got == "a member named . that is not a directory (./): deleted"


def test_names_that_are_not_utf_8_come_back_as_they_were(tmp_path):
    name = "work/caf\udce9.md"  # a Latin-1 é, as tarfile reads it
    symlink = (f"{name}.l", tarfile.SYMTYPE, name, b"")
    problem, _, new = repacked(tmp_path, tar_of(file_(name), symlink))
    assert problem == ""
    (first, _), (manifest, text) = contents(new)
    assert (first, manifest) == (name, "SYMLINKS.txt")
    assert text is not None and text.endswith(b"work/caf\xe9.md.l\twork/caf\xe9.md\n")


def answer_of(data: bytes, budget: int = 1 << 30) -> tuple[int, bytes]:
    """`answer` in this process, on `data`: its code and what it sent."""
    sent: list[bytes] = []
    code = ac_check.answer(lambda: io.BytesIO(data), budget, ac_check.Frames(sent.append))
    return code, b"".join(sent)


def test_hard_links_and_symlinks_are_bounded_in_number_and_size(monkeypatch):
    monkeypatch.setattr(ac_check, "MAX_LINKS", 3)
    links = [link(f"work/l{i}", "work/a") for i in range(4)]
    code, sent = answer_of(tar_of(file_("work/a"), *links[:3]))
    assert code == 0 and sent[:1] == b"D"
    refused = frames_of(lambda f: f.refuse("over 3 hard links"))
    assert answer_of(tar_of(file_("work/a"), *links)) == (1, refused)
    monkeypatch.setattr(ac_check, "MANIFEST_BYTES", 2 << 20)
    many = [(f"work/s{i}", tarfile.SYMTYPE, "t" * 4000, b"") for i in range(600)]
    refused = frames_of(lambda f: f.refuse("over 2 MB of symlink and renamed names"))
    assert answer_of(tar_of(*many, fmt=tarfile.PAX_FORMAT)) == (1, refused)


def test_the_answer_comes_in_frames_of_at_most_a_mib():
    code, sent = answer_of(tar_of(file_("work/noise", os.urandom(3 << 20))))
    frames, at = [], 0
    while at < len(sent):
        frames.append((sent[at : at + 1], int.from_bytes(sent[at + 1 : at + 5], "big")))
        at += 5 + frames[-1][1]
    assert code == 0 and [kind for kind, _ in frames] == [b"D"] * (len(frames) - 1) + [b"E"]
    assert all(0 < size <= ac_check.CHUNK for _, size in frames[:-1])
    end = b"%d " % sum(size for _, size in frames[:-1])
    assert sent.endswith(b"E" + len(end).to_bytes(4, "big") + end)


@pytest.mark.parametrize("size", [(1 << 20) - 9000, 3 << 20])
def test_a_refusal_in_the_second_pass_is_the_last_word(tmp_path, size):
    """Refused after some of the new archive was sent, the child says so last: what its
    writers still hold when it exits never follows the refusal."""
    data = tar_of(file_("work/noise", os.urandom(size)), link("work/l", "work/none"))
    assert check(tmp_path, data) == "a hard link to no earlier file (work/l): deleted"


# ------------------------------------------------------- the review of #383: Windows, and bounds

#: The reviewer's names: on Windows, `extractall()` puts the first two outside the directory,
#: and the other three at `.codex\auth.json`.
WINDOWS_NAMES = {
    "..\\..\\evil": "..%5C..%5Cevil",
    "C:\\Users\\x\\.codex\\auth.json": "C%3A%5CUsers%5Cx%5C.codex%5Cauth.json",
    ".codex\\auth.json": ".codex%5Cauth.json",
    ".codex/auth.json::$DATA": ".codex/auth.json%3A%3A$DATA",
    ".codex/auth.json.": ".codex/auth.json%2E",
    "work/CON": "work/%43ON",
    "work/nul.txt": "work/%6Eul.txt",
    "work/Com1.tar.gz": "work/%43om1.tar.gz",
    "work/con .txt": "work/%63on .txt",
    "work/lpt\u00b9": "work/%6Cpt\u00b9",
    "work/CONIN$": "work/%43ONIN$",
    "work/conout$.log": "work/%63onout$.log",
    "work/trailing ": "work/trailing%20",
    "work/dots../x": "work/dots%2E%2E/x",
    "work/100%.md": "work/100%25.md",
}


def test_names_windows_cant_take_are_renamed_and_listed(tmp_path):
    """#383: a component Windows can't take as it is gets `%XX` for what it can't take, and
    `RENAMED.txt` lists each such member. Every name is then one Windows takes (`safe`)."""
    data = tar_of(*(file_(name, name.encode()) for name in WINDOWS_NAMES))
    problem, note, new = repacked(tmp_path, data)
    assert (problem, note) == ("", f"{len(WINDOWS_NAMES)} renamed for Windows, in RENAMED.txt")
    got = dict(contents(new))
    for old, renamed in WINDOWS_NAMES.items():
        assert got[renamed] == old.encode()  # the data under the new name
    lines = got["RENAMED.txt"].splitlines()  # type: ignore[union-attr]
    assert lines[:3] == ac_check.RENAMES_HEAD.splitlines()
    assert lines[3:] == [ac_check._escape(o) + b"\t" + n.encode() for o, n in WINDOWS_NAMES.items()]


def test_renamed_names_never_collide(tmp_path):
    """`%` is renamed too, so a renamed name is no other member's, renamed or not, even
    folded: `a:b`, `a?b`, `a_b`, `a%3Ab` and `a%3Fb` stay five files. (Names that differ only
    in case are one on a case-insensitive filesystem, renamed or not.)"""
    names = ["work/a:b", "work/a?b", "work/a_b", "work/a%3Ab", "work/a%3Fb"]
    problem, _, new = repacked(tmp_path, tar_of(*(file_(n, n.encode()) for n in names)))
    assert problem == ""
    files = [(name, body) for name, body in contents(new) if name != "RENAMED.txt"]
    assert [body for _, body in files] == [n.encode() for n in names]
    assert len({ac_check.fold(name) for name, _ in files}) == len(names)


def test_a_name_that_renames_to_a_credentials_is_refused(monkeypatch, tmp_path):
    """A renamed name is checked as a credential's too (none can be one: `%` never forms a
    credential's name; this makes sure)."""
    monkeypatch.setattr(ac_check, "renamed", lambda name: "x/auth.json" if "?" in name else name)
    code, sent = answer_of(tar_of(file_("work/a?b")))
    assert (code, sent) == (1, refusal("it names a credential file once renamed (work/a?b)"))


def test_a_name_of_over_256_components_is_refused_at_once():
    """The review of #383: `_through` costs the square of a name's components; ten names of
    30,000, gzipped to 1.3 KB, took 37.9 s of CPU."""
    deep = "a/" * 256 + "x"
    started = time.process_time()
    code, sent = answer_of(tar_of(*(file_(f"{deep}{i}") for i in range(10))))
    assert code == 1 and sent[5:].decode().startswith("a name of over 256 components (")
    assert time.process_time() - started < 5
    assert answer_of(tar_of(file_("a/" * 255 + "x")))[0] == 0
    assert answer_of(tar_of(file_("work/x"), link("work/l", "./" * 256 + "work/x")))[0] == 1


def test_every_refusal_is_framed(monkeypatch):
    """The review of #383: the lists added after the second pass, a `MemoryError`, or any other
    failure, end in a refusal frame, never a traceback."""
    control = [(f"work/{i:03}" + "\x01" * 1300, tarfile.SYMTYPE, "t", b"") for i in range(500)]
    names = [file_("work/x"), *control]
    code, sent = answer_of(tar_of(*names, fmt=tarfile.PAX_FORMAT), budget=2 << 20)
    assert code == 1 and sent.endswith(refusal("the re-packed archive expands past 2 MB"))
    for failure, why in (
        (MemoryError, "it needs over 256 MB of memory to check"),
        (ValueError, "the re-pack failed (ValueError)"),
    ):

        def fail(raw, budget, failure=failure):
            raise failure()

        monkeypatch.setattr(ac_check, "scan", fail)
        assert answer_of(GOOD) == (1, refusal(why))


def test_the_hard_link_table_is_bounded_before_it_grows(monkeypatch):
    """The review of #383: 100,000 hard links with 30 KB targets ran pass 1 out of memory. Their
    targets' bytes are bounded as they come."""
    monkeypatch.setattr(ac_check, "LINK_BYTES", 1 << 20)
    target = "work/" + "t" * 30_000
    links = [link(f"work/l{i}", target) for i in range(40)]
    code, sent = answer_of(tar_of(file_(target), *links, fmt=tarfile.PAX_FORMAT))
    assert (code, sent) == (1, refusal("over 1 MB of hard link targets"))


def test_a_component_too_long_once_renamed_is_cut_short_with_a_hash(tmp_path):
    """Codex on #383: 254 `a` and a `%` would be 257 bytes once renamed, which Linux can't
    extract. It is cut short at a character and ends with `%~` and a hash of the original; so
    is a long multibyte name, never split mid-character. A 255-byte name Windows takes stays."""
    names = ["work/" + "a" * 254 + "%", "work/" + "a" * 254 + "?", "work/" + "\u00e9" * 127 + ":"]
    names.append("work/" + "b" * 255)
    problem, note, new = repacked(tmp_path, tar_of(*(file_(n, n.encode()) for n in names)))
    assert (problem, note) == ("", "3 renamed for Windows, in RENAMED.txt")
    got = contents(new)  # safe(): every component at most 255 bytes
    files = {name: body for name, body in got if name != "RENAMED.txt"}
    assert list(files.values()) == [n.encode() for n in names]
    long = [name for name in files if "%~" in name]
    assert len(long) == 3 and all(len(n.split("/")[1].encode()) <= 255 for n in long)
    assert names[3] in files  # unchanged


# ------------------------------------------------------------------ the review of #384: mutants


class FakeConnection:
    """The connection `serve` gets, with the caller's credentials as given: (pid, uid, gid)."""

    def __init__(self, creds: tuple[int, int, int]) -> None:
        self.creds, self.sent = creds, b""

    def getsockopt(self, level: int, option: int, size: int) -> bytes:
        assert (level, option) == (socket.SOL_SOCKET, socket.SO_PEERCRED)
        return struct.pack("3i", *self.creds)

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def settimeout(self, timeout: float | None) -> None:
        pass

    def recvmsg(self, *args: int) -> tuple:
        raise TimeoutError

    def close(self) -> None:
        pass


@pytest.mark.parametrize(
    "creds, why",
    [
        ((7, 1234, 5678), "no request (TimeoutError)"),  # the uid is the peer's: past the check
        ((7, 5678, 1234), "not a caller this checker answers"),  # its gid alone is not
    ],
)
def test_the_checker_reads_the_callers_uid_not_its_gid(monkeypatch, creds, why):
    """The review of #384: on the development machine a user's uid and gid are equal, so only
    credentials that differ tell them apart."""
    fake = FakeConnection(creds)
    monkeypatch.setattr(ac_check, "connection", lambda env, pid: fake)
    monkeypatch.setattr(ac_check.pwd, "getpwnam", lambda name: type("U", (), {"pw_uid": 1234}))
    assert ac_check.serve({}, 7, ["zab-admin"]) == 1
    assert fake.sent == refusal(why)


def test_collect_exports_reads_the_checkers_uid_not_its_gid():
    assert ce.peer_uid(FakeConnection((1, 0, 4242))) == 0  # type: ignore[arg-type]
    assert ce.peer_uid(FakeConnection((1, 4242, 0))) == 4242  # type: ignore[arg-type]


def test_a_slow_answer_is_given_up_on_as_a_whole(tmp_path, fake, monkeypatch):
    """The review of #384: the timeout bounds the whole answer, not each read. A checker that
    sends a byte every 0.15 s, each well within it, is given up on after 1 s in all."""
    monkeypatch.setattr(ce, "CHECK_TIMEOUT", 1.0)
    fake(
        f"import os, time\nfor b in {DATA!r}:\n    os.write(3, bytes([b]))\n    time.sleep(0.15)\n"
    )
    started = time.monotonic()
    assert check(tmp_path, GOOD) == "the archive check failed (timed out after 1 s): deleted"
    assert time.monotonic() - started < 2.5
