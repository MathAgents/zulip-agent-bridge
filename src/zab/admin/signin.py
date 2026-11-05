"""The console's one-time sign-in links (docs/ADMIN.md §4.4), both sides.

**The bot** (`SignIns`, user `zab-admin`), for `!console`: 32 random bytes as 64 hex digits (the
link's fragment, which no server log sees, and which Zulip's Markdown leaves whole); their
SHA-256 names a file in `console/tokens/`, which holds only the user's ID, their name and the
expiry, 5 minutes ahead. Written under a temporary name, `fchmod` 0640 and renamed (§1.3: every
unit has `UMask=0077`), so that the console, the directory's setgid group, can read it.

**The console** (`claim`, `consume`, user `zab-console`) looks a token up by its hash, checks the
expiry, and, once the allowlists let the user in, deletes the file: whichever request deletes it
first signs in, any other is refused. An expired or refused token stays until the bot deletes it.

**The record** (§2.8): the bot remembers what it issued; a token whose file is gone, which the
bot didn't delete, was used, and the bot writes `console_login` (`SignIns.used`). The bot deletes
a token at its expiry, and any stray file in the directory older than that. A token used while
the bots restart is never recorded.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

TTL = 300.0  # seconds a link lives (§4.4)
TOKEN = re.compile(r"[0-9a-f]{64}", re.ASCII)
NAME = re.compile(r"[0-9a-f]{64}", re.ASCII)  # a token file's name: its token's SHA-256
MAX_FILE = 4096


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def link(console_url: str, token: str) -> str:
    """`https://<the console's host>/login#<token>`: the token in the fragment (§4.4)."""
    parts = urlsplit(console_url)
    return f"{parts.scheme}://{parts.netloc}/login#{token}"


@dataclass(frozen=True, slots=True)
class Issued:
    user_id: int
    name: str
    where: str  # the workshop whose bot issued it; "" for the global admin bot's
    bot: int
    expires: float


class SignIns:
    """The bot's side (module doc)."""

    def __init__(
        self, tokens: Path, console_url: str, *, clock: Callable[[], float] = time.time
    ) -> None:
        self.tokens, self.console_url, self.clock = tokens, console_url, clock
        self.issued: dict[str, Issued] = {}  # a token file's name → what it was issued for

    def issue(self, user_id: int, name: str, where: str, bot: int) -> str:
        """A new token's file, and its link."""
        from zab.admin.collector import write  # the same temporary name, fchmod and rename

        token = secrets.token_hex(32)
        expires = self.clock() + TTL
        data = json.dumps({"user_id": user_id, "name": name[:60], "expires": expires})
        write(self.tokens / digest(token), data.encode(), 0o640)
        self.issued[digest(token)] = Issued(user_id, name[:60], where, bot, expires)
        return link(self.console_url, token)

    def used(self) -> list[Issued]:
        """The tokens used since the last call; each expired one deleted and forgotten, and any
        stray token file older than `TTL`, such as one issued before a restart."""
        now, out = self.clock(), []
        for name, issued in list(self.issued.items()):
            path = self.tokens / name
            if not path.exists():
                out.append(issued)
                del self.issued[name]
            elif now >= issued.expires:
                with suppress(OSError):
                    path.unlink()
                del self.issued[name]
        with suppress(OSError):
            for path in self.tokens.iterdir():
                if NAME.fullmatch(path.name) and path.name not in self.issued:
                    with suppress(OSError):
                        if now - path.lstat().st_mtime > TTL:
                            path.unlink()
        return out


class Refused(Exception):
    """A token that signs no one in, with what the sign-in page says."""


def claim(tokens: Path, token: str, now: float) -> tuple[Path, int, str]:
    """The console's side: the token's file, user ID and name, if the token is one the bot
    issued and it hasn't expired. Raises `Refused`. Nothing is deleted here (`consume`)."""
    if not TOKEN.fullmatch(token):
        raise Refused("That is not a sign-in link. Send !console again.")
    path = tokens / digest(token)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise Refused("That link was used already, or is not one. Send !console again.") from None
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        data = f.read(MAX_FILE + 1) if stat.S_ISREG(st.st_mode) else b""
    entry: Any = None
    with suppress(ValueError):
        entry = json.loads(data) if len(data) <= MAX_FILE else None
    if not (
        isinstance(entry, dict)
        and type(entry.get("user_id")) is int
        and isinstance(entry.get("name"), str)
        and isinstance(entry.get("expires"), int | float)
    ):
        raise Refused("That link can't be read. Send !console again.")
    if now >= entry["expires"]:
        raise Refused("That link has expired: links last 5 minutes. Send !console again.")
    return path, entry["user_id"], entry["name"][:60]


def consume(path: Path) -> bool:
    """Delete the token's file: True for the one request that did, False for any other.
    Raises `Refused` when it can't be deleted at all (the directory's mode or mount is wrong:
    the review of #496)."""
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError:
        raise Refused("The sign-in failed on the server. Send !console again, or tell a "
                      "deployer.") from None  # fmt: skip
    return True
