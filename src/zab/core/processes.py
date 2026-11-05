"""Reap only a verified Claude headless process group (SPEC §7.8.1, §9.6)."""

import asyncio
import contextlib
import logging
import os
import re
import shutil
import signal
import subprocess
from pathlib import Path


def is_bridge_child(pgid: int) -> bool:
    """A live child belongs to this bridge, not to restart cleanup."""
    try:
        if Path("/proc").is_dir():
            parent = int(Path(f"/proc/{pgid}/stat").read_text().rsplit(")", 1)[1].split()[1])
        else:
            result = subprocess.run(  # noqa: S603 - fixed ps and integer PID
                ["/bin/ps", "-p", str(pgid), "-o", "ppid="],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            )
            parent = int(result.stdout.strip())
        return parent == os.getpid()
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return False


def leader_identity(pgid: int, native_id: str, binary: str) -> str | None:
    """Refuse unknown/nonmatching leaders without signalling (Linux and macOS)."""
    if pgid <= 1 or pgid == os.getpgrp() or is_bridge_child(pgid):
        return None
    try:
        if os.getpgid(pgid) != pgid or os.getsid(pgid) != pgid:
            return None
        executable = str(Path(shutil.which(binary) or binary).resolve())
        if Path("/proc").is_dir():
            root = Path(f"/proc/{pgid}")
            uid = root.stat().st_uid
            argv = root.joinpath("cmdline").read_bytes().decode().strip("\0").split("\0")
            identity = root.joinpath("stat").read_text().rsplit(")", 1)[1].split()[19]
            actual = root.joinpath("exe").resolve()
            matches = os.path.samefile(actual, executable)
            # npm distributions execute the configured CLI script under node.
            if not matches and actual.name in ("node", "nodejs") and len(argv) > 1:
                matches = os.path.samefile(argv[1], executable)
            headless = "-p" in argv or "--print" in argv
            session = any(
                argv[i : i + 2] == [flag, native_id]
                for flag in ("--resume", "--session-id")
                for i in range(len(argv) - 1)
            )
        else:
            result = subprocess.run(  # noqa: S603 - fixed executable and integer PID, no shell
                ["/bin/ps", "-ww", "-p", str(pgid), "-o", "uid=,lstart=,command="],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            )
            fields = result.stdout.strip().split(None, 6)
            uid, identity, command = int(fields[0]), " ".join(fields[1:6]), fields[6]
            # ps prints raw argv joined by spaces, not shell-quoted text.
            prefixes = (binary + " ", executable + " ")
            matches = command.startswith(prefixes)
            interpreter, _, rest = command.partition(" ")
            if not matches and Path(interpreter).name in ("node", "nodejs"):
                matches = rest.startswith(prefixes)
            headless = bool(re.search(r" (?:-p|--print)(?: |$)", command))
            session = bool(
                re.search(r" --(?:resume|session-id) " + re.escape(native_id) + r"(?: |$)", command)
            )
        if uid != os.getuid() or not matches or not headless or not session:
            return None
        # Start time prevents signalling a recycled PID between TERM and KILL.
        return identity
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def zombie_identity(pgid: int) -> str | None:
    """Read a dead leader's identity without relying on its empty argv/exe."""
    try:
        if os.getpgid(pgid) != pgid or os.getsid(pgid) != pgid:
            return None
        if Path("/proc").is_dir():
            root = Path(f"/proc/{pgid}")
            fields = root.joinpath("stat").read_text().rsplit(")", 1)[1].split()
            return fields[19] if root.stat().st_uid == os.getuid() and fields[0] == "Z" else None
        result = subprocess.run(  # noqa: S603 - fixed ps and integer PID
            ["/bin/ps", "-p", str(pgid), "-o", "uid=,stat=,lstart="],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        uid, state, started = result.stdout.strip().split(None, 2)
        return (
            " ".join(started.split()) if int(uid) == os.getuid() and state.startswith("Z") else None
        )
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


async def reap_claude_turn(pgid: int, native_id: str, binary: str = "claude") -> None:
    identity = await asyncio.to_thread(leader_identity, pgid, native_id, binary)
    if identity is None:
        return
    try:
        await signal_orphan(pgid, native_id, binary, identity)
    except PermissionError:
        logging.getLogger("zab.recovery").warning(
            "Cannot signal remaining orphan group", extra={"pgid": pgid}
        )


async def signal_orphan(pgid: int, native_id: str, binary: str, identity: str) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, signal.SIGTERM)
    for _ in range(50):
        await asyncio.sleep(0.1)
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return
        current = await asyncio.to_thread(leader_identity, pgid, native_id, binary)
        if current is not None and current != identity:
            return
        if current is None:
            try:
                os.kill(pgid, 0)
            except ProcessLookupError:
                pass  # Original descendants can outlive their vanished leader.
            else:
                if await asyncio.to_thread(zombie_identity, pgid) != identity:
                    return  # An existing unverified replacement leader must not be killed.
                # Our original zombie cannot change groups; its descendants may still run.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, signal.SIGKILL)
