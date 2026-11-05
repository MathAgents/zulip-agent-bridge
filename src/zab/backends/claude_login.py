"""`!login` for Claude (issue #36, Drew's onboarding plan of Sun Oct 4): `claude auth login` in a
pseudo-terminal, driven from the owner's DM.

The CLI (2.1.282 to 2.1.289, probed on the development machine) prints

    Opening browser to sign in…
    If the browser didn't open, visit: <OSC 8 link to https://claude.com/cai/oauth/authorize?…>
    Paste code here if prompted >

then reads the code from the terminal. A bad code ends with `Login failed: Request failed with
status code 400` and exit 1; a good one exits 0. Success is confirmed with `claude auth status`
(`loggedIn`), never from credential files (§12). The pasted code is a secret: it is written to the
terminal only, never logged, stored or echoed back, and stripped from anything relayed.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import pty
import re
import signal
import struct
import termios
import time
from collections.abc import Awaitable, Callable, Mapping

from zab.render.redact import redact_secrets

log = logging.getLogger("zab.backends.claude")

LOGIN_TIMEOUT_S = 600.0  # the owner's time to sign in and paste the code
URL_TIMEOUT_S = 60.0  # the CLI's time to print the sign-in URL
RESULT_TIMEOUT_S = 90.0  # the CLI's time to exchange the code once it is pasted

_OSC8 = re.compile(r"\x1b\]8;;(https://[^\x07\x1b]+)(?:\x07|\x1b\\)")
_ANSI = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b\[[0-9;?]*[A-Za-z]")
# A plain URL counts only once whitespace follows it: terminal reads can split it anywhere, and
# an OSC 8 link counts only with its terminator (codex review on #89).
_URL = re.compile(r"https://\S*oauth/authorize\S*(?=\s)")
_FAILED = re.compile(r"(Login failed[^\r\n]*|Error[^\r\n]*)")
INSTRUCTIONS = (
    "To connect your Claude subscription: open this link, sign in, then paste the code it shows "
    "here (in this DM)."
)


def extract_url(output: str) -> str | None:
    """The sign-in URL: the target of a complete OSC 8 hyperlink if present, else the first
    authorize URL that whitespace follows; None while either may still be arriving."""
    if m := _OSC8.search(output):
        return m.group(1)
    m = _URL.search(_ANSI.sub("", output))
    return m.group(0) if m else None


def clean_code(text: str) -> str:
    """What the owner pasted, without surrounding whitespace, code fences or backticks."""
    return text.strip().strip("`").strip()


async def run_login(
    binary: str,
    env: Mapping[str, str],
    send: Callable[[str], Awaitable[None]],
    receive: Callable[[float], Awaitable[str | None]],
    *,
    status: Callable[[], Awaitable[bool]],
    timeout_s: float = LOGIN_TIMEOUT_S,
) -> bool:
    """Run `claude auth login` under `env` and complete it with a code received from the owner.

    `send` posts to the owner's DM; `receive(timeout)` returns the next DM text or None.
    `status()` reports `claude auth status`'s `loggedIn`. Returns True once signed in. The CLI's
    process group is killed on every exit path.
    """
    master, slave = pty.openpty()
    # wide enough that the URL is never wrapped by the terminal
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 1000, 0, 0))
    env = {**env, "BROWSER": "true", "TERM": "xterm-256color"}  # no browser on the VM
    try:
        proc = await asyncio.create_subprocess_exec(
            binary,
            "auth",
            "login",
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            start_new_session=True,
        )
    except OSError as exc:
        os.close(master)
        os.close(slave)
        log.warning("claude: `auth login` could not start: %s", type(exc).__name__)
        await send(f"Could not start `claude auth login` ({type(exc).__name__}).")
        return False
    os.close(slave)
    reader = _PtyReader(master)
    code = ""
    try:
        output = await reader.until(lambda text: extract_url(text) is not None, URL_TIMEOUT_S)
        url = extract_url(output)
        if url is None:
            await send("Claude did not show a sign-in link; try `!login` again.")
            return False
        await send(f"{INSTRUCTIONS}\n{url}")
        deadline = time.monotonic() + timeout_s
        while not code:
            remaining = deadline - time.monotonic()
            text = await receive(remaining) if remaining > 0 else None
            if text is None:
                await send("Sign-in timed out; send `!login` to start again.")
                return False
            code = clean_code(text)
        reader.mark()
        try:
            os.write(master, code.encode() + b"\r")
        except OSError:  # the CLI is gone (EIO): it cannot take the code any more
            await send("Claude's sign-in ended before the code arrived; send `!login` again.")
            return False
        exited = await _wait(proc, RESULT_TIMEOUT_S)
        after = reader.since_mark()
        if exited is None or exited != 0:
            reason = _failure_reason(after, code)
            await send(
                f"Sign-in failed{': ' + reason if reason else ''}. Send `!login` to try again."
            )
            return False
        if not await status():
            await send(
                "`claude auth status` does not show you signed in; send `!login` to try again."
            )
            return False
        return True
    finally:
        reader.close()
        await _kill_group(proc)
        code = ""


class _PtyReader:
    """Collects the CLI's output from the pseudo-terminal master in the background."""

    def __init__(self, fd: int) -> None:
        self.fd = fd
        self.buf = ""
        self.closed = False
        self._mark = 0
        self._changed = asyncio.Event()
        os.set_blocking(fd, False)
        asyncio.get_running_loop().add_reader(fd, self._on_readable)

    def _on_readable(self) -> None:
        try:
            chunk = os.read(self.fd, 65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:  # EIO once the CLI has exited and closed the terminal
            chunk = b""
        if not chunk:
            self._stop()
        else:
            self.buf += chunk.decode("utf-8", errors="replace")
        self._changed.set()

    def _stop(self) -> None:
        if not self.closed:
            self.closed = True
            asyncio.get_running_loop().remove_reader(self.fd)

    async def until(self, done: Callable[[str], bool], timeout_s: float) -> str:
        deadline = time.monotonic() + timeout_s
        while not done(self.buf) and not self.closed:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._changed.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._changed.wait(), remaining)
        return self.buf

    def mark(self) -> None:
        self._mark = len(self.buf)

    def since_mark(self) -> str:
        return self.buf[self._mark :]

    def close(self) -> None:
        self._stop()
        with contextlib.suppress(OSError):
            os.close(self.fd)
        self.buf = ""  # it holds the terminal's echo of the code


def _failure_reason(output: str, code: str) -> str:
    """The CLI's own failure line, without the echoed code and without escape sequences."""
    text = _ANSI.sub("", output)
    if code:
        text = text.replace(code, "")
    m = _FAILED.search(text)
    # redacted before the cut, which can leave too little of a secret for its rule (#661)
    return redact_secrets(m.group(1).replace("\x00", "")).strip()[:200] if m else ""


async def _wait(proc: asyncio.subprocess.Process, timeout_s: float) -> int | None:
    try:
        return await asyncio.wait_for(proc.wait(), timeout_s)
    except TimeoutError:
        return None


async def _kill_group(proc: asyncio.subprocess.Process) -> None:
    """SIGTERM, then SIGKILL, to the CLI's whole process group (it was started in its own)."""
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)
        if await _wait(proc, 3) is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            await _wait(proc, 3)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)  # stragglers of the group


async def logged_in(binary: str, env: Mapping[str, str], run_capture) -> bool:
    """`claude auth status`'s `loggedIn` (§12: never from credential files)."""
    try:
        out = await run_capture([binary, "auth", "status"], timeout_s=30, env=env)
        return bool(json.loads(out).get("loggedIn"))
    except (OSError, TimeoutError, json.JSONDecodeError) as exc:
        log.warning("claude: `auth status` after login failed: %s", type(exc).__name__)
        return False
