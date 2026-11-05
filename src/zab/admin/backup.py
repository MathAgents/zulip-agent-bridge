"""Encrypted backups of the admin server (docs/ADMIN.md §1.7, which lists what they hold and
leave out; #355, PR 13).

- **What.** A gzipped tar of the workshops' root `ws/`, whole: every workshop's directory, a
  retired one's too (§11.5), `retired-slugs.txt` and `audit.jsonl` (whole lines, under its lock),
  by their paths in the state directory, so that a restore unpacks it there
  (workshop/adminserver/README.md). Each member is a directory or a regular file with all its
  content, never a link. Whatever could be a private key, by name, place or content (PEM,
  OpenSSH, RFC 4716, PuTTY, DER), `exports/`, symbolic links and special files are left out, and
  named in the DM and the record.
- **How.** The tar is written straight into `age -e -r KEY…` (D28), whose ciphertext is read from
  its stdout: no plaintext reaches the disk, and nothing is left behind. An `age` that fails,
  stops reading, or writes less than the ciphertext of all it was given fails the backup. The
  plan's bot uploads the ciphertext alone, which skips the scrubber (§2.7), and DMs its link to
  each deployer.
- **When** (the runner's `_end`, after the result, under the workshop's apply lock): after an
  apply during which a backup's files changed (`state`), or any apply while one is owed: the
  last failed, or, at the bot's start, the files differ from the state the last sent backup
  recorded (`observe`, `resume`), whatever came between (a crash mid-apply). One at a time. A
  failure is said in the plan's topic and recorded; it never changes the apply.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import errno
import fcntl
import hashlib
import io
import json
import logging
import os
import re
import stat
import subprocess
import tarfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import IO, TYPE_CHECKING, Any

from zab.admin.audit import Head, line_hash
from zab.admin.checks import Modes, check_file
from zab.admin.reads import footer, short
from zab.admin.scrub import span

if TYPE_CHECKING:
    from zab.admin.bot import AdminServer
    from zab.admin.planning import Job

LOG = logging.getLogger("zab.admin.backup")
TIMEOUT = 120.0  # seconds for `age` to take the whole tar; then it is killed
MAX_FILE = 64 * 1024 * 1024  # a larger file is left out, and named
UPLOAD_MIB = 25  # Zulip's default upload limit, until the realm has said its own
LEFT_SHOWN = 20  # names of what was left out, in the DM and the record
_AGE1 = re.compile(r"age1[ac-hj-np-z02-9]{58}", re.ASCII)  # bech32; a plugin's has another "1"
_BECH32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_SSH = re.compile(r"ssh-ed25519 ([A-Za-z0-9+/]{68})(?:[ \t][\x20-\x7e]*)?", re.ASCII)
_ED25519 = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20"  # the key blob's head; 32 bytes follow
_KEY_NAME = re.compile(r"id_(?!.*\.pub\Z).*|.*_(?:ed25519|rsa|ecdsa|dsa)(?:_sk)?|.*\.ppk", re.S)
_KEY_TEXT = (b"PRIVATE KEY-----", b"PRIVATE KEY ----", b"PuTTY-User-Key-File-")  # RFC 4716 too
_DIRS = {"keys": "a key directory", ".ssh": "a key directory", "exports": "participants' exports"}
_HEADER = b"age-encryption.org/v1\n"


class BackupError(Exception):
    """A backup can't be made or sent: one line of its own words."""


def recipients(text: str) -> tuple[str, ...]:
    """The keys of `backup_recipients`, as `age -r` takes them: `age1…`, or `ssh-ed25519 KEY`
    without its comment. Blank lines and `#` comments are skipped, as `age -R` does; anything
    else is refused: an `ssh-rsa` key, a FIDO `sk-ssh-ed25519` key (it can't decrypt, §1.7), a
    plugin's recipient, a repeat. At least one key."""
    keys: list[str] = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key = line if _AGE1.fullmatch(line) and _bech32(line) else None
        if (m := _SSH.fullmatch(line)) is not None:
            try:
                blob = base64.b64decode(m[1], validate=True)
            except binascii.Error:
                blob = b""
            key = f"ssh-ed25519 {m[1]}" if len(blob) == 51 and blob.startswith(_ED25519) else None
        if key is None:
            raise BackupError(f"backup_recipients line {n}: not an ssh-ed25519 or age1 key")
        if key in keys:
            raise BackupError(f"backup_recipients line {n}: a key given twice")
        keys.append(key)
    if not keys:
        raise BackupError("backup_recipients holds no key")
    return tuple(keys)


def _bech32(key: str) -> bool:
    """BIP 173's checksum of an `age1…` key, which age checks only when it encrypts."""
    check = 1
    for value in [*(ord(c) >> 5 for c in "age"), 0, *(ord(c) & 31 for c in "age"),
                  *map(_BECH32.index, key[4:])]:  # fmt: skip
        top, check = check >> 25, (check & 0x1FFFFFF) << 5 ^ value
        for i, g in enumerate((0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)):
            check ^= g if top >> i & 1 else 0
    return check == 1


def read_recipients(path: str, modes: Modes) -> tuple[str, ...]:
    """`recipients` of the file, once it passes the checks admin.toml does (`checks`): root's,
    and nobody else may write it or a directory above it, or redirect the backups."""
    problem = check_file(path, modes)
    if problem:
        raise BackupError(problem)
    try:
        with open(path, "rb") as f:
            return recipients(f.read(65536).decode("utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise BackupError(f"{path}: {type(exc).__name__}") from None


@dataclass(frozen=True, slots=True)
class Member:
    arcname: str
    path: str
    why: str = ""  # why it is left out; "" when it goes in


def members(server: AdminServer) -> list[Member]:
    """What a backup holds, in order, directories before their contents, and what it leaves out
    by name or place: the workshops' root `ws/`, each workshop's directory whether or not
    admin.toml still names it, then `retired-slugs.txt`. Nothing is followed through a link."""
    top, found = server.paths.workshops, list[Member]()
    if os.path.islink(top):
        found = [Member("ws", top, "a symbolic link")]
    elif os.path.isdir(top):
        found = [Member("ws", top), *_tree(top, "ws")]
    return [*found, Member("retired-slugs.txt", server.paths.layout.retired)]


def _tree(top: str, arc: str) -> list[Member]:
    """Below `top`: a workshop's directory is never left out by its name (`keys`, `exports`)."""
    found: list[Member] = []

    def unreadable(exc: OSError) -> None:
        rel = os.path.relpath(exc.filename or top, top)
        found.append(Member(f"{arc}/{rel}", exc.filename or top, f"unreadable: {exc.strerror}"))

    for root, dirs, files in os.walk(top, onerror=unreadable):  # never into a linked directory
        base = arc + ("" if root == top else "/" + os.path.relpath(root, top))
        names = {} if root == top else _DIRS
        for name in sorted(dirs):
            path = os.path.join(root, name)
            why = "a symbolic link" if os.path.islink(path) else names.get(name, "")
            found.append(Member(f"{base}/{name}", path, why))
        dirs[:] = sorted(d for d in dirs if not (d in names or os.path.islink(f"{root}/{d}")))
        for name in sorted(files):
            why = "named like an SSH private key" if _KEY_NAME.fullmatch(name) else ""
            found.append(Member(f"{base}/{name}", os.path.join(root, name), why))
    return found


def digest(seen: tuple[Any, ...] | None) -> str | None:
    """`state`'s SHA-256, which a sent backup's record keeps; None for a state not read."""
    return hashlib.sha256(json.dumps(seen).encode()).hexdigest() if seen is not None else None


def state(server: AdminServer) -> tuple[Any, ...]:
    """What a backup would hold, by name, inode, size, mtime and mode: equal before and after
    an apply that wrote nothing there (`exports/` and the like don't count)."""
    seen: list[Any] = []
    for m in members(server):
        if m.why:
            seen.append((m.arcname, m.why))
            continue
        try:
            st = os.lstat(m.path)
            seen.append((m.arcname, m.why, st.st_ino, st.st_size, st.st_mtime_ns, st.st_mode))
        except OSError:
            seen.append((m.arcname, m.why))
    return tuple(seen)


@dataclass(slots=True)
class Made:
    """A backup, encrypted: its file name, ciphertext and what it holds."""

    name: str
    data: bytes = b""
    files: int = 0
    left: list[str] = field(default_factory=list)
    head: Head | None = None  # the audit log's, as the backup holds it
    state: str = ""  # `digest(state(…))` as the backup began

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


def _add(tar: tarfile.TarFile, m: Member) -> str:
    """Add one member: "" when it went in, else why not ("-" when it is gone)."""
    if m.why:
        return m.why
    try:
        if stat.S_ISDIR(os.lstat(m.path).st_mode):
            info, fd = tar.gettarinfo(m.path, m.arcname), None
        else:
            fd = os.open(m.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return "-"
    except OSError as exc:
        return "a symbolic link" if exc.errno == errno.ELOOP else f"unreadable: {exc.strerror}"
    if fd is None:
        tar.addfile(info)
        return ""
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return "not a regular file"
        data = f.read(MAX_FILE + 1) if st.st_size <= MAX_FILE else None
        if data is None or len(data) > MAX_FILE:
            return f"larger than {MAX_FILE // 2**20} MiB"
        if any(marker in data for marker in _KEY_TEXT) or _der_key(data):
            return "holds a private key"
        info = tar.gettarinfo(arcname=m.arcname, fileobj=f)
    _whole(tar, info, data)
    return ""


def _whole(tar: tarfile.TarFile, info: tarfile.TarInfo, data: bytes) -> None:
    """A regular file and all its content: `gettarinfo` makes a second name of an inode a hard
    link, whose data no reader expects (the review of #373)."""
    info.type, info.linkname, info.size = tarfile.REGTYPE, "", len(data)
    tar.addfile(info, io.BytesIO(data))


def _tlv(data: bytes, at: int) -> tuple[int, int, int] | None:
    """The DER element at `at`: its tag, where its content starts and where it ends."""
    if at + 2 > len(data):
        return None
    tag, size, at = data[at], data[at + 1], at + 2
    if size & 0x80:
        n = size & 0x7F
        if not 0 < n <= 4 or at + n > len(data):
            return None
        size, at = int.from_bytes(data[at : at + n], "big"), at + n
    return (tag, at, at + size) if at + size <= len(data) else None


def _der_key(data: bytes) -> bool:
    """A DER private key, the whole file one SEQUENCE: PKCS#1 (RSA, DSA) and PKCS#8 begin with
    version 0 or 1 and an INTEGER or the algorithm's SEQUENCE; SEC1 (EC), version 1 and an OCTET
    STRING; PKCS#12, version 3 and a SEQUENCE; an encrypted PKCS#8, a SEQUENCE that starts with
    an OID, then an OCTET STRING. A certificate or a public key has none of these shapes."""
    outer = _tlv(data, 0)
    if outer is None or outer[0] != 0x30 or outer[2] != len(data):
        return False
    first = _tlv(data, outer[1])
    second = _tlv(data, first[2]) if first else None
    if first is None or second is None:
        return False
    version = data[first[1] : first[2]]
    if first[0] == 0x02 and version in (b"\x00", b"\x01", b"\x03"):  # 3: PKCS#12, keys inside
        return second[0] in ((0x30,) if version == b"\x03" else (0x02, 0x30, 0x04))
    oid = _tlv(data, first[1]) if first[0] == 0x30 else None
    return oid is not None and oid[0] == 0x06 and second[0] == 0x04


def _audit(tar: tarfile.TarFile, path: str) -> Head | None:
    """The audit log, whole lines under its lock (appends take it exclusively), and its head."""
    try:
        f = open(path, "rb")  # closed below, after its tarinfo
    except FileNotFoundError:
        return None
    with f:
        fcntl.flock(f.fileno(), fcntl.LOCK_SH)
        data = f.read()
        info = tar.gettarinfo(arcname="audit.jsonl", fileobj=f)
    _whole(tar, info, data)
    last = data.rstrip(b"\n").rsplit(b"\n", 1)[-1]
    try:
        return Head(json.loads(last)["seq"], line_hash(last))
    except (ValueError, KeyError, TypeError):
        return None


class _Counted:
    """A pipe that counts the bytes written into it."""

    def __init__(self, pipe: IO[bytes]) -> None:
        self.pipe, self.bytes = pipe, 0

    def write(self, data: bytes) -> int:
        self.bytes += len(data)
        return self.pipe.write(data)

    def flush(self) -> None:
        self.pipe.flush()


def write(pipe: Any, server: AdminServer, made: Made) -> None:
    """The backup's tar, gzipped, into `pipe`; `made` counts what went in and what didn't."""
    with tarfile.open(fileobj=pipe, mode="w|gz", format=tarfile.PAX_FORMAT) as tar:
        for m in members(server):
            why = _add(tar, m)
            if why and why != "-":
                made.left.append(f"{m.arcname}: {why}")
        made.head = _audit(tar, server.paths.audit)
        made.files = sum(1 for info in tar.members if info.isfile())


class _Drain(threading.Thread):
    """A pipe read to its end, keeping at most `limit` bytes; past it, `over` and `kill`."""

    def __init__(self, pipe: IO[bytes], limit: int, kill: Callable[[], None]) -> None:
        super().__init__(daemon=True)
        self.pipe, self.limit, self.kill = pipe, limit, kill
        self.data, self.over = bytearray(), False
        self.start()

    def run(self) -> None:
        while chunk := self.pipe.read(65536):
            if self.over:
                continue  # read to the end, so the writer never blocks
            if len(self.data) + len(chunk) > self.limit:
                self.over = True
                self.kill()
            else:
                self.data += chunk


def make(server: AdminServer, keys: tuple[str, ...], limit: int) -> Made:
    """The backup, encrypted to `keys` with `age`, streamed: its ciphertext in memory, at most
    `limit` bytes. Run in a thread."""
    made = Made(time.strftime("backup-%Y%m%dT%H%M%SZ.tar.gz.age", time.gmtime(server.clock())))
    made.state = digest(state(server)) or ""  # before the tar: a later change only adds a backup
    argv = [server.paths.age, "-e", *(word for key in keys for word in ("-r", key))]
    try:  # validated keys, never a flag: no shell, no message text (CONTRIBUTING.md, rule 3)
        proc = subprocess.Popen(  # noqa: S603
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}, cwd="/",
        )  # fmt: skip
    except OSError as exc:
        raise BackupError(f"age can't be run ({exc.strerror})") from None
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    out, err = _Drain(proc.stdout, limit, proc.kill), _Drain(proc.stderr, 2000, lambda: None)
    slow, broken, sent = threading.Event(), False, _Counted(proc.stdin)
    timer = threading.Timer(TIMEOUT, lambda: (slow.set(), proc.kill()))
    timer.start()
    try:
        try:
            write(sent, server, made)
            proc.stdin.close()
        except BrokenPipeError:  # age stopped reading: whatever its exit, the backup failed
            broken = True
        proc.wait()
    finally:
        timer.cancel()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        with contextlib.suppress(OSError):
            proc.stdin.close()
        out.join()
        err.join()
    if slow.is_set():
        raise BackupError(f"age took over {TIMEOUT:.0f} s")
    if out.over:
        raise BackupError(f"it is over {limit // 2**20} MiB, the upload limit")
    if proc.returncode != 0:
        said = bytes(err.data).decode("utf-8", "replace").strip().splitlines()[:1] or [""]
        raise BackupError(f"age exited {proc.returncode}: {said[0][:300]}")
    if broken:
        raise BackupError("age stopped reading the backup before its end")
    # age's payload: a 16-byte nonce, then the input in 64 KiB chunks, each with a 16-byte tag
    least = len(_HEADER) + 16 + sent.bytes + 16 * max(1, -(-sent.bytes // 65536))
    if not out.data.startswith(_HEADER) or len(out.data) < least:
        raise BackupError("age's output is not the whole backup's ciphertext")
    made.data = bytes(out.data)
    return made


class Backups:
    """The bot process's backups (module doc)."""

    def __init__(self, server: AdminServer) -> None:
        self.server = server
        self.lock = asyncio.Lock()
        self.failed = False  # a backup is owed: the next apply makes one, changed or not
        self.sent: str | None = None  # the state the last sent backup recorded
        self.applied = False  # the log has an apply

    def observe(self, record: Mapping[str, Any]) -> None:
        """At start, each audit record in turn: the last sent backup's state, and whether any
        apply has begun."""
        if record.get("event") == "backup" and record.get("outcome") == "sent":
            self.sent = str(record.get("state") or "")
        self.applied = self.applied or record.get("event") == "apply_start"

    def resume(self) -> None:
        """At start, after `observe`: a backup is owed when applies have run and none was ever
        sent, or when the files differ from the state the last sent backup recorded. That covers
        a failed backup, an apply a stop interrupted, and a crash, which writes no record at all
        (Codex rounds 3 and 4 on #373). Run in a thread."""
        if self.sent is None:
            self.failed = self.applied
        else:
            now = digest(self.state())
            self.failed = now is None or now != self.sent

    def state(self) -> tuple[Any, ...] | None:
        """`state`, or None when it can't be read: then a backup follows the apply."""
        try:
            return state(self.server)
        except Exception as exc:
            LOG.warning("what a backup holds can't be read (%s)", type(exc).__name__)
            return None

    async def after(self, job: Job, before: tuple[Any, ...] | None) -> None:
        """After an apply's result: a backup if the apply changed what it holds, or the last
        one failed. Nothing here can reach the apply."""
        try:
            now = await asyncio.to_thread(self.state)
            changed = before is None or now is None or now != before
            if changed or self.failed:
                async with self.lock:
                    await self.send(job)
        except Exception as exc:
            LOG.warning("%s: the backup failed (%s)", job.plan.name, type(exc).__name__)

    async def send(self, job: Job) -> None:
        server, plan, bot = self.server, job.plan, job.bot
        made, why, sent, unsent = None, "", list[int](), list[int]()
        try:
            keys = await asyncio.to_thread(read_recipients, server.paths.recipients, server.modes)
            limit = bot.client.limits.get("max_file_upload_size_mib", UPLOAD_MIB) * 2**20
            made = await asyncio.to_thread(make, server, keys, limit)
            url = await bot.client.upload(made.name, made.data)  # the ciphertext, and only it
        except BackupError as exc:
            why = str(exc)
        except Exception as exc:  # the upload's: never its text
            why = f"{'the upload' if made else 'it'} failed ({type(exc).__name__})"
        if made and not why:
            text = self.text(job, made, f"[{made.name}]({url})")
            for uid in sorted(server.config.deployers):
                done = await bot.send(type="private", to=[uid], content=text)
                (sent if done is not None else unsent).append(uid)
            why = f"not sent to user {', '.join(map(str, unsent))}" if unsent else ""
        self.failed = bool(why)
        record: dict[str, Any] = {"outcome": "failed" if why else "sent", "to": sent}
        if made and made.data:
            record |= {"file": made.name, "bytes": len(made.data), "sha256": made.sha256,
                       "files": made.files, "left_out": made.left[:LEFT_SHOWN], "state": made.state,
                       "holds": str(made.head) if made.head else None}  # fmt: skip
        head = None
        try:
            head = await server.audit(
                "backup", workshop=plan.workshop, plan=plan.name, bot=plan.bot,
                **record, **({"reason": why} if why else {}),
            )  # fmt: skip
        except Exception as exc:
            LOG.warning("%s: the backup record failed (%s)", plan.name, type(exc).__name__)
        if why:
            text = (f"⚠ **{plan.name}**: the encrypted backup after it failed: {why}. The apply "
                    "is unaffected; a deployer should look (docs/ADMIN.md §1.7).")  # fmt: skip
            await bot.post(job.stream, job.topic, f"{text}\n{footer(head)}" if head else text)

    @staticmethod
    def text(job: Job, made: Made, link: str) -> str:
        plan = job.plan
        holds = f"the audit log through `{made.head}`" if made.head else "no audit log"
        lines = [
            f"🔐 Encrypted backup of the admin server after **{plan.name}** · "
            f"`{short(plan.command)}` ({plan.workshop}): {link}",
            f"{made.files} files, with {holds}; {len(made.data):,} bytes, sha256 "
            f"`{made.sha256[:16]}…`. Decrypt it with your key: `age -d -i ~/.ssh/KEY -o "
            f"backup.tar.gz {made.name}`; to restore, workshop/adminserver/README.md, "
            '"Backups and recovery".',
        ]
        if made.left:
            more = len(made.left) - LEFT_SHOWN
            shown = ", ".join(span(x) for x in made.left[:LEFT_SHOWN])
            lines.append(f"Left out: {shown}" + (f" and {more} more" if more > 0 else "") + ".")
        return "\n".join(lines)
