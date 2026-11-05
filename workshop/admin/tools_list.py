#!/usr/bin/env python3
"""The VM's list of its software, `/etc/zab/tools.json` (#475): the agents' section made from it,
and the rollout's list for an image that has none.

    sudo python3 tools_list.py install LIST.json [--dest PATH] [--with FEATURE]...

New images write the manifest when they are built. Schema 1, as `zab.core.software` reads it for
`!tools` (the same rules, kept equal by a test):

    {"schema": 1, "image": "<build name>", "built": "<UTC ISO date>",
     "tools": [{"name": "…", "version": "…", "area": "…", "call": "…", "summary": "…"}]}

An entry needs a non-empty string `name`; its other fields are strings, "" when missing or of
another type, and an entry that isn't an object with a name is skipped. Unknown keys are ignored:
`"unverified": true` marks an entry whose image build didn't check it.

`install` is run by every release rollout of `push_config.py`, as root, with the list checked in
for the Oct 9 image (`tools-nt-agents-2026-v3.json`, beside this script). It writes that list to
`/etc/zab/tools.json` (root, 0644, atomically) only where nothing is there, or where the file is
one it wrote itself (`"origin": "rollout"`), which it then replaces, so a corrected list reaches
the kept VMs. An image's own manifest, readable or not, is never touched. Each entry of the list
names the paths it `requires`; one whose paths aren't all on this VM is left out, so the list
never names a tool the VM lacks (a test image without Magma or Sage, say). An entry may also
hold `with`, fields by feature: where the caller says the VM has a feature (`--with FEATURE`,
which push_config.py passes from what it found there), the entry's fields are that feature's
(the SageMath entry's summary with the `sage` wrapper, #482; `sage_wrapper.py`). The written file
holds schema 1's fields, `unverified`, and the origin, nothing else. Its one line of output starts
with `tools:`.

`agent_instructions.py` imports `section()`: the managed block's tools section, from the VM's
manifest, with the standing rules for using the tools. Standard library only: the VM's system
Python runs it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import tempfile
import unicodedata
from typing import Any

MANIFEST = "/etc/zab/tools.json"
MAX_BYTES = 1 << 20  # as zab.core.software
ORIGIN = "rollout"
FIELDS = ("name", "version", "area", "call", "summary")
OTHER = "Other"
_SPACE = re.compile(r"\s+")
_TICKS = re.compile(r"`+")
#: The standing rule for the agents (Drew, Oct 18: "The agents should know to use the
#: appropriate tools even if the user does not request it"), with a list and without one. The
#: batch-mode line: Magma, run from a background shell that left stdin open, waited at its
#: prompt for 14-26+ minutes after its script (Oct 18).
_USE = "- Use the appropriate tool for the task even when the participant doesn't name one."
_SAY = "- Say which tool, and which version when it matters, produced a result."
_SUDO = "(you have passwordless `sudo` if something is missing)"
_LISTED = (
    "- Prefer the call listed for it (where a section below says more about a tool, as for "
    f"Lean, follow that), and these tools to installing new ones {_SUDO}."
)
_UNLISTED = f"- Prefer what is installed to installing new tools {_SUDO}."
_BATCH = (
    "- Run interpreters in batch mode, with stdin closed or read from a file (`</dev/null`, "
    "`< file`), as the calls below do: one left at its prompt never exits, and holds the turn."
)
RULES = "\n".join([_USE, _LISTED, _BATCH, _SAY])
RULES_UNLISTED = "\n".join([_USE, _UNLISTED, _BATCH.replace(", as the calls below do", ""), _SAY])
#: Each string is cut to this many characters in the instructions, as `!tools` cuts it
#: (zab.core.software.WIDTH; `image` as an area, `built` as a version), and the reason a list
#: can't be used to WHY_MAX.
WIDTH = {"name": 60, "version": 40, "area": 80, "call": 160, "summary": 160}
WHY_MAX = 120
#: Claude Code imports the file of every `@path` that its scanner finds in CLAUDE.md and the
#: files it imports, AGENTS.md among them. The scanner (Claude Code 2.1.302, read and checked
#: live by the review of #478; DECISIONS' log) lexes the file with marked, skips `code` and
#: `codespan` tokens, and runs `/(?:^|\s)@((?:[^\s\\]|\\ )+)/g`, with JavaScript's `\s`
#: (U+FEFF included), on every `text` token; but a list item's text token is its raw line,
#: code spans included, so a code span protects nothing there. Hence an allow-list, not a
#: whitespace class (review 3 of #478): every Unicode format character (category Cf, U+FEFF
#: and the zero-width and bidirectional ones) leaves every list string first; no other string
#: keeps an `@` (AT_LOOKALIKE, U+FF20, takes its place); and a call keeps one only right after
#: an ASCII letter, digit or `.` (`foo@1.0`, CALL_AT), or is left out (CALL_LEFT_OUT).
AT_LOOKALIKE = "\uff20"
CALL_AT = re.compile(r"(?<![A-Za-z0-9.])@")
CALL_LEFT_OUT = "(call left out: see `/etc/zab/tools.json`)"


class ManifestError(ValueError):
    """There is a manifest, but not schema 1's JSON."""


def _json(text: str) -> Any:
    """`json.loads`, any failure a ManifestError: a deeply nested document well under MAX_BYTES
    raises RecursionError, which isn't a ValueError (review of #478). That one's reason is fixed
    text, not the error's: Python 3.14's names the stack it used, which differs from run to run,
    and the reason goes into the managed block, which must not change between two rollouts with
    the same list (#622)."""
    try:
        return json.loads(text)
    except RecursionError:
        raise ManifestError("not JSON: nested too deeply") from None
    except ValueError as exc:
        raise ManifestError(f"not JSON: {str(exc)[:120]}") from None


def _text(value: Any) -> str:
    return _SPACE.sub(" ", value).strip() if isinstance(value, str) else ""


def parse(text: str) -> dict[str, Any]:
    """{"image", "built", "tools": [{FIELDS…, "unverified"}]}; raises ManifestError."""
    data = _json(text)
    if not isinstance(data, dict):
        raise ManifestError("not a JSON object")
    schema = data.get("schema")
    if type(schema) is not int or schema != 1:
        raise ManifestError(f"schema {schema!r}, not 1")
    entries = data.get("tools")
    if not isinstance(entries, list):
        raise ManifestError("no list of tools")
    tools = []
    for entry in entries:
        if isinstance(entry, dict) and _text(entry.get("name")):
            tool = {field: _text(entry.get(field)) for field in FIELDS}
            tools.append(tool | {"unverified": entry.get("unverified") is True})
    return {"image": _text(data.get("image")), "built": _text(data.get("built")), "tools": tools}


def read(path: str) -> str | None:
    """The file's text, None if there is none; raises ManifestError or OSError. Never waits on
    a FIFO, and reads at most MAX_BYTES."""
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
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ManifestError("not UTF-8") from None


def load(path: str = MANIFEST) -> dict[str, Any] | None:
    text = read(path)
    return None if text is None else parse(text)


def code(text: str) -> str:
    """`text` as a code span, whatever backticks it holds."""
    runs = {len(m) for m in _TICKS.findall(text)}
    tick = "`" * next(n for n in range(1, len(runs) + 2) if n not in runs)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{tick}{pad}{text}{pad}{tick}"


def cut(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1].rstrip() + "…"


def comments_off(text: str) -> str:
    """No HTML comment can open or close: the managed block's markers are comments, and the next
    rollout's agent_instructions.py counts them (review of #478)."""
    return text.replace("<!--", "&lt;!--").replace("-->", "--&gt;")


def no_format(text: str) -> str:
    """`text` without its Unicode format characters (category Cf), which no list needs and which
    whitespace classes disagree on (JavaScript's `\\s` takes U+FEFF, Python's doesn't)."""
    return "".join(c for c in text if unicodedata.category(c) != "Cf")


def inert(text: str, width: int) -> str:
    """A list string, cut to `width`, made safe to splice into the managed block: no format
    character, no HTML comment can open or close, and no `@` is left at all (each becomes
    AT_LOOKALIKE), so that no reading of the line, the import scanner's included, finds an
    import in it. This replaces rounds 1 to 3's code spans around `@` words (review 2 of #478:
    Claude Code reads a list item's raw line, code spans included); an address then reads with
    the lookalike."""
    return comments_off(cut(no_format(text), width)).replace("@", AT_LOOKALIKE)


def call_text(call: str) -> str:
    """The call as the section shows it: without format characters, cut, without comment
    delimiters, as code. An `@` stays only right after an ASCII letter, digit or `.`, where
    the import scanner never starts a path; a call with any other `@` is left out, with a
    pointer to the list, since a code span doesn't protect it. A test keeps every call of the
    Oct 9 list and of the image's `tools.toml` whole here: HomotopyContinuation's `@var x y`
    was left out until #486 replaced it."""
    call = comments_off(cut(no_format(call), WIDTH["call"]))
    if not call:
        return ""
    return CALL_LEFT_OUT if CALL_AT.search(call) else code(call)


def grouped(tools: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    """By area, in the order the areas first appear; one without an area last, as "Other"."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for tool in tools:
        groups.setdefault(tool["area"] or OTHER, []).append(tool)
    if OTHER in groups:
        groups[OTHER] = groups.pop(OTHER)
    return list(groups.items())


def section(path: str = MANIFEST) -> tuple[str, str]:
    """The tools section of the agents' instructions, from the manifest at `path`, and a one-line
    outcome for the rollout's output (`tools: …`). Never raises for the manifest's sake: without
    a readable one, the section says so and tells the agents to check before naming a tool.
    Every string of the list goes through `inert`, and the call through `call_text`, so that
    no list can break the block's markers, and every `@` left on a line follows an ASCII
    letter, digit or `.`, where Claude Code's import scanner starts no path: tests check that
    allow-list, and the scanner's pattern with JavaScript's `\\s`, on each rendered line."""
    try:
        manifest = load(path)
        problem = "" if manifest is not None else "missing"
    except (OSError, ManifestError) as exc:
        manifest, problem = None, str(exc) if isinstance(exc, ManifestError) else type(exc).__name__
    head = "## Software on this VM\n\n"
    if manifest is None or not manifest["tools"]:
        why = inert(problem or "it lists no tools", WHY_MAX)
        text = (
            f"{head}This VM's list of its software, `{path}`, can't be used ({why}). Before you "
            "name a tool to the participant or rely on it, check that it is installed "
            "(`command -v NAME`).\n\n" + RULES_UNLISTED + "\n"
        )
        return text, f"tools: none in the instructions ({path}: {why})"
    about = ", ".join(
        part
        for part in (
            f"image {inert(manifest['image'], WIDTH['area'])}" if manifest["image"] else "",
            f"built {inert(manifest['built'], WIDTH['version'])}" if manifest["built"] else "",
        )
        if part
    )
    unverified = any(tool["unverified"] for tool in manifest["tools"])
    lines = [
        f"{head}What this VM has, from its list `{path}`"
        + (f" ({about})" if about else "")
        + "; the participant sees the same list with `!tools`."
        + (
            " Entries marked *unverified* weren't checked when the image was built: try them "
            "before you rely on them."
            if unverified
            else ""
        ),
        "",
        RULES,
    ]
    for area, tools in grouped(manifest["tools"]):
        lines += ["", f"### {inert(area, WIDTH['area'])}"]
        for tool in tools:
            text = f"- **{inert(tool['name'], WIDTH['name'])}**"
            text += f" {inert(tool['version'], WIDTH['version'])}" if tool["version"] else ""
            call = call_text(tool["call"])
            text += f": {call}" if call else ""
            text += f" — {inert(tool['summary'], WIDTH['summary'])}" if tool["summary"] else ""
            lines.append(text + (" *(unverified)*" if tool["unverified"] else ""))
    return "\n".join(lines) + "\n", f"tools: {len(manifest['tools'])} from {path}"


def ours(dest: str) -> tuple[bool | None, str | None]:
    """Whether `dest` is a list this script wrote (None if nothing is there), and its text.
    Anything else there, a link or a file it can't read included, is the image's."""
    if os.path.islink(dest):
        return False, None
    try:
        text = read(dest)
    except (OSError, ManifestError):
        return False, None
    if text is None:
        return None, None
    try:
        data = _json(text)
    except ManifestError:
        return False, None
    return isinstance(data, dict) and data.get("origin") == ORIGIN, text


def featured(entry: dict[str, Any], features: frozenset[str]) -> dict[str, Any]:
    """The entry with the string fields of each of its `with` features that the VM has."""
    variants = entry.get("with")
    if not isinstance(variants, dict):
        return entry
    for feature, fields in variants.items():
        if feature in features and isinstance(fields, dict):
            entry = entry | {f: v for f, v in fields.items() if f in FIELDS and isinstance(v, str)}
    return entry


def install(listing: str, dest: str = MANIFEST, features: frozenset[str] = frozenset()) -> str:
    """Write `listing`'s entries that this VM has to `dest`, unless the image has its own list
    there (the module docstring), each with the fields of the `features` the VM has; the
    one-line outcome."""
    mine, old = ours(dest)
    if mine is False:
        return f"tools: kept the image's own {dest}"
    with open(listing, encoding="utf-8") as f:
        text = f.read()
    parse(text)  # schema 1, or ManifestError
    data = json.loads(text)
    kept = []
    for entry in data["tools"]:
        if not isinstance(entry, dict) or not _text(entry.get("name")):
            continue
        entry = featured(entry, features)
        needs = entry.get("requires", [])
        if all(isinstance(p, str) and os.path.isabs(p) and os.path.exists(p) for p in needs):
            kept.append(
                {f: entry[f] for f in FIELDS if isinstance(entry.get(f), str)}
                | ({"unverified": True} if entry.get("unverified") is True else {})
            )
    out = {"schema": 1, "image": data.get("image", ""), "built": data.get("built", "")}
    out |= {"origin": ORIGIN, "tools": kept}
    text = json.dumps(out, indent=1, ensure_ascii=False) + "\n"
    counted = f"{len(kept)} of {len(data['tools'])} tools are on this VM"
    if text == old:
        return f"tools: {dest} unchanged: {counted}"
    directory = os.path.dirname(dest)
    if not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)
        # 0755 whatever root's umask (a 027 one made it 0750 in CI): the bridge reads the
        # list as agent; an existing directory is the image's, and left as it is
        os.chmod(directory, 0o755)  # noqa: S103 - world-readable by design, root-owned
    fd, tmp = tempfile.mkstemp(prefix=".tools-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, dest)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    verb = "replaced" if mine else "installed"
    return f"tools: {verb} {dest} for {out['image'] or 'this image'}: {counted}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="tools_list.py", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="command", required=True)
    inst = sub.add_parser("install", help="install LIST unless the image has its own list")
    inst.add_argument("listing", metavar="LIST")
    inst.add_argument("--dest", default=MANIFEST)
    inst.add_argument(
        "--with", dest="features", action="append", default=[], metavar="FEATURE",
        help="the VM has FEATURE: entries take their fields under `with` for it",
    )  # fmt: skip
    args = p.parse_args(argv)
    try:
        print(install(args.listing, args.dest, frozenset(args.features)))
    except (OSError, ValueError, RecursionError) as exc:
        print(f"tools: not installed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
