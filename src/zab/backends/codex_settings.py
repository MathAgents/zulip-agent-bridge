"""Connection-scoped model discovery and settings validation (SPEC §7.9, §8.4)."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from zab.core import types as t

if TYPE_CHECKING:
    from zab.backends.codex import CodexBackend


class ModelCatalog:
    def __init__(self) -> None:
        self.connection: object = None
        self.models: list[dict[str, Any]] | None = None
        self.lock = asyncio.Lock()

    async def load(self, backend: CodexBackend) -> list[dict[str, Any]] | None:
        try:
            async with asyncio.timeout(2), self.lock:
                rpc = backend.rpc
                if rpc is not self.connection:
                    self.connection, self.models = rpc, None
                if self.models is not None:
                    return self.models
                models, seen = [], set()
                cursor = None
                while True:
                    response = await backend._call("model/list", includeHidden=True, cursor=cursor)
                    page = response["data"]
                    if not isinstance(page, list):
                        raise ValueError("invalid catalog")
                    for model in page:
                        # Fail open on a malformed catalog, never on a half-loaded one.
                        if not all(
                            isinstance(model[k], str)
                            for k in ("id", "model", "defaultReasoningEffort")
                        ):
                            raise ValueError("invalid model")
                        for effort in model["supportedReasoningEfforts"]:
                            if not isinstance(effort["reasoningEffort"], str):
                                raise ValueError("invalid effort")
                    models.extend(page)
                    cursor = response.get("nextCursor")
                    if cursor is None:
                        break
                    if cursor in seen:
                        raise ValueError("repeated catalog cursor")
                    seen.add(cursor)
                if not models or backend.rpc is not rpc:
                    raise ValueError("catalog unavailable")
                self.models = models
                return models
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "model/list unavailable; accepting settings without validation (%s)",
                type(exc).__name__,
            )
            return None

    def find(self, name: str | None) -> dict[str, Any] | None:
        return next(
            (
                m
                for m in self.models or []
                if (name in (m["id"], m["model"]) if name is not None else m.get("isDefault"))
            ),
            None,
        )

    def validate(
        self, settings: t.SessionSettings, previous: t.SessionSettings, default: t.SessionSettings
    ) -> t.SessionSettings:
        selected = self.find(settings.model or default.model)
        if selected is None:
            if settings.model is not None:
                raise t.InvalidSetting(
                    "model", settings.model, sorted({m["model"] for m in self.models or []})
                )
            return settings  # No known default: do not guess which effort list applies.
        valid = [e["reasoningEffort"] for e in selected["supportedReasoningEfforts"]]
        if settings.effort is not None and settings.effort not in valid:
            if settings.model != previous.model and settings.effort == previous.effort:
                return replace(settings, effort=None)
            raise t.InvalidSetting("effort", settings.effort, valid)
        return settings

    def effective(
        self, settings: t.SessionSettings, default: t.SessionSettings
    ) -> t.SessionSettings:
        selected = self.find(settings.model or default.model)
        model = selected["model"] if selected else settings.model or default.model
        effort = settings.effort
        if effort is None:
            effort = default.effort if model == default.model else None
            if effort is None and selected:
                effort = selected["defaultReasoningEffort"]
        return replace(settings, model=model, effort=effort)
