"""Resolve per-bot commit identity without reading agent credentials (SPEC §5.5)."""

from __future__ import annotations

import logging
import re

from zab.core import types as t


def resolve(setting: str, name: str = "", email: str = "") -> t.GitIdentity | None:
    if setting == "off":
        return None
    if setting != "auto":
        match = re.fullmatch(r"([^<>\r\n]+) <([^<>\s@]+@[^<>\s@]+)>", setting)
        if not match:
            raise ValueError('git_identity must be auto, off, or "Name <email>"')
        name, email = match.groups()
    if setting == "auto" and (
        not name.strip()
        or not re.fullmatch(r"[^<>\s@]+@[^<>\s@]+", email)
        or any(ord(c) < 32 for c in name + email)
    ):
        logging.getLogger("zab.git_identity").warning(
            "Bot profile has no usable commit identity; using user git config"
        )
        return None
    if not name.strip() or not re.fullmatch(r"[^<>\s@]+@[^<>\s@]+", email):
        raise ValueError("bot commit identity requires a name and visible Zulip bot email")
    if any(ord(c) < 32 for c in name + email):
        raise ValueError("invalid control character in git identity")
    return t.GitIdentity(name.strip(), email)


def display(identity: t.GitIdentity | None) -> str:
    from zab.render.markdown import redact_secrets
    from zab.render.mentions import disarm_mentions

    value = f"{identity.name} <{identity.email}>" if identity else "off (user git config)"
    return " · git: " + disarm_mentions(redact_secrets(value)).replace("`", "'")
