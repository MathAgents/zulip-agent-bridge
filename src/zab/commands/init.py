"""Idempotent config initialization; only Zulip credentials are copied (SPEC §13.1)."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any

from zab.config import load_config
from zab.zulip.client import ZulipClient, error_kind

#: `--profile`'s names (SPEC §5.3). Neither is written to the config: a profile only chooses the
#: values `zab init` writes.
PROFILES = ("personal", "workshop")
#: Earlier names, accepted as their profile so that existing commands keep working (#593).
ALIASES = {"drew": "personal"}


def _profile(name: str) -> str:
    """argparse converts before it checks `choices`, so an alias is its profile from here on."""
    return ALIASES.get(name, name)


def _value(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return (
            "{ "
            + ", ".join(
                f"{json.dumps(k, ensure_ascii=False)} = {_value(v)}" for k, v in value.items()
            )
            + " }"
        )
    raise ValueError("unsupported TOML value")


def _toml(data: dict[str, Any]) -> str:
    # Top-level inline tables/arrays retain all existing schema values without a
    # TOML-writing dependency. Comments are not preserved.
    return (
        "\n".join(f"{json.dumps(k, ensure_ascii=False)} = {_value(v)}" for k, v in data.items())
        + "\n"
    )


def _atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".zab-init-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


async def _identity(path: Path) -> dict[str, Any]:
    client = await asyncio.to_thread(ZulipClient.from_zuliprc, path)
    try:
        me = await client.request("GET", "users/me")
        if not me.get("is_bot"):
            raise ValueError(f"{path.name}: credentials belong to a human, not a bot")
        return me
    finally:
        await client.close()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="zab init")
    parser.add_argument(
        "--config", default=os.environ.get("ZAB_CONFIG", "~/.config/zab/config.toml")
    )
    parser.add_argument(
        "--profile",
        type=_profile,
        choices=PROFILES,
        default="personal",
        help="personal (the default) for your own machines, workshop for a workshop VM",
    )
    parser.add_argument("--bot", action="append", default=[])
    parser.add_argument("--zuliprc", action="append", default=[])
    parser.add_argument("--backend", action="append", choices=["codex", "claude"], default=[])
    parser.add_argument("--allow-user-id", type=int, action="append", default=[])
    parser.add_argument("--project")
    parser.add_argument("--codex-home")
    args = parser.parse_args(argv)
    if not args.bot or not (len(args.bot) == len(args.zuliprc) == len(args.backend)):
        parser.error("provide one --zuliprc and --backend per --bot")
    if len(set(args.bot)) != len(args.bot) or any(
        not re.fullmatch(r"[A-Za-z0-9_-]+", name) for name in args.bot
    ):
        parser.error("bot names must be unique and contain only letters, digits, _ and -")
    if args.profile == "workshop" and (not args.project or not args.allow_user_id):
        parser.error("workshop requires --project and --allow-user-id")
    target = Path(args.config).expanduser().resolve()
    try:
        if target.exists():
            load_config(target)
            data = tomllib.loads(target.read_text())
        else:
            data = {"bridge": {"default_workspace": "shared"}, "bots": [], "backends": {}}
        data.setdefault("bots", [])
        data.setdefault("backends", {})
        if args.project:
            data.setdefault("bridge", {})["default_project"] = args.project
        if args.profile == "workshop":
            data.setdefault("bridge", {}).update(
                max_concurrent_turns=2,
                default_workspace="shared",
                allow_public_channels=False,
                service_path_prepend=["~/venv/bin"],
                service_env={"VIRTUAL_ENV": "~/venv"},
            )
        copies = []
        identities = []
        for name, rc, backend in zip(args.bot, args.zuliprc, args.backend, strict=True):
            source = Path(rc).expanduser().resolve()
            me = asyncio.run(_identity(source))
            destination = target.parent / f"{name}.zuliprc"
            for existing in data["bots"]:
                path = Path(existing["zuliprc"]).expanduser()
                if (
                    existing["name"] != name
                    and (target.parent / path).resolve() == destination.resolve()
                ):
                    raise ValueError("credential destination belongs to another bot")
            copies.append((destination, source.read_bytes()))
            identities.append((name, me))
            old = next((b for b in data["bots"] if b["name"] == name), {})
            bot = old | {"name": name, "zuliprc": str(destination), "backend": backend}
            if args.allow_user_id:
                bot["allow_user_ids"] = args.allow_user_id
            data["bots"] = [b for b in data["bots"] if b["name"] != name] + [bot]
            settings = {"kind": backend, "binary": shutil.which(backend) or backend}
            if backend == "codex":
                settings |= {
                    "connect": "daemon",
                    "defaults": {
                        "approval": "never",
                        "sandbox": "danger-full-access"
                        if args.profile == "workshop"
                        else "workspace-write",
                    },
                }
            else:
                settings |= {
                    "accounts": {"main": "~/.claude"},
                    "default_account": "main",
                    "defaults": {"approval": "bypassPermissions"},
                }
            data["backends"].setdefault(backend, settings)
            if args.profile == "workshop":
                configured = data["backends"][backend]
                configured["binary"] = settings["binary"]
                configured.setdefault("defaults", {}).update(settings["defaults"])
                if backend == "codex":
                    configured["connect"] = "daemon"
            if backend == "codex" and args.codex_home:
                data["backends"][backend]["codex_home"] = args.codex_home
        content = _toml(data).encode()
        # Validate before replacing either the config or any credential destination.
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, check_path = tempfile.mkstemp(dir=target.parent, suffix=".toml")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
            load_config(check_path)
        finally:
            Path(check_path).unlink(missing_ok=True)
        for destination, credential in copies:
            _atomic(destination, credential)
        _atomic(target, content)
        for name, me in identities:
            print(f"{name}: bot {me['user_id']}, owner {me.get('bot_owner_id')}")
        print(f"Wrote {target}; next run zab doctor --config {target}, then zab run.")
        return 0
    except Exception as exc:
        # Never echo an HTTP request or credential-parser value.
        print(f"zab init failed ({error_kind(exc)})")
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
