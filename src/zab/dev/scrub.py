"""`zab dev scrub`: remove tokens and personal paths from recorded fixtures (SPEC §15.2).

Fixtures are JSON lines. The scrubber walks every object and rewrites strings with a table of
regular-expression rules, and blanks a few keys whose values describe the recording machine
rather than the protocol (thinking signatures, the account's slash commands, agents and MCP
servers). Review the output before committing (CONTRIBUTING.md, rule 1); `--check` exits 1 when a
file would change, for CI.

The secret patterns are those of SPEC §11.6, so a fixture can never carry a key even if the
agent printed one.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

Rule = tuple[re.Pattern[str], str]

SECRET_RULES: tuple[Rule, ...] = (
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"), "[redacted anthropic key]"),
    (re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{20,}"), "[redacted openai key]"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"), "[redacted github token]"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{50,}"), "[redacted github token]"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[redacted aws access key]"),
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
        "[redacted private key]",
    ),
    # a whole line `key=<32 alnum>`; the lookahead tolerates a CRLF ending and keeps it
    (
        re.compile(r"(?m)^([ \t]*)key=[A-Za-z0-9]{32}[ \t]*(?=\r?$)"),
        r"\1key=[redacted zulip api key]",
    ),
)

EMAIL_RULE: Rule = (
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "user@example.com",
)

#: Keys whose values are identifiers or enumerations, never free text: they are scrubbed with the
#: secret and path rules but never swallowed by a private-key block that spans records, so the
#: fixture keeps its structure (`kind`, `type`, ids) while the key material is redacted.
STRUCTURAL_KEYS = frozenset(
    {
        "kind",
        "type",
        "subtype",
        "role",
        "name",
        "id",
        "uuid",
        "session_id",
        "sessionId",
        "tool_use_id",
        "parent_tool_use_id",
        "hook_id",
        "hook_name",
        "hook_event",
        "model",
        "version",
        "scenario",
        "recorded_at",
        "stop_reason",
        "terminal_reason",
        "status",
        "outcome",
        "task_id",
        "request_id",
        "fixture",
    }
)

#: Keys whose values are replaced wholesale: they describe the recording machine, not the protocol.
BLANK_KEYS: dict[str, Any] = {
    "signature": "",
    "slash_commands": [],
    "terminal_slash_commands": [],
    "agents": [],
    "mcp_servers": [],
    "skills": [],
    "memory_paths": [],
    "messaging_socket_path": "",
    "plugins": [],
}

#: Identity keys replaced with placeholders wherever they appear (account e-mail, org ids).
ZERO_UUID = "00000000-0000-4000-8000-000000000000"
IDENTITY_KEYS: dict[str, Any] = {
    "email": "user@example.com",
    "userEmail": "user@example.com",
    "orgId": ZERO_UUID,
    "organizationUuid": ZERO_UUID,
    "accountUuid": ZERO_UUID,
    "orgName": "",
}

#: Entries dropped from the CLI's `tools` list: the names of the account's MCP tools.
MCP_TOOL_PREFIX = "mcp__"

SCRUBBED_HOME = "/home/user"
SCRUBBED_CWD = "/work/scratch"
SCRUBBED_CONFIG_DIR = "/home/user/.claude"


_PEM_BEGIN = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_PEM_END = re.compile(r"-----END [A-Z ]*PRIVATE KEY-----")
_PEM_REPLACEMENT = "[redacted private key]"


@dataclass(slots=True)
class Scrubber:
    """Applies the rules to strings, JSON values and JSONL lines.

    A private key can be split across records: the recorder stores each stderr line and
    each stdout line separately, so the BEGIN and END markers of a PEM block rarely share a
    string. The scrubber therefore keeps state across the strings it sees, in order: a BEGIN
    without an END redacts the rest of that string and every following string up to and
    including the END marker. Scrub a whole fixture with one `Scrubber` instance.
    """

    rules: tuple[Rule, ...]
    _in_pem: bool = False

    def text(self, value: str, *, structural: bool = False) -> str:
        """Scrub one string; `structural` values never join a PEM block that spans records."""
        if not structural:
            value = self._pem_state(value)
        for pattern, replacement in self.rules:
            value = pattern.sub(replacement, value)
        return value

    def _pem_state(self, value: str) -> str:
        """Redact PEM material that continues from, or into, another string."""
        if self._in_pem:
            end = _PEM_END.search(value)
            if end is None:
                return _PEM_REPLACEMENT
            self._in_pem = False
            value = _PEM_REPLACEMENT + value[end.end() :]
        # Complete blocks inside this string are handled by SECRET_RULES; an unterminated
        # BEGIN after the last END starts a block that continues in the next string.
        last_end = 0
        for m in _PEM_END.finditer(value):
            last_end = m.end()
        begin = _PEM_BEGIN.search(value, last_end)
        if begin is not None:
            self._in_pem = True
            value = value[: begin.start()] + _PEM_REPLACEMENT
        return value

    def json(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self.text(obj)
        if isinstance(obj, list):
            return [self.json(x) for x in obj]
        if isinstance(obj, dict):
            out: dict[str, Any] = {}
            for k, v in obj.items():
                if k in BLANK_KEYS:
                    out[k] = BLANK_KEYS[k]
                elif k in IDENTITY_KEYS and isinstance(v, str | int):
                    out[k] = IDENTITY_KEYS[k]
                elif k == "tools" and isinstance(v, list):
                    # drop the account's MCP tool names; everything kept is scrubbed as usual
                    out[k] = [
                        self.json(t)
                        for t in v
                        if not (isinstance(t, str) and t.startswith(MCP_TOOL_PREFIX))
                    ]
                elif k in STRUCTURAL_KEYS and isinstance(v, str):
                    out[k] = self.text(v, structural=True)
                else:
                    out[k] = self.json(v)
            return out
        return obj

    def line(self, line: str) -> str:
        """Scrub one JSON line; a non-JSON line is scrubbed as text."""
        stripped = line.rstrip("\n")
        if not stripped.strip():
            return stripped
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            return self.text(stripped)
        return json.dumps(self.json(obj), ensure_ascii=False, separators=(",", ":"))


def default_rules(
    *,
    home: str | None = None,
    cwd: str | None = None,
    config_dir: str | None = None,
    hostname: str | None = None,
    replacements: Iterable[tuple[str, str]] = (),
) -> tuple[Rule, ...]:
    """Secrets, then the given literal replacements (longest first), then emails and host name."""
    literals: list[tuple[str, str]] = list(replacements)
    if config_dir:
        literals.append((config_dir.rstrip("/"), SCRUBBED_CONFIG_DIR))
    if cwd:
        literals.append((cwd.rstrip("/"), SCRUBBED_CWD))
    if home:
        literals.append((home.rstrip("/"), SCRUBBED_HOME))
    # The CLI also stores paths in an encoded form (every non-alphanumeric character becomes
    # "-", §9.8): scrub those spellings too, so project directory names are anonymous as well.
    encoded = [(_encode(o), _encode(n)) for o, n in literals if o]
    literals = sorted(literals + encoded, key=lambda kv: -len(kv[0]))
    rules: list[Rule] = list(SECRET_RULES)
    for old, new in literals:
        if old:
            rules.append((re.compile(re.escape(old)), new.replace("\\", "\\\\")))
    rules.append(EMAIL_RULE)
    host = hostname if hostname is not None else socket.gethostname()
    if host and len(host) >= 4:
        rules.append((re.compile(re.escape(host)), "host"))
    return tuple(rules)


def _encode(path: str) -> str:
    """A path as Claude Code names its `projects/` directory for it (§9.8)."""
    return re.sub(r"[^A-Za-z0-9]", "-", path.rstrip("/"))


def scrub_lines(lines: Iterable[str], scrubber: Scrubber) -> list[str]:
    return [scrubber.line(line) for line in lines]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="zab dev scrub", description="Scrub secrets and personal paths from JSONL fixtures."
    )
    parser.add_argument("files", nargs="+", help="fixture files (rewritten in place unless -o)")
    parser.add_argument("-o", "--output", help="write here instead of in place (single input)")
    parser.add_argument("--home", default=os.path.expanduser("~"), help="home dir to replace")
    parser.add_argument("--cwd", help="scratch cwd to replace with /work/scratch")
    parser.add_argument("--config-dir", help="CLAUDE_CONFIG_DIR to replace")
    parser.add_argument("--hostname", help="host name to replace (default: this machine's)")
    parser.add_argument(
        "--replace", action="append", default=[], metavar="OLD=NEW", help="extra literal"
    )
    parser.add_argument("--check", action="store_true", help="exit 1 if any file would change")
    args = parser.parse_args(argv)
    if args.output and len(args.files) != 1:
        parser.error("-o takes exactly one input file")
    extra = []
    for item in args.replace:
        if "=" not in item:
            parser.error(f"--replace needs OLD=NEW, got {item!r}")
        old, new = item.split("=", 1)
        extra.append((old, new))
    rules = default_rules(
        home=args.home,
        cwd=args.cwd,
        config_dir=args.config_dir,
        hostname=args.hostname,
        replacements=extra,
    )
    changed = 0
    for path in args.files:
        with open(path, encoding="utf-8") as f:
            original = f.read().splitlines()
        # One Scrubber per file: a PEM block that spans records never continues into the next
        # fixture (its state is per recording, not per invocation).
        scrubbed = scrub_lines(original, Scrubber(rules))
        if scrubbed != original:
            changed += 1
        if args.check:
            if scrubbed != original:
                print(f"{path}: would change", file=sys.stderr)
            continue
        out = args.output or path
        with open(out, "w", encoding="utf-8") as f:
            f.write("\n".join(scrubbed) + ("\n" if scrubbed else ""))
        print(f"{out}: {'scrubbed' if scrubbed != original else 'unchanged'}", file=sys.stderr)
    return 1 if (args.check and changed) else 0


if __name__ == "__main__":
    raise SystemExit(main())
