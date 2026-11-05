"""A19's harness (`harness/sandbox.py`; docs/ADMIN.md §7; #374, #356), offline.

The client script and the corpus against the checker as the archive tests serve it (`Checker`:
systemd's `Accept=yes`, emulated), each judge given the product's own answers and then wrong
ones, and the copy of the units. Where this user's systemd manager can build the sandbox (the
condition of `test_adminserver_units.py`'s sandbox test), the copies run for real: the checker
under a held connection (`INSPECT`, and the journal's instances), and the reviewer's probe as
an `ExecStart=` word. What only the rig has, systemd as PID 1 with `DynamicUser`, is A19's.
"""

from __future__ import annotations

import ast
import base64
import io
import json
import os
import pathlib
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import time

import pytest

from e2e.admin.harness import rig as rigs
from e2e.admin.harness import sandbox as sb
from e2e.admin.harness.server import Server
from e2e.harness.config import CHECKOUT
from e2e.harness.vm import Result
from workshop.checker import SERVE, Checker, instance_slice, me

ADMIN_DIR = CHECKOUT / "workshop" / "admin"
UNITS = CHECKOUT / "workshop" / "adminserver"
TEMPLATE, SOCKET_UNIT = UNITS / "zab-archive-check@.service", UNITS / "zab-archive-check.socket"
CASES = {case.name: case for case in sb.cases()}
HONEST = CASES["a symlink and a hard link"]
NAME_OF = lambda case: case.name  # noqa: E731
ABSOLUTE = CASES["an absolute name"]


def client(data: bytes, path: str) -> dict:
    """`CLIENT`, run here as it runs on the server, against the socket at `path`."""
    done = subprocess.run(  # noqa: S603 - an argument list
        [sys.executable, "-I", "-c", sb.CLIENT, str(ADMIN_DIR), path],
        input=base64.b64encode(data).decode(), capture_output=True, text=True, timeout=120,
    )  # fmt: skip
    return sb.answer(Result(done.returncode, done.stdout, done.stderr), "the client")


@pytest.fixture
def served():
    checker = Checker()
    yield checker
    checker.close()


def test_the_archives_are_the_corpus_and_the_peers():
    assert list(CASES) == [
        "a symlink and a hard link", "names Windows can't take", f"a name of {sb.DEEP} components",
        "an absolute name",
        "a .. after a symlink", "a NUL before the size", f"a name of {sb.DEEP + 1} components",
        "zab-waker", "zab-waker in group zab-admin",
    ]  # fmt: skip
    windows = CASES["names Windows can't take"]
    renamed = dict(windows.members)
    assert renamed["work/%43ONIN$"] == b"work/CONIN$"  # #384's console devices
    assert renamed["work/%63onout$.log"] == b"work/conout$.log"
    assert len(renamed) == len(sb.corpus().WINDOWS_NAMES) + 1  # and RENAMED.txt
    for depth in (sb.DEEP, sb.DEEP + 1):  # the deepest name taken, and one deeper
        with tarfile.open(fileobj=io.BytesIO(CASES[f"a name of {depth} components"].data)) as tar:
            assert [len(m.name.split("/")) for m in tar] == [depth]
    peers = [(c.user, c.group, c.connects) for c in CASES.values() if c.user != sb.ADMIN]
    assert peers == [("zab-waker", None, False), ("zab-waker", "zab-admin", True)]
    assert all(c.problem for c in CASES.values() if not c.members)


@pytest.mark.parametrize("case", [c for c in CASES.values() if c.user == sb.ADMIN], ids=NAME_OF)
def test_each_archive_is_answered_as_the_case_says(case, served, tmp_path):
    """`CLIENT`, through `collect_exports.repack_file`, on the checker of this checkout."""
    got = client(case.data, served.path)
    assert sb.judged(case, got, tmp_path, served.path) == []
    assert got["uid"] == os.getuid() and got["pid"] > 0


def test_a_peer_it_doesnt_answer_is_refused(tmp_path):
    """`zab-waker` in `zab-admin`'s group reaches the checker, which answers only root and
    `zab-admin`: here, a checker that answers only root."""
    checker = Checker([sys.executable, *SERVE, "--peer", "root"])
    try:
        got = client(HONEST.data, checker.path)
    finally:
        checker.close()
    assert sb.judged(CASES["zab-waker in group zab-admin"], got, tmp_path, checker.path) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root connects to any socket")
def test_a_user_outside_the_sockets_group_cant_connect(served, tmp_path):
    os.chmod(served.path, 0)  # as root:zab-admin 0660 is to zab-waker
    try:
        got = client(HONEST.data, served.path)
    finally:
        os.chmod(served.path, 0o600)  # for its close
    assert sb.judged(CASES["zab-waker"], got, tmp_path, served.path) == []
    assert got["stored"] == ""


def test_the_judge_finds_what_is_wrong(served, tmp_path):
    good = client(HONEST.data, served.path)
    other = client(sb.corpus().tar_of(sb.corpus().file_("work/b.md")), served.path)
    same_names = client(
        sb.corpus().tar_of(
            ("work", tarfile.DIRTYPE, "", b""),
            sb.corpus().file_("work/a.md", b"omega"),
            sb.corpus().link("work/same.md", "work/a.md"),
            ("work/latest", tarfile.SYMTYPE, "a.md", b""),
        ),
        served.path,
    )
    raw = base64.b64encode(HONEST.data).decode()  # the participant's own: a symlink in it
    wrong = {
        "note": {**good, "note": "1 symlink(s) listed in SYMLINKS.txt"},
        "not files alone": {**good, "stored": raw},
        "other names": {**good, "stored": other["stored"]},
        "other data": {**good, "stored": same_names["stored"]},
        "refused": {**good, "problem": "no: deleted", "stored": ""},
    }
    found = {what: sb.judged(HONEST, got, tmp_path, served.path) for what, got in wrong.items()}
    assert "its note is" in found["note"][0]
    assert "not files and directories alone" in found["not files alone"][0]
    assert "the re-pack holds ['work/b.md'], not ['work', 'work/a.md'" in found["other names"][0]
    assert found["other data"] == [
        f"{HONEST.name}: the re-pack's ['work/a.md', 'work/same.md'] hold other data"
    ]
    assert found["refused"] == [f"{HONEST.name}: 'no: deleted', not 'accepted'"]
    refusal = client(ABSOLUTE.data, served.path)
    assert sb.judged(ABSOLUTE, refusal, tmp_path, served.path) == []
    assert sb.judged(ABSOLUTE, good, tmp_path, served.path) == [
        f"{ABSOLUTE.name}: 'accepted', not '{ABSOLUTE.problem}'"
    ]
    late = {**refusal, "stored": base64.b64encode(b"xyz").decode()}
    assert sb.judged(ABSOLUTE, late, tmp_path) == [
        f"{ABSOLUTE.name}: refused after 3 bytes of a re-pack"
    ]


def test_the_judge_finds_a_re_pack_without_its_last_member(served, tmp_path):
    """#493: a re-pack cut short of its last member (here `SYMLINKS.txt`, which the re-pack
    writes last), every member before it intact, is wrong."""
    good = client(HONEST.data, served.path)
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(good["stored"]))) as whole:
        kept = whole.getmembers()[:-1]
        cut = io.BytesIO()
        with tarfile.open(fileobj=cut, mode="w:gz") as short:
            for member in kept:
                short.addfile(member, whole.extractfile(member) if member.isfile() else None)
    expected = [name for name, _ in HONEST.members]
    assert expected[-1] == "SYMLINKS.txt" and [m.name for m in kept] == expected[:-1]
    stored = base64.b64encode(cut.getvalue()).decode()
    assert sb.judged(HONEST, {**good, "stored": stored}, tmp_path, served.path) == [
        f"{HONEST.name}: the re-pack holds {expected[:-1]}, not {expected}"
    ]
    assert sb.judged(HONEST, good, tmp_path, served.path) == []


def test_the_journal_is_the_current_boots():
    """#493: the instances counted are the current boot's only (`-b`), here and in `INSPECT`."""
    server = FakeServer(
        {
            "journalctl": Result(
                0, journal(("UNIT", "zab-archive-check@7-4321-998.service", "Started")), ""
            )
        }
    )
    assert sb.Sandbox(server).journal(1000) == {"zab-archive-check@7-4321-998.service": ["Started"]}
    ((argv, _),) = server.calls
    assert argv[:3] == ["sudo", "journalctl", "-b"] and "--since=@1000" in argv
    assert '"journalctl", scope, "-b",' in sb.INSPECT


def journal(*entries: tuple[str, str, str]) -> str:
    """JSON lines as `journalctl --output=json` prints them: (field, unit, message)."""
    lines = [
        json.dumps({field: unit, "MESSAGE": message, "_PID": "1"})
        for field, unit, message in entries
    ]
    return "\n".join(["not json", json.dumps({"MESSAGE": "no unit"}), *lines]) + "\n"


def test_the_journal_has_one_instance_per_connection():
    sent = [
        (HONEST, {"pid": 11, "uid": 998}),
        (ABSOLUTE, {"pid": 12, "uid": 998}),
        (CASES["zab-waker"], {"pid": 13, "uid": 997}),
        (CASES["zab-waker in group zab-admin"], {"pid": 14, "uid": 997}),
    ]
    first, refused, peer = (
        f"zab-archive-check@{i}.service" for i in ("0-11-998", "1-12-998", "2-14-997")
    )
    exited = "Main process exited, code=exited, status=1/FAILURE"
    ok = [
        ("UNIT", first, f"Started {first} - zab archive check: re-pack one export archive"),
        ("UNIT", first, f"{first}: Deactivated successfully."),
        ("UNIT", refused, f"{refused}: {exited}"),
        ("USER_UNIT", peer, f"{peer}: {exited}"),
        ("UNIT", "zab-archive-check.socket", "Listening on zab-archive-check.socket"),
    ]
    units = sb.instances(journal(*ok))
    assert sorted(units) == [first, refused, peer]
    assert sb.journaled(sent, units) == []
    waker = sb.instances(journal(*ok, ("UNIT", "zab-archive-check@3-13-997.service", "Started")))
    assert sb.journaled(sent, waker) == ["zab-waker: 1 instance(s) in the journal, not 0"]
    twice = sb.instances(journal(*ok, ("UNIT", "zab-archive-check@4-11-998.service", "Started")))
    assert sb.journaled(sent, twice) == [f"{HONEST.name}: 2 instance(s) in the journal, not 1"]
    failed = [(f, u, m.replace("Deactivated successfully", "status=1/FAILURE")) for f, u, m in ok]
    assert sb.journaled(sent, sb.instances(journal(*failed)))[0].startswith(
        f"{HONEST.name}: zab-archive-check@0-11-998.service did not end with 'Deactivated"
    )
    assert sb.journaled(sent, sb.instances(journal(*ok[2:]))) == [
        f"{HONEST.name}: 0 instance(s) in the journal, not 1"
    ]


FILTER = "_llseek accept4 brk close exit_group mmap openat pread64 read recvmsg sendmsg write"
FACTS = {
    "uid": 998,
    "pid": 4321,
    "show": {
        "Id": "zab-archive-check@7-4321-998.service", "ActiveState": "active", "MainPID": "4400",
        "UID": "61900", "GID": "61900", "User": "zab-ac-7-4321-998", "DynamicUser": "yes",
        "PrivateNetwork": "yes",
        "InaccessiblePaths": "-/etc/zab-admin -/var/lib/zab-admin -/dev/shm -/run/dbus",
        "SystemCallFilter": FILTER, "DropInPaths": "",
    },
    "uids": ["61900"] * 4,
    "cgroup": "zab-archive-check@7-4321-998.service",
    "name": "zab-ac-7-4321-998",
    "inside": {"/etc/zab-admin": 0, "/var/lib/zab-admin": 0},
    "outside": {"/etc/zab-admin": 5, "/var/lib/zab-admin": 9},
    "held": True,
    "started": True,
}  # fmt: skip


def changed(**what) -> dict:
    facts = json.loads(json.dumps(FACTS))
    facts["show"] |= what.pop("show", {})
    return facts | what


@pytest.mark.parametrize(
    "facts, problem",
    [
        (changed(show={"Id": ""}), "no instance for the held connection (pid 4321): nothing in"),
        (
            changed(show={"Id": ""}, ended=["x: Main process exited, status=226/NAMESPACE"]),
            "no instance for the held connection (pid 4321): x: Main process exited, status=226",
        ),
        (changed(show={"Id": "zab-archive-check@7-4322-998.service"}), "no instance for the held"),
        (changed(show={"DynamicUser": "no"}), "DynamicUser=no, User=zab-ac-7-4321-998"),
        (changed(show={"User": "zab-admin"}), "DynamicUser=yes, User=zab-admin"),
        (changed(show={"UID": "998"}), "UID=998: not one of systemd's dynamic users"),
        (changed(show={"UID": "[not set]"}), "UID=[not set]: not one of systemd's dynamic users"),
        (changed(uids=["61900", "0", "61900", "61900"]), "its process runs as"),
        (changed(name="zab-admin"), "uid 61900 is 'zab-admin' on the server"),
        (changed(cgroup="init.scope"), "MainPID is in 'init.scope', not the instance"),
        (changed(show={"PrivateNetwork": "no"}), "PrivateNetwork=no"),
        (
            changed(show={"DropInPaths": "/etc/systemd/system/service.d/x.conf"}),
            "DropInPaths=/etc/systemd/system/service.d/x.conf: not the installed unit alone",
        ),
        (changed(show={"DropInPaths": None}), "DropInPaths=None: not the installed unit alone"),
        (changed(show={"InaccessiblePaths": "-/dev/shm"}), "/etc/zab-admin is not in Inaccess"),
        (changed(inside={"/etc/zab-admin": 3, "/var/lib/zab-admin": 0}), "sees it: 3"),
        (changed(inside={"/etc/zab-admin": 0}), "/var/lib/zab-admin as the instance sees it: None"),
        (changed(outside={"/etc/zab-admin": 0, "/var/lib/zab-admin": 9}), "on the server: 0: no"),
        (changed(show={"SystemCallFilter": "~socket connect"}), "not an allow-list"),
        (changed(show={"SystemCallFilter": FILTER + " io_uring_setup"}), "['io_uring_setup']"),
        (changed(show={"SystemCallFilter": FILTER + " socket"}), "allows ['socket'], read: True"),
        (changed(show={"SystemCallFilter": "write"}), "allows [], read: False"),
        (changed(held=False), "the held connection ended before the instance was read"),
        (
            changed(started=False),
            "zab-archive-check@7-4321-998.service never ran the checker: nothing in the journal",
        ),
        (
            changed(started=False, ended=["a: Failed to set up mount namespacing: /run: x", "b"]),
            "zab-archive-check@7-4321-998.service never ran the checker: a: Failed to set up mount "
            "namespacing: /run: x; b",
        ),
    ],
)  # fmt: skip
def test_the_inspection_judge(facts, problem):
    assert sb.inspected(FACTS) == []
    assert sb.inspected(changed(name=None)) == []  # no nss-systemd: the name unread
    found = sb.inspected(facts)
    assert any(p.startswith(problem) or problem in p for p in found), found


NAME = sb.COPY + "0123456789ab"


def service_lines(text: str) -> list[str]:
    return text.split("[Service]\n", 1)[1].splitlines()


def test_the_copy_is_the_installed_units_but_execstart_and_names():
    template, socket_unit = TEMPLATE.read_text(), SOCKET_UNIT.read_text()
    service, sock = sb.copy_units(template, socket_unit, NAME, "/bin/probe -x")
    assert service_lines(service)[0] == "ExecStart=/bin/probe -x"
    shipped = service_lines(template)
    assert service_lines(service)[1:] == [ln for ln in shipped if not ln.startswith("ExecStart=")]
    settings = [ln for ln in service.splitlines() if not ln.startswith("#")]
    assert f"Requires={NAME}.socket" in settings and f"After={NAME}.socket" in settings
    assert not [ln for ln in settings if sb.UNIT in ln]
    sockets = sock.splitlines()
    assert f"ListenStream=/run/{NAME}/check.sock" in sockets and "RemoveOnStop=yes" in sockets
    assert {"SocketUser=root", "SocketGroup=zab-admin", "SocketMode=0660", "Accept=yes"} <= set(
        sockets
    )
    with pytest.raises(ValueError, match="not a copy's name"):
        sb.copy_units(template, socket_unit, "zab-archive-check", "/bin/probe")
    with pytest.raises(ValueError, match="one ExecStart"):
        sb.copy_units(template + "ExecStart=/bin/x\n", socket_unit, NAME, "/bin/probe")


def test_the_probe_is_one_word_that_systemd_passes_as_it_is():
    word = sb.embedded(sb.probe())
    assert re.fullmatch(r"exec\(bytes\(\[[0-9,]+\]\)\)", word)
    assert bytes(ast.literal_eval(word.removeprefix("exec(bytes(").removesuffix("))"))) == (
        sb.probe().encode()
    )
    code = "import sys\nprint('ran', sys.argv[1:], end='')\n"
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-S", "-B", "-c", sb.embedded(code), "x"],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    assert done.stdout == "ran ['x']"


@pytest.mark.skipif(not shutil.which("systemd-analyze"), reason="no systemd-analyze")
def test_the_copy_with_the_probe_verifies(tmp_path):
    exec_start = f"{sys.executable} -I -S -B -c {sb.embedded(sb.probe())}"
    service, sock = sb.copy_units(TEMPLATE.read_text(), SOCKET_UNIT.read_text(), NAME, exec_start)
    (tmp_path / f"{NAME}@.service").write_text(service)
    (tmp_path / f"{NAME}.socket").write_text(sock)
    units = [str(tmp_path / f"{NAME}@.service"), str(tmp_path / f"{NAME}.socket")]
    done = subprocess.run(  # noqa: S603
        [shutil.which("systemd-analyze") or "", "verify", "--man=no", *units],
        capture_output=True, text=True,
    )  # fmt: skip
    ours = [line for line in done.stderr.splitlines() if str(tmp_path) in line]
    assert done.returncode == 0 and not ours, done.stderr


def test_install_takes_no_other_name():
    done = subprocess.run(  # noqa: S603 - it exits before it reads or writes anything
        [sys.executable, "-I", "-c", sb.INSTALL, "zab-archive-check"],
        input="{}", capture_output=True, text=True,
    )  # fmt: skip
    assert done.returncode == 1 and done.stderr.strip() == "refused: zab-archive-check"


# --------------------------------------------------------------- the steps, on a fake server


class FakeServer:
    """`Server` as `Sandbox` uses it: each remote command recorded; `answers` gives each a
    result by the script it runs (or its first word), `units` the installed files."""

    def __init__(self, answers: dict) -> None:
        self.answers, self.calls = answers, []

    def _answer(self, argv, input=None) -> Result:
        self.calls.append((list(argv), input))
        key = next((k for k in self.answers if k in argv), argv[0])
        answer = self.answers.get(key, Result(0, "", ""))
        return answer(argv, input) if callable(answer) else answer

    def run(self, argv, *, input=None, timeout=120, check=True):
        done = self._answer(argv, input)
        if check and not done.ok:
            raise RuntimeError(f"{argv[:3]}: exit {done.returncode}")
        return done

    def read(self, path):
        self.calls.append((["sudo", "cat", "--", path], None))
        return (TEMPLATE if path.endswith("@.service") else SOCKET_UNIT).read_text()

    def running(self):
        return "v9.9.9rc1"

    def as_admin(self, *argv, timeout=300, input=None):
        return self._answer(["sudo", "-u", "zab-admin", *argv], input)

    def as_user(self, user, *argv, group=None, timeout=300, input=None):
        return self._answer(["as", user, group or "-", *argv], input)


LEFT_NONE = Result(0, json.dumps({"left": []}) + "\n", "")
PROBE = "print('probe')"


def test_the_probe_runs_in_a_copy_that_is_removed_after():
    probed = json.dumps(
        {"pid": 5, "uid": 998, "problem": f"{sb.PROBED}: deleted", "note": "", "stored": ""}
    )
    server = FakeServer({sb.REMOVE: LEFT_NONE, sb.CLIENT: Result(0, probed + "\n", "")})
    got, left = sb.Sandbox(server).probe(PROBE, b"archive")
    assert got["problem"] == f"{sb.PROBED}: deleted" and left == []
    scripts = [argv for argv, _ in server.calls if argv[:4] == ["sudo", "python3", "-I", "-c"]]
    install, remove = scripts
    assert install[4] == sb.INSTALL and remove[4] == sb.REMOVE
    name = install[5]
    assert re.fullmatch(rf"{sb.COPY}[0-9a-f]{{12}}", name)
    units = json.loads(next(stdin for argv, stdin in server.calls if argv == install))
    word = sb.embedded(PROBE)
    assert f"ExecStart={sb.PYTHON} -I -S -B -c {word}" in units["@.service"]
    clients = [argv for argv, _ in server.calls if sb.CLIENT in argv]
    assert clients == [["sudo", "-u", "zab-admin", sb.PYTHON, "-I", "-c", sb.CLIENT, sb.ADMIN_DIR,
                        f"/run/{name}/check.sock"]]  # fmt: skip
    assert next(stdin for argv, stdin in server.calls if sb.CLIENT in argv) == "YXJjaGl2ZQ=="


def test_the_copy_is_removed_when_the_probe_fails():
    server = FakeServer({sb.REMOVE: LEFT_NONE, sb.CLIENT: Result(1, "", "Traceback: boom")})
    with pytest.raises(AssertionError, match="the client, for the probe failed: exit 1: Traceback"):
        sb.Sandbox(server).probe(PROBE, b"archive")
    assert server.calls[-1][0][4] == sb.REMOVE
    server = FakeServer({sb.INSTALL: Result(1, "", "no systemd"), sb.REMOVE: LEFT_NONE})
    with pytest.raises(RuntimeError):
        sb.Sandbox(server).probe(PROBE, b"archive")
    assert server.calls[-1][0][4] == sb.REMOVE


def test_each_case_is_sent_as_its_user():
    said = json.dumps({"pid": 1, "uid": 2, "problem": "", "note": "", "stored": ""}) + "\n"
    server = FakeServer({sb.CLIENT: Result(0, said, "")})
    box = sb.Sandbox(server)
    for case in CASES.values():
        box.send(case)
    become = [argv[:3] for argv, _ in server.calls]
    assert become.count(["sudo", "-u", "zab-admin"]) == len(CASES) - 2 == 7
    assert become[-2:] == [["as", "zab-waker", "-"], ["as", "zab-waker", "zab-admin"]]
    assert all(argv[-2:] == [sb.ADMIN_DIR, sb.SOCKET] for argv, _ in server.calls)
    assert [base64.b64decode(stdin) for _, stdin in server.calls] == [
        c.data for c in CASES.values()
    ]


def test_as_user_runs_setpriv_for_a_group_and_sudo_otherwise(tmp_path):
    seen = []
    rig = rigs.load({"HOME": str(tmp_path)})

    def runner(argv, timeout, input=None):
        seen.append(argv[-1])
        return Result(0, "", "")

    server = Server("192.0.2.7", rig, runner=runner)
    server.as_user("zab-waker", "python3", "-c", "x", group="zab-admin", input="data")
    server.as_user("zab-waker", "python3")
    env = "env -i PATH=/usr/bin:/bin HOME=/ LANG=C.UTF-8"
    assert seen == [
        f"sudo setpriv --reuid=zab-waker --regid=zab-admin --clear-groups -- {env} python3 -c x",
        f"sudo -u zab-waker {env} python3",
    ]


def test_a19_is_selectable_and_not_a_go_criterion():
    assert rigs.select("A19") == [19] and 19 in rigs.select("all")
    assert 19 not in rigs.select("go") and rigs.select("go,A19")[-1] == 19
    assert rigs.label(19) == "A19" and rigs.SCENARIOS[19][2] == 3  # stage 3: exports


# ---------------------------------------------------------- the copies, under a user manager


def user_mode(
    service: str, sock: str, runtime: pathlib.Path, name: str, *shown: str
) -> tuple[str, str]:
    """A copy as this user's manager can run it (as the sandbox test of test_adminserver_units
    has the shipped unit): PrivateUsers for DynamicUser and User, ProtectHome=read-only, `shown`
    bound read-only; the socket in the runtime directory, this user's alone."""
    lines = [
        line.replace("ProtectHome=true", "ProtectHome=read-only")
        for line in service.splitlines()
        if not line.startswith(("DynamicUser=", "User="))
    ]
    service = "\n".join([*lines, "PrivateUsers=yes", "BindReadOnlyPaths=" + " ".join(shown), ""])
    sock = sock.replace(f"/run/{name}/", f"{runtime}/{name}/").replace(
        "SocketMode=0660", "SocketMode=0600"
    )
    sock = "\n".join(
        ln for ln in sock.splitlines() if not ln.startswith(("SocketUser", "SocketGroup"))
    )
    return service, sock + "\n"


def _no_sandbox_here() -> str:
    sb.corpus()  # the scripts on the path
    from workshop.test_adminserver_units import _no_sandbox_here as why

    return why()


def systemctl(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603
        [shutil.which("systemctl") or "systemctl", "--user", *args],
        capture_output=True, text=True, check=check, timeout=60,
    )  # fmt: skip


def test_the_copies_run_under_a_user_manager(tmp_path):
    """The checker held (`INSPECT`), its instances in the journal, and the probe: the scripts
    and the copies as A19 runs them, under this user's manager instead of PID 1. And a checker
    whose sandbox can't be set up (a path to hide that doesn't exist; under PID 1 on the rig,
    `-/run`): `INSPECT` reports how it ended, `226/NAMESPACE`."""
    if why := _no_sandbox_here():
        pytest.skip(why)
    runtime = pathlib.Path(os.environ["XDG_RUNTIME_DIR"])
    units = runtime / "systemd" / "user"
    units.mkdir(parents=True, exist_ok=True)
    python = os.path.realpath(sys.executable)
    checker, prober, broken = (sb.COPY + secrets.token_hex(6) for _ in range(3))
    serve = f"{python} -I -S -B {ADMIN_DIR / 'archive_check.py'} serve --peer {me()}"
    shown = (os.path.realpath(sys.base_prefix), str(ADMIN_DIR))
    for name, exec_start in (
        (checker, serve),
        (prober, f"{python} -I -S -B -c {sb.embedded(sb.probe())}"),
        (broken, serve),
    ):
        service, sock = sb.copy_units(
            TEMPLATE.read_text(), SOCKET_UNIT.read_text(), name, exec_start
        )
        service, sock = user_mode(service, sock, runtime, name, *shown)
        if name == broken:
            service += f"InaccessiblePaths=/{name}-missing\n"
        (units / f"{name}@.service").write_text(service)
        (units / f"{name}.socket").write_text(sock)
    since = int(time.time()) - 2
    try:
        systemctl("daemon-reload", check=True)
        for name in (checker, prober, broken):
            systemctl("start", f"{name}.socket", check=True)
        path = str(runtime / checker / "check.sock")
        got = client(HONEST.data, path)
        assert sb.judged(HONEST, got, tmp_path, path) == []
        argv = [sys.executable, "-I", "-c", sb.INSPECT, "--user", "-", python, path, sb.HOLD]
        done = subprocess.run([*argv, checker, "/run/dbus"], capture_output=True, text=True)  # noqa: S603
        facts = sb.answer(Result(done.returncode, done.stdout, done.stderr), "INSPECT")
        assert facts["started"] and facts["held"], facts  # else no "cgroup" to compare
        assert facts["cgroup"] == facts["show"]["Id"], facts
        assert facts["uids"] == [str(os.getuid())] * 4  # no DynamicUser under a user manager
        assert facts["inside"]["/run/dbus"] == "PermissionError" and facts["outside"]["/run/dbus"]
        problems = sb.inspected(facts, unit=checker)
        sound = ("no instance", "MainPID", "SystemCall", "the held", "PrivateNetwork")
        assert not [p for p in problems if p.startswith(sound)], problems
        assert any(p.startswith("DynamicUser=no") for p in problems)  # PID 1's, on the rig
        held = sb.Case("held", b"", problem="not a request")  # it ends refused, as a refusal
        sent = [(HONEST, got), (held, {"pid": facts["pid"], "uid": facts["uid"]})]
        journalctl = [shutil.which("journalctl") or "journalctl", "--user", "--no-pager"]
        journalctl += ["--output=json", f"--since=@{since}", f"--unit={checker}@*"]
        for _ in range(40):  # a user manager logs a success at debug level only: its start
            listed = subprocess.run(journalctl, capture_output=True, text=True).stdout  # noqa: S603
            seen = sb.instances(listed, checker)
            if not (late := sb.journaled(sent, seen, checker, ended_ok="Started ")):
                break
            time.sleep(0.5)
        assert not late, late
        probed = client(HONEST.data, str(runtime / prober / "check.sock"))
        assert probed["problem"] == f"{sb.PROBED}: deleted"
        path = str(runtime / broken / "check.sock")
        done = subprocess.run([*argv[:-2], path, sb.HOLD, broken], capture_output=True, text=True)  # noqa: S603
        facts = sb.answer(Result(done.returncode, done.stdout, done.stderr), "INSPECT")
        assert not facts["started"] and "inside" not in facts, facts
        assert any("status=226/NAMESPACE" in line for line in facts["ended"]), facts
        (problem,) = sb.inspected(facts, unit=broken)
        assert "never ran the checker: " in problem or "no instance for the held" in problem
        assert "Failed to set up mount namespacing" in problem and "226/NAMESPACE" in problem
    finally:
        for name in (checker, prober, broken):
            systemctl("stop", f"{name}.socket", instance_slice(name))
            (units / f"{name}@.service").unlink(missing_ok=True)
            (units / f"{name}.socket").unlink(missing_ok=True)
            shutil.rmtree(runtime / name, ignore_errors=True)
        systemctl("daemon-reload")
        systemctl("reset-failed", f"{sb.COPY}*")
