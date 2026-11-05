#!/usr/bin/env python3
"""`sage` on agent's PATH as the image's wrapper (#482), on a VM whose image lacks it.

    sudo -u agent -H /usr/bin/python3 sage_wrapper.py install WRAPPER [--path P] [--record R]
    sudo -u agent -H /usr/bin/python3 sage_wrapper.py remove [--path P] [--record R]

New images install `workshop/ami/files/bin/sage` as `~agent/.local/bin/sage` (#480): it puts the
Sage environment's `bin` first on PATH and execs the environment's `sage`, as `conda activate`
would, so that Sage's `giac()`, `r()` and `gap()` find Sage's own programs. The Oct 9 image has a
link there instead, `/home/agent/miniforge/envs/sage/bin/sage`, and those three interfaces fail.

`install` is run by every release rollout of `push_config.py`, as agent, with that same file
(WRAPPER). It looks at the file `--path` (`/home/agent/.local/bin/sage`) and acts only on what
it recognises exactly:

- a link whose target is exactly the program WRAPPER execs, as a string (WRAPPER's own
  `SAGE_ENV` default, then `/bin/sage`: the absolute path the Oct 9 build's `ln -sf` wrote), and
  that program an executable file: the image's link. The same string, so the wrapper execs the
  very file the link ran: a relative target, or one through `..`, is never taken for it, since
  a symlinked directory on the way could lead elsewhere (review of #536). It is replaced,
  atomically, by WRAPPER (0755), after the record (`--record`) has durably noted the link's
  target, so that `remove` can put it back;
- zab's wrapper, as WRAPPER is now, mode 0755: left as it is ("unchanged");
- zab's wrapper as an earlier version of this repository shipped it (KNOWN), or with another
  mode: replaced by WRAPPER ("updated");
- anything else (nothing there, no Sage environment, a link elsewhere, a relative one or one
  through `..` included, or to a missing program, another file, a directory): left alone, and
  said ("skipped").

`remove` puts the image's link back, only where the record says the rollout replaced it and
the file is still zab's wrapper; an image's own wrapper has no record and is never touched. No
rollout step runs it: `push_config.py --uninstall` leaves the wrapper, which the VM's list of its
software and the agents' instructions describe (README.md §6). It is the way back by hand.

One line of output, starting with `sage: `, and exit 0, whatever was found; an error of the file
system is one line on stderr and exit 1. Standard library only: the VM's system Python runs it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile

PATH = "/home/agent/.local/bin/sage"
RECORD = "/home/agent/.local/state/zab/sage-wrapper.json"
#: zab's wrapper as each version of `workshop/ami/files/bin/sage` was, by SHA-256: recognised as
#: zab's on any VM. A test keeps the file's current hash here, so that a change to the wrapper
#: adds a line, and the VMs that have the old one still get the new one.
KNOWN = {
    "f94c5cd554ae256ae6564cfb2db3fa59469d218dcfae31afadc8a9bef48465ed": (
        "#480's, in zab-fleet-20261019 (built from 536647bb)"
    ),
}
#: The wrapper's line that names the environment, `SAGE_ENV="${SAGE_ENV:-/abs/path}"`
_ENV_LINE = re.compile(r'^SAGE_ENV="\$\{SAGE_ENV:-(/[^"$`\\\s]+)\}"$', re.MULTILINE)
#: More than any wrapper: a larger file is not zab's
MAX_BYTES = 1 << 16
MODE = 0o755


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def program(wrapper: bytes) -> str:
    """The program the wrapper execs: its `SAGE_ENV` default, then `/bin/sage`."""
    found = _ENV_LINE.findall(wrapper.decode("utf-8", "replace"))
    if len(found) != 1:
        raise ValueError("the wrapper names no single SAGE_ENV default")
    if os.path.normpath(found[0]) != found[0]:  # no `..`, `.`, `//` or trailing `/`
        raise ValueError(f"the wrapper's SAGE_ENV default {found[0]} isn't a normal path")
    return found[0] + "/bin/sage"


def _small(path: str) -> bytes | None:
    """A regular file's bytes, None when it is larger than any wrapper."""
    with open(path, "rb") as f:
        data = f.read(MAX_BYTES + 1)
    return None if len(data) > MAX_BYTES else data


def _write(path: str, data: bytes) -> None:
    """`data` as `path`, mode 0755, by a rename in the same directory."""
    fd, tmp = tempfile.mkstemp(prefix=".sage-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fchmod(f.fileno(), MODE)
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.lexists(tmp):
            os.unlink(tmp)
        raise


def _write_record(record: str, data: dict[str, str]) -> None:
    """The record, on disk before the wrapper replaces the link: the file synced, renamed into
    place, then its directory synced, so that a power loss can't leave the wrapper with an
    empty or missing record (review of #536)."""
    directory = os.path.dirname(record)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".sage-wrapper-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, record)
    except BaseException:
        if os.path.lexists(tmp):
            os.unlink(tmp)
        raise
    dfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def install(wrapper_file: str, path: str = PATH, record: str = RECORD) -> str:
    """Install the wrapper where PATH is the image's link (the module docstring); the outcome."""
    with open(wrapper_file, "rb") as f:
        wrapper = f.read(MAX_BYTES + 1)
    if len(wrapper) > MAX_BYTES:
        raise ValueError(f"{wrapper_file}: larger than {MAX_BYTES} bytes")
    want = program(wrapper)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        if os.path.lexists(want):
            return f"sage: skipped: no {path}, though {want} exists: left alone"
        return f"sage: skipped: no SageMath here (no {path}, no {want})"
    if stat.S_ISLNK(st.st_mode):
        target = os.readlink(path)
        if target != want:  # the exact string: see the module docstring
            return f"sage: skipped: {path} links to {target}, not to {want}: left alone"
        if not (os.path.isfile(want) and os.access(want, os.X_OK)):
            return f"sage: skipped: {path} links to {want}, not an executable file: left alone"
        _write_record(record, {"path": path, "link": target, "wrapper": sha256(wrapper)})
        _write(path, wrapper)
        return f"sage: installed the wrapper as {path}, in place of its link to {target}"
    if not stat.S_ISREG(st.st_mode):
        return f"sage: skipped: {path} is neither a file nor a link: left alone"
    data = _small(path)
    ours = data is not None and (data == wrapper or sha256(data) in KNOWN)
    if not ours:
        return f"sage: skipped: {path} is not zab's wrapper, nor the image's link: left alone"
    if data == wrapper and stat.S_IMODE(st.st_mode) == MODE:
        return f"sage: unchanged: {path} is the wrapper"
    _write(path, wrapper)
    return f"sage: updated the wrapper {path}"


def remove(path: str = PATH, record: str = RECORD) -> str:
    """Put back the image's link that `install` replaced (the module docstring); the outcome."""
    try:
        with open(record, encoding="utf-8") as f:
            noted = json.load(f)
    except FileNotFoundError:
        return f"sage: nothing to restore: no record {record}; {path} left as it is"
    except (ValueError, RecursionError):  # json.load: deep nesting is a RecursionError
        return f"sage: skipped: {record} isn't the rollout's record: {path} left as it is"
    link = noted.get("link") if isinstance(noted, dict) else None
    if not isinstance(link, str) or not link or noted.get("path") != path:
        return f"sage: skipped: {record} isn't a record for {path}: left as it is"
    installed = noted.get("wrapper")
    zabs = {*KNOWN, installed} if isinstance(installed, str) else set(KNOWN)
    st = os.lstat(path) if os.path.lexists(path) else None
    data = _small(path) if st is not None and stat.S_ISREG(st.st_mode) else None
    if data is None or sha256(data) not in zabs:
        return f"sage: skipped: {path} is no longer zab's wrapper: left alone, and the record"
    tmp = os.path.join(os.path.dirname(path), f".sage-link-{os.getpid()}")
    if os.path.lexists(tmp):
        os.unlink(tmp)
    os.symlink(link, tmp)
    try:
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise
    os.unlink(record)
    return f"sage: restored {path} as a link to {link}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sage_wrapper.py", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="command", required=True)
    inst = sub.add_parser("install", help="replace the image's link by WRAPPER")
    inst.add_argument("wrapper", metavar="WRAPPER")
    rem = sub.add_parser("remove", help="put back the link that install replaced")
    for one in (inst, rem):
        one.add_argument("--path", default=PATH)
        one.add_argument("--record", default=RECORD)
    args = p.parse_args(argv)
    try:
        if args.command == "install":
            print(install(args.wrapper, args.path, args.record))
        else:
            print(remove(args.path, args.record))
    except (OSError, ValueError, RecursionError) as exc:
        print(f"sage: failed: {str(exc)[:200]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
