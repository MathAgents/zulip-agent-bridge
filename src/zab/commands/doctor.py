"""Configuration, identity and backend diagnostics (SPEC §13.1)."""

from __future__ import annotations

import argparse
import asyncio
import configparser
import json
import math
import shutil
import stat
import sys
import time
from contextlib import nullcontext
from dataclasses import asdict
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

from zab.config import Config, allowed_users, load_config
from zab.core.backends import make_backend
from zab.core.reason import asked_by
from zab.core.store import BotLock
from zab.core.types import Check
from zab.workspace.git import git_version_check
from zab.zulip.client import ZulipClient, ZulipError, error_kind


def disk_check(paths: set[str]) -> Check:
    """Warn before project builds fill the root or a separate project filesystem."""
    details = []
    devices: set[int] = set()
    warning = False
    for path in ["/", *sorted(paths - {"/"})]:
        try:
            device = Path(path).stat().st_dev
            if device in devices:
                continue
            usage = shutil.disk_usage(path)
            available_total = usage.used + usage.free
            percent = 100 * usage.used / available_total if available_total else 100
            warning |= math.ceil(percent) >= 85
            details.append(f"{path}: {percent:.1f}% used, {usage.free / 1024**3:.1f} GiB free")
            devices.add(device)
        except FileNotFoundError:
            continue  # Missing projects are reported by the project checks below.
        except OSError as exc:
            warning = True
            details.append(f"{path}: disk usage unavailable ({type(exc).__name__})")
    return Check(name="disk", ok=True, warning=warning, detail="; ".join(details))


#: The allowlist check's detail, when it fails, starts with this while the bot has e-mails to
#: allow, no user ID, and no Zulip user it can see has one of the e-mails (#401): a workshop VM
#: whose participant hasn't joined Zulip yet, or whose address is hidden from members (the
#: bots are members), so the bridge admits nobody (SPEC §5.2). The workshop's admin scripts
#: read it as pending on a roster row with no Zulip ID yet, whose next provisioning records the
#: ID (`workshop/admin/admin_common.ALLOWLIST_PENDING`, a copy).
ALLOWLIST_PENDING = "no allowed user yet"


def allowlist_check(bot: str, users: frozenset[int], emails: bool) -> Check:
    """`<bot>:allowlist`: the users the bridge would admit (`config.allowed_users`, with the
    e-mails resolved as the bridge resolves them); a failure says whether e-mails match nobody
    the bot can see or nothing is allowed at all."""
    if users:
        detail = ", ".join(map(str, sorted(users)))
    elif emails:
        detail = (
            f"{ALLOWLIST_PENDING}: no Zulip user with the allowed e-mail visible to this bot "
            "(not joined yet, or the address is hidden from members)"
        )
    else:
        detail = "nobody allowed: no allow_user_ids, no allow_emails and no bot owner"
    return Check(name=f"{bot}:allowlist", ok=bool(users), detail=detail)


#: What a sentinel check in one of `sentinel.DOWN`'s states adds (as `zab.admin.pages` does).
SENTINEL_DOWN = "DM !restart is unavailable, and the bridge answers DM !vm itself"


async def sentinel_checks(config: Config, bot_ids: dict[str, int]) -> list[Check]:
    """`<bot>:sentinel` for each bot (#588; SPEC §13.1): `zab-sentinel.service` installed and
    active, and the bot's mark from this boot and fresh (`sentinel.health`). Anything else is a
    warning, not a failure: DM `!restart` is then unavailable, and the bridge answers DM `!vm`
    itself; or, `unknown`, the user's systemd couldn't be asked. None where `zab.service`
    isn't installed either (macOS, or `zab run` alone): no sentinel runs there. It reads the
    unit's state and the marks, nothing that idle stop counts as activity."""
    from zab.commands.service import SENTINEL, unit_directory
    from zab.tools import sentinel

    units = unit_directory()
    if not (units / "zab.service").is_file():
        return []
    unit = None
    if (units / SENTINEL).is_file():  # its answer, "" for none (`sentinel.health`)
        _, unit = await sentinel.run_command(["systemctl", "--user", "is-active", SENTINEL], 10)
    results = []
    for bot in config.bots:
        state_dir = config.bridge.state_dir
        state, why = sentinel.health(state_dir, bot.name, bot_ids.get(bot.name), unit)
        if state == "ok":
            detail = f"{SENTINEL} active, {why}"
        else:
            detail = f"{state}: {why}" + f"; {SENTINEL_DOWN}" * (state in sentinel.DOWN)
        results.append(
            Check(name=f"{bot.name}:sentinel", ok=True, warning=state != "ok", detail=detail)
        )
    return results


async def checks(config: Config) -> list[Check]:
    results = [Check(name="config", ok=True, detail=str(config.path))]
    bot_ids: dict[str, int] = {}  # the bots that answered `users/me`, for their sentinel
    git = shutil.which("git")
    results.append(Check(name="git", ok=bool(git), detail=git or "not found"))
    if git:  # the bridge's git calls need 2.41 or newer (SPEC §10.6)
        ok, detail = await git_version_check(git)
        results.append(Check(name="git version", ok=ok, detail=detail))
    paths = {p.path for p in config.projects}
    if config.bridge.default_project:
        paths.add(config.bridge.default_project)
    results.append(await asyncio.to_thread(disk_check, paths))
    for path in sorted(paths):
        ok = False
        if git and await asyncio.to_thread(Path(path).is_dir):
            proc = await asyncio.create_subprocess_exec(
                git,
                "-C",
                path,
                "rev-parse",
                "--show-toplevel",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                ok = await asyncio.wait_for(proc.wait(), 10) == 0
            except TimeoutError:
                proc.kill()
                await proc.wait()
        results.append(Check(name=f"project:{path}", ok=ok, detail="git repository"))
    if not config.bots:
        results.append(Check(name="bots", ok=False, detail="no bots configured"))
    for bot in config.bots:
        client = None
        try:
            mode = stat.S_IMODE((await asyncio.to_thread(Path(bot.zuliprc).stat)).st_mode)
            results.append(
                Check(
                    name=f"{bot.name}:permissions",
                    ok=mode == 0o600,
                    detail=f"{mode:04o} (required 0600)",
                )
            )
            client = await asyncio.to_thread(ZulipClient.from_zuliprc, bot.zuliprc)
            me = await client.request("GET", "users/me")
            results.append(
                Check(
                    name=f"{bot.name}:identity",
                    ok=me.get("is_bot") is True,
                    detail=f"user_id={me['user_id']} name={me['full_name']}",
                )
            )
            bot_ids[bot.name] = me["user_id"]
            owner = me.get("bot_owner_id")
            members: list[dict] = []
            if bot.allow_emails:  # e-mails need the user list, as the bridge resolves them (#401)
                members = (await client.request("GET", "users"))["members"]
            users = allowed_users(bot, owner, members)
            for uid in users:
                await client.request("GET", f"users/{uid}")
            results.append(allowlist_check(bot.name, users, bool(bot.allow_emails)))
            response = await client.http.get("server_settings")
            response.raise_for_status()
            date = response.headers.get("Date")
            skew = abs(time.time() - parsedate_to_datetime(date).timestamp()) if date else None
            results.append(
                Check(
                    name=f"{bot.name}:clock",
                    ok=skew is not None and skew < 5,
                    detail=f"skew={skew:.1f}s" if skew is not None else "no Date header",
                )
            )
        except (
            OSError,
            ValueError,
            KeyError,
            configparser.Error,
            ZulipError,
            httpx.HTTPError,
        ) as exc:
            results.append(Check(name=f"{bot.name}:connection", ok=False, detail=error_kind(exc)))
        finally:
            if client:
                await client.close()
    results.extend(await sentinel_checks(config, bot_ids))
    for name, settings in config.backends.items():
        backend = make_backend(name, settings)
        locks = []
        try:
            if settings.kind == "codex" and settings.connect == "spawn":
                try:
                    for bot in config.bots:
                        if bot.backend == name:
                            locks.append(BotLock(config.bridge.state_dir, bot.name))
                except RuntimeError:
                    results.append(
                        Check(
                            name=f"backend:{name}",
                            ok=True,
                            warning=True,
                            detail="spawn diagnostic unavailable while bridge is running",
                        )
                    )
                    continue
            if settings.kind == "codex":
                await backend.start()
                try:
                    await asyncio.wait_for(backend.ready.wait(), 5)
                except TimeoutError:
                    pass
            with asked_by("zab doctor"):
                results.extend(await backend.doctor())
        finally:
            await backend.close()
            for lock in locks:
                lock.close()
    return results


async def service_restart_check() -> Check:
    """Expose systemd's current automatic-restart counter without resetting it."""
    count = None
    systemctl = shutil.which("systemctl")
    if systemctl:
        try:
            proc = await asyncio.create_subprocess_exec(
                systemctl,
                "--user",
                "show",
                "zab.service",
                "--property=NRestarts",
                "--value",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                output, _ = await asyncio.wait_for(proc.communicate(), 10)
            except TimeoutError:
                proc.kill()
                await proc.wait()
            else:
                if proc.returncode == 0:
                    count = int(output.strip())
        except (OSError, ValueError):
            pass
    return Check(
        name="service:restarts",
        ok=True,
        warning=count is None or count > 0,
        detail=(
            f"automatic restarts: {count}; journalctl --user -u zab.service"
            if count is not None
            else "systemd restart count unavailable"
        ),
    )


async def service_python_check() -> Check:
    """Verify the scientific Python imports in the installed unit's environment."""
    python = shutil.which("python3")
    if not python:
        return Check(name="service:python", ok=False, detail="python3 not found in service PATH")
    try:
        proc = await asyncio.create_subprocess_exec(
            python,
            "-c",
            "import numpy, cypari2",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            ok = await asyncio.wait_for(proc.wait(), 30) == 0
        except TimeoutError:
            proc.kill()
            await proc.wait()
            ok = False
    except OSError:
        ok = False
    return Check(
        name="service:python",
        ok=ok,
        detail=f"{python}: numpy, cypari2 imports " + ("OK" if ok else "failed"),
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="zab doctor")
    parser.add_argument("--config")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--service-env", action="store_true")
    args = parser.parse_args(argv)
    try:
        from zab.commands.service import service_environment

        config = load_config(args.config)
        with service_environment() if args.service_env else nullcontext():
            results = asyncio.run(checks(config))
            if args.service_env:
                results.append(asyncio.run(service_restart_check()))
                results.append(
                    Check(
                        name="service:gp",
                        ok=True,
                        detail=shutil.which("gp") or "not found in service PATH (informational)",
                    )
                )
            if args.service_env and (
                config.bridge.service_path_prepend or config.bridge.service_env
            ):
                results.append(asyncio.run(service_python_check()))
    except (OSError, ValueError) as exc:
        results = [Check(name="config", ok=False, detail=str(exc))]
    if args.json:
        print(json.dumps([asdict(check) for check in results]))
    else:
        for check in results:
            label = "WARN" if check.warning else "OK" if check.ok else "FAIL"
            print(f"{label} {check.name}: {check.detail}")
    return 0 if all(check.ok for check in results) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
