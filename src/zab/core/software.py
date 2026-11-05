"""`!tools`: the software on this VM, from its manifest `/etc/zab/tools.json` (SPEC §4.6; #475).

New images write the manifest; for an image without one, the rollout installs the list of the
Oct 9 image (`workshop/admin/tools_list.py`). Schema 1:

    {"schema": 1, "image": "<build name>", "built": "<UTC ISO date>",
     "tools": [{"name": "Macaulay2", "version": "1.26.05", "area": "Commutative algebra …",
                "call": "M2 --script file.m2", "summary": "one short line"}]}

An entry needs a non-empty string `name`; its other fields are strings, empty when missing or of
another type, and an entry that isn't an object with a name is skipped. Unknown keys are ignored,
so optional fields can join schema 1: the rollout's list marks an entry `"unverified": true`
when the image's build didn't record it. Any other `schema` is unreadable, never guessed at.

The reply groups the tools by `area`, in the order the areas first appear (one without an area
last, under "Other"), one line per tool: its name in bold, the version, the call as code and the
summary. Over the message limit, the summaries go first; then the tools at the end, with a
line that says how many are left out and where the whole list is. The bridge answers it itself,
in a DM or a topic, never through an agent turn; the text is the manifest's, with mentions
disarmed. A missing manifest, or one that can't be read, is one plain sentence.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import stat
from dataclasses import dataclass
from typing import Any

from zab.render.mentions import disarm_mentions

MANIFEST = "/etc/zab/tools.json"
#: Larger than any real list (the Oct 9 image's is about 6 KB); more is not read.
MAX_BYTES = 1 << 20
MISSING = (
    "This VM has no list of its software (`/etc/zab/tools.json`); ask an agent what is installed."
)
UNREADABLE = (
    "This VM's list of its software (`/etc/zab/tools.json`) can't be read; please tell an "
    "organizer."
)
EMPTY = "This VM's list of its software (`/etc/zab/tools.json`) names no tools."
OTHER = "Other"
FOOTER = (
    "The agents are given this list and told to use these tools when they fit, whether or not "
    "you name one."
)
#: Each field is cut to this many characters (an ellipsis marks the cut).
WIDTH = {"name": 60, "version": 40, "area": 80, "call": 160, "summary": 160}
_SPACE = re.compile(r"\s+")
_TICKS = re.compile(r"`+")


class ManifestError(ValueError):
    """The manifest is there but isn't schema 1's JSON."""


@dataclass(frozen=True)
class Tool:
    name: str
    version: str = ""
    area: str = ""
    call: str = ""
    summary: str = ""
    unverified: bool = False


@dataclass(frozen=True)
class Manifest:
    image: str
    built: str
    tools: tuple[Tool, ...]


def _text(value: Any, field: str) -> str:
    """One line of at most WIDTH[field] characters; "" for anything but a string."""
    if not isinstance(value, str):
        return ""
    text = _SPACE.sub(" ", value).strip()
    width = WIDTH.get(field, 80)
    return text if len(text) <= width else text[: width - 1].rstrip() + "…"


def parse(text: str) -> Manifest:
    """Schema 1, as the module docstring says; raises ManifestError."""
    try:
        data = json.loads(text)
    except (ValueError, RecursionError) as exc:  # deep nesting, under MAX_BYTES (#478 review)
        raise ManifestError(f"not JSON: {str(exc)[:120]}") from None
    if not isinstance(data, dict):
        raise ManifestError("not a JSON object")
    schema = data.get("schema")
    if type(schema) is not int or schema != 1:
        raise ManifestError(f"schema {schema!r}, not 1")
    tools = data.get("tools")
    if not isinstance(tools, list):
        raise ManifestError("no list of tools")
    found = []
    for entry in tools:
        if not isinstance(entry, dict) or not (name := _text(entry.get("name"), "name")):
            continue
        found.append(
            Tool(
                name=name,
                **{f: _text(entry.get(f), f) for f in ("version", "area", "call", "summary")},
                unverified=entry.get("unverified") is True,
            )
        )
    image, built = _text(data.get("image"), "area"), _text(data.get("built"), "version")
    return Manifest(image, built, tuple(found))


def load(path: str | None = None) -> Manifest | None:
    """The manifest at `path` (MANIFEST), None if there is none; raises ManifestError or
    OSError. Never waits on a FIFO, and reads at most MAX_BYTES."""
    path = path or MANIFEST
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ManifestError("not a regular file")
        data = b""
        while len(data) <= MAX_BYTES and (chunk := os.read(fd, MAX_BYTES + 1 - len(data))):
            data += chunk
    finally:
        os.close(fd)
    if len(data) > MAX_BYTES:
        raise ManifestError(f"larger than {MAX_BYTES} bytes")
    try:
        return parse(data.decode("utf-8"))
    except UnicodeDecodeError:
        raise ManifestError("not UTF-8") from None


def code(text: str) -> str:
    """`text` as a code span, whatever backticks it holds."""
    runs = {len(m) for m in _TICKS.findall(text)}
    tick = "`" * next(n for n in range(1, len(runs) + 2) if n not in runs)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{tick}{pad}{text}{pad}{tick}"


def line(tool: Tool, *, summary: bool = True) -> str:
    parts = [f"**{tool.name}**" + (f" {tool.version}" if tool.version else "")]
    if tool.call:
        parts.append(code(tool.call))
    if summary and tool.summary:
        parts.append(tool.summary)
    return "- " + " · ".join(parts) + (" (unverified)" if tool.unverified else "")


def _grouped(tools: tuple[Tool, ...]) -> list[tuple[str, list[Tool]]]:
    groups: dict[str, list[Tool]] = {}
    for tool in tools:
        groups.setdefault(tool.area or OTHER, []).append(tool)
    if OTHER in groups:
        groups[OTHER] = groups.pop(OTHER)  # last
    return list(groups.items())


def _compose(manifest: Manifest, shown: int, *, summary: bool) -> str:
    about = ", ".join(
        part
        for part in (
            f"image {manifest.image}" if manifest.image else "",
            f"built {manifest.built}" if manifest.built else "",
        )
        if part
    )
    out = ["**Software on this VM**" + (f" ({about})" if about else "")]
    left = shown
    for area, tools in _grouped(manifest.tools):
        if left <= 0:
            break
        out += ["", f"**{area}**", *(line(t, summary=summary) for t in tools[:left])]
        left -= len(tools)
    out.append("")
    hidden = len(manifest.tools) - shown
    if hidden > 0:
        out.append(
            f"… and {hidden} more: ask an agent to show you the whole list, `/etc/zab/tools.json`."
        )
    out.append(FOOTER)
    return disarm_mentions("\n".join(out))


def render(manifest: Manifest, limit: int) -> str:
    """The reply, at most `limit` characters (see the module docstring)."""
    if not manifest.tools:
        return EMPTY
    total = len(manifest.tools)
    for summary in (True, False):
        text = _compose(manifest, total, summary=summary)
        if len(text) <= limit:
            return text
    # the most tools, in the reply's order, that fit with the line saying how many are left
    fits, over = 0, total
    while over - fits > 1:
        middle = (fits + over) // 2
        if len(_compose(manifest, middle, summary=False)) <= limit:
            fits = middle
        else:
            over = middle
    return _compose(manifest, fits, summary=False)[:limit]


async def reply(limit: int, path: str | None = None) -> str:
    """`!tools`'s answer, from `path` (MANIFEST); never raises."""
    path = path or MANIFEST
    try:
        manifest = await asyncio.to_thread(load, path)
        return MISSING if manifest is None else render(manifest, limit)
    except Exception as exc:
        logging.getLogger("zab.tools").warning(
            "Can't read %s: %s", path, exc if isinstance(exc, ManifestError) else type(exc).__name__
        )
        return UNREADABLE
