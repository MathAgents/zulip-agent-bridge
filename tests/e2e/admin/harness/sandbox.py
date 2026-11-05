"""A19, the export re-pack's sandbox on the rig's server (docs/ADMIN.md §1.2, §7; #374, #356).

A9's `!export` needs a provisioned row, which needs Q9, so until then it never reaches the
checker. A19 does, without a VM: over the harness's SSH, crafted archives go straight to
`/run/zab-archive-check/check.sock` through the real client, `collect_exports.repack_file` of the
release the server runs. What only the server adds to the offline tests is systemd as PID 1
(`DynamicUser`, the socket's owner and group) and arm64 (a t4g).

- **The archives** are the archive tests' corpus (`tests/workshop/test_archive_check.py`, built
  here): honest (a symlink and a hard link), names Windows can't take (`CONIN$` among them), the
  deepest name taken, and refusals (an absolute name, a `..`, a reviewer's crafted header, a name
  one component deeper). Each goes to
  the server on standard input, into an unnamed file of its `/tmp` (`O_TMPFILE`, 0600), as does
  the re-pack (`CLIENT`): nothing has a name to leave behind, whatever ends the client. The
  answer comes back, with the re-packed archive, which is checked here as the archive tests
  check theirs (`safe`): only regular files and directories, listed alike by GNU tar and tarfile.
- **Peers:** `zab-waker` can't connect (the socket is root:zab-admin 0660); `zab-waker` with the
  `zab-admin` group connects, and the checker refuses it (`SO_PEERCRED`).
- **The instance** behind a connection held open (`INSPECT`): a user of its own (a uid in
  systemd's dynamic range, `User=zab-ac-<instance>`), nothing in `/etc/zab-admin` or
  `/var/lib/zab-admin` as it sees them (`/proc/<pid>/root`, read as root), and an effective system
  call filter without io_uring or `socket`.
- **The journal:** one instance per connection, named after its client's pid and uid, ending as
  its answer did (`journaled`).
- **The kernel:** the reviewer's probe of #384 (`tests/workshop/test_adminserver_units.py`'s
  `PROBE`) in place of the checker, in a copy of the installed units (`copy_units`), under PID 1:
  no socket, no fork, no write, no io_uring, no D-Bus. The copy lives in `/run` (gone at the
  next boot anyway) and is removed after (`REMOVE`), whatever happened.
"""

from __future__ import annotations

import base64
import contextlib
import gzip
import json
import re
import secrets
import subprocess
import sys
import tarfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from e2e.admin.harness.server import CURRENT
from e2e.harness.config import CHECKOUT
from e2e.harness.vm import Result

PYTHON = f"{CURRENT}/.venv/bin/python"
ADMIN_DIR = f"{CURRENT}/workshop/admin"
UNIT = "zab-archive-check"
SOCKET = f"/run/{UNIT}/check.sock"  # collect_exports.CHECKER_SOCKET
INSTALLED = "/etc/systemd/system"
ADMIN, WAKER = "zab-admin", "zab-waker"
#: systemd's uids for `DynamicUser=` (DYNAMIC_UID_MIN to DYNAMIC_UID_MAX).
DYNAMIC = range(61184, 65520)
#: The rig's server's machine (a t4g): A19's kernel probe is arm64's (ADMIN §7; #493).
MACHINE = "aarch64"
#: What the instance must not reach (ADMIN §1.2): the keys, the config and the bot's state.
KEYS = ("/etc/zab-admin", "/var/lib/zab-admin")
#: System calls its effective filter must not allow (the review of #384: io_uring makes sockets).
DENIED = frozenset({"io_uring_setup", "io_uring_enter", "io_uring_register", "socket", "connect"})
#: The copy of the units the probe runs in: `zab-e2e-probe-<12 hex>`, in /run only.
COPY = "zab-e2e-probe-"
_COPY = re.compile(rf"{COPY}[0-9a-f]{{12}}")
#: The probe's answer when the sandbox holds: an AF_UNIX and an AF_INET socket, fork, a write to
#: /tmp, to /var/tmp and to /dev/shm, io_uring_setup, and D-Bus's socket (as the sandbox test of
#: test_adminserver_units.py expects it).
PROBED = "EPERM EPERM EPERM EROFS EROFS EACCES EPERM EACCES"
#: The most components a name may have (#384, `archive_check.MAX_DEPTH`): one more is refused.
DEEP = 256

# ------------------------------------------------------------------- scripts, run on the server

#: As the user under test: the archive on stdin (base64) into an unnamed file of /tmp, re-packed
#: by `collect_exports.repack_file` of the release (`$1`, its workshop/admin) on the socket `$2`
#: into another. Prints its pid, uid, the answer and what the re-pack wrote (base64).
CLIENT = r"""
import base64, json, os, sys, tempfile
sys.path.insert(0, sys.argv[1])
import collect_exports
data = base64.b64decode(sys.stdin.read())
with tempfile.TemporaryFile() as archive, tempfile.TemporaryFile() as dest:
    archive.write(data)
    archive.flush()
    problem, note = collect_exports.repack_file(archive, dest, checker=sys.argv[2])
    dest.seek(0)
    stored = dest.read()
print(json.dumps({"pid": os.getpid(), "uid": os.getuid(), "problem": problem, "note": note,
                  "stored": base64.b64encode(stored).decode()}))
"""
#: As `INSPECT`'s user: connects to the socket `$1`, says its pid, and holds the connection,
#: sending nothing, until its stdin closes.
HOLD = r"""
import os, socket, sys
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
    sock.connect(sys.argv[1])
    print(os.getpid(), flush=True)
    sys.stdin.read()
"""
#: As root (or, offline, as this user with `--user` and `-`): holds a connection as `$2` (`HOLD`)
#: and reads the instance it started, by its name (`<unit>@<n>-<pid>-<uid>.service`), once it
#: runs the checker (`started`: an instance is `active` from its fork, before its sandbox and its
#: user are in place): its properties, its process's uids and cgroup, and how many entries each
#: of `paths` has as it sees them (`/proc/<pid>/root`) and as the server does. An instance that
#: never runs the checker (it ended, or failed in its sandbox's setup: `226/NAMESPACE`) gets the
#: journal's lines on how it ended instead (`ended`). Prints them; judges nothing.
INSPECT = r"""
import json, os, pwd, subprocess, sys, time
scope, user, python, sock, hold, unit, *paths = sys.argv[1:]
PROPERTIES = ("Id,ActiveState,MainPID,UID,GID,User,DynamicUser,PrivateNetwork,"
              "InaccessiblePaths,SystemCallFilter,DropInPaths")
def systemctl(*args):
    return subprocess.run(["systemctl", scope, *args], capture_output=True, text=True,
                          timeout=30).stdout
def entries(path):
    try:
        return len(os.listdir(path))
    except OSError as exc:
        return type(exc).__name__
def started(main):  # it runs the checker: past exec, so its sandbox and user are in place
    try:
        with open("/proc/%d/cmdline" % main, "rb") as cmdline:
            return b"/archive_check.py\0serve\0" in cmdline.read()
    except OSError:
        return False
who = pwd.getpwuid(os.getuid()) if user == "-" else pwd.getpwnam(user)
become = {} if user == "-" else {"user": who.pw_uid, "group": who.pw_gid,
                                 "extra_groups": os.getgrouplist(who.pw_name, who.pw_gid)}
holder = subprocess.Popen([python, "-I", "-c", hold, sock], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, env={"PATH": "/usr/bin:/bin"}, cwd="/", **become)
out = {"uid": who.pw_uid, "show": {}}
try:
    out["pid"] = pid = int(holder.stdout.readline() or 0)
    suffix = "-%d-%d.service" % (pid, who.pw_uid)
    deadline, main = time.monotonic() + 8, 0
    while pid and time.monotonic() < deadline:
        listed = systemctl("list-units", "--all", "--plain", "--no-legend", "--full", unit + "@*")
        found = [w[0] for w in map(str.split, listed.splitlines()) if w and w[0].endswith(suffix)]
        if found:
            text = systemctl("show", found[0], "--property=" + PROPERTIES)
            out["show"] = dict(ln.split("=", 1) for ln in text.splitlines() if "=" in ln)
            main = int(out["show"].get("MainPID") or 0)
            if out["show"].get("ActiveState") == "active" and main and started(main):
                break
            if out["show"].get("ActiveState") == "failed":
                break
        elif out["show"]:
            break  # it was there, and is gone: it ended
        time.sleep(0.1)
    out["started"] = bool(main) and started(main)
    if pid and not out["started"]:  # how it ended, from the journal (systemd may lag a little)
        name = out["show"].get("Id") or unit + "@*" + suffix
        journal = ["journalctl", scope, "-b", "--no-pager", "--output=cat", "--unit=" + name]
        for _ in range(25):
            text = subprocess.run(journal, capture_output=True, text=True, timeout=30).stdout
            out["ended"] = [ln for ln in text.splitlines() if "status=" in ln or "Failed" in ln]
            out["ended"] = out["ended"][-3:]
            if any("status=" in ln for ln in out["ended"]):
                break
            time.sleep(0.2)
    if out["started"]:
        with open("/proc/%d/status" % main) as status:
            out["uids"] = next(ln.split()[1:] for ln in status if ln.startswith("Uid:"))
        with open("/proc/%d/cgroup" % main) as cgroup:
            out["cgroup"] = cgroup.read().strip().rsplit("/", 1)[-1]
        try:
            out["name"] = pwd.getpwuid(int(out["uids"][0])).pw_name
        except KeyError:
            out["name"] = None
        out["inside"] = {p: entries("/proc/%d/root%s" % (main, p)) for p in paths}
        out["outside"] = {p: entries(p) for p in paths}
    out["held"] = holder.poll() is None
finally:
    holder.stdin.close()
    try:
        holder.wait(timeout=15)
    except subprocess.TimeoutExpired:
        holder.kill()
        holder.wait()
print(json.dumps(out))
"""
#: As root: the copy `$1` (`zab-e2e-probe-<12 hex>` only) of the units, from stdin (JSON:
#: "@.service" and ".socket"), into /run/systemd/system; its socket started.
INSTALL = r"""
import json, pathlib, re, subprocess, sys
name = sys.argv[1]
if not re.fullmatch(r"zab-e2e-probe-[0-9a-f]{12}", name):
    sys.exit("refused: " + name)
units = json.load(sys.stdin)
for suffix in ("@.service", ".socket"):
    path = pathlib.Path("/run/systemd/system") / (name + suffix)
    path.write_text(units[suffix])
    path.chmod(0o644)
subprocess.run(["systemctl", "daemon-reload"], check=True)
subprocess.run(["systemctl", "start", name + ".socket"], check=True)
"""
#: As root: every copy gone (this run's and any a killed run left): its sockets stopped, its
#: files and socket directories removed, systemd reloaded. Prints what is left (nothing).
REMOVE = r"""
import glob, json, os, shutil, subprocess
units = "/run/systemd/system/zab-e2e-probe-"
for path in sorted(glob.glob(units + "*.socket")):
    subprocess.run(["systemctl", "stop", os.path.basename(path)], capture_output=True)
for path in glob.glob(units + "*"):
    os.unlink(path)
subprocess.run(["systemctl", "daemon-reload"], check=True)
subprocess.run(["systemctl", "reset-failed", "zab-e2e-probe-*"], capture_output=True)
for path in glob.glob("/run/zab-e2e-probe-*"):
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    else:
        os.unlink(path)
listed = subprocess.run(["systemctl", "list-units", "--all", "--plain", "--no-legend",
                         "zab-e2e-probe-*"], capture_output=True, text=True).stdout.split()
left = glob.glob(units + "*") + glob.glob("/run/zab-e2e-probe-*")
print(json.dumps({"left": left + [w for w in listed if w.startswith("zab-e2e-probe-")]}))
"""

# --------------------------------------------------------------------------------- the corpus


def corpus() -> Any:
    """`tests/workshop/test_archive_check.py`, imported as its conftest has it: with the scripts
    of `workshop/admin` and `workshop/adminserver` on the path."""
    _scripts()
    from workshop import test_archive_check

    return test_archive_check


def probe() -> str:
    """The reviewer's probe of #384 (`tests/workshop/test_adminserver_units.py`'s `PROBE`)."""
    _scripts()
    from workshop import test_adminserver_units

    return test_adminserver_units.PROBE


def _scripts() -> None:
    for sub in ("admin", "adminserver"):
        path = str(CHECKOUT / "workshop" / sub)
        if path not in sys.path:
            sys.path.insert(0, path)


@dataclass(frozen=True)
class Case:
    """One archive sent, as `user` (with `group` as its only group, if given), and what must
    come back: the refusal (`{socket}` is the socket's path), or the re-pack's note and members
    (name, data or None for a directory), in order."""

    name: str
    data: bytes
    problem: str = ""
    note: str = ""
    members: tuple[tuple[str, bytes | None], ...] = ()
    user: str = ADMIN
    group: str | None = None
    connects: bool = True  # it reaches the checker: one instance in the journal


def cases() -> list[Case]:
    """The archives A19 sends: re-packed, refused, and the peers refused (module doc)."""
    t = corpus()
    check = t.ac_check
    honest = t.tar_of(
        ("work", tarfile.DIRTYPE, "", b""),
        t.file_("work/a.md", b"alpha"),
        t.link("work/same.md", "work/a.md"),
        ("work/latest", tarfile.SYMTYPE, "a.md", b""),
    )
    copied = (("work", None), ("work/a.md", b"alpha"), ("work/same.md", b"alpha"))
    listed = (check.MANIFEST, check.MANIFEST_HEAD + b"work/latest\ta.md\n")
    names = t.WINDOWS_NAMES  # `CONIN$` and `conout$.log` among them (#384)
    windows = t.tar_of(*(t.file_(old, old.encode()) for old in names))
    renames = b"".join(
        check._escape(old) + b"\t" + new.encode() + b"\n" for old, new in names.items()
    )
    renamed = tuple((new, old.encode()) for old, new in names.items())
    deepest, deep = "a/" * (DEEP - 1) + "x", "a/" * DEEP + "x"
    return [
        Case(
            "a symlink and a hard link", honest,
            note="1 symlink(s) listed in SYMLINKS.txt; 1 hard link(s) copied",
            members=(*copied, listed),
        ),
        Case(
            "names Windows can't take", windows,
            note=f"{len(names)} renamed for Windows, in RENAMED.txt",
            members=(*renamed, (check.RENAMES, check.RENAMES_HEAD + renames)),
        ),
        Case(
            f"a name of {DEEP} components", t.tar_of(t.file_(deepest)),
            members=((deepest, b"x"),),
        ),
        Case(
            "an absolute name", t.THROUGH["an absolute symlink, then the file"],
            "an absolute name or link target (/work/l): deleted",
        ),
        Case(
            "a .. after a symlink", t.THROUGH["a .. after a symlink"],
            "a `..` in a member's name (work/l/../zab/key): deleted",
        ),
        Case(
            "a NUL before the size", gzip.compress(t.CRAFTED["a NUL before the size"]),
            "a header's numbers are not in canonical form: deleted",
        ),
        Case(
            f"a name of {DEEP + 1} components", t.tar_of(t.file_(deep)),
            f"a name of over {DEEP} components ({deep[-80:]}): deleted",
        ),
        Case(
            WAKER, honest, "the archive check failed (no checker on {socket}: PermissionError): "
            "deleted", user=WAKER, connects=False,
        ),
        Case(
            f"{WAKER} in group {ADMIN}", honest, "not a caller this checker answers: deleted",
            user=WAKER, group=ADMIN,
        ),
    ]  # fmt: skip


# ------------------------------------------------------------------------------ the judges


def answer(done: Result, what: str) -> dict[str, Any]:
    """The JSON line a script printed last; AssertionError if it failed or printed none."""
    lines = done.stdout.strip().splitlines()
    if not done.ok or not lines:
        tail = (done.stderr or done.stdout).strip()[-500:]
        raise AssertionError(f"{what} failed: exit {done.returncode}: {tail}")
    return json.loads(lines[-1])


def judged(case: Case, got: Mapping[str, Any], tmp: Path, socket: str = SOCKET) -> list[str]:
    """What is wrong with the answer `got` to `case`: [] when it is the one expected, and a
    re-pack holds only files and directories (`safe`), with the members expected."""
    wanted = case.problem.replace("{socket}", socket)
    stored = base64.b64decode(got.get("stored", ""))
    problem = got.get("problem", "")
    if problem != wanted:
        return [f"{case.name}: {problem or 'accepted'!r}, not {wanted or 'accepted'!r}"]
    if wanted:
        return [f"{case.name}: refused after {len(stored)} bytes of a re-pack"] * bool(stored)
    problems = []
    if got.get("note") != case.note:
        problems.append(f"{case.name}: its note is {got.get('note')!r}, not {case.note!r}")
    path = tmp / f"{re.sub(r'[^a-z0-9]+', '-', case.name.lower())}.tar.gz"
    path.write_bytes(stored)
    try:
        members = tuple(corpus().contents(path))
    except (AssertionError, tarfile.TarError, OSError, subprocess.CalledProcessError) as exc:
        problems.append(f"{case.name}: the re-pack is not files and directories alone: {exc!r}")
        return problems
    names, expected = [name for name, _ in members], [name for name, _ in case.members]
    if names != expected:
        problems.append(f"{case.name}: the re-pack holds {names}, not {expected}")
    elif changed := [
        name for (name, data), (_, want) in zip(members, case.members, strict=True) if data != want
    ]:
        problems.append(f"{case.name}: the re-pack's {changed} hold other data")
    return problems


def instances(text: str, unit: str = UNIT) -> dict[str, list[str]]:
    """`journalctl --output=json` lines → each instance of `unit@` named in them (by systemd,
    system or user, or by itself) → its messages, in order."""
    name = re.compile(rf"{re.escape(unit)}@\d+-\d+-\d+\.service")
    found: dict[str, list[str]] = {}
    for line in text.splitlines():
        with contextlib.suppress(ValueError):
            entry = json.loads(line)
            fields = ("UNIT", "USER_UNIT", "_SYSTEMD_UNIT", "_SYSTEMD_USER_UNIT")
            for named in {entry.get(field) for field in fields}:
                if isinstance(named, str) and name.fullmatch(named):
                    message = entry.get("MESSAGE", "")
                    found.setdefault(named, []).append(str(message))
    return found


#: How PID 1 ends an instance's journal: a re-pack (exit 0), or a refusal (`serve` exits 1). A
#: user manager logs the first at debug level only (systemd's `unit_log_success`).
ENDED_OK, ENDED_REFUSED = "Deactivated successfully", "status=1/FAILURE"


def journaled(
    sent: Iterable[tuple[Case, Mapping[str, Any]]], units: Mapping[str, list[str]],
    unit: str = UNIT, ended_ok: str = ENDED_OK,
) -> list[str]:  # fmt: skip
    """What is wrong with the journal's instances for the cases `sent` (each with its client's
    answer, which names its pid and uid): one instance per client that connects, none for one
    that can't, each ended as its answer did (`ended_ok` for a re-pack)."""
    problems = []
    for case, got in sent:
        name = re.compile(rf"{re.escape(unit)}@\d+-{got['pid']}-{got['uid']}\.service")
        mine = sorted(u for u in units if name.fullmatch(u))
        if len(mine) != case.connects:
            problems.append(
                f"{case.name}: {len(mine)} instance(s) in the journal, not {+case.connects}"
            )
            continue
        end = ENDED_REFUSED if case.problem else ended_ok
        problems += [
            f"{case.name}: {u} did not end with {end!r}: {units[u][-3:]}"
            for u in mine
            if not any(end in message for message in units[u])
        ]
    return problems


def inspected(facts: Mapping[str, Any], unit: str = UNIT) -> list[str]:
    """What is wrong with `INSPECT`'s facts about the held connection's instance: a dynamic
    user of its own, named after the instance; no network; no drop-in, so the installed unit is
    all that applies (`systemctl show` takes no template: the instance's are its template's
    and any other); the keys hidden from it, while the server has them; a system call filter
    that allows `read` and none of `DENIED`."""
    show = facts.get("show") or {}
    found = show.get("Id", "")
    pattern = rf"{re.escape(unit)}@(\d+-{facts.get('pid')}-{facts.get('uid')})\.service"
    ended = "; ".join(facts.get("ended") or []) or "nothing in the journal"
    if not (named := re.fullmatch(pattern, found)):
        return [f"no instance for the held connection (pid {facts.get('pid')}): {ended}"]
    if not facts.get("started"):
        return [f"{found} never ran the checker: {ended}"]
    problems = []
    if show.get("DynamicUser") != "yes" or show.get("User") != f"zab-ac-{named[1]}":
        problems.append(f"DynamicUser={show.get('DynamicUser')}, User={show.get('User')}")
    uid = show.get("UID", "")
    if not (uid.isdigit() and int(uid) in DYNAMIC):
        problems.append(f"UID={uid}: not one of systemd's dynamic users")
    if facts.get("uids") != [uid] * 4:
        problems.append(f"its process runs as {facts.get('uids')}, not {uid}")
    if facts.get("name") not in (None, show.get("User")):
        problems.append(f"uid {uid} is {facts.get('name')!r} on the server")
    if facts.get("cgroup") != found:
        problems.append(f"MainPID is in {facts.get('cgroup')!r}, not the instance")
    if show.get("PrivateNetwork") != "yes":
        problems.append(f"PrivateNetwork={show.get('PrivateNetwork')}")
    if (drop_ins := show.get("DropInPaths")) != "":  # the installed unit as it is (#493)
        problems.append(f"DropInPaths={drop_ins}: not the installed unit alone")
    hidden = {path.lstrip("-+") for path in show.get("InaccessiblePaths", "").split()}
    for path in KEYS:
        inside, outside = (facts.get(side, {}).get(path) for side in ("inside", "outside"))
        if path not in hidden:
            problems.append(f"{path} is not in InaccessiblePaths")
        if inside != 0:
            problems.append(f"{path} as the instance sees it: {inside} entries, not 0")
        if not (isinstance(outside, int) and outside > 0):
            problems.append(f"{path} on the server: {outside}: no control")
    allowed = show.get("SystemCallFilter", "")
    calls = set(allowed.split())
    if not allowed or allowed.startswith("~"):
        problems.append(f"SystemCallFilter is not an allow-list: {allowed[:80]!r}")
    elif DENIED & calls or "read" not in calls:
        problems.append(
            f"SystemCallFilter allows {sorted(DENIED & calls)}, read: {'read' in calls}"
        )
    if not facts.get("held"):
        problems.append("the held connection ended before the instance was read")
    return problems


# --------------------------------------------------------------------- the copy for the probe


def embedded(code: str) -> str:
    """`code` as one word of `ExecStart=` that systemd passes on as it is (no space, quote,
    `%`, `$`, `\\` or `;`), and Python runs: `exec(bytes([…]))`."""
    return "exec(bytes([" + ",".join(str(b) for b in code.encode()) + "]))"


def copy_units(template: str, socket_unit: str, name: str, exec_start: str) -> tuple[str, str]:
    """The installed `zab-archive-check@.service` and `.socket` (their text) copied as `name`,
    with `exec_start` for `ExecStart=`. Every other line of `[Service]` is the installed one;
    the copy's socket is removed when it stops."""
    if not _COPY.fullmatch(name):
        raise ValueError(f"not a copy's name: {name!r}")
    head, marker, body = template.partition("[Service]\n")
    lines = body.splitlines()
    kept = [line for line in lines if not line.startswith("ExecStart=")]
    if not marker or len(lines) - len(kept) != 1 or "[Socket]\n" not in socket_unit:
        raise ValueError("not the units of zab-archive-check: one [Service], one ExecStart=")
    service = head.replace(UNIT, name) + marker + f"ExecStart={exec_start}\n"
    service += "".join(line + "\n" for line in kept)
    socket = socket_unit.replace(UNIT, name).replace("[Socket]\n", "[Socket]\nRemoveOnStop=yes\n")
    return service, socket


# ------------------------------------------------------------------------------ on the server


class Sandbox:
    """A19's steps on the rig's server (`harness/server.Server`)."""

    def __init__(self, server: Any) -> None:
        self.server = server
        self.machine = ""  # `ready`'s
        self.copy = ""  # the last copy of the units `probe` made

    def ready(self, release: str) -> str:
        """The server runs `release` and listens on the socket; its machine (`aarch64`, which
        the kernel probe's row needs: `MACHINE`)."""
        running = self.server.running()
        assert running == release, f"the server runs {running}, not {release}"
        state = self.server.run(["systemctl", "is-active", f"{UNIT}.socket"], check=False)
        assert state.stdout.strip() == "active", f"{UNIT}.socket is {state.stdout.strip()!r}"
        return self.server.run(["uname", "-m"]).stdout.strip()

    def now(self) -> int:
        """The server's clock, in seconds."""
        return int(self.server.run(["date", "+%s"]).stdout.strip())

    def send(self, case: Case, socket: str = SOCKET) -> dict[str, Any]:
        """`case`'s archive through the client, as its user, on `socket`: the client's answer."""
        argv = (PYTHON, "-I", "-c", CLIENT, ADMIN_DIR, socket)
        data = base64.b64encode(case.data).decode()
        if case.user == ADMIN and case.group is None:
            done = self.server.as_admin(*argv, input=data, timeout=1000)
        else:
            done = self.server.as_user(case.user, *argv, group=case.group, input=data, timeout=1000)
        return answer(done, f"the client, for {case.name}")

    def inspect(self) -> dict[str, Any]:
        argv = ["sudo", "python3", "-I", "-c", INSPECT, "--system", ADMIN, PYTHON, SOCKET, HOLD]
        return answer(self.server.run([*argv, UNIT, *KEYS], check=False), "the inspection")

    def journal(self, since: int, unit: str = UNIT) -> dict[str, list[str]]:
        """The instances of `unit@` in the journal since `since`, with their messages."""
        argv = ["sudo", "journalctl", "-b", "--no-pager", "--output=json", f"--since=@{since}"]
        return instances(self.server.run([*argv, f"--unit={unit}@*"], timeout=120).stdout, unit)

    def left_in_tmp(self, since: int) -> list[str]:
        """Files of the clients' users made in /tmp or /var/tmp since `since` (none: theirs
        have no name)."""
        users = ["(", "-user", ADMIN, "-o", "-user", WAKER, ")"]
        tmp = ["/tmp", "/var/tmp"]  # noqa: S108 - the server's, only read
        argv = ["sudo", "find", *tmp, "-mindepth", "1", "-maxdepth", "1", *users]
        return self.server.run([*argv, "-newermt", f"@{since}", "-print"]).stdout.split()

    def probe(self, code: str, data: bytes) -> tuple[dict[str, Any], list[str]]:
        """`code` in place of the checker, in a copy of the installed units, answering the
        client's request for `data`: (the client's answer, what is left of the copy after)."""
        name = self.copy = COPY + secrets.token_hex(6)
        template = self.server.read(f"{INSTALLED}/{UNIT}@.service")
        socket_unit = self.server.read(f"{INSTALLED}/{UNIT}.socket")
        exec_start = f"{PYTHON} -I -S -B -c {embedded(code)}"
        service, socket = copy_units(template, socket_unit, name, exec_start)
        units = json.dumps({"@.service": service, ".socket": socket})
        try:
            self.server.run(["sudo", "python3", "-I", "-c", INSTALL, name], input=units)
            got = self.send(Case("the probe", data), socket=f"/run/{name}/check.sock")
        finally:
            left = self.remove()
        return got, left

    def remove(self) -> list[str]:
        """Every copy removed (`REMOVE`): what is left."""
        return answer(self.server.run(["sudo", "python3", "-I", "-c", REMOVE]), "REMOVE")["left"]
