"""Owner upload references and durable local attachments (SPEC §10.3)."""

import hashlib
import mimetypes
import re
from dataclasses import asdict
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlsplit

import httpx

from zab.core.types import Attachment
from zab.zulip.client import AttachmentRejected, ZulipError

if TYPE_CHECKING:
    from zab.core.session import SessionActor

LINK = re.compile(r"(?<![\w/])(?:https?://[^\s/<>]+)?/user_uploads/[^\s)<>\]]+")


def upload_paths(content: str, site: str) -> tuple[str, ...]:
    paths = []
    for match in LINK.finditer(content):
        try:
            url = urlsplit(match[0])
        except ValueError:
            continue
        if url.netloc and (
            url.scheme != "https" or url.netloc.lower() != urlsplit(site).netloc.lower()
        ):
            continue
        path = unquote(url.path)
        parts = path.removeprefix("/user_uploads/").split("/")
        if len(parts) < 2 or not parts[0].isdigit() or any(p in ("", ".", "..") for p in parts):
            continue
        if any(ord(char) < 32 for char in path):
            continue
        paths.append(url.path.removeprefix("/"))
    return tuple(dict.fromkeys(paths))


async def load_attachments(actor: "SessionActor", message_ids: list[int]) -> list[Attachment]:
    attachments = []
    held = {row["message_id"]: row["content"] for row in actor.store.held(actor.row["id"])}
    for mid in message_ids:
        for path in upload_paths(held.get(mid, ""), actor.poster.client.site):
            digest = hashlib.sha256(path.encode()).hexdigest()
            key = f"attachment:{actor.row['id']}:{mid}:{digest}"
            saved = actor.store.get(key, None)
            if saved and saved.get("rejected"):
                await report_rejection(actor, key, saved)
                continue
            if saved:
                attachments.append(Attachment(**saved))
                continue
            try:
                temporary = await actor.poster.call(
                    lambda path=path: actor.poster.client.request("GET", path)
                )
                data, mime = await actor.poster.client.download(
                    temporary["url"], actor.context.max_upload_bytes
                )
            except (AttachmentRejected, ZulipError, httpx.HTTPStatusError) as exc:
                status = (
                    exc.status
                    if isinstance(exc, ZulipError)
                    else exc.response.status_code
                    if isinstance(exc, httpx.HTTPStatusError)
                    else None
                )
                if status is not None and (not 400 <= status < 500 or status in (408, 429)):
                    raise
                saved = {"rejected": True, "notified": False}
                actor.store.set(key, saved)
                await report_rejection(actor, key, saved)
                continue
            filename = unquote(path.rsplit("/", 1)[-1])
            mime = mime or mimetypes.guess_type(filename)[0] or "application/octet-stream"
            attachment = await actor.manager.save_attachment(
                actor.context.workspace, mid, filename, data, mime
            )
            actor.store.set(key, asdict(attachment))
            attachments.append(attachment)
    return attachments


async def report_rejection(actor: "SessionActor", key: str, saved: dict) -> None:
    if not saved["notified"]:
        await actor.post(
            actor.note(
                "An attachment was rejected (unavailable, unsafe download, or over the size "
                "limit). Continuing without that file; upload it again to retry."
            )
        )
        actor.store.set(key, saved | {"notified": True})
