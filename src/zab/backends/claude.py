"""Claude Code backend (SPEC §9): drives the unmodified `claude` binary in headless mode.

One `claude -p --output-format stream-json --verbose` process per turn (§9.1, DECISIONS D5).
This module has three layers, from pure to impure:

1. `ClaudeConfig`, `build_argv`, `turn_env`, `new_turn_id`: how a turn is invoked (§9.1 to §9.3).
2. `ClaudeTurn` and `run_turn`: the per-turn subprocess, in its own session and process group,
   with the interrupt sequence of §9.6 and a bounded stderr tail.

The pure stream-json → contract-event mapping (§9.4) is `claude_stream.py`.

The `Backend` implementation that plugs these into the core arrives in M2 (§16).
All protocol facts were checked against Claude Code 2.1.283 (docs/DECISIONS.md).
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import math
import os
import random
import re
import shlex
import shutil
import signal
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from zab.core.presentation import bridge_note
from zab.core.reason import doctor_reason
from zab.core.types import (
    ApprovalDecision,
    BackendEvent,
    BackendKind,
    BackendStatus,
    BackendUnavailable,
    Capabilities,
    Check,
    ErrorKind,
    GitIdentity,
    InvalidSetting,
    SessionChanged,
    SessionInfo,
    SessionSettings,
    SessionSpec,
    SteerRejected,
    TokenUsage,
    TurnCompleted,
    TurnStatus,
    TurnSummary,
    Usage,
    UsageUpdated,
    UserInput,
    Workspace,
)
from zab.render.redact import redact_secrets

log = logging.getLogger("zab.backends.claude")

PINNED_VERSION = "2.1.283"  # docs/DECISIONS.md, Pins

#: The doctor's line for a signed-out account: the workshop path is `!login` in a DM (issue #36).
NOT_SIGNED_IN = "not signed in: send !login to this bot in a direct message"

#: Removed from every turn's environment (§9.1): an API key silently overrides the login.
SCRUBBED_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",  # a headless login token would override the account dir (DECISIONS)
    "ANTHROPIC_BASE_URL",  # would redirect the API traffic
    "ANTHROPIC_CUSTOM_HEADERS",
    "CLAUDE_CODE_USE_BEDROCK",  # third-party providers: Phase 1 is the first-party login only
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
    "AWS_BEARER_TOKEN_BEDROCK",
    "CLAUDE_CODE_EFFORT_LEVEL",  # overrides `--effort`, so `!effort` would do nothing (DECISIONS)
)

#: Always removed too: they mark a nested Claude Code session (set when zab itself runs inside
#: one, as the dev tools do) and would make the child believe it is nested.
NESTING_ENV = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")
#: zab's own, set from the spec or removed, never inherited from the bridge: the session's bot
#: for `zab history` (issue #248) and its id for `zab job` (issue #655; SPEC §5.6).
ZAB_ENV = ("ZAB_BOT", "ZAB_SESSION")
#: Who the agent's commits are made as (issue #214, SPEC §5.5): set for a session with a
#: `git_identity`, removed otherwise so that the user's own git config applies.
GIT_IDENTITY_ENV = (
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
)
#: With a `git_identity` the author already says it was Claude, so Claude Code's own commit
#: attribution (a `Co-Authored-By: Claude … <noreply@anthropic.com>` trailer, which GitHub shows
#: as a second author) is turned off for the turn. The object form, which every version since
#: 2.1.283 accepts; `--settings` merges with the user, project and managed settings (DECISIONS).
NO_COMMIT_ATTRIBUTION = '{"attribution":{"commit":""}}'

#: `--effort` levels, from `claude --help` (2.1.283 and 2.1.289). The CLI runs any other value
#: as if no effort were given, without a word (DECISIONS), so the bridge refuses it (§7.9).
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
#: `--model` aliases: the CLI's own table (the same in 2.1.283 and 2.1.289; `--help` names three
#: as examples), plus `default`, in any case. Any other model must be a full `claude-…` name,
#: which may still not exist: `update_settings` checks it with one short turn (§7.9).
MODEL_ALIASES = (
    "default",
    "opus",
    "sonnet",
    "haiku",
    "fable",
    "best",
    "opusplan",
    "opus[1m]",
    "sonnet[1m]",
    "fable[1m]",
)
_FULL_MODEL = re.compile(r"claude-[a-z0-9][a-z0-9.-]*(\[1m\])?", re.I)
MODEL_CHECK_TIMEOUT_S = 60.0
#: The check's working directory: always the same one, because the CLI makes a project entry
#: (an empty `memory/`) in the account for every directory it runs in.
MODEL_CHECK_DIR = "~/.cache/zab/model-check"
MODEL_CHECK_PROMPT = "Reply with the single word OK."
#: Where the latest usage observations are kept across restarts (`claude_usage`, `!usage`).
USAGE_FILE = "~/.cache/zab/claude-usage.json"
#: The CLI's OAuth refresh lock (2.1.283 bundle, DECISIONS 2026-10-18, #450 and #483): a lock
#: untouched for CLI_REFRESH_LOCK_STALE_S is stale and taken (its holder touches it every 5 s).
#: A process that finds it held looks again after CLI_REFRESH_LOCK_WAIT_S: touched meanwhile,
#: the holder counts as alive and its new token is picked up, without an error. Untouched, the
#: CLI tries a dead-holder takeover, which needs an owner record naming a pid it can prove gone
#: (and a flag on by default); failing that, the try loses the race. So a loss means a holder
#: that didn't touch the lock for 7.5 s: gone without a usable owner record (SIGKILL skips the
#: CLI's exit handler), or alive but stalled. The lock goes when its holder releases it, at a
#: takeover, or when it is stale. Its mtime may be up to 1 s ahead (set to the next whole
#: second when taken); the last try's own lock retries, 5 to 10 s of them, absorb that.
CLI_REFRESH_LOCK_STALE_S = 60.0
CLI_REFRESH_LOCK_WAIT_S = 7.5
#: The lock (a directory, proper-lockfile's) in the account's config directory, its owner
#: record (JSON: the holder's `pid`, process start and PID namespace, the lock's birth time),
#: and the legacy lock `<realpath of the config directory>.lock` that the CLI takes after it:
#: what a lost race's log line reports (`refresh_lock_state`, #483). None holds a credential.
CLI_REFRESH_LOCK = ".oauth_refresh.lock"
CLI_REFRESH_LOCK_OWNER = ".oauth_refresh.lock.owner"
CLI_REFRESH_LEGACY_LOCK = ".lock"  # a suffix to the config directory's real path
#: A turn that loses the refresh race before doing anything (`is_refresh_race`) runs again, up
#: to this many times: after `REFRESH_RETRY_DELAY_S` plus up to `REFRESH_RETRY_JITTER_S`, in
#: case the lock was about to go stale, and the last try `REFRESH_LAST_TRY_S` after the first
#: loss at the earliest, when the lock that try met is stale for sure.
REFRESH_RETRIES = 2
REFRESH_RETRY_DELAY_S = 2.0
REFRESH_RETRY_JITTER_S = 3.0
REFRESH_LAST_TRY_S = CLI_REFRESH_LOCK_STALE_S - CLI_REFRESH_LOCK_WAIT_S + 1.5  # 54 s
#: How long a turn waits, before its process starts, for the account's `claude auth status` to
#: end (`ClaudeBackend.doctor`): the two never refresh the sign-in at once (#307, #450). Kept
#: after a boot too (#483, DECISIONS 2026-10-18): in the run that asked, no wait was cut short,
#: so a longer one would not have changed it. A longer wait helps only when the bridge's own
#: `auth status` holds the lock and stalls past it; the log lines show that if it happens
#: (`starts while auth status still runs`, then a lost race naming its pid).
AUTH_WAIT_S = 15.0
#: `claude auth status` (the doctor's and `!login`'s): its time limit; then SIGTERM, and SIGKILL
#: `AUTH_STATUS_KILL_GRACE_S` later (`_run_capture`). On SIGTERM the CLI runs its own shutdown,
#: waits up to 2 s for a refresh it holds, and exits within a failsafe of at least 5 s (6.5 s
#: with the default 1.5 s for session-end hooks, 2 s more while a refresh is held); its lock
#: library's exit handler removes the lock at that exit. A SIGKILL before then leaves the lock.
AUTH_STATUS_TIMEOUT_S = 30.0
AUTH_STATUS_KILL_GRACE_S = 10.0
#: `close()`: how long a running `auth status` may go on before its SIGTERM, so that the
#: bridge's stop stays well inside systemd's `TimeoutStopSec=30`.
AUTH_STATUS_CLOSE_WAIT_S = 2.0
#: `signed_in`: how long a cached "not signed in" from `auth status` is believed (#687). An
#: older one is unknown: the owner may have signed in at the terminal since.
SIGNED_OUT_TRUSTED_S = 600.0

PlanStep = tuple[str, str]  # (text, "pending" | "in_progress" | "completed")


# ------------------------------------------------------------------------------ invocation (§9.1)


@dataclass(slots=True, kw_only=True)
class ClaudeConfig:
    """The `[backends.claude]` table (§5.2). The config loader builds it."""

    binary: str = "claude"
    accounts: dict[str, str] = field(default_factory=lambda: {"main": "~/.claude"})
    default_account: str = "main"
    defaults: SessionSettings = field(default_factory=SessionSettings)
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    # The claude.ai connectors of the signed-in account (Gmail, Drive, …) are off in bridge
    # turns unless this is set: unattended `bypassPermissions` agents must not reach a
    # participant's mail (PM on issue #36; DECISIONS). User-scope MCP servers are unaffected.
    claudeai_connectors: bool = False
    # Mid-turn steering (PM on issue #36, Fri Oct 9): the prompt goes in as stream-json, stdin
    # stays open, and a message sent during a turn is written to it (`StreamInput`); off, a
    # turn runs exactly as before. The table turns it on when the key is absent (Drew, Oct 15,
    # issue #310: `BackendConfig.steer`, and `from_mapping` below); a `ClaudeConfig` built
    # directly (the dev tools, unit tests) keeps the plain invocation unless told otherwise.
    steer: bool = False

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> ClaudeConfig:
        """Build from the parsed TOML table; empty strings mean "agent default" (§5.2)."""
        d = dict(data.get("defaults") or {})
        defaults = SessionSettings(
            model=d.get("model") or None,
            effort=d.get("effort") or None,
            approval=d.get("approval") or None,
        )
        return cls(
            binary=data.get("binary") or "claude",
            accounts=dict(data.get("accounts") or {"main": "~/.claude"}),
            default_account=data.get("default_account") or "main",
            defaults=defaults,
            allowed_tools=list(data.get("allowed_tools") or []),
            disallowed_tools=list(data.get("disallowed_tools") or []),
            claudeai_connectors=bool(data.get("claudeai_connectors", False)),
            steer=bool(data.get("steer", True)),  # on unless `steer = false` (#310)
        )

    def config_dir(self, account: str | None) -> str:
        """The CLAUDE_CONFIG_DIR for an account name (§9.2), `~` expanded."""
        name = account or self.default_account
        try:
            return os.path.expanduser(self.accounts[name])
        except KeyError:
            raise KeyError(
                f"unknown Claude account {name!r}; known: {sorted(self.accounts)}"
            ) from None


def effective_settings(config: ClaudeConfig, settings: SessionSettings) -> SessionSettings:
    """Backend defaults overlaid by the session's settings (§7.2 precedence, backend part)."""
    d = config.defaults
    return SessionSettings(
        model=settings.model or d.model,
        effort=settings.effort or d.effort,
        approval=settings.approval or d.approval,
        sandbox=None,
        account=settings.account or config.default_account,
        extra=dict(settings.extra),
    )


#: The last "valid" choice of a refused model: any full name (§7.9).
FULL_NAME_CHOICE = "claude-<full name>"


def check_model_name(model: str) -> None:
    """Raise `InvalidSetting` unless `model` is an alias or a full `claude-…` name (§7.9).

    An unknown value carries no message: the core's "Unknown model … Valid: …" says it all. A
    message is only for refusals that sentence would misstate, and is plain text (no Markdown):
    the core quotes it.
    """
    if model.lower() in MODEL_ALIASES or _FULL_MODEL.fullmatch(model):
        return
    raise InvalidSetting("model", model, [*MODEL_ALIASES, FULL_NAME_CHOICE])


def check_effort(effort: str) -> str:
    """The effort level in lower case; `InvalidSetting` for anything else (§7.9)."""
    if effort.lower() in EFFORT_LEVELS:
        return effort.lower()
    raise InvalidSetting("effort", effort, list(EFFORT_LEVELS))


def build_argv(
    config: ClaudeConfig,
    settings: SessionSettings,
    *,
    session_id: str,
    resume: bool,
    context_prompt: str = "",
    commit_attribution: bool = True,
    fork_from: str | None = None,
) -> list[str]:
    """The argument vector of one turn (§9.1). Never a shell string (§10.6).

    `commit_attribution=False` (a session with a `git_identity`, issue #214) adds
    `--settings NO_COMMIT_ATTRIBUTION`; otherwise the argv is unchanged and the CLI's default
    applies.

    `--permission-prompts none` makes anything that would prompt be denied instead of hanging
    the headless process (DECISIONS, §9.1 permission modes). Settings that are None or empty
    are not passed, so the agent's own default applies (§5.2). With `config.steer` the prompt is
    a stream-json line and the CLI echoes each user message once it takes it in
    (`--replay-user-messages`, `StreamInput`).
    """
    argv = [
        config.binary,
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-prompts",
        "none",
    ]
    if fork_from:
        # a fork's first turn (#202): the cut copy's history under the id zab chose (DECISIONS)
        argv += ["--resume", fork_from, "--fork-session", "--session-id", session_id]
    else:
        argv += ["--resume" if resume else "--session-id", session_id]
    if config.steer:
        argv += ["--input-format", "stream-json", "--replay-user-messages"]
    if settings.approval:
        argv += ["--permission-mode", settings.approval]
    if settings.model:
        argv += ["--model", settings.model]
    if settings.effort:
        argv += ["--effort", settings.effort]
    if config.allowed_tools:
        argv += ["--allowedTools", *config.allowed_tools]
    if config.disallowed_tools:
        argv += ["--disallowedTools", *config.disallowed_tools]
    if context_prompt:
        argv += ["--append-system-prompt", context_prompt]
    if not commit_attribution:
        argv += ["--settings", NO_COMMIT_ATTRIBUTION]
    return argv


#: `!btw` (#716, SPEC §9.10): the side question's frame, the user's question after it. On resume
#: the CLI sends the conversation's recorded system prompt, `--append-system-prompt` ignored
#: (2.1.283 and 2.1.316, `--system-prompt-snapshot`; DECISIONS), so the frame goes in the prompt.
SIDE_QUESTION_FRAME = (
    "[A side question from your user (`!btw`), asked while you work. Answer it in one reply "
    "from what this conversation already holds: the messages, your replies and the tool "
    "results so far, the running turn's included. You have no tools for this reply, so don't "
    "try to read files, run commands or search. The question and your answer stay out of the "
    "conversation, and your work goes on as it was. If the conversation doesn't tell, say "
    "so.]\n\n"
)
#: No hooks in a side question's process, which is no session of its own (DECISIONS).
SIDE_QUESTION_SETTINGS = '{"disableAllHooks":true}'


def build_side_argv(config: ClaudeConfig, settings: SessionSettings, resume_id: str) -> list[str]:
    """A side question's argv (`!btw`, #716): a copy of the conversation that is never saved,
    with no tools. `--tools ""` leaves the tools of MCP servers, `--strict-mcp-config` without
    `--mcp-config` removes them (verified on 2.1.283 and 2.1.316, DECISIONS). The session's
    model and effort; no permission mode, since there is nothing to permit."""
    argv = [
        config.binary,
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-prompts",
        "none",
        "--resume",
        resume_id,
        "--fork-session",
        "--no-session-persistence",
        "--tools",
        "",
        "--strict-mcp-config",
        "--settings",
        SIDE_QUESTION_SETTINGS,
    ]
    if settings.model:
        argv += ["--model", settings.model]
    if settings.effort:
        argv += ["--effort", settings.effort]
    return argv


DEFAULT_CONFIG_DIR = "~/.claude"


def is_default_config_dir(config_dir: str) -> bool:
    """Whether an account directory is the CLI's own default, `~/.claude` (paths resolved)."""
    return os.path.realpath(os.path.expanduser(config_dir)) == os.path.realpath(
        os.path.expanduser(DEFAULT_CONFIG_DIR)
    )


def turn_env(
    config_dir: str,
    base: Mapping[str, str] | None = None,
    *,
    nested_ok: bool = False,
    connectors: bool = False,
    git_identity: GitIdentity | None = None,
    bot: str = "",
    session: str = "",
) -> dict[str, str]:
    """The environment of a turn: credential and provider variables removed (§9.1), the
    nested-session markers removed (`nested_ok` is kept for callers and has no effect: the
    markers are always dropped, issue #36), and CLAUDE_CONFIG_DIR set to the account directory
    (§9.2), except for the default `~/.claude`: with the variable set, the CLI reads its global
    config from `~/.claude/.claude.json` instead of `~/.claude.json`, so bridge sessions would
    not see the MCP servers, settings and onboarding state of interactive `claude` (PM review
    on PR #53). For the default account the variable is left unset, inherited values included.

    Unless `connectors` is set, `ENABLE_CLAUDEAI_MCP_SERVERS=false` turns off the claude.ai
    connectors of the signed-in account; user-scope servers (`lmfdb`, `lean-lsp`) stay
    (verified on 2.1.283 and 2.1.289, DECISIONS).

    `git_identity` (issue #214, SPEC §5.5) becomes git's author and committer in the turn, which
    the Bash tool's processes inherit. Without one, inherited `GIT_AUTHOR_*`/`GIT_COMMITTER_*`
    are removed too, so the user's git config applies.

    `bot` (issue #248) becomes `ZAB_BOT`, so that `zab history` in the turn reads as this
    session's bot; without one an inherited `ZAB_BOT` is removed. `session` (issue #655) becomes
    `ZAB_SESSION` the same way, so that `zab job` knows the session that started a job (§5.6).
    """
    del nested_ok
    env = dict(os.environ if base is None else base)
    for name in (*SCRUBBED_ENV, *NESTING_ENV, *GIT_IDENTITY_ENV, "CLAUDE_CONFIG_DIR", *ZAB_ENV):
        env.pop(name, None)
    if git_identity is not None:
        env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = git_identity.name
        env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = git_identity.email
    if bot:
        env["ZAB_BOT"] = bot
    if session:
        env["ZAB_SESSION"] = session
    if not is_default_config_dir(config_dir):
        env["CLAUDE_CONFIG_DIR"] = config_dir
    if not connectors:
        env["ENABLE_CLAUDEAI_MCP_SERVERS"] = "false"
    return env


def new_session_id() -> str:
    """A fresh session UUID chosen by zab for the first turn (§9.3)."""
    return str(uuid.uuid4())


#: Opt-in record of each turn's raw stream, for diagnosing a lost answer on the fleet (PM on
#: issue #36, Wed 14:45, "(no final message)"): `ZAB_CLAUDE_RAW_LOG=1` in the bridge's environment
#: (`push_config.py --claude-raw-log`) writes one JSON line per stdout or stderr line to
#: `RAW_LOG_DIR/<turn id>.jsonl`: its type, subtype, content block kinds and lengths, never the
#: content; `=content` adds the line itself. Off by default.
RAW_LOG_ENV = "ZAB_CLAUDE_RAW_LOG"
RAW_LOG_DIR = "~/.local/state/zab/raw"


def raw_log_mode(environ: Mapping[str, str] | None = None) -> str | None:
    """None (off), "meta" or "content", from `ZAB_CLAUDE_RAW_LOG`."""
    value = (os.environ if environ is None else environ).get(RAW_LOG_ENV, "").strip().lower()
    if value in ("", "0", "off", "no", "false"):
        return None
    return "content" if value == "content" else "meta"


def _block_shape(block: Any) -> list[Any]:
    if not isinstance(block, Mapping):
        return [type(block).__name__, None]
    kind = block.get("type")
    body = block.get("text") if kind == "text" else block.get("input", block.get("content"))
    size = len(body) if isinstance(body, str) else len(json.dumps(body)) if body else 0
    return [kind, size]


def describe_raw_line(stream: str, line: str) -> dict[str, Any]:
    """What a raw stream line is, without its content: for `RawLog`."""
    record: dict[str, Any] = {"stream": stream, "bytes": len(line.encode("utf-8", "replace"))}
    if stream != "stdout":
        return record
    try:
        obj = json.loads(line)
    except ValueError:
        return record
    if not isinstance(obj, Mapping):
        return record
    record["type"] = obj.get("type")
    for key in ("subtype", "num_turns", "is_error", "stop_reason", "status", "task_id"):
        if key in obj and isinstance(obj[key], str | int | bool | type(None)):
            record[key] = obj[key]
    if obj.get("parent_tool_use_id"):
        record["subagent"] = True
    message = obj.get("message")
    content = message.get("content") if isinstance(message, Mapping) else None
    if isinstance(content, list):
        record["blocks"] = [_block_shape(b) for b in content]
        if isinstance(message.get("stop_reason"), str):
            record["stop_reason"] = message["stop_reason"]
    if obj.get("type") == "result":
        text = obj.get("result")
        record["result_chars"] = len(text) if isinstance(text, str) else None
    return record


class RawLog:
    """`on_raw` for one turn (see `RAW_LOG_ENV`): a file of mode 0600 in a 0700 directory. A
    write error turns it off for the rest of the turn; it never fails the turn."""

    def __init__(self, turn_id: str, mode: str, directory: str = RAW_LOG_DIR) -> None:
        self.mode = mode
        name = re.sub(r"[^0-9A-Za-z._-]", "_", turn_id)
        self.path = os.path.join(os.path.expanduser(directory), f"{name}.jsonl")
        self._file: Any = None
        self._failed = False

    @classmethod
    def for_turn(cls, turn_id: str, directory: str | None = None) -> RawLog | None:
        mode = raw_log_mode()
        return None if mode is None else cls(turn_id, mode, directory or RAW_LOG_DIR)

    def write(self, record: Mapping[str, Any]) -> None:
        if self._failed:
            return
        try:
            if self._file is None:
                os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                self._file = os.fdopen(fd, "a", encoding="utf-8")
            self._file.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._file.flush()
        except OSError as exc:
            self._failed = True
            log.warning("claude raw log %s off: %s", self.path, type(exc).__name__)

    def __call__(self, stream: str, line: str, t: float) -> None:
        record = {"t": round(t, 3), **describe_raw_line(stream, line)}
        if self.mode == "content":
            record["line"] = line
        self.write(record)

    def close(self) -> None:
        if self._file is not None:
            with contextlib.suppress(OSError):
                self._file.close()
            self._file = None


def new_turn_id(session_id: str) -> str:
    """`<session uuid>:<uuid4 hex>`, unique across bridge restarts (§9.4)."""
    return f"{session_id}:{uuid.uuid4().hex}"


def shorten(text: str, limit: int = 120) -> str:
    """One line, at most `limit` characters, for titles and previews (§9.4). Redacted first:
    one line, or a cut, defeats the whole-line zuliprc rule (§11.6; review of #658). And again
    once its blanks are single spaces, which can make a zuliprc line (`key=<32>` and U+3000,
    or `key` U+3000 `=` …) or a `ZULIP_API_KEY =` that the first pass didn't see (#666)."""
    text = redact_secrets(" ".join(redact_secrets(str(text).replace("\x00", "")).split()))
    return text if len(text) <= limit else text[: limit - 1] + "…"


def encode_project_dir(cwd: str) -> str:
    """Name of `<config_dir>/projects/<…>` for a cwd, as Claude Code 2.1.283 forms it (§9.8).

    Every character that is not an ASCII letter or digit becomes `-`: `/`, `.`, `_`, spaces and
    non-ASCII letters all collapse, so the map is not injective. Names longer than about 200
    characters are truncated and given `-` plus a six-character hash that this function cannot
    reproduce (DECISIONS). Callers therefore locate transcripts by session id with
    `find_transcript` and use this only as the first guess.
    """
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


_SESSION_ID_RE = re.compile(r"[0-9a-fA-F-]{36}")
#: The cut transcript copy a fork starts from is named after the fork's own id (uuid5), so a
#: bridge restarted before the fork's first turn finds it again (#202).
_FORK_NAMESPACE = uuid.UUID("9d6c3e1a-5b0f-4c55-9a43-2f8e7d1c0b6a")
#: In prose (a command, a message), where a worktree root may begin and end: after the start of
#: the text, whitespace, a quote, `=`, `(`, `[`, `:`, `,`, `<`, `>` or `@`; before `/`, the end,
#: whitespace, a quote, `:`, `,`, `)`, `]`, `;`, `|`, `&`, `<` or `>`.
_ROOT_BEFORE = r"""(?:(?<=[\s"'`=(\[:,<>@])|^)"""
_ROOT_AFTER = r"""(?=[/\s"'`:,)\];|&<>]|$)"""
#: The cut copies, in one directory of fixed length (Claude Code finds a session file under any
#: project directory; verified on 2.1.283): the child cwd's own directory name can pass 255 bytes.
FORK_COPY_DIR = "-zab-fork-copies"


def fork_copy_id(child_id: str) -> str:
    return str(uuid.uuid5(_FORK_NAMESPACE, child_id))


def fork_copy_path(config_dir: str, child_id: str) -> str:
    return os.path.join(config_dir, "projects", FORK_COPY_DIR, f"{fork_copy_id(child_id)}.jsonl")


def _rewrite_paths(value: Any, old: str, new: str) -> Any:
    """`value` (decoded JSON) with the worktree root `old` moved to `new`.

    A string that is the root is moved. One that starts with the root but goes on with anything
    but `/` names a sibling (`<root> backup/A.lean`, `<root>+old`) and is left alone (codex
    rounds 2 and 3 on #227). In any other string, a path or prose such as a command, every
    whole-root occurrence is moved, by `_ROOT_BEFORE`/`_ROOT_AFTER`. In prose, a root followed
    by a space reads as the root itself, as in `cd <root> && ls`.
    """
    if isinstance(value, str):
        if value == old:
            return new
        if value.startswith(old) and not value.startswith(old + "/"):
            return value
        if old in value:
            pattern = _ROOT_BEFORE + re.escape(old) + _ROOT_AFTER
            return re.sub(pattern, lambda _m: new, value)
        return value
    if isinstance(value, list):
        return [_rewrite_paths(v, old, new) for v in value]
    if isinstance(value, dict):
        return {k: _rewrite_paths(v, old, new) for k, v in value.items()}
    return value


def fork_roots(parent_cwd: str, child_cwd: str) -> tuple[str, str]:
    """The worktree roots behind two cwds: their common trailing path (a subdir) removed."""
    a, b = parent_cwd.rstrip("/").split("/"), child_cwd.rstrip("/").split("/")
    while len(a) > 1 and len(b) > 1 and a[-1] == b[-1]:
        a, b = a[:-1], b[:-1]
    return "/".join(a) or "/", "/".join(b) or "/"


def _file_size(path: str) -> int:
    return os.path.getsize(path)


def write_fork_copy(
    transcript: str, limit: int | None, parent_cwd: str, child_cwd: str, dest: str
) -> bool:
    """Copy `transcript` up to `limit` bytes (its last complete line) to `dest`, with the parent
    worktree's paths pointing at the child's; False when nothing completed is there (#202).

    The cut is the size the transcript had when the running turn started: each turn is its own
    `claude -p` process, the file only grows, and so its first bytes are exactly the completed
    turns (DECISIONS, the #202 VERIFY). The paths are rewritten because Claude Code re-reads the
    files an earlier turn touched, by absolute path: unchanged, the fork would read and edit the
    parent's worktree, and see the running parent's edits (verified on 2.1.283). Each line is
    decoded, rewritten (`_rewrite_paths`) and encoded again, so no character of a path needs
    special care; a line that isn't JSON is copied as it is.
    """
    with open(transcript, "rb") as f:
        data = f.read() if limit is None else f.read(limit)
    data = data[: data.rfind(b"\n") + 1]
    if not data.strip():
        return False
    old, new = fork_roots(parent_cwd, child_cwd)
    if old != new:
        lines = []
        for line in data.splitlines(keepends=True):
            try:
                record = json.loads(line)
            except ValueError:
                lines.append(line)
                continue
            moved = _rewrite_paths(record, old, new)
            lines.append(
                json.dumps(moved, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
            )
        data = b"".join(lines)
    os.makedirs(os.path.dirname(dest), mode=0o700, exist_ok=True)
    tmp = f"{dest}.{uuid.uuid4().hex}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, dest)
    return True


def find_transcript(config_dir: str, session_id: str, cwd: str | None = None) -> str | None:
    """The transcript of a session: `<config_dir>/projects/*/<session_id>.jsonl` (§9.8).

    The directory for `cwd` is tried first; otherwise every project directory is searched,
    because the directory name is lossy and long names carry an unpredictable suffix. Only a
    UUID-shaped id is accepted, so no caller-supplied text ever becomes a path component.
    """
    if not _SESSION_ID_RE.fullmatch(session_id):
        return None
    projects = os.path.join(config_dir, "projects")
    name = f"{session_id}.jsonl"
    if cwd is not None:
        guess = os.path.join(projects, encode_project_dir(cwd), name)
        if os.path.isfile(guess) and _inside_projects(projects, guess):
            return guess
    try:
        entries = os.listdir(projects)
    except OSError:
        return None
    for d in sorted(entries):
        if d == FORK_COPY_DIR:
            continue  # a fork's frozen source, never a session of its own (codex on #227)
        path = os.path.join(projects, d, name)
        if os.path.isfile(path) and _inside_projects(projects, path):
            return path
    return None


def _inside_projects(projects: str, path: str) -> bool:
    """Whether `path` (symlinks resolved) still lies under `<config_dir>/projects`.

    A symlink under `projects/`, a directory or a transcript, must not lead the backend to read
    files elsewhere (PM review on PR #44); such entries are logged by name and skipped.
    """
    root = os.path.realpath(projects)
    real = os.path.realpath(path)
    if real == root or real.startswith(root + os.sep):
        return True
    log.debug("claude: %s resolves outside %s; skipped", path, projects)
    return False


# --------------------------------------------------------------------------- process (§9.1, §9.6)

_LINE_LIMIT = 64 * 1024 * 1024  # a single stream-json line can exceed 200 KB
_STDERR_TAIL = 100

#: How long stdin stays open after a `result` that left a written line unechoed, if no further
#: loop starts; then it is closed, and the CLI still runs whatever it has read (`StreamInput`).
STEER_IDLE_CLOSE_S = 30.0

#: How long stdin stays open after a `result` while only a background sub-agent keeps the turn
#: going (#660). Then it is closed, and the CLI's own wait applies: in 2.1.283's bundle, `-p`
#: waits for a background sub-agent once stdin is closed, at most 10 minutes of idle time
#: (`CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS`, default 600000), and then stops it. So a sub-agent
#: that never ends holds the turn about 20 minutes at most, or until `!stop`.
SUBAGENT_IDLE_CLOSE_S = 600.0

#: The background task types the CLI waits for at the end of a `-p` run instead of stopping
#: them: a sub-agent of the Agent tool with `run_in_background` (seen live, DECISIONS, the
#: 2026-10-25 row of #653's VERIFY a), and a workflow, which 2.1.283's print loop waits for
#: alike (DECISIONS, 2026-11-05). A background `local_bash` command is stopped about 5 s after
#: stdin closes, so it keeps nothing open.
WAITED_TASK_TYPES = frozenset({"local_agent", "local_workflow"})


def waited_tasks(obj: Mapping[str, Any]) -> set[str]:
    """The ids of the background sub-agents a `background_tasks_changed` lists as running.

    The object lists every live background task after the change (REPLACE semantics: its
    schema in 2.1.283's bundle), each with a `task_type`; a task marked `ambient` is no
    activity. Should a later version list ended tasks with a status, only running ones count.
    """
    tasks = obj.get("tasks")
    return {
        str(task.get("task_id"))
        for task in (tasks if isinstance(tasks, list) else [])
        if isinstance(task, Mapping)
        and task.get("task_type") in WAITED_TASK_TYPES
        and not task.get("ambient")
        and task.get("status") in (None, "running", "pending")
    }


def user_line(text: str, uid: str) -> bytes:
    """One `--input-format stream-json` line: a user message with zab's uuid, which the CLI keeps
    in its echo (`--replay-user-messages`) and in the result's `user_message_uuids` (2.1.283)."""
    message = {
        "type": "user",
        "message": {"role": "user", "content": text},
        "parent_tool_use_id": None,
        "session_id": "",
        "uuid": uid,
    }
    return (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")


class StreamInput:
    """The stdin of a steerable turn (`ClaudeConfig.steer`; PM on issue #36, Fri Oct 9).

    The prompt and each steer are user lines with a uuid of zab's. Claude Code 2.1.283 echoes a
    line (`isReplay`, the same uuid) when it takes the message into the conversation, not when
    it reads it: a steer written during a tool call is echoed after that call's result, and one
    written while the model writes its final text, or just after a `result`, is echoed in a
    further loop that ends with its own `result` (DECISIONS, the `steer_*` fixtures).

    So at each `result`, stdin is closed once every line written has been echoed (or is listed in
    the result's `user_message_uuids`) and no background sub-agent runs: no further loop is
    coming and the CLI exits as it does without steering. Otherwise stdin stays open for the
    next `result`. If no further loop starts within `idle_close_s` of a `result` that left a
    line unechoed, stdin is closed anyway; the CLI still runs what it has read, then exits.
    After the close, `send` raises `SteerRejected` and the core holds the message for the next
    turn (§7.3).

    A background sub-agent (`waited_tasks`) outlives the `result` of the loop that started it:
    the CLI waits for it, and its notification starts a further loop with its own `result`
    (#660; DECISIONS, #653's VERIFY a). Until then stdin stays open, so that a message is taken
    in by a loop of its own instead of being held until the sub-agent ends. When the CLI no
    longer lists one after a `result`, its notification's loop is due, within `idle_close_s`.
    While only a sub-agent keeps stdin open, it closes `subagent_idle_close_s` after the last
    `result`, whatever other task-list updates come meanwhile, and the CLI's own wait for the
    sub-agent applies (`SUBAGENT_IDLE_CLOSE_S`).
    """

    def __init__(
        self,
        prompt: str,
        *,
        idle_close_s: float | None = None,
        subagent_idle_close_s: float | None = None,
    ) -> None:
        self.prompt = prompt
        self.prompt_uuid = str(uuid.uuid4())
        self.idle_close_s = STEER_IDLE_CLOSE_S if idle_close_s is None else idle_close_s
        self.subagent_idle_close_s = (
            SUBAGENT_IDLE_CLOSE_S if subagent_idle_close_s is None else subagent_idle_close_s
        )
        self.pending: set[str] = {self.prompt_uuid}  # written, not echoed yet
        self.steers: list[str] = []  # the steers written, in order
        # each steer's uuid -> the core's client_id: the parser reports its echo (#654)
        self.client_ids: dict[str, str] = {}
        self.agents: set[str] = set()  # the background sub-agents running (`waited_tasks`)
        self.closed = False
        self._after_result = False  # a `result` came, and no further loop has started since
        self._writer: asyncio.StreamWriter | None = None
        self._timer: asyncio.TimerHandle | None = None

    def first_line(self) -> bytes:
        return user_line(self.prompt, self.prompt_uuid)

    def attach(self, writer: asyncio.StreamWriter) -> None:
        """The prompt is written (`ClaudeTurn.start`): steers may follow."""
        if self.closed:
            writer.close()
        else:
            self._writer = writer

    @property
    def open(self) -> bool:
        return self._writer is not None and not self.closed

    @property
    def undelivered(self) -> list[str]:
        """Steers written and never echoed: the CLI did not take them in."""
        return [uid for uid in self.steers if uid in self.pending]

    async def send(self, text: str, client_id: str = "") -> str:
        """Write one steer; its uuid. `SteerRejected` once stdin is closed or gone."""
        writer = self._writer
        if writer is None or self.closed:
            raise SteerRejected("this Claude turn takes no more input")
        uid = str(uuid.uuid4())
        self.pending.add(uid)
        self.steers.append(uid)
        if client_id:
            self.client_ids[uid] = client_id
        try:
            writer.write(user_line(text, uid))
            await writer.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            self.pending.discard(uid)
            self.steers.remove(uid)
            self.client_ids.pop(uid, None)
            self.close()
            raise SteerRejected("Claude's turn has ended") from exc
        return uid

    def observe(self, obj: Mapping[str, Any]) -> None:
        """Called with every stdout object, in order (`ClaudeTurn.objects`)."""
        kind = obj.get("type")
        if kind == "user" and obj.get("isReplay"):
            self.pending.discard(str(obj.get("uuid")))
            self._after_result = False
            self._disarm()
        elif kind == "system" and obj.get("subtype") == "init":
            self._after_result = False
            self._disarm()  # a further loop has started
        elif (
            kind == "system"
            and obj.get("subtype") == "background_tasks_changed"
            and not obj.get("parent_tool_use_id")  # as `StreamParser` reads it
        ):
            ran, self.agents = self.agents, waited_tasks(obj)
            if self._after_result and ran and not self.agents:
                # the last sub-agent ended while the model was idle: its notification's loop is
                # due, so the wait shortens to `idle_close_s` (never lengthens: Codex on #814)
                self._arm(self.idle_close_s, shorten_only=True)
        elif kind == "result":
            listed = obj.get("user_message_uuids")
            for uid in listed if isinstance(listed, list) else []:
                self.pending.discard(str(uid))
            self._after_result = True
            if not self.pending and not self.agents:
                self.close()
            else:
                self._arm(self.subagent_idle_close_s if self.agents else self.idle_close_s)

    def _arm(self, delay: float, *, shorten_only: bool = False) -> None:
        """Close stdin `delay` seconds from now, unless a further loop starts first. The deadline
        is set at a `result`; any other task-list update may only bring it nearer, so that
        updates about other tasks never hold stdin open past it (Codex on #814)."""
        if self.closed:
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + delay
        if shorten_only and self._timer is not None and self._timer.when() <= deadline:
            return
        self._disarm()
        self._timer = loop.call_at(deadline, self._idle)

    def _idle(self) -> None:
        self._timer = None
        if self.agents:
            log.warning(
                "claude: a background sub-agent still runs %.0f s after a result, with no "
                "further loop; closing stdin",
                self.subagent_idle_close_s,
            )
        else:
            log.warning(
                "claude: no further loop %.0f s after a result, %d line(s) unechoed; closing stdin",
                self.idle_close_s,
                len(self.pending),
            )
        self.close()

    def _disarm(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def close(self) -> None:
        """No more input: the CLI exits once it has run what it has read. Idempotent."""
        self._disarm()
        if self.closed:
            return
        self.closed = True
        writer, self._writer = self._writer, None
        if writer is not None:
            with contextlib.suppress(Exception):
                writer.close()


class ClaudeTurn:
    """One `claude -p` process: started in its own session, fed the prompt on stdin (§9.1).

    With a `StreamInput` the prompt is a stream-json line and stdin stays open for steers until
    the stream input closes it; it is closed at the latest when stdout ends or the turn is
    interrupted.
    """

    def __init__(
        self,
        argv: list[str],
        *,
        env: Mapping[str, str],
        cwd: str,
        prompt: str,
        on_raw: Callable[[str, str, float], None] | None = None,
        on_spawn: Callable[[ClaudeTurn], Any] | None = None,
        stream_input: StreamInput | None = None,
    ) -> None:
        self.argv = argv
        self.env = dict(env)
        self.cwd = cwd
        self.prompt = prompt
        self.on_raw = on_raw
        self.on_spawn = on_spawn  # called as soon as the process and its group exist (§9.6)
        self.stream_input = stream_input
        self.proc: asyncio.subprocess.Process | None = None
        self.pgid: int | None = None
        self.started_at: float | None = None
        self._stderr: deque[str] = deque(maxlen=_STDERR_TAIL)
        self._stderr_task: asyncio.Task[None] | None = None

    @property
    def stderr_tail(self) -> str:
        return "\n".join(self._stderr)

    async def start(self) -> None:
        self.started_at = time.monotonic()
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv,
            cwd=self.cwd,
            env=self.env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            limit=_LINE_LIMIT,
        )
        try:
            self.pgid = os.getpgid(self.proc.pid)
        except ProcessLookupError:  # already gone; the group id equals the pid anyway
            self.pgid = self.proc.pid
        if self.on_spawn is not None:
            # Before the prompt is written: a stop must be able to signal a child that has
            # not started reading stdin yet, while drain() below is blocked.
            res = self.on_spawn(self)
            if asyncio.iscoroutine(res):
                await res
        assert self.proc.stdin is not None
        self._stderr_task = asyncio.create_task(self._read_stderr())
        stream = self.stream_input
        try:
            self.proc.stdin.write(
                self.prompt.encode("utf-8") if stream is None else stream.first_line()
            )
            await self.proc.stdin.drain()  # may block on a long prompt: cancellable
        except (BrokenPipeError, ConnectionResetError):
            pass  # the process died early; the exit code tells the story
        except BaseException:  # cancelled while writing: never leave the process behind
            self._signal_group(signal.SIGKILL)
            raise
        if stream is None:
            self.proc.stdin.close()
        else:
            stream.attach(self.proc.stdin)

    async def _read_stderr(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        while True:
            try:
                raw = await self.proc.stderr.readline()
            except (ValueError, asyncio.LimitOverrunError):
                continue
            if not raw:
                return
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            self._stderr.append(line)
            if self.on_raw:
                self.on_raw("stderr", line, self._elapsed())

    def _elapsed(self) -> float:
        return time.monotonic() - (self.started_at or time.monotonic())

    async def objects(self, *, eof_grace_s: float = 5.0) -> AsyncIterator[dict[str, Any]]:
        """Parsed stdout objects, one per line; non-JSON lines are logged and skipped (§9.1).

        Once the CLI itself has exited, at most `eof_grace_s` more seconds are spent waiting
        for the pipe to close: a tool process that inherited stdout and lingers would
        otherwise hold the turn open indefinitely. After the grace the group is killed (§9.6).
        """
        assert self.proc is not None and self.proc.stdout is not None
        # Not `proc.wait()`: asyncio resolves that only once every pipe has closed, which is
        # exactly what a lingering grandchild prevents. The return code itself is set as soon
        # as the child is reaped, so poll it.
        exited = asyncio.ensure_future(self._exited())
        loop = asyncio.get_running_loop()
        deadline: float | None = None  # set once, when the exit is observed
        try:
            while True:
                reader = asyncio.ensure_future(self._readline())
                if deadline is None:
                    done, _pending = await asyncio.wait(
                        {reader, exited}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if exited in done:
                        deadline = loop.time() + eof_grace_s
                if deadline is not None and not reader.done():
                    # The CLI is gone: one deadline for the whole drain, so a descendant that
                    # keeps writing cannot extend it line by line.
                    remaining = deadline - loop.time()
                    try:
                        if remaining <= 0:
                            raise TimeoutError
                        await asyncio.wait_for(asyncio.shield(reader), remaining)
                    except TimeoutError:
                        log.warning(
                            "claude: stdout still open %.0f s after exit; killing group",
                            eof_grace_s,
                        )
                        self._signal_group(signal.SIGKILL)
                        reader.cancel()
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await reader
                        await self._wait_group(1.0)  # let the killed holder be reaped
                        return
                raw = reader.result()
                if raw is None:
                    continue  # an over-limit line was discarded whole (§9.1)
                if not raw:
                    return
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("claude: non-JSON stdout line (%d bytes) skipped", len(line))
                    if self.on_raw:
                        self.on_raw("stdout-text", line, self._elapsed())
                    continue
                if self.on_raw:
                    self.on_raw("stdout", line, self._elapsed())
                if isinstance(obj, dict):
                    if self.stream_input is not None:
                        self.stream_input.observe(obj)
                    yield obj
        finally:
            if self.stream_input is not None:
                self.stream_input.close()  # stdout has ended: nothing will read a steer
            if not exited.done():
                exited.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await exited

    async def _readline(self) -> bytes | None:
        """One stdout line, `b""` at EOF, or None when a line over `_LINE_LIMIT` was discarded.

        `readuntil` says whether the separator was already buffered when the limit was hit:
        if so the bad line is exactly `consumed + 1` bytes; if not, the buffered prefix is
        consumed and the rest of the line is dropped up to its newline. Either way nothing of
        the over-limit line is ever parsed as a record (§9.1).
        """
        assert self.proc is not None and self.proc.stdout is not None
        stdout = self.proc.stdout
        try:
            return await stdout.readuntil(b"\n")
        except asyncio.IncompleteReadError as exc:
            return exc.partial  # EOF: a last line without newline, or b""
        except asyncio.LimitOverrunError as exc:
            log.warning("claude: stdout line over %d bytes skipped", _LINE_LIMIT)
            found = str(exc).startswith("Separator is found")
            await stdout.readexactly(exc.consumed + (1 if found else 0))
            while not found:
                try:
                    await stdout.readuntil(b"\n")
                    found = True
                except asyncio.LimitOverrunError as more:
                    found = str(more).startswith("Separator is found")
                    await stdout.readexactly(more.consumed + (1 if found else 0))
                except asyncio.IncompleteReadError:
                    break  # EOF inside the bad line
            return None

    async def _exited(self) -> int:
        """The exit code, as soon as the child has been reaped (pipes may still be open)."""
        assert self.proc is not None
        # Polling on purpose: the only event asyncio offers is `wait()`, which also waits for
        # the pipes to close, the very condition a lingering grandchild defeats.
        while self.proc.returncode is None:  # noqa: ASYNC110
            await asyncio.sleep(0.05)
        return self.proc.returncode

    async def wait(self, *, eof_grace_s: float = 5.0) -> int:
        """Exit code of the turn. If the pipes are still held open `eof_grace_s` after the
        exit, the group is killed and the transport closed so the turn can end (§9.6)."""
        assert self.proc is not None
        code = await self._exited()
        try:
            await asyncio.wait_for(self.proc.wait(), eof_grace_s)
        except TimeoutError:
            self._drop_pipes("pipes", eof_grace_s)
        if self._stderr_task is not None and not self._stderr_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._stderr_task), eof_grace_s)
            except TimeoutError:
                # the same lingering holder, on stderr: kill it and stop reading
                self._drop_pipes("stderr", eof_grace_s)
                self._stderr_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._stderr_task
            except Exception:  # noqa: S110 - the reader's own failure is already logged
                pass
        return code

    def _drop_pipes(self, what: str, grace_s: float) -> None:
        """Kill whatever still holds the child's pipes and close our ends of them (§9.6)."""
        log.warning("claude: %s still open %.0f s after exit; killing the group", what, grace_s)
        self._signal_group(signal.SIGKILL)
        transport = getattr(self.proc, "_transport", None)
        if transport is not None:
            transport.close()  # drops the pipe ends we hold; the lingering holder is gone

    def _signal_group(self, sig: signal.Signals) -> bool:
        if self.pgid is None:
            return False
        try:
            os.killpg(self.pgid, sig)
            return True
        except ProcessLookupError:
            return False

    def group_alive(self) -> bool:
        """True while any process of the turn's group exists (§9.6)."""
        if self.pgid is None:
            return False
        try:
            os.killpg(self.pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    async def _wait_group(self, timeout_s: float) -> bool:
        """True once no process is left in the group, False after `timeout_s`."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while self.group_alive():
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(0.05)
        return True

    async def interrupt(self, *, grace_s: float = 5.0) -> None:
        """SIGINT, then SIGTERM after `grace_s`, then SIGKILL after another `grace_s` (§9.6).

        Each signal goes to the whole process group, and the escalation continues until no
        member of the group is left, not merely until `claude` itself has exited: a tool
        process that ignores SIGINT must not outlive a stop (§9.9 "kill process groups"). A
        steerable turn's stdin is closed first, so no steer is accepted after a stop.
        """
        if self.stream_input is not None:
            self.stream_input.close()
        if not self.group_alive():
            return
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
            if not self._signal_group(sig):
                return
            if await self._wait_group(grace_s):
                return
        log.warning("claude: process group %s still alive after SIGKILL", self.pgid)

    async def terminate(self) -> None:
        """Shutdown path: the same sequence, used for every running turn (§9.6)."""
        await self.interrupt()

    async def close(self, *, grace_s: float = 1.0) -> None:
        """Abnormal exit (cancellation, exception): kill the group quickly and reap."""
        if self.proc is None:
            return
        await self.interrupt(grace_s=grace_s)
        if self.proc.returncode is None:
            try:
                await asyncio.wait_for(self.proc.wait(), 5.0)
            except TimeoutError:
                log.warning("claude: pid %s not reaped after close", self.proc.pid)
        if self._stderr_task is not None and not self._stderr_task.done():
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._stderr_task


async def run_turn(
    argv: list[str],
    *,
    env: Mapping[str, str],
    cwd: str,
    prompt: str,
    on_object: Callable[[dict[str, Any]], Any] | None = None,
    on_raw: Callable[[str, str, float], None] | None = None,
    on_start: Callable[[ClaudeTurn], Any] | None = None,
    turn_holder: list[ClaudeTurn] | None = None,
    stream_input: StreamInput | None = None,
) -> tuple[int, str]:
    """Run one turn to completion; returns (exit code, stderr tail).

    `on_object` receives each parsed stdout object (a coroutine function or a plain callable).
    `on_start` (plain or coroutine function) is called with the `ClaudeTurn` as soon as the
    process exists (its `pgid` is then known, §9.6), before the prompt is written, and
    `turn_holder`, if given, receives it at the same moment, so the caller can interrupt it even
    while the stdin write is blocked. If the
    caller is cancelled or a callback raises, the process group is killed before the exception
    propagates. The stream-json → event mapping lives in `claude_stream.py`. `stream_input`
    makes the turn steerable (`StreamInput`; the argv needs `--input-format stream-json`).
    """

    async def spawned(t: ClaudeTurn) -> None:
        if turn_holder is not None:
            turn_holder.append(t)
        if on_start is not None:
            res = on_start(t)
            if asyncio.iscoroutine(res):
                await res

    turn = ClaudeTurn(
        argv,
        env=env,
        cwd=cwd,
        prompt=prompt,
        on_raw=on_raw,
        on_spawn=spawned,
        stream_input=stream_input,
    )
    finished = False
    try:
        await turn.start()  # inside the try: a cancel during the stdin write must clean up too
        async for obj in turn.objects():
            if on_object is not None:
                res = on_object(obj)
                if asyncio.iscoroutine(res):
                    await res
        code = await turn.wait()
        finished = True
        return code, turn.stderr_tail
    finally:
        if not finished:  # cancelled, or a callback raised: never leave the group running
            await turn.close()


# ------------------------------------------------------------------------------- backend (§9)


@dataclass(slots=True)
class _AuthState:
    """One account's `claude auth status` and starting turns (#307 item 1, #450).

    Both may refresh the account's OAuth token, so they never run at once: the doctor reports
    the last answer, with its age, while a turn is starting, and a turn waits for a running
    `auth status` before it starts. The doctor's caller may give up early (`!status` and `!vm`
    give it 2 s), but `auth status` always runs to its end: killed mid-refresh, it would leave
    the refresh lock behind, and every turn of the account would lose the race for a minute.
    """

    check: Check | None = None  # the last answer
    checked_at: float = 0.0  # its time.monotonic()
    probe: asyncio.Task[Check] | None = None  # `auth status`, while it runs
    ran: _Ran | None = None  # its process, or the last one's (#483: the log lines' pid)
    login_ran: _Ran | None = None  # `!login`'s `auth status`, likewise
    starting: int = 0  # the account's turns between their start and the model's first answer


@dataclass(slots=True)
class _Session:
    native_id: str
    cwd: str
    settings: SessionSettings  # effective settings (backend defaults overlaid)
    context_prompt: str
    config_dir: str
    git_identity: GitIdentity | None = None  # the agent's commits (issue #214), from the spec
    bot: str = ""  # ZAB_BOT for `zab history` (issue #248), from the spec
    session: str = ""  # ZAB_SESSION for `zab job` (issue #655), from the spec
    usage_total: TokenUsage = field(default_factory=TokenUsage)
    plan: dict[str, tuple[str, str]] = field(default_factory=dict)  # carried across turns
    resolved_model: str | None = None  # from the last `system/init` or model check, for display
    effort_supported: bool | None = None  # ditto (`per_turn_effort_active`); None = unknown
    turn: Any = None  # the running ClaudeTurn, once its process exists
    turn_id: str | None = None
    busy: bool = False  # a turn was started and has not completed (§7.10 per-session lock)
    last_completed: TurnSummary | None = None  # the last turn that ended, with its real id (§7.8.1)
    interrupting: bool = False
    task: asyncio.Task[None] | None = None
    stop_task: asyncio.Task[None] | None = None  # interrupt requested before the process existed
    # set while a turn waits to start: to retry a lost refresh race, or for `auth status`
    retry_wake: asyncio.Event | None = None
    stdin: StreamInput | None = None  # the running turn's, when steering is on
    # #202: the cut copy's id while a fork's first turn hasn't created its own transcript
    fork_from: str | None = None
    turn_start_size: int | None = None  # the transcript's size when the running turn started


def _model_check_dir() -> str:
    path = os.path.expanduser(MODEL_CHECK_DIR)
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


@dataclass(frozen=True, slots=True)
class ModelCheck:
    """What one short turn said about a model (`ClaudeBackend.check_model`, §7.9)."""

    model: str | None = None  # resolved, from `system/init`; None when the check could not tell
    effort_supported: bool | None = None  # `per_turn_effort_active`
    unavailable: bool = False  # the CLI refused the model (`is_model_unavailable`)


def transcript_path(config_dir: str, cwd: str, session_id: str) -> str:
    """Where the CLI is expected to write a new session's transcript (§9.2, §9.8).

    Use `find_transcript` to locate an existing one: the real directory name may differ.
    """
    return os.path.join(config_dir, "projects", encode_project_dir(cwd), f"{session_id}.jsonl")


def read_transcript_head(path: str) -> dict[str, Any]:
    """The recorded cwd and first user prompt of a transcript, reading as little as possible."""
    first_user = cwd = None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                if cwd is None and isinstance(rec.get("cwd"), str) and rec["cwd"]:
                    cwd = rec["cwd"]
                msg = rec.get("message")
                if (
                    first_user is None
                    and rec.get("type") == "user"
                    and isinstance(msg, dict)
                    and isinstance(msg.get("content"), str)
                    and msg["content"].strip()
                ):
                    first_user = msg["content"]
                if cwd is not None and first_user is not None:
                    break
    except OSError:
        pass
    return {"cwd": cwd, "first_user": first_user}


def read_transcript_summary(path: str) -> dict[str, Any]:
    """First and last user prompts, the last assistant text and the cwd of a transcript (§9.8).

    Only `user` records whose content is a string (a prompt, not a tool result) and
    `assistant` records with text blocks are considered; everything else in the file
    (attachments, queue operations, cost state, and any valid but unexpected shape: a bare
    list or string, a string `message`, a non-object block) is skipped, so `open_session`
    never fails on a transcript it can read (PM review on PR #44). The whole file is streamed
    line by line, in a worker thread, so a transcript of any size yields its latest exchange.
    """
    first_user = last_user = last_assistant = cwd = None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue  # a bare list, string or number is valid JSON the CLI may write
            if cwd is None and isinstance(rec.get("cwd"), str) and rec["cwd"]:
                cwd = rec["cwd"]
            kind = rec.get("type")
            msg = rec.get("message")
            content = msg.get("content") if isinstance(msg, dict) else None
            if kind == "user" and isinstance(content, str) and content.strip():
                if first_user is None:
                    first_user = content
                last_user = content
            elif kind == "assistant" and isinstance(content, list):
                texts = [
                    b.get("text")
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
                ]
                if texts:
                    last_assistant = "\n".join(texts)
    return {
        "first_user": first_user,
        "last_user": last_user,
        "last_assistant": last_assistant,
        "cwd": cwd,
    }


class ClaudeBackend:
    """The `Backend` protocol for Claude Code (§9). Constructed by the core from the config."""

    kind: BackendKind = "claude"
    capabilities = Capabilities(
        steer=False,
        approvals=False,
        live_attach=False,
        rename=False,
        fork=True,
        side_question=True,  # `!btw`, verified on 2.1.283 and 2.1.316 (#716, DECISIONS)
    )

    def __init__(
        self,
        name: str,
        config: ClaudeConfig,
        *,
        pinned_version: str = PINNED_VERSION,
        discovery_backoff: tuple[float, float] = (1.0, 60.0),
    ):
        self._name = name
        self.config = config
        if config.steer:  # off, the class's capabilities stand
            self.capabilities = replace(self.capabilities, steer=True)
        self.pinned_version = pinned_version
        self.discovery_backoff = discovery_backoff  # first delay and cap, seconds (§7.8.3)
        self._sessions: dict[str, _Session] = {}
        self._aliases: dict[str, str] = {}  # replaced session id -> the id the CLI reported (§9.3)
        self._auth: dict[str, _AuthState] = {}  # by config directory
        from zab.backends.claude_usage import shared_store

        self._usage = shared_store(os.path.expanduser(USAGE_FILE) if USAGE_FILE else None)
        self._events: asyncio.Queue[BackendEvent | None] = asyncio.Queue()
        self._up = False
        self._closed = False
        self._discovery: asyncio.Task[None] | None = None
        self._side_questions: set[asyncio.Task[tuple[int, str]]] = set()  # `!btw` processes

    # --- identity

    @property
    def name(self) -> str:
        return self._name

    # --- lifecycle (§7.1)

    async def start(self) -> None:
        """Never raises for an unavailable agent: reports BackendStatus instead (§7.1)."""
        binary = await asyncio.to_thread(_find_binary, self.config.binary)
        if binary is None:
            self._up = False
            await self._events.put(
                BackendStatus(session_id="", up=False, message=f"{self.config.binary} not found")
            )
            self._discovery = asyncio.create_task(self._discover(), name="claude-discovery")
            return
        self._up = True
        await self._events.put(BackendStatus(session_id="", up=True))

    async def _discover(self) -> None:
        """Retry finding the binary with exponential backoff and jitter (§7.1, §7.8.3)."""
        delay, cap = self.discovery_backoff
        while not self._closed:
            await asyncio.sleep(delay + random.uniform(0, delay / 2))  # noqa: S311 - jitter only
            if await asyncio.to_thread(_find_binary, self.config.binary) is not None:
                self._up = True
                await self._events.put(BackendStatus(session_id="", up=True))
                return
            delay = min(delay * 2, cap)

    async def close(self) -> None:
        """Terminate every running turn the §9.6 way, then stop the event stream."""
        self._closed = True
        probes = asyncio.create_task(self._end_auth_status())
        if self._discovery is not None:
            self._discovery.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._discovery
        for session in list(self._sessions.values()):
            if not session.busy:
                continue
            session.interrupting = True  # a turn whose process is still starting stops on start
            if session.retry_wake is not None:
                session.retry_wake.set()  # a turn waiting to retry stops now
            if session.turn is not None:
                await session.turn.terminate()
            if session.task is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await session.task
        for task in list(self._side_questions):  # `!btw`: run_turn stops its process group
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await probes
        await self._events.put(None)  # ends events()

    async def _end_auth_status(self) -> None:
        """At `close()`, alongside the turns: a short wait for a running `auth status` to end by
        itself (`AUTH_STATUS_CLOSE_WAIT_S`), then its SIGTERM and grace (`_run_capture`)."""
        probes = [a.probe for a in self._auth.values() if a.probe and not a.probe.done()]
        if not probes:
            return
        await asyncio.wait(probes, timeout=AUTH_STATUS_CLOSE_WAIT_S)
        for probe in probes:  # all first, so that their graces run at once (codex on #451)
            probe.cancel()
        await asyncio.gather(*probes, return_exceptions=True)

    async def doctor(self) -> list[Check]:
        """Binary present, version pinned, each account logged in (§13.1).

        Login status comes from `claude auth status`, never from credential files (§12).
        Only `loggedIn`, `authMethod` and `subscriptionType` are reported. `claude account
        <name>` is the sign-in answer alone; a missing config directory (`claude config <name>`)
        or a failed `auth status` (`claude auth <name>`) has its own name, so that tools reading
        the doctor (push_config's "sign-in pending") never take an operational failure for a
        participant who has not signed in yet (codex on #152).

        `auth status` never runs for an account while one of its turns is starting, and is
        never cut short by a caller that stops waiting (`_AuthState`, #307, #450).
        """
        checks: list[Check] = []
        binary = await asyncio.to_thread(_find_binary, self.config.binary) or self.config.binary
        try:
            version = await _run_capture([binary, "--version"], timeout_s=30)
        except (OSError, TimeoutError) as exc:
            checks.append(
                Check(name="claude binary", ok=False, detail=f"{binary}: {type(exc).__name__}")
            )
            return checks
        ver = version.split()[0] if version.split() else version
        if ver == self.pinned_version:
            checks.append(Check(name="claude version", ok=True, detail=ver))
        else:
            checks.append(
                Check(
                    name="claude version",
                    ok=True,
                    warning=True,
                    detail=f"{ver} installed, {self.pinned_version} pinned (docs/DECISIONS.md)",
                )
            )
        for account in self.config.accounts:
            config_dir = self.config.config_dir(account)
            if not await asyncio.to_thread(os.path.isdir, config_dir):
                checks.append(
                    Check(
                        name=f"claude config {account}",
                        ok=False,
                        detail=f"{config_dir} is not a directory",
                    )
                )
                continue
            state = self._auth.setdefault(config_dir, _AuthState())
            if state.starting or self._closed:  # closed: `close()` would not end a new one
                checks.append(_last_auth(account, state))
                continue
            if state.probe is None or state.probe.done():
                env = turn_env(config_dir, connectors=self.config.claudeai_connectors)
                state.ran = _Ran()  # before the task runs: a starting turn's line names it
                state.probe = asyncio.create_task(
                    _auth_status(binary, account, env, state, config_dir, doctor_reason.get()),
                    name=f"claude-auth-{account}",
                )
            checks.append(await asyncio.shield(state.probe))  # a caller's timeout ends the wait
        return checks

    async def signed_in(self) -> bool | None:
        """Sign-in as last seen, without asking (#687): the accounts' last `auth status`
        answers (`_AuthState`, from the doctor: `!vm`, `!status`, `!login`). True if one was
        signed in; False if each was signed out, within `SIGNED_OUT_TRUSTED_S`; else None
        (unknown: never asked since the start, a failed check, an old answer). Never runs
        `auth status`, which takes seconds and holds up the account's turns."""
        now = time.monotonic()
        answers = []
        for account in self.config.accounts:
            state = self._auth.get(self.config.config_dir(account))
            check = state.check if state else None
            if check is not None and check.name.startswith("claude account "):
                answers.append((check.ok, now - state.checked_at))
        if any(ok for ok, _ in answers):
            return True
        if answers and len(answers) == len(self.config.accounts):
            if all(age < SIGNED_OUT_TRUSTED_S for _, age in answers):
                return False
        return None

    async def login(
        self,
        account: str | None,
        send: Callable[[str], Awaitable[None]],
        receive: Callable[[float], Awaitable[str | None]],
    ) -> bool:
        """`!login` (issue #36): `claude auth login` in a pseudo-terminal under the account's
        config directory (CLAUDE_CONFIG_DIR unset for `~/.claude`, as for turns), the sign-in
        URL sent with one line of instructions, the owner's pasted code written back, success
        confirmed with `claude auth status`. True once signed in."""
        from zab.backends.claude_login import logged_in, run_login

        binary = await asyncio.to_thread(_find_binary, self.config.binary)
        if binary is None:
            # Not installed or not found yet (§7.1): BackendUnavailable, which the core's
            # !login waits out and then reports as such, rather than a failed sign-in.
            raise BackendUnavailable(f"{self.config.binary} not available")
        config_dir = self.config.config_dir(account)
        await asyncio.to_thread(os.makedirs, config_dir, 0o700, True)
        env = turn_env(config_dir, connectors=self.config.claudeai_connectors)
        state = self._auth.setdefault(config_dir, _AuthState())
        name = account or self.config.default_account
        capture = functools.partial(_capture_auth_status, account=name, state=state)
        ok = await run_login(
            binary, env, send, receive, status=lambda: logged_in(binary, env, capture)
        )
        log.info(
            "claude: login for account %s %s",
            account or self.config.default_account,
            "succeeded" if ok else "did not complete",
        )
        return ok

    async def events(self) -> AsyncIterator[BackendEvent]:
        while True:
            ev = await self._events.get()
            if ev is None:
                return
            yield ev

    # --- sessions (§9.2, §9.3, §9.8)

    def _require_up(self) -> None:
        if not self._up:
            raise BackendUnavailable(f"{self.config.binary} not available")

    def _info(self, session: _Session) -> SessionInfo:
        return SessionInfo(
            native_id=session.native_id,
            cwd=session.cwd,
            settings=session.settings,
            resume_hint=self.resume_hint(session.config_dir, session.cwd, session.native_id),
        )

    @staticmethod
    def resume_hint(config_dir: str, cwd: str, session_id: str) -> str:
        """The terminal command shown on the card (§9.2, Appendix B.1), shell-quoted so that a
        path with spaces or metacharacters pastes correctly."""
        prefix = (
            ""
            if is_default_config_dir(config_dir)
            else (f"CLAUDE_CONFIG_DIR={shlex.quote(config_dir)} ")
        )
        return f"cd {shlex.quote(cwd)} && {prefix}claude --resume {shlex.quote(session_id)}"

    async def create_session(self, spec: SessionSpec) -> SessionInfo:
        """Choose the UUID; no process runs until the first turn (§9.3)."""
        self._require_up()
        settings = effective_settings(self.config, spec.settings)
        session = _Session(
            native_id=new_session_id(),
            cwd=spec.cwd,
            settings=settings,
            context_prompt=spec.context_prompt,
            config_dir=self.config.config_dir(settings.account),
            git_identity=spec.git_identity,
            bot=spec.bot,
            session=spec.session,
        )
        self._sessions[session.native_id] = session
        return self._info(session)

    def _resolve(self, session_id: str) -> str:
        """The live id behind one the CLI has since replaced (`SessionChanged`, §9.3), so that a
        caller still holding the old id reaches the same session instead of a second idle one
        (PM review on PR #44)."""
        seen: set[str] = set()
        while session_id in self._aliases and session_id not in seen:
            seen.add(session_id)
            session_id = self._aliases[session_id]
        return session_id

    async def lookup_session(self, native_id: str) -> SessionInfo | None:
        """The transcript of `native_id` in any configured account, as a `SessionInfo` with the
        account and cwd it records; None when no account has one. Nothing is opened or created,
        so `!attach ID` can refuse an unknown id (§9.8; issue #36)."""
        self._require_up()
        native_id = self._resolve(native_id)
        session = self._sessions.get(native_id)
        if session is not None:
            info = self._info(session)
            info.active_turn_id = session.turn_id if session.busy else None
            return info
        located = await asyncio.to_thread(
            _locate_transcript, self.config, native_id, self.config.default_account, None
        )
        if located is None:
            return None
        account, config_dir, recorded_cwd = located
        summary, updated_at = await asyncio.to_thread(
            _transcript_summary_and_mtime, config_dir, native_id, recorded_cwd
        )
        cwd = recorded_cwd or ""
        info = SessionInfo(
            native_id=native_id,
            cwd=cwd,
            settings=SessionSettings(account=account),
            resume_hint=self.resume_hint(config_dir, cwd, native_id),
            preview=summary.get("last_user"),
            updated_at=updated_at,
        )
        if summary.get("last_assistant") is not None:
            info.last_turn = TurnSummary(
                turn_id="", status=TurnStatus.COMPLETED, final_text=summary["last_assistant"]
            )
        return info

    async def fork_session(self, native_id: str, spec: SessionSpec) -> SessionInfo:
        """An independent child through the parent's last completed turn (#202, SPEC §8.8).

        The parent's transcript is copied up to the size it had when its running turn started
        (all of it when idle), with the parent worktree's paths pointing at `spec.cwd`, under
        an id derived from the child's (`fork_copy_id`). The child's first turn runs
        `--resume <copy> --fork-session --session-id <child>`, so its id is final now and no
        `SessionChanged` follows; the copy goes once the child has its own transcript. The
        parent's transcript and process are never touched (DECISIONS, the #202 VERIFY).
        """
        self._require_up()
        parent = self._sessions.get(self._resolve(native_id))
        if parent is None:
            raise ValueError("The session to fork from isn't open.")
        transcript = await asyncio.to_thread(
            find_transcript, parent.config_dir, parent.native_id, parent.cwd
        )
        if transcript is None:
            raise ValueError("There is no completed turn to fork from yet.")
        # The boundary, with no await between this check and the size (codex round 3 on #227):
        # a running turn's start size; else the file as it is, which a turn that is starting
        # can't have grown yet, since its process only starts after it records that same size.
        limit = parent.turn_start_size if parent.busy else None
        if limit is None:
            limit = _file_size(transcript)
        # The parent's effective settings, with what the core sets explicitly on top. A value
        # the parent cleared stays cleared (backend defaults must not come back, codex round 5
        # on #227), and what the parent runs with by default, its approval mode above all,
        # carries over when the core hands over only its persisted overrides (the live check).
        base, given = parent.settings, spec.settings
        settings = SessionSettings(
            model=given.model or base.model,
            effort=given.effort or base.effort,
            approval=given.approval or base.approval,
            sandbox=None,
            account=given.account or base.account or self.config.default_account,
            extra=dict(given.extra),
        )
        config_dir = self.config.config_dir(settings.account)
        child_id = new_session_id()
        dest = fork_copy_path(config_dir, child_id)
        if not await asyncio.to_thread(
            write_fork_copy, transcript, limit, parent.cwd, spec.cwd, dest
        ):
            raise ValueError("There is no completed turn to fork from yet.")
        child = _Session(
            native_id=child_id,
            cwd=spec.cwd,
            settings=settings,
            context_prompt=spec.context_prompt,
            config_dir=config_dir,
            git_identity=spec.git_identity,  # the fork commits as the agent too (#214)
            fork_from=fork_copy_id(child_id),
            bot=spec.bot,  # and its turns read other topics as this bot (#248)
            session=spec.session,  # and name the fork's own session to `zab job` (#655)
        )
        self._sessions[child_id] = child
        return self._info(child)

    @staticmethod
    def _drop_fork_copy(session: _Session) -> None:
        """Once a fork has its own transcript, the cut copy it started from goes."""
        if find_transcript(session.config_dir, session.native_id, session.cwd) is None:
            return  # the first turn didn't get that far: the next one starts from the copy
        with contextlib.suppress(OSError):
            os.remove(fork_copy_path(session.config_dir, session.native_id))
        session.fork_from = None

    async def open_session(self, native_id: str, spec: SessionSpec) -> SessionInfo:
        """Reattach after a restart or for !attach (§7.8.1, §9.8). No turn ever survives.

        An id this process does not know is looked up in the transcripts of every configured
        account (the requested account first); when one is found the session takes the account
        where it lives and the cwd it records, so a reattached session resumes where it ran.
        With no transcript anywhere the session is still reconstructed from `spec` (lenient:
        a persisted session whose first turn never started has no transcript, and recovery
        must not lose it; codex review on PR #68); `!attach ID` refuses unknown ids through
        `lookup_session` instead.
        """
        self._require_up()
        native_id = self._resolve(native_id)
        settings = effective_settings(self.config, spec.settings)
        config_dir = self.config.config_dir(settings.account)
        session = self._sessions.get(native_id)
        if session is None:
            cwd = spec.cwd
            located = await asyncio.to_thread(
                _locate_transcript, self.config, native_id, settings.account, spec.cwd
            )
            fork_from = None
            if located is not None:
                account, config_dir, recorded_cwd = located
                settings = replace(settings, account=account)
                cwd = recorded_cwd or spec.cwd
            elif await asyncio.to_thread(os.path.isfile, fork_copy_path(config_dir, native_id)):
                fork_from = fork_copy_id(native_id)  # a fork whose first turn never ran (#202)
            session = _Session(
                native_id=native_id,
                cwd=cwd,
                settings=settings,
                context_prompt=spec.context_prompt,
                config_dir=config_dir,
                git_identity=spec.git_identity,
                fork_from=fork_from,
                bot=spec.bot,
                session=spec.session,
            )
            self._sessions[native_id] = session
        else:
            # read at each turn's start, so a running turn keeps the one it began with
            session.git_identity = spec.git_identity
            session.bot = spec.bot
            session.session = spec.session
            # A live session is kept as it is: a running turn must keep running and be reported
            # (§7.8.1, `!status` during a turn); only an idle one takes the new settings.
            if not session.busy:
                if settings.model != session.settings.model:
                    session.resolved_model = session.effort_supported = None
                session.cwd = spec.cwd
                session.settings = settings
                session.config_dir = config_dir
            if spec.context_prompt:
                session.context_prompt = spec.context_prompt
        summary, updated_at = await asyncio.to_thread(
            _transcript_summary_and_mtime, session.config_dir, native_id, session.cwd
        )
        info = self._info(session)
        info.active_turn_id = session.turn_id if session.busy else None
        info.preview = summary.get("last_user")
        info.updated_at = updated_at
        if session.last_completed is not None:
            # The last turn this process ran, with its real id: a `!status` landing just as a
            # turn finished then completes it instead of marking it interrupted (PM review on
            # PR #44, §7.8.1). After a bridge restart only the transcript is left (below).
            info.last_turn = session.last_completed
        elif summary.get("last_assistant") is not None:
            info.last_turn = TurnSummary(
                turn_id="", status=TurnStatus.COMPLETED, final_text=summary["last_assistant"]
            )
        return info

    async def list_sessions(self, cwd: str | None, limit: int = 20) -> list[SessionInfo]:
        """Transcripts under `<config_dir>/projects/<encoded cwd>/` of every account (§9.8)."""
        return await asyncio.to_thread(_scan_sessions, self.config, cwd, limit)

    # --- turns (§9.1, §9.4, §9.6)

    async def start_turn(self, session_id: str, turn_input: UserInput) -> str:
        """Start a turn (§7.4). The task exists before this method first yields, so a stop or a
        shutdown that arrives while the turn is starting is never lost."""
        self._require_up()
        if self._closed:
            raise BackendUnavailable("Claude backend is closed")
        session = self._sessions.get(self._resolve(session_id))
        if session is None:
            raise KeyError(f"unknown Claude session {session_id}")
        if session.busy:
            raise RuntimeError(f"session {session_id} already has a running turn")
        session.busy = True
        session.interrupting = False
        turn_id = new_turn_id(session_id)
        session.turn_id = turn_id
        session.task = asyncio.create_task(
            self._run(session, turn_id, turn_input.text), name=f"claude-turn-{turn_id}"
        )
        return turn_id

    async def _run(self, session: _Session, turn_id: str, text: str) -> None:
        from zab.backends.claude_stream import StreamParser, run_parsed_turn

        # One snapshot for the argv and for what the turn reports: a `!model`/`!effort` during
        # the awaits below applies to the next turn (codex round 3 on #113, B.2).
        settings = session.settings
        stream_input = StreamInput(text) if self.config.steer else None
        session.stdin = stream_input
        parser = StreamParser(
            session.native_id,
            turn_id,
            cwd=session.cwd,
            plan=session.plan,
            effort=settings.effort,
            multi_result=stream_input is not None,
            steers=stream_input.client_ids if stream_input is not None else None,
        )
        holder: list[ClaudeTurn] = []
        completion: list[BackendEvent] = []
        raw_log = RawLog.for_turn(turn_id)
        if raw_log is not None:
            raw_log.write({"turn": turn_id, "session": session.native_id, "cwd": session.cwd})

        async def on_event(ev: BackendEvent) -> None:
            if isinstance(ev, SessionChanged) and ev.new_session_id:
                self._sessions[ev.new_session_id] = self._sessions.pop(ev.session_id, session)
                self._aliases[ev.session_id] = ev.new_session_id
                session.native_id = ev.new_session_id
                self._usage.rename(ev.session_id, ev.new_session_id)
            if isinstance(ev, TurnCompleted):
                if session.interrupting:
                    ev = TurnCompleted(
                        session_id=ev.session_id,
                        turn_id=ev.turn_id,
                        status=TurnStatus.INTERRUPTED,
                        final_text=ev.final_text,
                        error=None,
                        usage=ev.usage,
                    )
                if stream_input is not None:
                    # A steered turn can end in several `result`s; each TurnCompleted covers all
                    # of them so far (`StreamParser(multi_result=True)`), so the last one stands.
                    completion[:] = [ev]
                    return
                session.usage_total = _add_usage(session.usage_total, ev.usage)
                # Held back until the process is gone and the session can take the next turn
                # (§7.4 step 6 starts held messages on completion).
                completion.append(ev)
                return
            await self._events.put(ev)

        def on_start(turn: ClaudeTurn) -> None:
            holder.append(turn)
            session.turn = turn
            if session.interrupting:  # stop or shutdown asked before the process existed
                session.stop_task = asyncio.create_task(turn.interrupt())

        lost_before = 0  # steers written to a try that lost the refresh race: never taken in
        auth = self._auth.setdefault(session.config_dir, _AuthState())
        release_auth = _no_op
        try:
            transcript = await asyncio.to_thread(
                find_transcript, session.config_dir, session.native_id, session.cwd
            )
            resume = transcript is not None
            # complete now that the previous turn's process is gone: what a fork taken during
            # this turn starts from (#202)
            session.turn_start_size = (
                await asyncio.to_thread(os.path.getsize, transcript) if transcript else 0
            )
            await self._auth_settled(session, turn_id, auth)
            # no await since the wait: the doctor runs no `auth status` from here until the
            # model answers or the turn ends (#307 item 1)
            release_auth = _starting(auth)
            first_loss: float | None = None
            for attempt in range(REFRESH_RETRIES + 1):
                if session.interrupting or self._closed:
                    # Stopped or shut down while starting: never launch the process (§9.6).
                    for ev in parser.finish(None, "stopped before the process started"):
                        await on_event(ev)
                    return
                argv = build_argv(
                    self.config,
                    settings,
                    session_id=session.native_id,
                    resume=resume,
                    context_prompt=session.context_prompt,
                    commit_attribution=session.git_identity is None,
                    fork_from=None if resume else session.fork_from,
                )
                env = turn_env(
                    session.config_dir,
                    connectors=self.config.claudeai_connectors,
                    git_identity=session.git_identity,
                    bot=session.bot,
                    session=session.session,
                )
                parser.on_answer = release_auth
                await run_parsed_turn(
                    argv,
                    env=env,
                    cwd=session.cwd,
                    prompt=text,
                    parser=parser,
                    on_event=on_event,
                    on_raw=raw_log,
                    on_start=on_start,
                    turn_holder=holder,
                    stream_input=stream_input,
                )
                if not (
                    parser.info.refresh_race
                    and not parser.output_seen
                    and attempt < REFRESH_RETRIES
                    and not session.interrupting
                    and not self._closed
                ):
                    break
                # Lost the CLI's OAuth refresh race before doing anything: the account is
                # signed in, so run the same prompt again in a fresh process, as the CLI's own
                # message advises. Its failed try (the prompt and an API-error message) is in
                # the transcript now, so the next try resumes it. Asked, the model reported the
                # prompt twice in one user turn and no error message (2.1.299, DECISIONS; the
                # CLI's request builder was not traced). The core sees one turn: the
                # failed completion is dropped, and the next TurnStarted brings the new pgid.
                now = time.monotonic()
                first_loss = now if first_loss is None else first_loss
                delay = refresh_retry_delay(attempt, now - first_loss)
                own = self._own_processes(auth)
                session.turn = None
                session.stdin = None  # a steer during the wait is held for the next turn (§7.3)
                if stream_input is not None:
                    stream_input.close()
                    lost_before += len(stream_input.undelivered)
                    stream_input = StreamInput(text)
                # set before the lock is read: a stop or shutdown during that read wakes it
                session.retry_wake = wake = asyncio.Event()
                lock = await asyncio.to_thread(refresh_lock_state, session.config_dir, own)
                log.warning(
                    "claude: turn %s lost the OAuth refresh race (try %d of %d); again in %.0f s;"
                    " %s",
                    turn_id,
                    attempt + 1,
                    REFRESH_RETRIES + 1,
                    delay,
                    lock,
                )
                if raw_log is not None:
                    raw_log.write({"retry": attempt + 1, "reason": "oauth_refresh_race"})
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(wake.wait(), delay)
                session.retry_wake = None
                session.stdin = stream_input
                completion.clear()  # with its parser: one completion reaches the core
                parser = StreamParser(
                    session.native_id,
                    turn_id,
                    cwd=session.cwd,
                    plan=parser.plan,
                    effort=settings.effort,
                    multi_result=stream_input is not None,
                    steers=stream_input.client_ids if stream_input is not None else None,
                )
                transcript = await asyncio.to_thread(
                    find_transcript, session.config_dir, session.native_id, session.cwd
                )
                resume = transcript is not None
        except Exception as exc:  # the process could not even be started
            log.exception("claude turn %s failed to run", turn_id)
            if not parser.completed:
                for ev in parser.finish(None, f"{type(exc).__name__}: {exc}"):
                    await on_event(ev)
        finally:
            release_auth()
            session.retry_wake = None
            if stream_input is not None:
                session.stdin = None
                stream_input.close()
                lost = lost_before + len(stream_input.undelivered)
                completion[:] = [_with_undelivered_note(ev, lost) for ev in completion]
                if session.interrupting:
                    completion[:] = [_stopped_after_result(ev) for ev in completion]
                for ev in completion:
                    if isinstance(ev, TurnCompleted):
                        session.usage_total = _add_usage(session.usage_total, ev.usage)
            if raw_log is not None:
                # what the core is about to get: one line per TurnCompleted, with its text length
                for ev in completion:
                    if isinstance(ev, TurnCompleted):
                        raw_log.write(
                            {
                                "completed": ev.status.value,
                                "final_text_chars": len(ev.final_text or ""),
                            }
                        )
                raw_log.close()
            session.turn_start_size = None
            if session.fork_from:
                await asyncio.to_thread(self._drop_fork_copy, session)
            session.plan = dict(parser.plan)  # the plan outlives the turn (issue #36)
            if (
                parser.info.model
                and not parser.info.model_unavailable
                and session.settings.model == settings.model  # no `!model` during the turn
            ):
                session.resolved_model = parser.info.model
                session.effort_supported = parser.info.effort_supported
            await self._record_usage(session, parser.info)
            if session.stop_task is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await session.stop_task
                session.stop_task = None
            for ev in completion:
                if isinstance(ev, TurnCompleted):
                    session.last_completed = TurnSummary(
                        turn_id=ev.turn_id,
                        status=ev.status,
                        final_text=ev.final_text,
                        error=ev.error,
                        usage=ev.usage,
                    )
            session.turn = None
            session.turn_id = None
            session.busy = False
            for ev in completion:
                await self._events.put(ev)
            await self._events.put(
                UsageUpdated(session_id=session.native_id, usage=session.usage_total)
            )

    def _own_processes(self, auth: _AuthState) -> dict[int, str]:
        """This bridge's `claude` processes by pid, for `refresh_lock_state`: the account's
        running or last `auth status`, the doctor's and `!login`'s, and the running turns."""
        procs = ((s.turn_id, getattr(s.turn, "proc", None)) for s in self._sessions.values())
        own = {proc.pid: f"this bridge's turn {turn_id}" for turn_id, proc in procs if proc}
        if auth.login_ran is not None and auth.login_ran.pid is not None:
            own[auth.login_ran.pid] = "this bridge's `auth status` for !login"
        if auth.ran is not None and auth.ran.pid is not None:
            own[auth.ran.pid] = "this bridge's `auth status`"
        return own

    async def _auth_settled(self, session: _Session, turn_id: str, auth: _AuthState) -> None:
        """Before a turn's first process: wait while the account's `auth status` runs, at most
        `AUTH_WAIT_S` in all, or until a stop or shutdown (`retry_wake`), so that the two never
        refresh the OAuth token at once (#307 item 1). The caller marks the turn starting with
        no await after this returns. The wait, if any, is logged when it begins and ends."""
        start = time.monotonic()
        deadline = start + AUTH_WAIT_S
        waited = False
        while (probe := auth.probe) is not None and not probe.done():
            left = deadline - time.monotonic()
            if session.interrupting or self._closed:
                if waited:
                    log.info("claude: turn %s stopped while it waited for `auth status`", turn_id)
                return
            if left <= 0:
                log.warning(
                    "claude: turn %s starts while `auth status` still runs, after %.1f s",
                    turn_id,
                    time.monotonic() - start,
                )
                return
            if not waited:
                ran = auth.ran
                log.info(
                    "claude: turn %s waits for the account's `auth status` (pid %s, %.1f s in)",
                    turn_id,
                    ran.pid if ran else "?",
                    time.monotonic() - ran.started if ran else 0.0,
                )
                waited = True
            session.retry_wake = wake = asyncio.Event()
            woken = asyncio.create_task(wake.wait())
            try:
                await asyncio.wait(
                    {probe, woken}, timeout=left, return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                woken.cancel()
                session.retry_wake = None
        if waited:
            log.info(
                "claude: turn %s waited %.1f s for `auth status`, which ended",
                turn_id,
                time.monotonic() - start,
            )

    async def steer(self, session_id: str, turn_id: str, turn_input: UserInput) -> None:
        """With `steer` on, write the message into the running turn's stdin (`StreamInput`).

        `SteerRejected`, so that the core holds the message for the next turn (§7.3), when the
        flag is off, no turn of that id is running, it is being stopped, its process has not
        taken the prompt yet, or its stdin is closed (the turn is ending).
        """
        if not self.config.steer:
            raise SteerRejected("Claude Code cannot take input during a turn (§9.7)")
        session = self._sessions.get(self._resolve(session_id))
        stream = session.stdin if session is not None else None
        if session is None or stream is None or session.turn_id != turn_id or session.interrupting:
            raise SteerRejected("no Claude turn is taking input")
        await stream.send(turn_input.text, turn_input.client_id)

    async def interrupt(self, session_id: str) -> None:
        """SIGINT, SIGTERM, SIGKILL to the turn's process group (§9.6)."""
        session = self._sessions.get(self._resolve(session_id))
        if session is None or not session.busy:
            return
        session.interrupting = True
        if session.retry_wake is not None:
            session.retry_wake.set()  # waiting to retry a lost refresh race: no new process
        if session.turn is not None:
            await session.turn.interrupt()
        # else: the turn is still starting; on_start stops it as soon as the process exists

    async def respond(self, request_id: str, decision: ApprovalDecision) -> None:
        raise RuntimeError("Claude Code has no approvals in Phase 1 (§9.5)")

    async def update_settings(self, session_id: str, settings: SessionSettings) -> SessionSettings:
        """Validate, then apply to the next turn's argv (§7.9); the account cannot change (§9.2).

        `settings` is the session's whole set of overrides, as the core sends it (`!model ""`
        arrives as `model=None`): a field that is `None` or empty means the agent's own default
        (§5.2), never "unchanged". Only the account is kept from the current state (PM review on
        PR #49). Every refusal raises `InvalidSetting` before anything changes:

        - an effort must be one of `EFFORT_LEVELS`, a model an alias or a `claude-…` name;
        - a new model is tried with one short turn on the session's account (`check_model`),
          so a model the account cannot use is refused now instead of failing every later turn
          (PM on issue #36). The check also gives the resolved name and whether the model takes
          an effort: an inherited effort it does not take is cleared. When the check cannot
          tell (network, usage limit, timeout), the model is accepted;
        - a new effort on a model known not to take one (haiku) is refused.

        Returns the accepted overrides, which the next turn runs on as they are: `None` omits
        the flag (the agent's own default, PM review on PR #49). The backend's defaults were
        folded in by the core when the session was created (§7.2 step 2), never again here.
        """
        session = self._sessions.get(self._resolve(session_id))
        if session is None:
            raise KeyError(f"unknown Claude session {session_id}")
        if settings.model:
            check_model_name(settings.model)
        accepted = SessionSettings(
            model=settings.model or None,
            effort=check_effort(settings.effort) if settings.effort else None,
            approval=settings.approval or None,
            sandbox=None,
            account=session.settings.account,
            extra=dict(settings.extra),
        )
        resolved, supported = session.resolved_model, session.effort_supported
        if accepted.model != session.settings.model:
            check = await self.check_model(session, accepted.model)
            if check.unavailable:
                raise InvalidSetting(
                    "model",
                    accepted.model,
                    [*MODEL_ALIASES, FULL_NAME_CHOICE],
                    f"Model {accepted.model or 'default'} isn't available on this Claude account.",
                )
            resolved, supported = check.model, check.effort_supported
            if supported is False and accepted.effort is not None:
                accepted = replace(accepted, effort=None)  # the core notes the reset (§7.9)
        elif accepted.effort is not None and accepted.effort != session.settings.effort:
            if supported is False:
                raise InvalidSetting(
                    "effort",
                    accepted.effort,
                    [],
                    f"{resolved or accepted.model or 'This model'} takes no effort level. Switch "
                    "to a model that does first, for example !model opus.",
                )
        session.settings = accepted
        session.resolved_model, session.effort_supported = resolved, supported
        return accepted

    async def check_model(self, session: _Session, model: str | None) -> ModelCheck:
        """One short turn with `model` (None: the default) on the session's account (§7.9).

        No tools, no session file, an empty working directory (`MODEL_CHECK_DIR`), a one-word
        prompt. Returns what
        `system/init` and the result said; an empty `ModelCheck` when the check itself failed
        for another reason (the caller then accepts the model, as before the check existed).
        """
        from zab.backends.claude_stream import is_model_unavailable

        argv = [
            self.config.binary,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--no-session-persistence",
            "--tools",
            "",
        ]
        if model:
            argv += ["--model", model]
        found: dict[str, Mapping[str, Any]] = {}

        def on_object(obj: dict[str, Any]) -> None:
            if obj.get("type") == "system" and obj.get("subtype") == "init":
                found["init"] = obj
            elif obj.get("type") == "result":
                found["result"] = obj
            elif obj.get("type") == "rate_limit_event":
                found["limits"] = obj

        try:
            cwd = await asyncio.to_thread(_model_check_dir)
            await asyncio.wait_for(
                run_turn(
                    argv,
                    env=turn_env(session.config_dir, connectors=self.config.claudeai_connectors),
                    cwd=cwd,
                    prompt=MODEL_CHECK_PROMPT,
                    on_object=on_object,
                ),
                MODEL_CHECK_TIMEOUT_S,
            )
        except Exception as exc:  # timeout, spawn failure: the check cannot tell
            log.warning("claude: model check failed (%s); model accepted", type(exc).__name__)
            return ModelCheck()
        result = found.get("result") or {}
        if is_model_unavailable(result):
            return ModelCheck(unavailable=True)
        init = found.get("init") or {}
        if result.get("is_error") or not init:
            log.warning("claude: model check inconclusive; model accepted")
            return ModelCheck()
        resolved = init.get("model")
        active = init.get("per_turn_effort_active")
        info = (found.get("limits") or {}).get("rate_limit_info")
        if isinstance(info, Mapping):  # the check is a turn too: fresher account limits
            self._usage.record_limits(
                session.config_dir,
                info.get("unifiedWindows"),
                resolved if isinstance(resolved, str) else None,
                time.time(),
            )
            await self._save_usage()
        return ModelCheck(
            model=resolved if isinstance(resolved, str) and resolved else None,
            effort_supported=active if isinstance(active, bool) else None,
        )

    async def side_question(self, native_id: str, question: str, *, timeout_s: float) -> str:
        """`!btw` (#716, SPEC §9.10): one `claude -p` process on a copy of the conversation.

        It resumes the session's transcript as it is, a running turn's progress included (its
        tool call in flight reads as never returned), `--fork-session` and never saved, so the
        transcript and the running turn are untouched; a half-written last line is skipped
        (verified on 2.1.283 and 2.1.316, DECISIONS). A fork whose first turn hasn't written
        its own transcript yet is asked from the copy it starts from. No tools, no MCP servers,
        no hooks, no claude.ai connectors (`build_side_argv`). The process counts as a turn
        starting for the account's `auth status` (#307), and a timeout or a cancel stops it
        the §9.6 way. The answer is the last non-empty `result`: resuming a session that runs a
        background command adds a `result` of its own for that command's notification.
        """
        self._require_up()
        session = self._sessions.get(self._resolve(native_id))
        if session is None:
            raise ValueError("This session isn't open yet; send !btw again in a moment.")
        transcript = await asyncio.to_thread(
            find_transcript, session.config_dir, session.native_id, session.cwd
        )
        source = session.native_id if transcript else session.fork_from
        # the session's first turn is running: its transcript holds no ended turn yet
        first = session.busy and session.turn_start_size == 0 and session.fork_from is None
        if source is None or first:
            raise ValueError("There is no completed turn to ask about yet.")
        argv = build_side_argv(self.config, session.settings, source)
        results: list[Mapping[str, Any]] = []
        auth = self._auth.setdefault(session.config_dir, _AuthState())
        release = _no_op
        holder: list[ClaudeTurn] = []
        task: asyncio.Task[tuple[int, str]] | None = None

        def on_object(obj: dict[str, Any]) -> None:
            from zab.backends.claude_stream import model_answered

            if obj.get("type") == "result":
                results.append(obj)
            if model_answered(obj):
                release()

        try:
            async with asyncio.timeout(timeout_s):
                if (probe := auth.probe) is not None and not probe.done():
                    await asyncio.wait({probe}, timeout=AUTH_WAIT_S)  # never both refresh
                release = _starting(auth)
                task = asyncio.create_task(
                    run_turn(
                        argv,
                        env=turn_env(session.config_dir),  # connectors off, no identity
                        cwd=session.cwd,
                        prompt=SIDE_QUESTION_FRAME + question,
                        on_object=on_object,
                        turn_holder=holder,
                    )
                )
                self._side_questions.add(task)
                _code, stderr = await asyncio.shield(task)
        finally:
            release()
            if task is not None:
                try:
                    if not task.done() and holder:  # timed out, or the bridge stops: §9.6
                        with contextlib.suppress(Exception):
                            await holder[0].interrupt()
                finally:  # even if that interrupt is cancelled itself: never leave the group
                    if not task.done():
                        task.cancel()  # run_turn's own cleanup kills what is left, and reaps
                        with contextlib.suppress(BaseException):
                            await task
                    self._side_questions.discard(task)
        answers = [r.get("result") for r in results if not r.get("is_error")]
        answer = next((a for a in reversed(answers) if isinstance(a, str) and a.strip()), "")
        if answer or (results and not results[-1].get("is_error")):
            return answer
        from zab.backends.claude_stream import classify_error

        last = results[-1] if results else {}
        error = classify_error(None, str(last.get("result") or ""), stderr)
        if error.kind == ErrorKind.AUTH:
            raise ValueError(
                "This agent isn't connected to your Claude subscription yet: send me !login in "
                "a direct message."
            )
        if error.kind == ErrorKind.BACKEND_UNAVAILABLE:
            raise ValueError(error.message)  # the lost refresh race: try again in a minute
        line = error.message.splitlines()[0][:300]
        raise ValueError(f"Claude couldn't answer ({error.kind.value}): {line}")

    async def _record_usage(self, session: _Session, info: Any) -> None:
        """Keep what a finished turn reported for `!usage` (`claude_usage`); never raises."""
        try:
            now = time.time()
            model = None if info.model_unavailable else info.model
            if info.rate_limits is not None:
                self._usage.record_limits(session.config_dir, info.rate_limits, model, now)
            if info.result is not None:
                self._usage.record_turn(session.native_id, info.result, model, now)
            await self._save_usage()
        except Exception as exc:  # usage is a convenience: the turn must complete regardless
            log.warning("claude: usage not recorded (%s)", type(exc).__name__)

    async def _save_usage(self) -> None:
        try:
            await asyncio.to_thread(self._usage.save)
        except Exception as exc:
            log.warning("claude: usage file not written (%s)", type(exc).__name__)

    async def usage(self, session_id: str) -> Usage | None:
        """`!usage` (issue #36): the account's latest limits and this session's context and
        per-model totals, as the last turns reported them; never starts a turn (the CLI
        reports usage only during one). None before anything was reported."""
        session = self._sessions.get(self._resolve(session_id))
        account = session.config_dir if session else self.config.config_dir(None)
        native = session.native_id if session else self._resolve(session_id)
        return await asyncio.to_thread(self._usage.usage, native, account)

    def effective_settings(self, session_id: str) -> SessionSettings | None:
        """For display only (§7.9), without I/O: the resolved model from the last `system/init`
        or model check, and the effort the next turn passes (`None`: the CLI's default, or a
        model that takes none). None when nothing is known yet."""
        session = self._sessions.get(self._resolve(session_id))
        if session is None or (session.resolved_model is None and not session.settings.effort):
            return None
        return SessionSettings(
            model=session.resolved_model,
            effort=session.settings.effort if session.effort_supported is not False else None,
            approval=session.settings.approval,
            account=session.settings.account,
        )

    async def rename(self, session_id: str, name: str) -> None:
        return None  # capabilities.rename is False

    def workspace(self, root: str, cwd: str) -> Workspace:
        """A local workspace: zab and Claude run as the same user in Phase 1 (§10, §12.3)."""
        from zab.workspace.local import LocalWorkspace

        return LocalWorkspace(root, cwd)


def _with_undelivered_note(ev: BackendEvent, lost: int) -> BackendEvent:
    """A steered turn's completion, saying so when steers it accepted never reached Claude (the
    process ended, or was stopped, before taking them in): the core had already cleared them."""
    if not lost or not isinstance(ev, TurnCompleted):
        return ev
    note = bridge_note(
        "claude",
        "A message sent during this turn did not reach Claude; send it again."
        if lost == 1
        else f"{lost} messages sent during this turn did not reach Claude; send them again.",
    )
    return replace(ev, final_text=((ev.final_text or "").rstrip() + "\n\n" + note).lstrip())


def _stopped_after_result(ev: BackendEvent) -> BackendEvent:
    """A steered turn stopped after one of its `result`s, while its process ran on (a background
    sub-agent, or a steer's loop, still to come): the turn did not end by itself, so it is
    `INTERRUPTED`, as a stop before any `result` is (#660). A completion that came after the stop
    was already made so as it arrived."""
    if not isinstance(ev, TurnCompleted) or ev.status is not TurnStatus.COMPLETED:
        return ev
    return replace(ev, status=TurnStatus.INTERRUPTED, error=None)


def _add_usage(total: TokenUsage, turn: TokenUsage | None) -> TokenUsage:
    if turn is None:
        return total

    def add(a: int | None, b: int | None) -> int | None:
        return None if a is None and b is None else (a or 0) + (b or 0)

    def addf(a: float | None, b: float | None) -> float | None:
        return None if a is None and b is None else (a or 0.0) + (b or 0.0)

    return TokenUsage(
        input_tokens=add(total.input_tokens, turn.input_tokens),
        output_tokens=add(total.output_tokens, turn.output_tokens),
        total_tokens=add(total.total_tokens, turn.total_tokens),
        context_window=turn.context_window or total.context_window,
        cost_usd=addf(total.cost_usd, turn.cost_usd),
    )


def _find_binary(binary: str) -> str | None:
    if os.sep in binary:
        return binary if os.access(binary, os.X_OK) else None
    return shutil.which(binary)


def _locate_transcript(
    config: ClaudeConfig, session_id: str, account: str | None, cwd: str | None
) -> tuple[str, str, str | None] | None:
    """(account, config_dir, recorded cwd) of the transcript for `session_id`, searching the
    requested account first and then every other configured account; None when no account
    has it (§9.8)."""
    order = [account] if account in config.accounts else []
    order += [name for name in config.accounts if name not in order]
    for name in order:
        config_dir = config.config_dir(name)
        path = find_transcript(config_dir, session_id, cwd)
        if path is None:
            continue
        recorded = read_transcript_head(path).get("cwd")
        return name, config_dir, str(recorded) if recorded else None
    return None


def _transcript_summary_and_mtime(
    config_dir: str, session_id: str, cwd: str | None
) -> tuple[dict[str, Any], float | None]:
    path = find_transcript(config_dir, session_id, cwd)
    if path is None:
        return {}, None
    try:
        return read_transcript_summary(path), os.path.getmtime(path)
    except OSError:
        return {}, None


def _scan_sessions(config: ClaudeConfig, cwd: str | None, limit: int) -> list[SessionInfo]:
    """Transcripts of every account, newest first (§9.8).

    Every project directory is scanned, always: the directory names are lossy (`_`, spaces and
    `.` all become `-`) and long cwds carry a hash suffix the encoder cannot reproduce, so a
    session of this cwd may live under a name other than `encode_project_dir(cwd)`. The
    recorded cwd decides membership; a transcript without one counts only under its own key.
    A scan of 20k transcript heads takes about 0.3 s (PM review on PR #44), and listing is rare.
    """
    found: list[tuple[float, SessionInfo]] = []
    wanted = os.path.normpath(cwd) if cwd is not None else None
    for account, raw_dir in config.accounts.items():
        config_dir = os.path.expanduser(raw_dir)
        projects = os.path.join(config_dir, "projects")
        try:
            dirs = [os.path.join(projects, d) for d in sorted(os.listdir(projects))]
        except OSError:
            continue
        for directory in dirs:
            if not _inside_projects(projects, directory):
                continue  # a symlinked project directory leading elsewhere
            if os.path.basename(directory) == FORK_COPY_DIR:
                continue  # forks' frozen sources: not attachable (codex on #227)
            try:
                entries = list(os.scandir(directory))
            except OSError:
                continue
            for entry in entries:
                if not entry.name.endswith(".jsonl") or not entry.is_file():
                    continue
                if entry.is_symlink() and not _inside_projects(projects, entry.path):
                    continue
                sid = entry.name[: -len(".jsonl")]
                head = read_transcript_head(entry.path)
                recorded = head.get("cwd")
                if wanted is not None:
                    if recorded and os.path.normpath(recorded) != wanted:
                        continue
                    if not recorded and directory != os.path.join(
                        projects, encode_project_dir(cwd)
                    ):
                        continue  # a transcript with no cwd record only counts under its own key
                    session_cwd = cwd
                else:
                    session_cwd = recorded
                if not session_cwd:
                    log.debug("claude: transcript %s has no cwd; skipped", entry.path)
                    continue
                mtime = entry.stat().st_mtime
                preview = head.get("first_user") or ""
                found.append(
                    (
                        mtime,
                        SessionInfo(
                            native_id=sid,
                            cwd=session_cwd,
                            settings=SessionSettings(account=account),
                            resume_hint=ClaudeBackend.resume_hint(config_dir, session_cwd, sid),
                            preview=shorten(preview, 80) if preview else None,
                            updated_at=mtime,
                        ),
                    )
                )
    found.sort(key=lambda item: -item[0])
    return [info for _m, info in found[:limit]]


async def _run_capture(
    argv: list[str],
    *,
    timeout_s: float,
    env: Mapping[str, str] | None = None,
    check: bool = True,
    kill_grace_s: float = 0.0,
    ran: _Ran | None = None,
) -> str:
    """Run a short argument vector and return its stdout (doctor helpers). With `check` a
    non-zero exit raises OSError; without it, stdout is returned whatever the exit code. `ran`
    gets the process's pid, its exit status, and the signal that stopped it, if one had to.

    On the timeout, or when cancelled, the process group is killed: with `kill_grace_s`, by
    SIGTERM first and SIGKILL that much later. On SIGTERM the CLI runs its own shutdown and
    exits within a failsafe of at least 5 s, and its lock library removes the OAuth refresh
    lock at that exit; a SIGKILL before then leaves the lock (the 2.1.283 bundle, DECISIONS
    2026-10-18; `AUTH_STATUS_KILL_GRACE_S`)."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=dict(env) if env is not None else None,
        start_new_session=True,
    )
    ran = ran if ran is not None else _Ran()
    ran.pid = proc.pid
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout_s)
    except (TimeoutError, asyncio.CancelledError) as exc:
        cancelled = isinstance(exc, asyncio.CancelledError)
        # Once begun, the stop runs to its end: a later cancel (`close()` while the time limit's
        # grace runs) would otherwise cut the grace short with the SIGKILL (codex on #451).
        stop = asyncio.ensure_future(_stop_group(proc, kill_grace_s, ran))
        while not stop.done():
            try:
                await asyncio.shield(stop)
            except asyncio.CancelledError:
                cancelled = True  # passed on once the group is gone
        # the stop itself cancelled (the loop's teardown): its `finally` has killed the group
        if cancelled or stop.cancelled():
            raise asyncio.CancelledError from None
        raise TimeoutError(f"{argv[0]} timed out after {timeout_s:.0f} s") from None
    finally:
        ran.returncode = proc.returncode
    if check and proc.returncode != 0:
        # redacted before the cut, which can leave too little of a secret for its rule (#661),
        # and after the strip, which can make a zuliprc line whole (#666)
        detail = redact_secrets(err.decode(errors="replace").replace("\x00", "").strip())[:200]
        raise OSError(f"{' '.join(argv[1:])} exited {proc.returncode}: {detail}")
    return out.decode("utf-8", errors="replace")


async def _stop_group(
    proc: asyncio.subprocess.Process, grace_s: float, ran: _Ran | None = None
) -> None:
    """`_run_capture`'s stop: with `grace_s`, SIGTERM and up to that long for an exit; then
    SIGKILL to the process group, its stragglers included, and the reap."""
    ran = ran if ran is not None else _Ran()
    try:
        if grace_s > 0:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGTERM)
            ran.stopped = "SIGTERM"
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), grace_s)
    finally:
        if proc.returncode is None:
            ran.stopped = "SIGKILL"
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()


@dataclass(slots=True)
class _Ran:
    """What `_run_capture` did with its process, for the log lines of `auth status` (#483)."""

    started: float = field(default_factory=time.monotonic)
    pid: int | None = None
    returncode: int | None = None
    stopped: str = ""  # the signal that `_stop_group` had to end it with

    def summary(self) -> str:
        """`pid 123, exit 0, after 2.4 s`, or `stopped by SIGTERM`, `killed by signal 9`."""
        code = self.returncode
        if self.stopped:
            how = f"stopped by {self.stopped}"
        elif code is None:
            how = "not started" if self.pid is None else "still running"
        elif code < 0:
            how = f"killed by signal {-code}"
        else:
            how = f"exit {code}"
        return f"pid {self.pid}, {how}, after {time.monotonic() - self.started:.1f} s"


async def _capture_auth_status(
    argv: list[str], *, account: str = "?", state: _AuthState | None = None, **kwargs: Any
) -> str:
    """`_run_capture` for `!login`'s `claude auth status`: SIGTERM first, like the doctor's;
    logged when it starts and ends, its pid kept in `state` (#483)."""
    ran = _Ran()
    if state is not None:
        state.login_ran = ran
    log.info("claude: `auth status` for account %s starts, for !login", account)
    try:
        return await _run_capture(argv, kill_grace_s=AUTH_STATUS_KILL_GRACE_S, ran=ran, **kwargs)
    finally:
        log.info(
            "claude: `auth status` for account %s, for !login, ended: %s", account, ran.summary()
        )


async def _auth_status(
    binary: str,
    account: str,
    env: Mapping[str, str],
    state: _AuthState,
    config_dir: str,
    why: str,
) -> Check:
    """`claude auth status` for one account (`ClaudeBackend.doctor`): its answer, which is also
    kept in `state`. Only `loggedIn`, `authMethod` and `subscriptionType` are read (§12). Its
    start and end are logged with what asked for it, its pid, how it ended, how long it took,
    and the refresh lock it leaves, if any (#483)."""
    ran = state.ran = state.ran or _Ran()  # `doctor()` makes it
    log.info("claude: `auth status` for account %s starts, for %s", account, why)
    try:
        # `auth status` exits 1 when nobody is signed in, with the same JSON on stdout
        # (2.1.283 on the v2 VMs, 2.1.284 and 2.1.289 on the development machine): read it either
        # way.
        out = await _run_capture(
            [binary, "auth", "status"],
            timeout_s=AUTH_STATUS_TIMEOUT_S,
            env=env,
            check=False,
            kill_grace_s=AUTH_STATUS_KILL_GRACE_S,
            ran=ran,
        )
        status = json.loads(out)
        if not isinstance(status, dict):
            raise json.JSONDecodeError("not an object", out, 0)
    except asyncio.CancelledError:  # `close()`, after its short wait
        log.info("claude: `auth status` for account %s cut short: %s", account, ran.summary())
        raise
    except Exception as exc:  # spawn failure, timeout, no JSON: unknown, never signed out
        log.warning("claude: `auth status` failed for account %s: %r", account, exc)
        check = Check(
            name=f"claude auth {account}",
            ok=False,
            detail=f"`claude auth status` failed ({type(exc).__name__}); see the log",
        )
    else:
        logged_in = bool(status.get("loggedIn"))
        detail = f"{status.get('authMethod') or '?'}, {status.get('subscriptionType') or '?'}"
        check = Check(
            name=f"claude account {account}",
            ok=logged_in,
            detail=detail if logged_in else NOT_SIGNED_IN,
        )
    state.check, state.checked_at = check, time.monotonic()
    own = {ran.pid: "this `auth status`"} if ran.pid is not None else {}
    lock = await asyncio.to_thread(refresh_lock_state, config_dir, own)
    log.info("claude: `auth status` for account %s ended: %s; %s", account, ran.summary(), lock)
    return check


def refresh_lock_state(config_dir: str, own: Mapping[int, str] | None = None) -> str:
    """The CLI's OAuth refresh locks for `config_dir`, for a log line (#483): how long ago the
    lock was last touched (a live holder does every 5 s); the pid its owner record names, with
    what that process is now (`process_state`) and whether it is one of `own`, and how old the
    lock was that the record was written for (a record outlives its lock: one far older than
    the lock is an earlier holder's); and the legacy lock, if it is there. A turn that meets a
    lock untouched for `CLI_REFRESH_LOCK_WAIT_S` that the CLI can't take over loses the race
    (DECISIONS 2026-10-18, #483). Never raises: a diagnostic must not fail a turn."""
    try:
        return _refresh_lock_state(config_dir, own or {})
    except Exception as exc:  # RecursionError from a deeply nested record on 3.11, say
        return f"the refresh lock's state is unknown ({type(exc).__name__})"


def _refresh_lock_state(config_dir: str, own: Mapping[int, str]) -> str:
    now = time.time()
    legacy = os.path.realpath(config_dir) + CLI_REFRESH_LEGACY_LOCK
    try:
        legacy_age = f"; the legacy lock was touched {now - os.lstat(legacy).st_mtime:.0f} s ago"
    except OSError:
        legacy_age = ""
    try:
        age = now - os.lstat(os.path.join(config_dir, CLI_REFRESH_LOCK)).st_mtime
    except OSError:
        return "no refresh lock" + legacy_age
    touched = f"the refresh lock was touched {max(age, 0.0):.0f} s ago"
    record: Any = None
    with contextlib.suppress(Exception):
        fd = os.open(
            os.path.join(config_dir, CLI_REFRESH_LOCK_OWNER),
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
        )
        try:
            record = json.loads(os.read(fd, 4096))
        finally:
            os.close(fd)
    pid = record.get("pid") if isinstance(record, dict) else None
    if type(pid) is not int or pid <= 1:
        return f"{touched}, without an owner record{legacy_age}"
    name = own.get(pid)
    born = record.get("lockBirthtimeMs")
    taken = ""
    if type(born) in (int, float) and math.isfinite(born):
        taken = f", in a record for a lock taken {max(now - born / 1000, 0.0):.0f} s ago"
    holder = f"pid {pid} ({process_state(pid)}{f', {name}' if name else ''})"
    return f"{touched}, by {holder}{taken}{legacy_age}"


#: Interpreters, by their exact names: a script is named for itself (`process_state`).
_INTERPRETER = re.compile(rb"(?:ba|da)?sh|python(?:3(?:\.\d+)?)?")


def process_state(pid: int) -> str:
    """`gone`, `a zombie`, or `alive: claude auth status, started 12 s ago`: the command only as
    `<program> auth status`, `<program> -p` or the program's name, never its other arguments,
    which for a turn include the session's prompts."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            fields = f.read().rsplit(b")", 1)[-1].split()
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            argv = f.read(1 << 16).split(b"\0")
        with open("/proc/uptime", "rb") as f:
            uptime = float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return "gone" if os.path.isdir("/proc/self") else "unknown"
    if not fields or fields[0] in (b"Z", b"X"):
        return "a zombie"
    if _INTERPRETER.fullmatch(os.path.basename(argv[0])) and len(argv) > 1:
        argv = argv[1:]  # a script, named for itself rather than its interpreter
    name = os.path.basename(argv[0].decode(errors="replace"))[:40] or "?"
    if argv[1:3] == [b"auth", b"status"]:
        name += " auth status"
    elif argv[1:2] == [b"-p"]:
        name += " -p"
    try:
        age = f", started {uptime - int(fields[19]) / os.sysconf('SC_CLK_TCK'):.0f} s ago"
    except (IndexError, ValueError, OSError):
        age = ""
    return f"alive: {name}{age}"


def _last_auth(account: str, state: _AuthState) -> Check:
    """The doctor's answer while a turn of the account is starting: the last one, with its
    age, or a warning when there is none yet (#307 item 1)."""
    from zab.tools.vmstat import words

    if state.check is None:
        return Check(
            name=f"claude auth {account}",
            ok=True,
            warning=True,
            detail="not checked while a turn of this account starts; ask again in a moment",
        )
    age = words(time.monotonic() - state.checked_at)
    return replace(state.check, detail=f"{state.check.detail} (checked {age} ago)")


def _no_op() -> None:
    return None


def _starting(auth: _AuthState) -> Callable[[], None]:
    """Count a turn as starting on its account (`_AuthState.starting`); the returned function
    ends that, once, however often it is called."""
    auth.starting += 1
    pending = True

    def release() -> None:
        nonlocal pending
        if pending:
            pending = False
            auth.starting -= 1

    return release


def refresh_retry_delay(attempt: int, since_first_loss: float) -> float:
    """Seconds before the next try of a turn whose try `attempt` (0-based) lost the refresh
    race, `since_first_loss` seconds after the first try lost it (`REFRESH_RETRIES`)."""
    delay = REFRESH_RETRY_DELAY_S + random.uniform(0, REFRESH_RETRY_JITTER_S)  # noqa: S311
    if attempt + 1 >= REFRESH_RETRIES:  # the next try is the last: past the lock's staleness
        last = REFRESH_LAST_TRY_S + random.uniform(0, REFRESH_RETRY_JITTER_S)  # noqa: S311
        delay = max(delay, last - since_first_loss)
    return delay
