"""Read-only resolved settings for commands and details (SPEC §4.6, §7.9)."""

import inspect
import logging

from zab.core import types as t


def effective_settings(backend: t.Backend, session_id: str) -> t.SessionSettings | None:
    """Tolerate backends without the display hook during the additive rollout."""
    hook = getattr(backend, "effective_settings", None)
    if hook is None or inspect.iscoroutinefunction(hook):
        return None
    try:
        result = hook(session_id)
        return result if isinstance(result, t.SessionSettings) else None
    except Exception as exc:
        logging.getLogger(__name__).warning("Settings display unavailable: %s", type(exc).__name__)
        return None


def display_setting(
    field: str, overrides: t.SessionSettings, effective: t.SessionSettings | None
) -> str:
    explicit = getattr(overrides, field)
    resolved = getattr(effective, field) if effective else None
    value = resolved or explicit
    return (value + (" (default)" if explicit is None else "")) if value else "default"
