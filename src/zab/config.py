"""Strict TOML configuration with config-relative paths (SPEC §5)."""

from __future__ import annotations

import os
import re
import socket
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from zab.core.types import SessionSettings


class ConfigError(ValueError):
    """A configuration error safe to display without credential contents."""


@dataclass
class BridgeConfig:
    machine: str = field(default_factory=lambda: socket.gethostname().split(".")[0])
    state_dir: str = "~/.local/state/zab"
    projects_root: str = "~/src"
    default_project: str = ""
    auto_clone: bool = False
    allow_public_channels: bool = False
    multi_party: bool = True
    mention_after_s: int = 120
    status_edit_interval_s: int = 300  # at least 60: Zulip Cloud's guidance (SPEC §4.9, #754)
    max_concurrent_turns: int = 3
    default_workspace: str = "shared"
    service_path_prepend: list[str] = field(default_factory=list)
    service_env: dict[str, str] = field(default_factory=dict)
    # Run in every new topic worktree (issue #205): an argument list, no shell, 60 s, in the
    # worktree; a failure is logged and never stops the session. The workshop VMs use
    # ["/usr/local/bin/lean-project", "--repair-all"] to link committed Lean projects.
    worktree_post_create: list[str] = field(default_factory=list)


@dataclass
class BotConfig:
    name: str = ""
    zuliprc: str = ""
    backend: str = ""
    allow_user_ids: list[int] = field(default_factory=list)
    allow_emails: list[str] = field(default_factory=list)


@dataclass
class BackendConfig:
    kind: str = ""
    binary: str = ""
    connect: str = "daemon"
    socket: str = ""
    codex_home: str = ""
    answer_external_approvals: bool = False
    defaults: dict[str, str] = field(default_factory=dict)
    accounts: dict[str, str] = field(default_factory=dict)
    default_account: str = "main"
    # Claude restricted-mode defaults (SPEC §9.5); explicit tool lists are preserved.
    allowed_tools: list[str] = field(
        default_factory=lambda: ["Read", "Glob", "Grep", "Edit", "Write"]
    )
    disallowed_tools: list[str] = field(default_factory=list)
    # Claude: the signed-in account's claude.ai connectors in bridge turns (off, issue #36)
    claudeai_connectors: bool = False
    # Claude: messages sent during a turn steer it (stream-json input, issue #36); on unless
    # `steer = false` (Drew, Oct 15, issue #310). Codex always steers and ignores the key
    steer: bool = True
    git_identity: str = "auto"


@dataclass
class ProjectConfig:
    channel: str | int = ""
    path: str = ""
    workspace: str = ""
    subdir: str = ""
    backend_defaults: dict[str, dict[str, str]] = field(default_factory=dict)


@dataclass
class RenderConfig:
    auto_upload_ext: list[str] = field(
        default_factory=lambda: ["png", "jpg", "jpeg", "gif", "svg", "pdf"]
    )
    max_upload_mb: int = 10
    diff_inline_max_lines: int = 300
    # The central file server that serves turn snapshots from GitHub, e.g.
    # `https://files.mathagents.org` (rc6, SPEC §10.7); empty = off. No secret in it.
    refserve_url: str = ""
    # The GitHub owner whose repositories that server serves (its ZAB_FILES_ORG): snapshots are
    # pushed, and linked, only for a checkout whose origin is one of them. `push_config.py`
    # writes it from the workshop's `github_org` (#693). The default, the example deployment's
    # organization, stays for configs written before that, which have only `refserve_url`.
    refserve_owner: str = "MathAgents"
    # Text files a final answer references, uploaded as `<name>.<ext>.txt` when they would
    # otherwise show as *(local)* (issue #36): valid UTF-8 only, per-file cap, per-answer cap.
    auto_upload_text_ext: list[str] = field(
        default_factory=lambda: [
            "lean",
            "tex",
            "bib",
            "sty",
            "py",
            "sage",
            "gp",
            "m",
            "g",
            "mag",
            "txt",
            "md",
            "json",
            "csv",
            "toml",
            "yaml",
            "yml",
            "sh",
            "c",
            "h",
            "cpp",
            "rs",
            "jl",
            "log",
            "out",
        ]
    )
    max_text_upload_kb: int = 512
    max_text_uploads: int = 10  # per answer; 0 turns text uploads off


@dataclass
class LeanConfig:
    # Stop a Lean server a Codex thread holds (`lake serve` and the processes under it) once it
    # has been idle this long: the same processes, under 0.04 s of CPU a minute each, at least
    # 0.1 s; 0 = never (SPEC §7.12, #249)
    reap_idle_minutes: int = 30


@dataclass
class Config:
    path: Path
    bridge: BridgeConfig
    bots: list[BotConfig]
    backends: dict[str, BackendConfig]
    projects: list[ProjectConfig]
    render: RenderConfig
    lean: LeanConfig = field(default_factory=LeanConfig)


def load_config(path: str | Path | None = None) -> Config:
    """Load and validate config; never open any credentials or agent home files."""
    source = Path(path or os.environ.get("ZAB_CONFIG", "~/.config/zab/config.toml"))
    source = source.expanduser().resolve()
    text = source.read_text()

    def fail(key: str, reason: str) -> None:
        leaf = key.rsplit(".", 1)[-1]
        match = re.search(r"(?m)^.*?\b" + re.escape(leaf) + r"\s*=", text)
        line = text.count("\n", 0, match.start()) + 1 if match else 1
        raise ConfigError(f"{source}:{line}: {key}: {reason}")

    def table(cls: type, value: Any, key: str) -> Any:
        if not isinstance(value, dict):
            fail(key, "expected a table")
        defaults = cls()
        valid = {f.name for f in fields(cls)}
        for name, item in value.items():
            if name not in valid:
                fail(f"{key}.{name}", "unknown key")
            default = getattr(defaults, name)
            if cls is ProjectConfig and name == "channel":
                good = type(item) in (str, int)
            else:
                good = type(item) is type(default)
            if not good:
                fail(f"{key}.{name}", "invalid value type")
            if isinstance(item, list):
                element = int if name == "allow_user_ids" else str
                if any(type(x) is not element for x in item):
                    fail(f"{key}.{name}", "invalid list element type")
            setattr(defaults, name, item)
        return defaults

    def absolute(value: str) -> str:
        p = Path(value).expanduser()
        return str((source.parent / p).resolve()) if value else ""

    def settings(value: Any, key: str) -> None:
        if not isinstance(value, dict):
            fail(key, "expected a settings table")
        for name, item in value.items():
            if name not in {"model", "effort", "approval", "sandbox", "account"}:
                fail(f"{key}.{name}", "unknown setting")
            if not isinstance(item, str):
                fail(f"{key}.{name}", "expected a string")

    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{source}: invalid TOML: {exc}") from exc
    for key in raw:
        if key not in {"bridge", "bots", "backends", "projects", "render", "lean"}:
            fail(key, "unknown key")
    bridge = table(BridgeConfig, raw.get("bridge", {}), "bridge")
    render = table(RenderConfig, raw.get("render", {}), "render")
    lean = table(LeanConfig, raw.get("lean", {}), "lean")
    for path in bridge.service_path_prepend:
        if not path or ":" in path or any(ord(c) < 32 for c in path):
            fail(
                "bridge.service_path_prepend",
                "expected nonempty directories without controls or colons",
            )
    bridge.service_path_prepend = [absolute(path) for path in bridge.service_path_prepend]
    for key, value in bridge.service_env.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or key in ("PATH", "CODEX_HOME"):
            fail("bridge.service_env", "invalid or reserved environment key")
        if not isinstance(value, str) or any(ord(c) < 32 for c in value):
            fail("bridge.service_env", "expected string values without control characters")
        if key == "VIRTUAL_ENV":
            bridge.service_env[key] = absolute(value)

    for name in ("state_dir", "projects_root", "default_project"):
        setattr(bridge, name, absolute(getattr(bridge, name)))
    for name in ("state_dir", "projects_root", "machine"):
        if not getattr(bridge, name):
            fail(f"bridge.{name}", "must not be empty")
    if bridge.default_workspace not in ("shared", "worktree"):
        fail("bridge.default_workspace", "expected shared or worktree")
    for obj, prefix, names in (
        (bridge, "bridge", ("status_edit_interval_s", "max_concurrent_turns")),
        (render, "render", ("max_upload_mb", "diff_inline_max_lines", "max_text_upload_kb")),
    ):
        for name in names:
            if getattr(obj, name) <= 0:
                fail(f"{prefix}.{name}", "must be positive")
    if render.max_text_uploads < 0:
        fail("render.max_text_uploads", "must be nonnegative")
    if lean.reap_idle_minutes < 0:
        fail("lean.reap_idle_minutes", "must be nonnegative")
    if bridge.mention_after_s < 0:
        fail("bridge.mention_after_s", "must be nonnegative")
    if bridge.status_edit_interval_s < 60:
        fail("bridge.status_edit_interval_s", "must be at least 60")
    if not isinstance(raw.get("backends", {}), dict):
        fail("backends", "expected a table")
    backends = {}
    for name, data in raw.get("backends", {}).items():
        backend = table(BackendConfig, data, f"backends.{name}")
        if backend.git_identity not in ("auto", "off"):
            if not re.fullmatch(r"[^<>\r\n]+ <[^<>\s@]+@[^<>\s@]+>", backend.git_identity):
                fail(f"backends.{name}.git_identity", 'expected auto, off, or "Name <email>"')
        if backend.kind not in ("codex", "claude"):
            fail(f"backends.{name}.kind", "expected codex or claude")
        if backend.connect not in ("daemon", "spawn"):
            fail(f"backends.{name}.connect", "expected daemon or spawn")
        settings(backend.defaults, f"backends.{name}.defaults")
        backend.binary = backend.binary or backend.kind
        if "/" in backend.binary:
            backend.binary = absolute(backend.binary)
        backend.codex_home = absolute(backend.codex_home)
        backend.socket = absolute(backend.socket)
        for account, directory in backend.accounts.items():
            if not isinstance(directory, str) or not directory:
                fail(f"backends.{name}.accounts.{account}", "expected a nonempty path")
        backend.accounts = {k: absolute(v) for k, v in backend.accounts.items()}
        if backend.kind == "claude" and backend.default_account not in backend.accounts:
            fail(f"backends.{name}.default_account", "unknown account")
        backends[name] = backend
    for key in ("bots", "projects"):
        if not isinstance(raw.get(key, []), list):
            fail(key, "expected an array of tables")
    bots = [table(BotConfig, x, "bots") for x in raw.get("bots", [])]
    names = set()
    for bot in bots:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", bot.name) or bot.name in names:
            fail("bots.name", "expected a unique filename-safe bot name")
        names.add(bot.name)
        if not bot.zuliprc:
            fail("bots.zuliprc", "required")
        bot.zuliprc = absolute(bot.zuliprc)
        if bot.backend not in backends:
            fail("bots.backend", "unknown backend")
        if any(not re.fullmatch(r"[^\s@]+@[^\s@]+", email) for email in bot.allow_emails):
            fail("bots.allow_emails", "expected nonempty email addresses without whitespace")
        if any(uid <= 0 for uid in bot.allow_user_ids):
            fail("bots.allow_user_ids", "user ids must be positive")
    projects = [table(ProjectConfig, x, "projects") for x in raw.get("projects", [])]
    channels = set()
    for project in projects:
        if not project.channel or project.channel in channels:
            fail("projects.channel", "expected a unique nonempty channel")
        channels.add(project.channel)
        if not project.path:
            fail("projects.path", "required")
        project.path = absolute(project.path)
        if project.workspace not in ("", "shared", "worktree"):
            fail("projects.workspace", "expected shared or worktree")
        if Path(project.subdir).is_absolute() or ".." in Path(project.subdir).parts:
            fail("projects.subdir", "must stay inside the project")
        for name, value in project.backend_defaults.items():
            if name not in backends:
                fail(f"projects.backend_defaults.{name}", "unknown backend")
            settings(value, f"projects.backend_defaults.{name}")
    return Config(source, bridge, bots, backends, projects, render, lean)


def merge_settings(*layers: dict[str, str]) -> SessionSettings:
    """Apply increasing precedence; empty strings reset to the agent default."""
    merged = {key: value or None for layer in layers for key, value in layer.items()}
    return SessionSettings(**merged)


def allowed_users(
    bot: BotConfig, owner_id: int | None, users: Iterable[dict[str, Any]] = ()
) -> frozenset[int]:
    """Explicit IDs + resolved emails replace the owner, even if none resolve."""
    if not bot.allow_user_ids and not bot.allow_emails:
        return frozenset([owner_id] if owner_id else [])
    emails = {email.casefold() for email in bot.allow_emails}
    return frozenset(bot.allow_user_ids) | frozenset(
        user["user_id"]
        for user in users
        if user.get("is_active", True)
        and user.get("is_bot") is False
        and (user.get("delivery_email") or user.get("email") or "").casefold() in emails
    )
