"""Ephemeral owner DM login flow (SPEC §4.6; D24)."""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

from zab.core import types as t
from zab.core.addressing import without_typed_address
from zab.core.command_text import as_command
from zab.core.reason import asked_by

if TYPE_CHECKING:
    from zab.core.bridge import BotBridge

TIMEOUT = 600
# A !login that arrives while the backend is still connecting (Codex's daemon right after a
# deploy) waits this long for it, retrying every READY_POLL seconds, instead of failing.
READY_WAIT = 60
READY_POLL = 2
NAMES = {"codex": "Codex", "claude": "Claude"}


class Login:
    def __init__(self, bridge: BotBridge) -> None:
        self.bridge = bridge
        self.owner: int | None = None
        self.task: asyncio.Task | None = None
        self.inbox: asyncio.Queue[str] = asyncio.Queue()
        self.seen: set[int] = set()

    async def send(self, uid: int, text: str) -> None:
        await self.bridge.poster.call(
            lambda: self.bridge.client.request(
                "POST", "messages", type="private", to=[uid], content=text
            )
        )

    async def handle(self, message: dict[str, Any]) -> bool:
        uid = message["sender_id"]
        # Fail closed for group DMs; never relay a login code from a group.
        if message["type"] != "private" or {
            r["id"] for r in message.get("display_recipient", [])
        } != {uid, self.bridge.router.user_id}:
            return False
        text = message["content"]
        router = self.bridge.router
        # `@claude !login` (#713), then `!Login`, "`!login`", `@**Bot** !login` (#709). A pasted
        # code is no command, and comes as `without_typed_address` gives it: `@claude CODE`, `CODE`
        command = as_command(without_typed_address(text, router.kind), router.mention)
        parts = command.split()
        # `cancel` in any case and spacing too: `!login  Cancel` (review of #725)
        cancel = [p.casefold() for p in parts] == ["!login", "cancel"]
        if uid == self.owner:
            if message["id"] not in self.seen:
                self.seen.add(message["id"])
                if cancel:
                    if self.task and not self.task.cancelling():
                        self.task.cancel()
                elif command.startswith("!"):
                    if not self.bridge.sentinel_answers(message):  # `!vm`, `!restart` (#204)
                        await self.send(
                            uid,
                            "A login is in progress: paste the code here, or send !login cancel.",
                        )
                else:
                    self.inbox.put_nowait(command)  # `@claude CODE` passes on `CODE`
            return True
        if not parts or parts[0] != "!login":
            return False
        if self.task:
            await self.send(uid, "❌ A login is already in progress.")
        elif len(parts) > 2 or cancel:
            await self.send(uid, "❌ No login started. Use !login [account].")
        else:
            self.owner = uid
            self.seen = {message["id"]}
            self.task = asyncio.create_task(self.run(uid, parts[1] if len(parts) == 2 else None))
            # Enter the try/finally before a subsequent DM/shutdown can cancel it.
            await asyncio.sleep(0)
        return True

    async def receive(self, timeout_s: float) -> str | None:
        try:
            return await asyncio.wait_for(self.inbox.get(), timeout_s)
        except TimeoutError:
            return None

    def agent(self) -> str:
        backend = self.bridge.bot.backend
        return NAMES.get(self.bridge.config.backends[backend].kind, backend)

    async def attempt(self, uid: int, account: str | None) -> bool | None:
        """One backend login; None when the backend is not connected yet, which its methods
        signal with BackendUnavailable (types.BackendStatus) before anything was sent."""
        sent = False

        async def send(text: str) -> None:
            nonlocal sent
            sent = True
            await self.send(uid, text)

        try:
            # bool: None is reserved for "not connected"; a backend that returned None after
            # relaying a code must not be retried (review of #288).
            return bool(await self.bridge.backend.login(account, send, self.receive))
        except t.BackendUnavailable:
            if sent:
                raise  # it went down during the sign-in: a failure like any other
            return None

    async def login(self, uid: int, account: str | None) -> bool | None:
        """The backend's login, after waiting up to READY_WAIT for it to connect; None if it
        never did."""
        deadline = asyncio.get_running_loop().time() + READY_WAIT
        waited = False
        while (ok := await self.attempt(uid, account)) is None:
            if asyncio.get_running_loop().time() >= deadline:
                return None
            if not waited:
                waited = True
                await self.send(
                    uid,
                    f"⏳ {self.agent()} isn't connected yet; it's probably still starting. "
                    "The sign-in begins as soon as it is, within a minute.",
                )
            await asyncio.sleep(READY_POLL)
            # Anything sent before the sign-in began can't be its code.
            while not self.inbox.empty():
                self.inbox.get_nowait()
        return ok

    async def run(self, uid: int, account: str | None) -> None:
        ok: bool | None = False
        try:
            async with asyncio.timeout(TIMEOUT):
                ok = await self.login(uid, account)
        except (Exception, asyncio.CancelledError):
            # Backend failures may contain the pasted authorization code. Never log them.
            ok = False
        if ok is None:
            reply = (
                f"⏳ {self.agent()} still isn't connected, so the sign-in didn't start. "
                "Send !login again in a minute."
            )
        else:
            reply = "✅ connected" if ok else "❌ Login failed or cancelled."
        try:
            with contextlib.suppress(Exception):
                await self.send(uid, reply)
        finally:
            try:
                with contextlib.suppress(Exception), asked_by("!login"):
                    await self.bridge.backend.doctor()
            finally:
                self.bridge.forget_sign_in()  # its routing asks again (#713)
                self.owner = self.task = None
                self.inbox = asyncio.Queue()
                self.seen.clear()

    async def close(self) -> None:
        if self.task:
            if not self.task.cancelling():
                self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                from zab.core.watchdog import waiting

                await waiting(self.task)
