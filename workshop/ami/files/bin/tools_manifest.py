#!/usr/bin/env python3
"""tools_manifest.py: the image's tools, each checked, as /etc/zab/tools.json (issue #474).

    tools_manifest.py build --source tools.toml --image NAME [--have magma,sage] > tools.json
    tools_manifest.py check --source tools.toml --manifest /etc/zab/tools.json [--have …]
                            [--only NAME]
    tools_manifest.py lint --source tools.toml

The source (`workshop/ami/files/tools.toml`, its header documents the format) lists every tool a
participant or an agent would call. `build` runs each tool's version command and its check, a
small real computation, in a scratch directory and in this process's environment (`build.sh`
runs it as agent, with the services' PATH), one line per tool on stderr. Only if every check
passed does it print the manifest on stdout; a failing tool fails the build, nothing is
dropped. `check` is the smoke test on a fresh instance from the image: the same checks again,
and each version must be the one the manifest records. A tool with `requires` is checked only
on an image built with that component (`--have`, from build.sh's MAGMA_TARBALL and WITH_SAGE).
A `remote` tool is a service off the VM (the LMFDB server): its check is tried three times, and
`check` reports a version that moved since the build instead of failing on it (review on #480).

The manifest's contract (`!tools` and the agents' instructions read it):
    {"schema": 1, "image": "<build name>", "built": "<UTC ISO date>",
     "tools": [{"name", "version", "area", "call", "summary"}, ...]}   in the source's order.

Standard library only: the VM's system Python runs it.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

SCHEMA = 1
#: The optional components a tool may require (build.sh: MAGMA_TARBALL, WITH_SAGE=1).
COMPONENTS = ("magma", "sage")
REQUIRED_KEYS = ("name", "area", "call", "summary", "version", "check", "expect")
OPTIONAL_KEYS = ("timeout", "requires", "remote")
#: The manifest's keys per tool, in this order (the contract above).
MANIFEST_KEYS = ("name", "version", "area", "call", "summary")
MAX_SUMMARY = 120
MAX_CALL = 100
DEFAULT_TIMEOUT = 300
#: A remote tool's check: attempts, and seconds between them.
REMOTE_TRIES = (3, 20)
VERSION_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+~_:-]{0,39}")


class SourceError(ValueError):
    """The source file breaks the format its header documents."""


@dataclass(frozen=True)
class Tool:
    name: str
    area: str
    call: str
    summary: str
    version: str
    check: str
    expect: str
    timeout: int = DEFAULT_TIMEOUT
    requires: str = ""
    remote: bool = False

    def entry(self, version: str) -> dict[str, str]:
        return {
            "name": self.name,
            "version": version,
            "area": self.area,
            "call": self.call,
            "summary": self.summary,
        }


@dataclass(frozen=True)
class Source:
    areas: tuple[str, ...]
    tools: tuple[Tool, ...]

    def applicable(self, have: set[str]) -> list[Tool]:
        return [t for t in self.tools if not t.requires or t.requires in have]


def parse_source(data: dict[str, Any]) -> Source:
    """Validate the parsed TOML; every problem is named, the first one raises."""
    unknown = set(data) - {"schema", "areas", "tool"}
    if unknown:
        raise SourceError(f"unknown top-level keys: {sorted(unknown)}")
    if data.get("schema") != SCHEMA:
        raise SourceError(f"schema must be {SCHEMA}, not {data.get('schema')!r}")
    areas = data.get("areas")
    if not isinstance(areas, list) or not areas or not all(isinstance(a, str) and a for a in areas):
        raise SourceError("areas must be a non-empty list of names")
    if len(set(areas)) != len(areas):
        raise SourceError("areas has duplicates")
    raw = data.get("tool")
    if not isinstance(raw, list) or not raw:
        raise SourceError("no [[tool]] entries")
    tools: list[Tool] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw):
        where = (
            f"tool {i + 1} ({entry.get('name', '?')!r})"
            if isinstance(entry, dict)
            else f"tool {i + 1}"
        )
        if not isinstance(entry, dict):
            raise SourceError(f"{where}: not a table")
        extra = set(entry) - set(REQUIRED_KEYS) - set(OPTIONAL_KEYS)
        if extra:
            raise SourceError(f"{where}: unknown keys {sorted(extra)}")
        for key in REQUIRED_KEYS:
            value = entry.get(key)
            if not isinstance(value, str) or not value.strip():
                raise SourceError(f"{where}: {key} must be a non-empty string")
        for key, limit in (("name", 40), ("call", MAX_CALL), ("summary", MAX_SUMMARY)):
            value = entry[key]
            if "\n" in value or value != value.strip() or len(value) > limit:
                raise SourceError(f"{where}: {key} must be one line of at most {limit} characters")
        if "\n" in entry["expect"] or entry["expect"] != entry["expect"].strip():
            raise SourceError(f"{where}: expect must be one line without outer spaces")
        if entry["area"] not in areas:
            raise SourceError(f"{where}: area {entry['area']!r} is not in areas")
        if entry["name"].casefold() in seen:
            raise SourceError(f"{where}: duplicate name")
        seen.add(entry["name"].casefold())
        timeout = entry.get("timeout", DEFAULT_TIMEOUT)
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 3600:
            raise SourceError(f"{where}: timeout must be 1 to 3600 seconds")
        requires = entry.get("requires", "")
        if requires and requires not in COMPONENTS:
            raise SourceError(f"{where}: requires must be one of {COMPONENTS}")
        remote = entry.get("remote", False)
        if not isinstance(remote, bool):
            raise SourceError(f"{where}: remote must be true or false")
        # the manifest lists the tools grouped by area, the areas in their fixed order
        if tools and areas.index(entry["area"]) < areas.index(tools[-1].area):
            raise SourceError(f"{where}: area {entry['area']!r} comes after {tools[-1].area!r}")
        tools.append(
            Tool(
                **{k: entry[k] for k in REQUIRED_KEYS},
                timeout=timeout,
                requires=requires,
                remote=remote,
            )
        )
    unused = [a for a in areas if not any(t.area == a for t in tools)]
    if unused:
        raise SourceError(f"areas without a tool: {unused}")
    return Source(tuple(areas), tuple(tools))


def load_source(path: str) -> Source:
    with open(path, "rb") as f:
        return parse_source(tomllib.load(f))


@dataclass
class Run:
    code: int | None  # None: timed out
    out: str
    err: str


def run_shell(command: str, cwd: str, timeout: int) -> Run:
    """`bash -c command` in its own process group, killed whole on timeout (Julia, Sage and
    Magma start children)."""
    proc = subprocess.Popen(  # noqa: S603 - the commands are the checked-in source's
        ["bash", "-c", command],  # noqa: S607
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:  # a grandchild that left the group may still hold the pipes
            out, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = b"", b""
        return Run(None, out.decode(errors="replace"), err.decode(errors="replace"))
    return Run(proc.returncode, out.decode(errors="replace"), err.decode(errors="replace"))


#: A hardware address, colon- or dash-separated. A failing tool's output can list the host's
#: (a licence refusal, say), and `fail` keeps the last lines for the build log and the smoke
#: output: they carry none (review of #595). No word boundaries: an address glued to other
#: text is still one, and hiding a little too much in a failure's log costs nothing.
HARDWARE_ADDRESS = re.compile(r"(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}")


def redacted(run: Run) -> Run:
    """`run` with every hardware address in its output replaced, before anything is cut short
    (a truncated address would no longer match)."""
    hide = "<hardware address>"
    return Run(run.code, HARDWARE_ADDRESS.sub(hide, run.out), HARDWARE_ADDRESS.sub(hide, run.err))


def lines(text: str) -> list[str]:
    return [line.rstrip() for line in text.replace("\r", "\n").split("\n") if line.strip()]


@dataclass
class Result:
    tool: Tool
    version: str
    ok: bool
    reason: str
    seconds: float
    detail: list[str]


Runner = Callable[[str, str, int], Run]


def check_tool(
    tool: Tool, runner: Runner = run_shell, pause: Callable[[float], None] = time.sleep
) -> Result:
    """The version command, then the check, each in a fresh scratch directory; a remote tool's
    up to REMOTE_TRIES[0] times (a service off the VM can blink)."""
    tries, wait = REMOTE_TRIES if tool.remote else (1, 0)
    for attempt in range(1, tries + 1):
        result = check_once(tool, runner)
        if result.ok or attempt == tries:
            if attempt > 1:
                result.reason += f" (attempt {attempt} of {tries})"
            return result
        pause(wait)
    raise AssertionError("unreachable")


def check_once(tool: Tool, runner: Runner) -> Result:
    start = time.monotonic()

    def fail(reason: str, run: Run, version: str = "") -> Result:
        detail = (lines(run.err) or lines(run.out))[-6:]
        return Result(tool, version, False, reason, time.monotonic() - start, detail)

    with tempfile.TemporaryDirectory(prefix="zab-tool-") as cwd:
        run = redacted(runner(tool.version, cwd, tool.timeout))
    if run.code is None:
        return fail(f"version command timed out after {tool.timeout} s", run)
    out = lines(run.out)
    version = out[0].strip() if out else ""
    if run.code != 0 or not VERSION_RE.fullmatch(version) or not re.search(r"\d", version):
        return fail(f"version command gave {version[:60]!r} (exit {run.code})", run)
    with tempfile.TemporaryDirectory(prefix="zab-tool-") as cwd:
        run = redacted(runner(tool.check, cwd, tool.timeout))
    if run.code is None:
        return fail(f"check timed out after {tool.timeout} s", run, version)
    out = lines(run.out)
    got = out[-1].strip() if out else ""
    if run.code != 0 or got != tool.expect:
        return fail(
            f"check gave {got[:80]!r}, expected {tool.expect!r} (exit {run.code})", run, version
        )
    return Result(tool, version, True, "", time.monotonic() - start, [])


def report(result: Result, log: Callable[[str], None]) -> None:
    if result.ok:
        log(f"ok   {result.tool.name} {result.version} ({result.seconds:.1f} s)")
        return
    log(f"FAIL {result.tool.name}: {result.reason} ({result.seconds:.1f} s)")
    for line in result.detail:
        log(f"     | {line[:200]}")


def manifest(
    image: str, results: list[Result], today: datetime.date | None = None
) -> dict[str, Any]:
    built = (today or datetime.datetime.now(datetime.UTC).date()).isoformat()
    return {
        "schema": SCHEMA,
        "image": image,
        "built": built,
        "tools": [r.tool.entry(r.version) for r in results],
    }


def parse_have(text: str) -> set[str]:
    have = {c.strip() for c in text.split(",") if c.strip()}
    unknown = have - set(COMPONENTS)
    if unknown:
        raise SystemExit(f"--have: unknown components {sorted(unknown)} (known: {COMPONENTS})")
    return have


def cmd_build(
    source: Source, image: str, have: set[str], runner: Runner, log: Callable[[str], None]
) -> tuple[int, str]:
    results = []
    for tool in source.applicable(have):
        result = check_tool(tool, runner)
        report(result, log)
        results.append(result)
    failed = [r.tool.name for r in results if not r.ok]
    if failed:
        names = ", ".join(failed)
        log(f"tools manifest NOT written: {len(failed)} of {len(results)} checks failed: {names}")
        return 1, ""
    log(f"tools manifest: {len(results)} of {len(results)} tools checked")
    return 0, json.dumps(manifest(image, results), indent=1, ensure_ascii=False) + "\n"


def cmd_check(
    source: Source,
    recorded: dict[str, Any],
    have: set[str],
    only: list[str],
    runner: Runner,
    log: Callable[[str], None],
) -> int:
    fail = 0
    tools = source.applicable(have)
    listed = recorded.get("tools") if isinstance(recorded, dict) else None
    if not isinstance(listed, list) or not all(isinstance(e, dict) for e in listed):
        log("FAIL the manifest is not an object with a list of tool objects")
        return 1
    entries = {e.get("name"): e for e in listed}
    if len(entries) != len(listed):
        log("FAIL the manifest lists a tool twice")
        fail = 1
    if recorded.get("schema") != SCHEMA or not recorded.get("image") or not recorded.get("built"):
        log(f"FAIL the manifest's header: schema {recorded.get('schema')!r}, image, built")
        fail = 1
    names = [t.name for t in tools]
    if sorted(entries) != sorted(names):
        log(
            f"FAIL the manifest lists {sorted(set(entries) - set(names))} beyond the source and "
            f"lacks {sorted(set(names) - set(entries))}"
        )
        fail = 1
    if only:
        missing = set(only) - set(names)
        if missing:
            log(f"FAIL --only: no such tool {sorted(missing)}")
            return 1
        tools = [t for t in tools if t.name in only]
    checked = 0
    for tool in tools:
        result = check_tool(tool, runner)
        entry = entries.get(tool.name, {})
        recorded_version = str(entry.get("version", ""))
        moved = (
            result.ok
            and tool.remote
            and VERSION_RE.fullmatch(recorded_version) is not None
            and entry == tool.entry(recorded_version)
        )
        if result.ok and entry != tool.entry(result.version) and not moved:
            result.ok = False
            result.reason = (
                f"the manifest records {entry}, the check saw version {result.version!r}"
            )
        report(result, log)
        if moved and entry.get("version") != result.version:
            log(f"     | note: a remote service; the build recorded {entry.get('version')!r}")
        checked += 1
        fail |= not result.ok
    log(f"tools check: {checked} checked, {'FAILED' if fail else 'all as recorded'}")
    return 1 if fail else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("build", "check", "lint"):
        p = sub.add_parser(name)
        p.add_argument("--source", required=True)
        if name != "lint":
            p.add_argument("--have", default="", help="comma-separated: magma, sage")
    sub.choices["build"].add_argument("--image", required=True)
    sub.choices["check"].add_argument("--manifest", required=True)
    sub.choices["check"].add_argument("--only", action="append", default=[], metavar="NAME")
    opts = parser.parse_args(argv)

    def log(msg: str) -> None:
        print(msg, file=sys.stderr, flush=True)

    try:
        source = load_source(opts.source)
    except (OSError, tomllib.TOMLDecodeError, SourceError) as exc:
        log(f"FAIL {opts.source}: {exc}")
        return 1
    if opts.cmd == "lint":
        log(f"ok   {opts.source}: {len(source.tools)} tools in {len(source.areas)} areas")
        return 0
    have = parse_have(opts.have)
    if opts.cmd == "build":
        code, text = cmd_build(source, opts.image, have, run_shell, log)
        sys.stdout.write(text)
        return code
    try:
        with open(opts.manifest) as f:
            recorded = json.load(f)
    except (OSError, ValueError) as exc:
        log(f"FAIL {opts.manifest}: {exc}")
        return 1
    return cmd_check(source, recorded, have, opts.only, run_shell, log)


if __name__ == "__main__":
    sys.exit(main())
