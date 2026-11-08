#!/usr/bin/env python3
"""`archive_check.py serve --peer zab-admin`: one participant's archive, checked and re-packed
(README.md §7.2, step 3), in `zab-archive-check@.service`, the sandbox of #374 (no
network, no key, no state; docs/ADMIN.md §1.2). `zab-archive-check.socket` starts one instance
per connection, `collect_exports.py`'s, and passes it the connection. The caller sends one line,
`repack <budget>`, with the archive's descriptor, read-only (`request`); the answer is framed
(`Frames`): the re-packed archive and its end, or a refusal. `selfcheck` re-packs a small archive
in memory, for `deploy.sh`.

**What it claims.** What `collect_exports.py` stores is not the participant's archive but one
that this program's own writer produced (`Repack`), with `tarfile`, from the members the check
approved. It holds only regular files and directories:
- a regular member becomes a regular file; a hard link, a copy of the earlier regular file it
  names (refused if there is none); a directory, a directory. Nothing else is written;
- each name is the member's, normalized (`posixpath.normpath`): relative, with no `.`, `..` or
  empty component, and none a credential's (`CREDENTIAL`) in any case. A component Windows
  can't take as it is (`portable`: a character it forbids, a backslash and `:` among them, a
  trailing dot or space, a device name such as `CON`, `conin$` or `com1.txt`) is renamed, those
  characters and `%` written `%XX`, and one still over `NAME_MAX` bytes cut short and ended
  with a hash; `RENAMED.txt` at the top lists each renamed member's name and its new one;
- modes are 0644, or 0755 for a file its owner could run and for a directory; owner and group
  are 0 and unnamed; GNU format, with no PAX attributes, device numbers or extended attributes;
- symlinks are not written. `SYMLINKS.txt` at the top lists each one's name and target, so that
  the participant can recreate them. No member may be at or under either list's name
  (`RESERVED`).

GNU tar and `tarfile` list it alike. Extracted by either into an empty directory, on Linux, macOS
or Windows, it puts nothing outside that directory, and no **file** at a credential's path
(`auth.json`, `.credentials.json`, `*.zuliprc`, `.config/zab/`): it writes only those files and
directories, and the directories their names imply, which may lie at such a path (a member
`.codex/auth.json/x` makes a directory `auth.json`). That holds on a case-sensitive filesystem,
and on a case-insensitive one such as APFS or NTFS: names are compared folded (`fold`), which
covers their comparison and may equate more names (`ß` and `ss`), which only refuses more; and
renamed names hold nothing Windows reads as a separator, a drive, a stream (`::$DATA`), a device
or a name it trims. Not claimed: a credential under another name, or inside a nested archive (a
zip or a tar within the tar), which no name check can see; HFS+, which also ignores some code
points in a name.

**The sandbox and the limits are the guarantee.** The archive comes from a VM the participant
controls, so it is untrusted input to `gzip` and `tarfile`. The unit runs each instance as a
user of its own, allocated for it (`DynamicUser`), with no network, no access to the admin
server's keys or state, nothing writable, and a system call filter; and with memory, CPU and
time limits. Before it reads a byte of the archive, this process also limits itself (`limit`)
to `MEMORY` of address space, `CPU_SECONDS` of CPU, no file written, no core dump and
`OPEN_FILES` files. It answers only root and the `--peer` users (`SO_PEERCRED`). The caller
adds a wall-clock timeout, bounds what it reads, and stores the archive only if the answer ends
as `Frames` ends it. A parser pathology ends in a refusal, never in an exhausted server. A
parser exploit, code run in the checker, gets an empty sandbox: no key, no network, no socket
(neither `socket()` nor io_uring), no other archive. **The claim above holds as long as the
checker runs its own code:** such code could answer any bytes as the re-pack, and
`collect_exports.py` stores what comes, unexamined. They go back to the participant, who
controls the input anyway. **The checks give the refusal its
reason.** Both passes read the archive with `tarfile`, the headers through `_Stream`, under the
same rules (`_read`):
- a whole gzip stream, its CRC and length checked at its end;
- headers to the end-of-archive marker: from where `tarfile` stops to the end of the stream
  only zeros, at least two blocks of them (`tarfile` reads a corrupt header after the first
  member as the archive's end);
- each header in canonical form: every numeric field base-256, or octal digits between spaces
  and NULs, nothing else (`_canonical`), so that no reader can take a size or a type
  differently (`tarfile` reads `0o…` and stops at a NUL; GNU tar skips a leading NUL);
- no NUL in a member's name or link name, no `GNU.sparse.*` attribute, no sparse member;
- **no name or link target read two ways** (`_alike`, #888), since GNU tar and tarfile choose
  differently among the headers that give one: before a member, at most one GNU `L` record,
  one `K` record and one PAX header; no global PAX `path` or `linkpath`; a PAX `path` or
  `linkpath` only as tarfile reads the name or target; and no `prefix` field that tarfile
  joins to the name and GNU tar doesn't. `answer` first reads a built-in archive (`seen`), and
  refuses every archive if tarfile didn't show `_Header` each of its headers;
- at most `META` bytes of headers and metadata before each member, consecutive extension
  headers included; at most `GLOBAL_META` of global PAX attributes, which `tarfile` copies for
  every later member; at most `--budget` bytes decompressed in all;
- **no member goes through a link** (`_through`): no name or hard link target is absolute or
  has a `..` component, and none equals or lies under the name of an earlier link member: of
  a symlink, names compared folded; of a hard link, names compared as written, normalized only
  as paths (#819);
- **no name is a credential's** (`credential`): none matches `CREDENTIAL`, as written or as a
  path normalizes it, folded;
- no name or link target of more than `MAX_DEPTH` components (`_through` costs their square);
- no member at or under a `RESERVED` name, no member but a directory named `.`, at most
  `MAX_LINKS` hard links and `LINK_BYTES` of their targets, held from the first pass, and at
  most `MANIFEST_BYTES` of symlink names and targets and renamed names, counted before escaping
  (up to four times that escaped), held until the end (`_Rules.kept`).
The first pass (`scan`) reads only the headers, each member's data skipped, and notes the hard
links' targets; an archive it refuses gets no output. The second (`Repack`) reads the archive
again from its start and writes the new one as it goes: each regular member's data copied
once, each hard link's from a second reader of the same file, and at most `--budget` bytes
written, copies included.

Every refusal is framed, whatever raises it, `MemoryError` included (`answer`). The refusal is
one line, with at most the end of one name, in printable ASCII. Standard library only: it runs
as `python -I -S -B`, which puts no directory of this tree, and no site-packages, on its path.
"""

from __future__ import annotations

import argparse
import array
import fcntl
import gzip
import hashlib
import io
import os
import posixpath
import pwd
import re
import resource
import socket
import stat
import struct
import tarfile
import unicodedata
import zlib
from collections.abc import Callable
from typing import BinaryIO

#: What `export.sh` and the check (README.md §7.2, step 3) refuse: no credential's name in an
#: archive. `credential` matches it against folded names (`fold`), so `.codex/AUTH.JSON` is one too.
CREDENTIAL = re.compile(
    r"(^|/)(auth\.json|\.credentials\.json|[^/]*\.zuliprc)$|(^|/)\.config/zab(/|$)"
)
CHUNK = 1 << 20
TAIL = 1024  # bytes of the stream kept for the check of its end
#: Headers and metadata read before one member, consecutive extension headers included. GNU
#: tar's long names are at most PATH_MAX and its PAX headers small. 64 KiB of 8-byte PAX
#: records cost tarfile 4.5 ms of CPU per member, where 1 MiB of them cost 124 ms (the development
#: machine, Zen 5; the review of #360): RLIMIT_CPU then takes some 130,000 such members, about 20
#: MB.
META = 64 << 10
#: Global PAX attributes: tarfile copies them for every later member, so their number
#: multiplies the work per member (the review of #360: 1 MB of them cost 6.3 ms per member).
#: `export.sh`'s GNU-format archives have none; 1 KiB leaves room for a tool that adds a few,
#: and bounds the cost to about 30 µs per member (measured on the development machine, Zen 5).
GLOBAL_META = 1 << 10
MEMORY = 256 << 20  # RLIMIT_AS
CPU_SECONDS = 600  # RLIMIT_CPU: 1 GiB of small files re-packs in about 12 s (Zen 5)
OPEN_FILES = 32
#: The lists of symlinks and of renamed members, at the top of the new archive; no member may
#: be at or under either.
MANIFEST, RENAMES = "SYMLINKS.txt", "RENAMED.txt"
RESERVED = (MANIFEST, RENAMES)
MANIFEST_BYTES = 8 << 20  # symlink names and targets, and renamed names, before escaping
MAX_LINKS = 100_000  # hard links: their targets are held from the first pass
LINK_BYTES = 16 << 20  # and those targets' bytes
#: Components in a name or link target: `_through` costs their square (the review of #383: a
#: 1.3 KB gzip of ten 30,000-component names took 37.9 s of CPU; at 1,024, about 7 MB of
#: crafted names still reached RLIMIT_CPU). Honest trees are far below: a Lean `.lake` has 14
#: below it, npm's flattened node_modules tens, and 256 components take 511 characters at
#: least, twice Windows' MAX_PATH.
MAX_DEPTH = 256
NAME_MAX = 255  # bytes in a name component, on Linux and macOS (and UTF-16 units, on Windows)
#: gzip's level: on text, twice as fast as its default (6), for 6% more bytes (the development
#: machine)
LEVEL = 3
#: ustar's numeric fields (offset, length): mode, uid, gid, size, mtime, chksum, devmajor,
#: devminor. Each is base-256 or octal digits between spaces and NULs (`_canonical`).
NUMERIC = ((100, 8), (108, 8), (116, 8), (124, 12), (136, 12), (148, 8), (329, 8), (337, 8))
_OCTAL = re.compile(rb" *[0-7]*[ \0]*")


def fold(name: str) -> str:
    """`name` folded so that two names a case-insensitive filesystem equates fold alike (a
    Mac's default APFS, which ignores Unicode normalization too; the review of #360 at
    162c432): Unicode's canonical caseless form, decomposed, casefolded and decomposed again.
    `work/L` is `work/l`, `AUTH.JSON` is `auth.json`, and a composed `é` is a decomposed one.
    It covers APFS's comparison and may equate more (`ß` and `ss`), which only refuses more."""
    return unicodedata.normalize("NFD", unicodedata.normalize("NFD", name).casefold())


def credential(name: str) -> bool:
    """Whether `name` names a credential, as written or as extracted: GNU tar and tarfile
    drop a trailing slash, `.` and empty components (Codex on #360: a regular member
    `auth.json/` is extracted as `auth.json`), `..` is resolved as a path would be, and both
    are folded (`fold`): on a case-insensitive filesystem, `AUTH.JSON` is `auth.json`."""
    name = fold(name)
    return bool(CREDENTIAL.search(name) or CREDENTIAL.search(posixpath.normpath(name or ".")))


class Refused(Exception):
    """The archive is refused: one line, its reason."""


def shown(name: str) -> str:
    """The end of a member's name, printable ASCII only: it reaches a table and a chat."""
    tail = name[-80:]
    return "".join(c if " " <= c <= "~" else "?" for c in tail)


def _header(block: bytes) -> bool:
    """Whether a 512-byte block passes tarfile's own checksum test, as a header does: the
    unsigned sum first, in C (`sum`), then tarfile's own test for the signed one."""
    field = block[148:156]
    try:
        value = tarfile.nti(field)
    except (tarfile.HeaderError, ValueError):
        return False
    return value == sum(block) - sum(field) + 256 or value in tarfile.calc_chksums(block)


def _canonical(block: bytes) -> bool:
    return all(
        block[at] in (0x80, 0xFF) or _OCTAL.fullmatch(block[at : at + size]) for at, size in NUMERIC
    )


class _Stream:
    """The decompressed stream as tarfile reads it, in its seekable mode, so that tarfile
    skips each member's data rather than reading it: forward only; each `read` is a header or
    a header's metadata, and at most `META` of them before each member (`since`, which the loop
    resets); every header canonical; at most `budget` bytes decompressed in all, skips and the
    end's zeros included. It keeps the last `TAIL` bytes, for the check of the archive's end,
    and the type of each header tarfile has read since the loop's reset (`kinds`, `_Header`).
    A refusal is kept here too: some tarfile versions wrap it in a ReadError (3.13.16)."""

    def __init__(self, raw: BinaryIO, budget: int) -> None:
        self.raw, self.budget = raw, budget
        self.pos, self.since, self.tail = 0, 0, b""
        self.kinds: list[bytes] = []
        self.block = (-1, b"")  # the last block read, and where it starts (`_Header`)
        self.refused = ""

    def refuse(self, why: str) -> Refused:
        self.refused = self.refused or why
        return Refused(why)

    def take(self, size: int) -> bytes:
        data = self.raw.read(min(size, self.budget + 1 - self.pos))
        self.pos += len(data)
        if self.pos > self.budget:
            raise self.refuse(f"it expands past {self.budget >> 20} MB")
        self.tail = (self.tail + data[-TAIL:])[-TAIL:]
        return data

    def read(self, size: int = -1) -> bytes:
        if size < 0 or self.since + size > META:
            raise self.refuse(f"over {META >> 10} KB of headers and metadata before one member")
        self.since += size
        at, data = self.pos, self.take(size)
        if len(data) == tarfile.BLOCKSIZE:
            if not _canonical(data) and _header(data):
                raise self.refuse("a header's numbers are not in canonical form")
            self.block = (at, data)
        return data

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = 0) -> int:
        if whence != 0 or offset < self.pos:
            raise self.refuse("its headers point backwards")
        while self.pos < offset and self.take(min(CHUNK, offset - self.pos)):
            pass
        return self.pos


def _global_size(attributes: dict[str, str]) -> int:
    return sum(len(key) + len(value) for key, value in attributes.items())


def _member(member: tarfile.TarInfo, extended: bool) -> str:
    """Why this member is refused, or "". `extended`: extension headers came before it, so its
    PAX attributes, the global ones included, may have changed since the last member's."""
    if "\0" in member.name or "\0" in member.linkname:
        return f"a NUL in a member's name ({shown(member.name)})"
    if max(member.name.count("/"), member.linkname.count("/")) >= MAX_DEPTH:
        return f"a name of over {MAX_DEPTH} components ({shown(member.name)})"
    sparse = extended and any(key.startswith("GNU.sparse.") for key in member.pax_headers)
    if sparse or member.type == tarfile.GNUTYPE_SPARSE or member.sparse is not None:
        return f"a sparse member, which export.sh never makes ({shown(member.name)})"
    if credential(member.name):
        return f"it names a credential file ({shown(member.name)})"
    return ""


class _Header(tarfile.TarInfo):
    """A member as tarfile reads it, with what `_alike` needs and tarfile keeps to itself.
    `_proc_member`, the entry point tarfile leaves to a subclass, is called for each header
    just after tarfile reads it (`_Stream.block`, which starts at the header's `offset`), and
    notes:
    - the header's type on the stream (`_Stream.kinds`), so the types of a member's headers,
      first to last;
    - `dropped`: the header has a `prefix` field, which tarfile joins to the name whatever the
      magic, and a magic other than POSIX's (`ustar\\0`), without which GNU tar doesn't. It is
      None when the block read last isn't the header's.
    `seen` checks that tarfile still calls it so."""

    dropped: bool | None = None

    def _proc_member(self, tar: tarfile.TarFile) -> tarfile.TarInfo:
        at, block = tar.fileobj.block  # type: ignore[attr-defined]
        tar.fileobj.kinds.append(self.type)  # type: ignore[attr-defined]
        if at == self.offset:
            prefix = bool(block[345]) and self.type not in tarfile.GNU_TYPES  # tarfile joins it
            self.dropped = prefix and block[257:263] != b"ustar\0"
        return super()._proc_member(tar)  # type: ignore[misc]


_PAX = (tarfile.XHDTYPE, tarfile.SOLARIS_XHDTYPE)  # a member's own PAX header, either type
_RECORDS = (tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK)  # GNU's `L` and `K`
#: The refusals when tarfile doesn't show `_Header` its headers: `seen` failed, or it passed
#: and a member's own header wasn't shown all the same (`_alike`)
HIDES = "this Python's tarfile hides its headers from the check"
HIDDEN = f"{HIDES} (its self-check failed)"


def _alike(member: tarfile.TarInfo, kinds: list[bytes], attributes: dict[str, str]) -> str:
    """Why GNU tar and tarfile may read this member's name or link target differently, or ""
    (#888): `kinds`, the types of its headers (`_Header`); `attributes`, the global PAX ones so
    far. Where each reads them from (GNU tar 1.35; the verification log, 2026-11-06):
    - **GNU tar:** the member's PAX header (`path` unless empty), else the last global one,
      else its `L` or `K` record, else its own header, whose `prefix` it joins to the name only
      under POSIX's magic; of two PAX headers, two global ones or two records of a kind, it
      reads only the last;
    - **tarfile:** the first of its records and PAX headers to give one, else the global
      headers so far, all of them, else its own header, its `prefix` joined whatever the magic.
    So #360's two targets, a `K` record naming `a` and then a PAX `linkpath=c`, are `a` to
    tarfile and `c` to GNU tar. An `L` record naming `x`, then a PAX `path=c`, name a symlink
    `x` and `c`: a later hard link to `c` was a copy of a file for tarfile, and a link to that
    symlink for GNU tar (the review of #892, B2).

    So a member is refused with two `L` records, two `K` records or two PAX headers before it;
    after a global PAX `path` or `linkpath`; with a PAX `path` that is empty or, trailing
    slashes aside, not the name tarfile read, or a PAX `linkpath` other than the target it
    read; or with a `prefix` that GNU tar drops (`_Header.dropped`) when its own header names
    it. Then both take each name and target from the same place. Both take the type from the
    header, so they see the same symlinks and hard links, with the same names and targets but
    for trailing slashes, which `_through` normalizes away. So `_through`'s rules hold for GNU
    tar's reading too: a hard link that it matches by its exact name (#819) is the same link to
    either reader.

    GNU tar, tarfile and libarchive (bsdtar) write one `L` and one `K` record at most in GNU
    format, one PAX header in PAX format, and pass, in each format tried (the verification
    log), but for GNU tar's incremental archives (`--incremental`, `--listed-incremental`):
    their headers hold an access time where POSIX's hold `prefix`, which tarfile joins to every
    name (`15273376256/work/a.md`), and they are refused. `export.sh` makes none."""
    dropped = getattr(member, "dropped", None)  # `_Header`'s
    if not kinds or kinds[-1] != member.type or dropped is None:  # not shown `_Header`
        return f"{HIDES} (it couldn't see the headers of {shown(member.name)})"
    twice = any(kinds.count(kind) > 1 for kind in _RECORDS) or sum(k in _PAX for k in kinds) > 1
    path = member.pax_headers.get("path")
    named = path is not None or tarfile.GNUTYPE_LONGNAME in kinds  # not by its own header
    if (
        twice
        or "path" in attributes
        or "linkpath" in attributes
        or (path is not None and (path.rstrip("/") or None) != member.name)
        or member.pax_headers.get("linkpath", member.linkname) != member.linkname
        or (dropped and not named)
    ):
        what = "a name or link target that GNU tar and tarfile can read two ways"
        return f"{what} ({shown(member.name)})"
    return ""


def _where(name: str) -> str:
    """A name or link target as the symlink rule compares it: folded, then normalized."""
    return posixpath.normpath(fold(name) or ".")


def _under(path: str, names: set[str]) -> bool:
    """Whether `path` (normalized) is one of `names` or lies under one."""
    parts = path.split("/")
    return any("/".join(parts[:i]) in names for i in range(1, len(parts) + 1))


def _through(member: tarfile.TarInfo, symlinks: set[str], hard: set[str]) -> str:
    """Why this member breaks the claim's rule 1, or "", and the link members recorded: the
    symlinks in `symlinks` (folded, `_where`), the hard links in `hard` (as written, `_normal`).
    The rule applies alike to the member's name and to a hard link's target:
    - **absolute:** tarfile's filters and GNU tar strip the leading `/`, where `normpath`
      keeps it (the review of #360 at 162c432: a symlink `/work/l`, then a file `work/l`);
    - **`..`:** refused before any normalization, since extraction resolves
      `work/l/../zab/key` through the symlink `work/l`, where `normpath` reads `work/zab/key`
      (Codex round 5 on #360);
    - **at or under an earlier link member**, symlink or hard link: tarfile writes a file at
      `work/l` through the symlink `work/l` (the approval of #360), and links `work/h` to the
      symlink `work/l` when the target is `work/a/l` and `work/a` → `.` (Codex round 6 on
      #360), so a hard link to a link is refused too. A hard link counts as a link because the
      readers may see two targets: a GNU `K` record naming `a` and a PAX `linkpath=c` make `h`
      a link to the file `a` for tarfile and to the symlink `c` for GNU tar (the re-review of
      #360 at 7d6801c), a link `_alike` now refuses itself (#888). A symlink's name is
      compared folded (on a Mac, `work/L` is `work/l`); a hard link's as written (#819).
    GNU tar's `--create` and tarfile's `add` make none of these from a tree (they strip the
    `/`, write no `..`, name each path once and link each hard link to the first, regular,
    member of its inode) but a hard link to a symlink, which needs two hard links to one
    symlink in the tree. Folding refuses an archive from a case-sensitive filesystem only if
    a member lies at or under a symlink's name spelled in another case.

    Why a hard link's name is not folded (#819): either writer links each later name of an
    inode to the first one it meets, in directory order, so with case twins `a.md` and `A.md`
    and a second name `h.md` of `a.md`, the order `h.md`, `a.md`, `A.md` put `A.md` at a hard
    link's name, folded: refused, and refused again at a retry, which lists the same order
    (#802). Folding protected nothing there: the participant's archive is read, never
    extracted, and deleted (#374), and its re-pack, which holds no link, is the same plain files
    in every order. Nor for an extraction of the participant's archive: no member there has a
    name or link target that GNU tar reads otherwise (`_alike`, #888), so GNU tar sees the
    symlinks and hard links this rule checks, and no hard link has a target at or under an
    earlier symlink in any case. On a case-insensitive filesystem, a name under a hard link's
    twin then never lies under a symlink."""
    paths = [member.name, member.linkname] if member.islnk() else [member.name]
    if any(path.startswith("/") for path in paths):
        return f"an absolute name or link target ({shown(member.name)})"
    if any(".." in path.split("/") for path in paths):
        return f"a `..` in a member's name ({shown(member.name)})"
    for at, path in enumerate(paths):
        if _under(_where(path), symlinks) or _under(_normal(path), hard):
            what = "a member written" if at == 0 else "a hard link to or"
            return f"{what} through an earlier symlink or hard link ({shown(member.name)})"
    if member.issym():
        symlinks.add(_where(member.name))
    elif member.islnk():
        hard.add(_normal(member.name))
    return ""


def _normal(name: str) -> str:
    """A member's name, or a hard link's target, as the new archive writes it."""
    return posixpath.normpath(name or ".")


class _Rules:
    """The rules that span members: the link members so far (`_through`), and the re-pack's
    own (`kept`)."""

    def __init__(self) -> None:
        self.symlinks: set[str] = set()  # the symlinks' names, as `_where` reads them
        self.hardlinks: set[str] = set()  # the hard links' names, as `_normal` reads them
        self.hard = self.link_bytes = 0  # hard links so far, and their targets' length
        self.manifest = 0  # bytes of symlink names and targets, and renamed names, so far

    def kept(self, member: tarfile.TarInfo) -> str:
        """Why the re-pack can't keep this member, or ""."""
        name = _normal(member.name)
        top = fold(name).split("/")[0]
        if reserved := next((r for r in RESERVED if top == fold(r)), ""):
            return f"a member at {reserved}, a list's name ({shown(member.name)})"
        if name == "." and not member.isdir():
            return f"a member named . that is not a directory ({shown(member.name)})"
        if member.islnk():
            self.hard += 1
            self.link_bytes += len(member.linkname)
            if self.hard > MAX_LINKS:
                return f"over {MAX_LINKS} hard links"
            if self.link_bytes > LINK_BYTES:
                return f"over {LINK_BYTES >> 20} MB of hard link targets"
        if member.issym():
            self.manifest += len(member.name) + len(member.linkname) + 2
        elif name != "." and (new := renamed(name)) != name:
            if credential(new):
                return f"it names a credential file once renamed ({shown(member.name)})"
            self.manifest += len(name) + len(new) + 2
        if self.manifest > MANIFEST_BYTES:
            return f"over {MANIFEST_BYTES >> 20} MB of symlink and renamed names"
        return ""


#: What Windows can't take in a name component (or the renaming uses: `%`), and its device
#: names, with any extension (Microsoft's "Naming Files, Paths, and Namespaces").
_WINDOWS = re.compile(r'[\x00-\x1f"*:<>?\\|%]')
_TRAILING = re.compile(r"[. ]+$")
_DEVICE = re.compile(
    r"(con|prn|aux|nul|conin\$|conout\$|com[0-9\u00b9\u00b2\u00b3]|lpt[0-9\u00b9\u00b2\u00b3])"
    r" *(\..*)?",
    re.I,
)


def _percent(text: str) -> str:
    return "".join(f"%{b:02X}" for b in text.encode("utf-8", "surrogateescape"))


def portable(component: str) -> str:
    """`component` as Windows takes it too, unchanged if it can: each character Windows forbids
    (control characters and `\\ : * ? " < > |`), `%`, and trailing dots and spaces, written as
    `%XX` (UTF-8, upper case); a device name with the first letter so. Since `%` itself is
    written `%25`, two components stay two: no renamed name is another's, renamed or not.

    A component that would then pass `NAME_MAX` bytes, which Linux can't extract (Codex on
    #383: 254 `a` and a `%`), is cut short at a character and ends `%~` and 32 hex digits of the
    original's SHA-256. No other component has `%~` (an encoded `%` is `%25`), so such names
    stay apart from the rest, and from each other but for a 128-bit collision."""
    head = _TRAILING.sub(
        lambda m: _percent(m[0]), _WINDOWS.sub(lambda m: _percent(m[0]), component)
    )
    if _DEVICE.fullmatch(component):
        head = _percent(head[0]) + head[1:]
    if len(head.encode("utf-8", "surrogateescape")) <= NAME_MAX:
        return head
    raw = component.encode("utf-8", "surrogateescape")
    tail = "%~" + hashlib.sha256(raw).hexdigest()[:32]
    room, cut = NAME_MAX - len(tail), 0
    for at, char in enumerate(head):
        room -= len(char.encode("utf-8", "surrogateescape"))
        if room < 0:
            break
        cut = at + 1
    return head[:cut] + tail


def renamed(name: str) -> str:
    """A normalized name, each component `portable`."""
    return "/".join(portable(component) for component in name.split("/"))


def _read(raw: BinaryIO, budget: int, each: Callable[[tarfile.TarInfo, _Stream], None]) -> str:
    """One pass over the gzip tar in `raw`, from its start, under every rule (module doc): why
    it is refused, or "". `each` gets each member that passes, with the stream positioned at its
    data; it may read that data (`_Stream.take`), and may raise `Refused`."""
    stream: _Stream | None = None
    rules = _Rules()
    try:
        with gzip.GzipFile(fileobj=raw, mode="rb") as gz:
            stream = _Stream(gz, budget)  # type: ignore[arg-type]
            with tarfile.open(fileobj=stream, mode="r:", tarinfo=_Header) as tar:  # type: ignore[call-overload]
                while (member := tar.next()) is not None:
                    tar.members.clear()  # tarfile would keep every member read
                    extended = stream.since > tarfile.BLOCKSIZE  # extension headers came
                    kinds, stream.since, stream.kinds = stream.kinds, 0, []
                    if extended and _global_size(tar.pax_headers) > GLOBAL_META:
                        raise Refused(f"over {GLOBAL_META >> 10} KB of global PAX attributes")
                    if why := (
                        _member(member, extended)
                        or _alike(member, kinds, tar.pax_headers)
                        or _through(member, rules.symlinks, rules.hardlinks)
                        or rules.kept(member)
                    ):
                        return why
                    each(member, stream)
                if stream.refused:  # whatever tarfile made of it
                    return stream.refused
                end = tar.offset  # where tarfile stopped: the marker, or what it couldn't read
            read = stream.pos - end  # the bytes from there that tarfile has read
            if read > len(stream.tail) or stream.tail[len(stream.tail) - read :].strip(b"\0"):
                return "malformed tar data after its last member"
            zeros = read
            while chunk := stream.take(CHUNK):  # to the end, where gzip checks CRC and length
                if chunk.strip(b"\0"):
                    return "malformed tar data after its last member"
                zeros += len(chunk)
            if zeros < 2 * tarfile.BLOCKSIZE:
                return "no end-of-archive marker"
    except Refused as exc:
        return str(exc)
    except (tarfile.TarError, OSError, EOFError, zlib.error, RecursionError) as exc:
        if stream is not None and stream.refused:
            return stream.refused
        return f"not a whole gzip tar ({type(exc).__name__})"
    return ""


def scan(raw: BinaryIO, budget: int) -> tuple[str, set[str]]:
    """The first pass, on the headers only: why the archive in `raw` (gzip) is refused, or "",
    and the names its hard links point to, normalized."""
    targets: set[str] = set()

    def each(member: tarfile.TarInfo, stream: _Stream) -> None:
        if member.islnk():
            targets.add(_normal(member.linkname))

    return _read(raw, budget, each), targets


class _PRead(io.RawIOBase):
    """The file at `fd`, read with `pread` from a position of its own, so that two readers of
    one file never move each other."""

    def __init__(self, fd: int) -> None:
        super().__init__()
        self.fd, self.pos = fd, 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:  # type: ignore[no-untyped-def, override]
        data = os.pread(self.fd, len(buffer), self.pos)
        buffer[: len(data)] = data
        self.pos += len(data)
        return len(data)

    def seek(self, offset: int, whence: int = 0) -> int:
        if whence != 0 or offset < 0:
            raise OSError("only absolute seeks")
        self.pos = offset
        return offset

    def tell(self) -> int:
        return self.pos


def opener(fd: int) -> Callable[[], BinaryIO]:
    """A reader of the archive at `fd` from its start, a new one at each call."""
    return lambda: io.BufferedReader(_PRead(fd), CHUNK)  # type: ignore[return-value]


class _Exact:
    """`size` bytes from `read`, as tarfile's `addfile` copies them."""

    def __init__(self, read: Callable[[int], bytes], size: int) -> None:
        self._read, self.left = read, size

    def read(self, size: int) -> bytes:
        data = self._read(min(size, self.left))
        self.left -= len(data)
        return data


class Gone(Exception):
    """The answer's reader went away: nothing more to say."""


class Frames:
    """The answer, framed, on `send`: `D` frames carry the new archive, at most `CHUNK` bytes
    each; then `E`, the number of bytes they carried and a note, or `R` and a refusal instead,
    at any point. A frame is its kind (one byte), the length of what follows (4 bytes, big
    endian), and that. It is the gzip writer's file, and nothing follows `E` or `R`: what a
    writer flushes after a refusal, even as the process exits, is dropped."""

    def __init__(self, send: Callable[[bytes], None]) -> None:
        self._send, self.buffer, self.sent, self.said = send, bytearray(), 0, False

    def write(self, data: bytes) -> int:
        if self.said:
            return len(data)
        self.buffer += data
        while len(self.buffer) >= CHUNK:
            self._data(CHUNK)
        return len(data)

    def flush(self) -> None:
        pass

    def _data(self, size: int) -> None:
        chunk = bytes(self.buffer[:size])
        del self.buffer[:size]
        self._frame(b"D", chunk)
        self.sent += size

    def _frame(self, kind: bytes, payload: bytes) -> None:
        try:
            self._send(kind + struct.pack(">I", len(payload)) + payload)
        except OSError as exc:
            raise Gone(type(exc).__name__) from None

    def end(self, note: str) -> None:
        if self.buffer:
            self._data(len(self.buffer))
        self.said = True
        self._frame(b"E", f"{self.sent} {note}".encode("ascii"))

    def refuse(self, why: str) -> None:
        self.said = True
        self._frame(b"R", "".join(c if " " <= c <= "~" else "?" for c in why)[:300].encode())


MTIME = 8**11 - 1  # the largest mtime in octal; later ones are clamped to it
MANIFEST_HEAD = (
    b"# Symbolic links in the original archive, which this one leaves out. One per line: its\n"
    b"# name, a tab, its target. A tab, newline, backslash or control character is \\ooo.\n"
)
RENAMES_HEAD = (
    b"# Members renamed so that Windows can take their names. One per line: the name in the\n"
    b"# original archive, a tab, the name here. A tab, newline, backslash or control character\n"
    b"# is \\ooo. Here, a character Windows forbids, a trailing dot or space, and % are %XX.\n"
)
_UNSAFE = re.compile(rb"[\0-\x1f\x7f\\]")


def _escape(text: str) -> bytes:
    raw = text.encode("utf-8", "surrogateescape")
    return _UNSAFE.sub(lambda m: b"\\%03o" % m[0][0], raw)


def _mtime(value: float) -> int:
    try:
        return min(max(int(value), 0), MTIME)
    except (ValueError, OverflowError, TypeError):
        return 0


class Repack:
    """The second pass: the new archive (module doc), gzip-compressed into `out` as the archive
    is read again. `fresh()` reads the archive from its start, a new reader at each call: the
    pass is one, each hard link's copy another. `targets` are the first pass's."""

    def __init__(self, fresh: Callable[[], BinaryIO], budget: int, targets: set[str], out: Frames):
        self.fresh, self.budget, self.targets = fresh, budget, targets
        self.gz = gzip.GzipFile(fileobj=out, mode="wb", compresslevel=LEVEL, mtime=0)  # type: ignore[arg-type]
        self.tar = tarfile.open(  # type: ignore[call-overload]
            fileobj=self.gz, mode="w|", format=tarfile.GNU_FORMAT, encoding="utf-8",
            errors="surrogateescape", copybufsize=CHUNK,
        )  # fmt: skip
        #: the hard links' targets met so far: their data's offset and size, mode and mtime
        self.sources: dict[str, tuple[int, int, int, int]] = {}
        self.copier: gzip.GzipFile | None = None
        self.work = 0  # bytes decompressed for the copies
        self.symlinks: list[bytes] = []
        self.renames: list[bytes] = []
        self.copies = self.other = self.newest = 0

    def run(self) -> str:
        """Why the archive is refused after all, or "" once the new one is whole."""
        why = _read(self.fresh(), self.budget, self.member)
        if why:
            return why
        for lines, head, path in (
            (self.symlinks, MANIFEST_HEAD, MANIFEST),
            (self.renames, RENAMES_HEAD, RENAMES),
        ):
            if lines:
                text = head + b"".join(lines)
                self._add(self._info(path, len(text), 0o644, self.newest), io.BytesIO(text))
        self.tar.close()
        self.gz.close()
        return ""

    def note(self) -> str:
        """What the new archive leaves out, or turns into copies, for the table."""
        parts = [f"{len(self.symlinks)} symlink(s) listed in {MANIFEST}"] * bool(self.symlinks)
        parts += [f"{len(self.renames)} renamed for Windows, in {RENAMES}"] * bool(self.renames)
        parts += [f"{self.copies} hard link(s) copied"] * bool(self.copies)
        parts += [f"{self.other} other member(s) left out"] * bool(self.other)
        return "; ".join(parts)

    def member(self, member: tarfile.TarInfo, stream: _Stream) -> None:
        name = _normal(member.name)
        new = renamed(name)  # the name the new archive writes
        if new != name and name != "." and (member.isreg() or member.islnk() or member.isdir()):
            self.renames.append(_escape(name) + b"\t" + _escape(new) + b"\n")
        if name in self.targets and not member.isreg():
            self.sources.pop(name, None)  # that name is no file now
        if member.isreg():
            if stream.pos != member.offset_data:
                raise Refused(f"its data is not where tarfile read it ({shown(member.name)})")
            info = self._info(new, member.size, member.mode, member.mtime)
            self._add(info, _Exact(stream.take, member.size))
            if name in self.targets:
                self.sources[name] = (member.offset_data, member.size, info.mode, info.mtime)
        elif member.islnk():
            source = self.sources.get(_normal(member.linkname))
            if source is None:
                raise Refused(f"a hard link to no earlier file ({shown(member.name)})")
            at, size, mode, mtime = source
            self._add(self._info(new, size, mode, mtime), self._copy(at, size))
            self.copies += 1  # no link can name this one: #360's link rule refuses it
        elif member.isdir():
            if name != ".":
                self._add(self._info(new, 0, 0o755, member.mtime, tarfile.DIRTYPE), None)
        elif member.issym():
            self.symlinks.append(_escape(name) + b"\t" + _escape(member.linkname) + b"\n")
        else:
            self.other += 1  # a device, a FIFO, a type tarfile doesn't know

    @staticmethod
    def _info(name: str, size: int, mode: int, mtime: float, kind: bytes = tarfile.REGTYPE):
        info = tarfile.TarInfo(name)
        info.type, info.size, info.mtime = kind, size, _mtime(mtime)
        info.mode = 0o755 if kind == tarfile.DIRTYPE or mode & 0o100 else 0o644
        return info

    def _add(self, info: tarfile.TarInfo, data: object) -> None:
        if self.tar.offset + info.size + 4 * len(info.name) + 3 * tarfile.BLOCKSIZE > self.budget:
            raise Refused(f"the re-packed archive expands past {self.budget >> 20} MB")
        self.tar.addfile(info, data)  # type: ignore[arg-type]
        self.tar.members.clear()  # tarfile would keep every member written
        self.newest = max(self.newest, info.mtime)

    def _copy(self, at: int, size: int) -> _Exact:
        """A hard link's data: its target's, read again from the archive by a second reader,
        which moves forward from one copy to the next and starts again for an earlier one."""
        if self.copier is None:
            self.copier = gzip.GzipFile(fileobj=self.fresh(), mode="rb")
        here = self.copier.tell()
        self.work += (at - here if at >= here else at) + size
        if self.work > self.budget:
            raise Refused(f"its hard links take over {self.budget >> 20} MB to copy")
        self.copier.seek(at)
        return _Exact(self.copier.read, size)


#: The types `seen` requires `_Header` to note for its archive: a `K` record, a PAX header and
#: an `L` record before a hard link
SEEN = (tarfile.GNUTYPE_LONGLINK, tarfile.XHDTYPE, tarfile.GNUTYPE_LONGNAME, tarfile.LNKTYPE)


def _extension(kind: bytes, data: bytes) -> bytes:
    """An extension header of type `kind`, with `data`."""
    info = tarfile.TarInfo("././@LongLink")
    info.type, info.size = kind, len(data)
    return info.tobuf(format=tarfile.GNU_FORMAT) + data + b"\0" * (-len(data) % tarfile.BLOCKSIZE)


def seen() -> bool:
    """Whether this Python's tarfile still shows `_Header` what `_alike` needs (#888; the review
    of #892, B1). tarfile shows each header only to `_proc_member`, a method of its own, called
    just after it reads the header. Should a tarfile stop calling it for each header, or read
    in between, `_alike` would see too few headers and pass what it refuses. How tarfile reaches
    that method has changed before: in 3.13, and in Ubuntu's security update of 3.12.3 for
    CVE-2025-13462, which installs itself, without a deploy. So `answer` and `selfcheck` first
    read a built-in archive, a `K` record (`a`), a PAX header and an `L` record (`h`) before a
    hard link whose own header has GNU's magic and a `prefix`, and refuse every archive unless
    `_Header` noted exactly those four headers (`SEEN`) and the dropped prefix, and tarfile
    read `h` linked to `a`."""
    link = tarfile.TarInfo("x")
    link.type, link.linkname = tarfile.LNKTYPE, "z"
    own = bytearray(link.tobuf(format=tarfile.GNU_FORMAT))
    own[345] = ord("p")  # a prefix, under GNU's magic: tarfile joins it, GNU tar doesn't
    own[148:156] = b"%06o\0 " % (sum(own) - sum(own[148:156]) + 8 * ord(" "))
    raw = b"".join(
        (
            _extension(tarfile.GNUTYPE_LONGLINK, b"a\0"),
            _extension(tarfile.XHDTYPE, b"13 comment=q\n"),
            _extension(tarfile.GNUTYPE_LONGNAME, b"h\0"),
            bytes(own),
            b"\0" * 2 * tarfile.BLOCKSIZE,
        )
    )
    try:
        stream = _Stream(io.BytesIO(raw), len(raw))
        with tarfile.open(fileobj=stream, mode="r:", tarinfo=_Header) as tar:  # type: ignore[call-overload]
            member = tar.next()
            got = (tuple(stream.kinds), member.name, member.linkname, member.dropped)
    except Exception:  # whatever fails, the check can't see the headers
        return False
    return got == (SEEN, "h", "a", True)


def answer(fresh: Callable[[], BinaryIO], budget: int, frames: Frames) -> int:
    """Both passes over the archive that `fresh()` reads, the answer in `frames`: 0 when the
    new archive was sent whole, 1 when the archive is refused. Whatever raises, but the reader's
    going away, refuses it with a frame (the review of #383). If tarfile no longer shows the
    check its headers (`seen`), every archive is refused."""
    try:
        why, targets = scan(fresh(), budget) if seen() else (HIDDEN, set())
        if not why:
            repack = Repack(fresh, budget, targets, frames)
            if not (why := repack.run()):
                frames.end(repack.note())
                return 0
    except Gone:
        raise
    except Refused as exc:
        why = str(exc)
    except MemoryError:
        why = f"it needs over {MEMORY >> 20} MB of memory to check"
    except Exception as exc:  # any failure is a refusal, said as one
        why = f"the re-pack failed ({type(exc).__name__})"
    frames.refuse(why)
    return 1


def limit() -> None:
    """This process's own limits, hard, before any of the archive is read."""
    for kind, value in (
        (resource.RLIMIT_AS, MEMORY),
        (resource.RLIMIT_CPU, CPU_SECONDS),
        (resource.RLIMIT_FSIZE, 0),
        (resource.RLIMIT_CORE, 0),
        (resource.RLIMIT_NOFILE, OPEN_FILES),
    ):
        resource.setrlimit(kind, (value, value))


REQUEST = re.compile(rb"repack ([1-9][0-9]{0,18})\n")
REQUEST_TIMEOUT = 10.0  # seconds for the caller's request
LISTEN_FD = 3  # where systemd passes the connection (sd_listen_fds)


def peer_uid(sock: socket.socket) -> int:
    """The uid of the process at the other end of `sock` (`SO_PEERCRED`)."""
    creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", creds)[1]


def connection(env: dict[str, str], pid: int) -> socket.socket:
    """The one connection systemd passes an instance of `zab-archive-check@.service`
    (`Accept=yes`): fd 3, a Unix stream socket."""
    if env.get("LISTEN_PID") != str(pid) or env.get("LISTEN_FDS") != "1":
        raise SystemExit("no connection from systemd (zab-archive-check.socket)")
    sock = socket.socket(fileno=LISTEN_FD)
    if sock.family != socket.AF_UNIX or sock.type != socket.SOCK_STREAM:
        raise SystemExit("systemd passed a socket that is not a Unix stream socket")
    return sock


def request(sock: socket.socket) -> tuple[str, int, int]:
    """The caller's request, in one message: the line `repack <budget>` and one descriptor, a
    regular file opened read-only. (why it is refused, or "", the budget, the descriptor)."""
    sock.settimeout(REQUEST_TIMEOUT)
    try:
        data, ancillary, flags, _ = sock.recvmsg(64, socket.CMSG_SPACE(2 * 4))
    except OSError as exc:
        return f"no request ({type(exc).__name__})", 0, -1
    fds = array.array("i")
    for level, kind, payload in ancillary:
        if (level, kind) == (socket.SOL_SOCKET, socket.SCM_RIGHTS):
            fds.frombytes(payload[: len(payload) - len(payload) % fds.itemsize])
    for extra in fds[1:]:
        os.close(extra)
    line = REQUEST.fullmatch(data)
    if not line or len(fds) != 1 or flags & socket.MSG_CTRUNC:
        if fds:
            os.close(fds[0])
        return "not a request: one line and one file", 0, -1
    fd = fds[0]
    if not stat.S_ISREG(os.fstat(fd).st_mode) or fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE:
        os.close(fd)
        return "the archive didn't come as a file opened read-only", 0, -1
    return "", int(line[1]), fd


def serve(env: dict[str, str], pid: int, peers: list[str]) -> int:
    """Answer the connection systemd passed: 0 once the new archive was sent whole, else 1."""
    sock = connection(env, pid)
    frames = Frames(sock.sendall)
    try:
        allowed = {0}
        for name in peers:
            try:
                allowed.add(pwd.getpwnam(name).pw_uid)
            except KeyError:
                pass  # no such user: nobody to answer
        if peer_uid(sock) not in allowed:
            frames.refuse("not a caller this checker answers")
            return 1
        why, budget, fd = request(sock)
        if why:
            frames.refuse(why)
            return 1
        sock.settimeout(None)  # the unit's RuntimeMaxSec bounds a caller that stops reading
        return answer(opener(fd), budget, frames)
    except Gone:
        return 1
    finally:
        sock.close()


#: `selfcheck`'s archive: (name, type, link target, data); what its re-pack must hold.
SAMPLE = (
    ("work", tarfile.DIRTYPE, "", b""),
    ("work/a.md", tarfile.REGTYPE, "", b"alpha"),
    ("work/same.md", tarfile.LNKTYPE, "work/a.md", b""),
    ("work/latest", tarfile.SYMTYPE, "a.md", b""),
)
REPACKED = [("work", None), ("work/a.md", b"alpha"), ("work/same.md", b"alpha")]
REPACKED += [(MANIFEST, MANIFEST_HEAD + b"work/latest\ta.md\n")]


def selfcheck() -> int:
    """Re-pack `SAMPLE` in memory and read the result back: what `deploy.sh` runs, as
    `zab-build`, before a release is switched to. 0 if it holds `REPACKED`, and tarfile still
    shows the check its headers (`seen`)."""
    if not seen():
        print(f"archive_check: selfcheck FAILED: {HIDDEN}")
        return 1
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz", format=tarfile.GNU_FORMAT) as tar:
        for name, kind, target, body in SAMPLE:
            info = tarfile.TarInfo(name)
            info.type, info.linkname, info.size = kind, target, len(body)
            tar.addfile(info, io.BytesIO(body))
    sent: list[bytes] = []
    code = answer(lambda: io.BytesIO(data.getvalue()), 1 << 20, Frames(sent.append))
    stream, at, new = b"".join(sent), 0, bytearray()
    while at < len(stream) and stream[at : at + 1] == b"D":
        size = int.from_bytes(stream[at + 1 : at + 5], "big")
        new += stream[at + 5 : at + 5 + size]
        at += 5 + size
    with tarfile.open(fileobj=io.BytesIO(bytes(new)), mode="r:gz") as tar:
        got = [(m.name, tar.extractfile(m).read() if m.isreg() else None) for m in tar]  # type: ignore[union-attr]
    ok = code == 0 and stream[at : at + 1] == b"E" and got == REPACKED
    print("archive_check: selfcheck " + ("ok" if ok else f"FAILED: {code} {got}"))
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="archive_check.py", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    serving = commands.add_parser("serve", help="answer the connection systemd passes")
    serving.add_argument(
        "--peer", action="append", default=[], metavar="USER", help="a user it answers, as root"
    )
    commands.add_parser("selfcheck", help="re-pack a small archive in memory")
    args = parser.parse_args(argv)
    if args.command == "selfcheck":  # trusted input, and a report to print
        return selfcheck()
    limit()
    return serve(dict(os.environ), os.getpid(), args.peer)


if __name__ == "__main__":
    raise SystemExit(main())
