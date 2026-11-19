"""The generated Codex app-server schemas the contract tests check against (SPEC §8.3, §15.2).

`SCHEMAS` is the pinned version's, `docs/protocol/codex-<PIN>`, so a pin move needs its schema
committed and no test edit. `CHECKED` adds each schema committed for a newer version, so one
committed ahead of its pin move is checked before the move.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path

from jsonschema import Draft7Validator

from zab.backends.codex import PIN

PROTOCOL = Path(__file__).resolve().parents[2] / "docs/protocol"
SCHEMAS = PROTOCOL / f"codex-{PIN}"


def version(schemas: Path) -> tuple[int, ...]:
    return tuple(int(part) for part in schemas.name.removeprefix("codex-").split("."))


#: The pinned version's schemas, then every newer one committed, oldest first
CHECKED = sorted(
    (p for p in PROTOCOL.glob("codex-*") if version(p) >= version(SCHEMAS)), key=version
)


@functools.cache
def validator(name: str, schemas: Path = SCHEMAS, closed: bool = False) -> Draft7Validator:
    """A validator for the generated `<name>.json` of `schemas`. With `closed`, every object
    `*Params` definition takes no property it doesn't define: the generated ones leave
    additional properties open (review of #568)."""
    schema = json.loads(next(schemas.rglob(f"{name}.json")).read_text())
    if closed:
        for key, definition in schema["definitions"].items():
            if key.endswith("Params") and definition.get("type") == "object":
                definition["additionalProperties"] = False
    return Draft7Validator(schema)
