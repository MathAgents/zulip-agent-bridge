"""Construct backend adapters from the common configuration (SPEC §3.1, §9)."""

from dataclasses import asdict

from zab.backends.codex import CodexBackend
from zab.config import BackendConfig
from zab.core.types import Backend


def make_backend(name: str, config: BackendConfig) -> Backend:
    if config.kind == "codex":
        return CodexBackend(name, config)
    if config.kind == "claude":
        from zab.backends.claude import ClaudeBackend, ClaudeConfig

        return ClaudeBackend(name, ClaudeConfig.from_mapping(asdict(config)))
    raise ValueError(f"unsupported backend kind: {config.kind}")
