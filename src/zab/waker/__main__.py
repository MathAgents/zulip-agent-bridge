"""Optional `waker run|status` entry point; SDK installed only on the waker host.

Two modes (docs/ADMIN.md §1.2):

- **Direct**, on the files host (#207): the config and the bots' zuliprcs are root's, 0600
  (`/etc/zab-waker/`), and the waker starts VMs with boto3 and the host's role.
- **Starter**, on the admin server (PR 21): the config has `starter`, the socket of
  zab-admin-starter (`zab.admin.starter`), and `[[workshop]]` entries, each with its organizers
  channel and its opted-in `[[workshop.bots]]`. The config and the zuliprcs, beside it in
  `/var/lib/zab-admin/waker/`, are zab-admin's, group zab-waker, 0640. The waker runs as
  zab-waker with no AWS credential: it asks the starter for states and starts and never
  imports boto3. It writes `status_file` for the collector, and exits when its config changes,
  for systemd to start it again with the new one.

Either way, a waker that starts catches up on the messages sent while it was down
(`zab.waker.queues`, #403): a config change's restart, a deploy, a reboot.
"""

from __future__ import annotations

import argparse
import asyncio
import grp
import json
import logging
import os
import pwd
import re
import stat
import tempfile
import time
import tomllib
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zab.config import BotConfig
from zab.core.store import BotLock, Store
from zab.waker.service import READY, STATES, WORKSHOP, error_code, forget
from zab.zulip.client import ZulipClient

LOG = logging.getLogger("zab.waker")
#: The admin server's users (workshop/adminserver/sysusers.conf): the bot writes the waker's
#: files, which the waker reads through its group.
ADMIN_USER, WAKER_GROUP = "zab-admin", "zab-waker"
#: admin.toml's workshop names (`zab.admin.config`).
_WORKSHOP_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")
STARTER_KEYS = {"starter", "state_dir", "status_file", "workshop"}
BOT_KEYS = {"name", "instance_id", "zuliprc", "allow_user_ids", "allow_emails"}
#: Starter mode: seconds between writes of the status file and checks of the config.
PUBLISH = 30.0
#: Starter mode: a workshop's first state refresh is retried after RETRY seconds, doubling to
#: RETRY_MAX; `--wait-for-config` looks for the config every WAIT seconds.
RETRY, RETRY_MAX, WAIT = 5.0, 600.0, 30.0
#: Starter mode: seconds a workshop's waker must have run before its `ready_at` moves (#717).
UP_AFTER = 60.0


def private_file(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError(f"{path}: expected a root-owned regular file with mode 0600")


def shared_file(path: Path) -> None:
    """The admin server's rule (ADMIN §1.3): the bot writes the file, the waker's group reads
    it."""
    try:
        owners, group = {0, pwd.getpwnam(ADMIN_USER).pw_uid}, grp.getgrnam(WAKER_GROUP).gr_gid
    except KeyError:
        raise ValueError(f"{path}: no user {ADMIN_USER} or group {WAKER_GROUP} here") from None
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid not in owners
        or info.st_gid != group
        or stat.S_IMODE(info.st_mode) != 0o640
    ):
        raise ValueError(
            f"{path}: expected a regular file of {ADMIN_USER} or root, group {WAKER_GROUP}, "
            "mode 0640"
        )


def layout(path: Path) -> bool:
    """Whether the config follows the admin server's rule (starter mode) rather than the files
    host's (direct mode); neither is an error."""
    try:
        private_file(path)
        return False
    except ValueError as direct:
        try:
            shared_file(path)
        except ValueError as shared:
            raise ValueError(f"{direct}; on the admin server: {shared}") from None
        return True


def entry(row: dict, names: set[str], check: Callable[[Path], None]) -> tuple[BotConfig, str]:
    row = dict(row)
    instance = row.pop("instance_id")
    row.pop("region", None)
    if not re.fullmatch(r"i-[a-f0-9]{8,17}", instance):
        raise ValueError("invalid instance_id")
    bot = BotConfig(**row)
    if not bot.name or bot.name in names:
        raise ValueError("bot names must be nonempty and unique")
    names.add(bot.name)
    if not isinstance(bot.allow_user_ids, list) or any(
        type(uid) is not int or uid <= 0 for uid in bot.allow_user_ids
    ):
        raise ValueError("allow_user_ids must contain positive integers")
    if not isinstance(bot.allow_emails, list) or any(
        not isinstance(email, str) or not re.fullmatch(r"[^\s@]+@[^\s@]+", email)
        for email in bot.allow_emails
    ):
        raise ValueError("invalid allow_emails")
    check(Path(bot.zuliprc))
    return bot, instance


def load(path: Path) -> tuple[dict, list[tuple[BotConfig, str]]]:
    shared = layout(path)
    config = tomllib.loads(path.read_text())
    if shared != ("starter" in config):
        raise ValueError("`starter` goes with the admin server's file layout, and only with it")
    if shared:
        return load_fleets(path, config)
    if type(config.get("organizers_channel")) is not int or config["organizers_channel"] <= 0:
        raise ValueError("organizers_channel must be a positive channel ID")
    workshop = config.setdefault("workshop", WORKSHOP)
    if not isinstance(workshop, str) or not re.fullmatch(r"[A-Za-z0-9._:/=+@-]{1,256}", workshop):
        raise ValueError("workshop must be the value of the VMs' workshop tag")
    names: set[str] = set()
    entries = [entry(row, names, private_file) for row in config.get("bots", [])]
    if not entries:
        raise ValueError("configure at least one opted-in bot")
    return config, entries


@dataclass
class Fleet:
    """Starter mode: one workshop's organizers channel and opted-in bots."""

    name: str
    organizers: int
    entries: list[tuple[BotConfig, str]] = field(default_factory=list)


def load_fleets(path: Path, config: dict) -> tuple[dict, list[tuple[BotConfig, str]]]:
    """Starter mode: every workshop's bots, their zuliprcs under the config's directory;
    `config["fleets"]` lists the workshops. A refusal across workshops here (a bot or an
    instance in two of them) refuses the whole config: `push_waker.check_shared` makes the same
    checks at the plan's dry run, so add any new one there too (#347)."""
    if unknown := sorted(set(config) - STARTER_KEYS):
        raise ValueError(f"unknown key {unknown[0]}")
    for key in ("starter", "state_dir", "status_file"):
        if key in config and not (isinstance(config[key], str) and config[key].startswith("/")):
            raise ValueError(f"{key} must be an absolute path")
    home = f"{path.absolute().parent}/"
    fleets: list[Fleet] = []
    owners: dict[str, str] = {}
    names: set[str] = set()
    tables = config.get("workshop", [])  # none left after the last opt-out: the waker idles
    if not isinstance(tables, list):
        raise ValueError("expected [[workshop]] entries")
    for table in tables:
        if not isinstance(table, dict) or set(table) - {"name", "organizers_channel", "bots"}:
            raise ValueError("a [[workshop]] has name, organizers_channel and [[workshop.bots]]")
        name, organizers = table.get("name"), table.get("organizers_channel")
        if not isinstance(name, str) or not _WORKSHOP_NAME.fullmatch(name):
            raise ValueError("a workshop's name is its name in admin.toml")
        if any(fleet.name == name for fleet in fleets):
            raise ValueError(f"workshop {name} appears twice")
        if type(organizers) is not int or organizers <= 0:
            raise ValueError("organizers_channel must be a positive channel ID")
        fleet = Fleet(name, organizers)
        for row in table.get("bots", []):
            zuliprc = row.get("zuliprc") if isinstance(row, dict) else None
            if not isinstance(zuliprc, str) or set(row) - BOT_KEYS:
                raise ValueError(f"a bot has {', '.join(sorted(BOT_KEYS))}")
            if os.path.normpath(zuliprc) != zuliprc or not zuliprc.startswith(home):
                raise ValueError(f"{zuliprc}: a zuliprc must lie under {home}")
            bot, instance = entry(row, names, shared_file)
            if owners.setdefault(instance, name) != name:
                raise ValueError(f"{instance} is in two workshops")
            fleet.entries.append((bot, instance))
        if not fleet.entries:
            raise ValueError(f"workshop {name}: configure at least one opted-in bot")
        fleets.append(fleet)
    config["fleets"] = fleets
    return config, [e for fleet in fleets for e in fleet.entries]


class Regions:
    """Keep region routing inside the EC2 adapter; instance IDs remain opt-in only."""

    def __init__(self, regions: dict, clients: dict) -> None:
        self.regions, self.clients = regions, clients

    def describe_instances(self, *, Filters: list) -> dict:
        reservations = []
        for region, client in self.clients.items():
            filters = [f for f in Filters if f["Name"] != "instance-id"]
            filters.append(
                {
                    "Name": "instance-id",
                    "Values": [i for i, r in self.regions.items() if r == region],
                }
            )
            response = client.describe_instances(Filters=filters)
            reservations.extend(response["Reservations"])
            while response.get("NextToken"):
                response = client.describe_instances(
                    Filters=filters, NextToken=response["NextToken"]
                )
                reservations.extend(response["Reservations"])
        return {"Reservations": reservations}

    def start_instances(self, *, InstanceIds: list[str]) -> dict:
        if len(InstanceIds) != 1:
            raise ValueError("start one opted-in instance at a time")
        return self.clients[self.regions[InstanceIds[0]]].start_instances(InstanceIds=InstanceIds)

    def close(self) -> None:
        for client in self.clients.values():
            client.close()


def report(config: dict, entries: list[tuple[BotConfig, str]], store: Store) -> dict:
    """`waker status` and the status file: the states seen last and the last wake attempts,
    without an AWS call or a credential read; in starter mode, by workshop."""

    def bots(entries: list[tuple[BotConfig, str]], snapshot: dict) -> list[dict]:
        return [
            {
                "bot": bot.name,
                "instance": instance,
                "state": snapshot.get("states", {}).get(instance, "unknown"),
                "last_wake_attempt": store.get(f"wake:{instance}"),
            }
            for bot, instance in entries
        ]

    if "starter" not in config:
        snapshot = store.get("waker_states", {})
        return {"checked_at": snapshot.get("at"), "bots": bots(entries, snapshot)}
    workshops = []
    for fleet in config["fleets"]:
        snapshot = store.get(f"{STATES}{fleet.name}", {})
        checked, up = snapshot.get("at"), stored(store, READY, fleet.name)
        workshops.append({
            "name": fleet.name, "checked_at": checked, "ready_at": up,
            "bots": bots(fleet.entries, snapshot),
        })  # fmt: skip
    return {"written_at": time.time(), "workshops": workshops}


def stored(store: Store, prefix: str, name: str) -> Any:
    try:
        return store.get(f"{prefix}{name}")
    except ValueError:  # not JSON: damaged
        return None


def ready(store: Store, names: list[str], running: dict[str, float]) -> None:
    """Starter mode: each workshop's `ready_at` (#717), when its waker last served: now, once
    its run has lasted `UP_AFTER` (`running` holds each run's start). A waker killed at every
    start, as on Nov 1, writes the status file at each, but never moves this. It is kept in the
    store, so that a restart (each config change) shows the last run's; a workshop without one,
    new or from an earlier release, gets the first start's. The bots say a waker whose
    `ready_at` is a few minutes old is down (`zab.admin.wakerdown`)."""
    now = time.time()
    for name in names:
        since, last = running.get(name), stored(store, READY, name)
        if (since is not None and now - since >= UP_AFTER) or type(last) not in (int, float):
            store.set(f"{READY}{name}", now)  # none yet, or damaged: this start's


def publish(path: Path, data: dict) -> None:
    """The status file, for the collector (zab-admin): replaced whole, mode 0640, in a directory
    whose setgid group is zab-admin (tmpfiles.conf)."""
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".status-")
    try:
        os.fchmod(fd, 0o640)
        with os.fdopen(fd, "w") as file:
            json.dump(data, file, indent=1)
        os.replace(temporary, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(temporary)
        raise


def fingerprint(path: Path) -> tuple:
    try:
        info = path.stat()
    except FileNotFoundError:
        return ()
    return info.st_ino, info.st_mtime_ns, info.st_size


async def supervise(
    name: str, make: Callable[[], Any], running: dict[str, float] | None = None
) -> None:
    """Starter mode: one workshop's waker, contained, so that its failure never stops another
    workshop's (review on #343). Its first state refresh is retried with a backoff, from `RETRY`
    to `RETRY_MAX`: the starter may not know the workshop (yet, or any more), or fail. Each new
    error is logged once. A run that fails is logged and a new waker built, since `Waker.run`
    closes its Zulip clients. The backoff and the log start afresh only after a run that lasted
    `RETRY_MAX`. While a run lasts, `running` holds its start, for `ready`."""
    delay, last, announced = RETRY, None, False
    while True:
        waker, started = None, None
        try:
            waker = make()
            await waker.refresh(force=True)
            if last is not None and not announced:
                LOG.info("Workshop %s: waker running again", name)
                announced = True
            started = time.monotonic()
            if running is not None:
                running[name] = time.time()
            await waker.run()
        except Exception as exc:
            if started is not None and time.monotonic() - started >= RETRY_MAX:
                delay, last = RETRY, None  # it ran for a while: a new episode
            error = error_code(exc)
            if error != last:
                LOG.warning("Workshop %s: no waker (%s); retrying with a backoff", name, error)
                last, announced = error, False
        finally:
            if running is not None:
                running.pop(name, None)
            for target in getattr(waker, "targets", []):  # closing twice is harmless
                await target.client.close()
        await asyncio.sleep(delay)
        delay = min(delay * 2, RETRY_MAX)


async def serve(
    fleets: list[tuple[str, Callable[[], Any]]],
    path: Path,
    config: dict,
    entries: list[tuple[BotConfig, str]],
    store: Store,
) -> None:
    """Starter mode: each workshop's waker under `supervise`, the status file every `PUBLISH`
    seconds, with each workshop's `ready_at` (`ready`), and an exit when the config changes
    (`push_waker.py --config-dir`), for systemd to restart it."""
    seen, running = fingerprint(path), {}
    names = [name for name, _ in fleets]
    async with asyncio.TaskGroup() as group:
        tasks = [group.create_task(supervise(name, make, running)) for name, make in fleets]
        while fingerprint(path) == seen:
            if status := config.get("status_file"):
                try:
                    ready(store, names, running)
                    publish(Path(status), report(config, entries, store))
                except Exception as exc:
                    LOG.warning("Status file not written (%s)", type(exc).__name__)
            await asyncio.sleep(PUBLISH)
        LOG.info("Config changed: exiting, for systemd to start the waker again")
        for task in tasks:
            task.cancel()


def run_fleets(path: Path, config: dict, entries: list[tuple[BotConfig, str]], store: Store):
    """Starter mode (module doc): no AWS SDK and no AWS credential in this process."""
    from zab.waker.service import Target, Waker
    from zab.waker.starter_client import StarterClient

    fleets = config["fleets"]
    client = StarterClient(config["starter"], {i: f.name for f in fleets for _, i in f.entries})
    written = path.stat().st_mtime  # a bot's opt-in, at the latest: its catch-up (#403)

    def maker(fleet: Fleet) -> Callable[[], Waker]:
        def make() -> Waker:  # new Zulip clients each time: a finished run closed the last
            targets = [
                Target(bot, i, ZulipClient.from_zuliprc(bot.zuliprc)) for bot, i in fleet.entries
            ]
            key = f"{STATES}{fleet.name}"
            return Waker(
                targets, client, store, fleet.organizers, fleet.name, key, opted_in=written
            )

        return make

    makers = [(fleet.name, maker(fleet)) for fleet in fleets]
    asyncio.run(serve(makers, path, config, entries, store))


def wait_for(path: Path) -> None:
    """`run --wait-for-config` (the admin server's unit): a fresh server has no waker config
    until the first `!waker on`, so wait for one rather than fail every few seconds."""
    if os.path.lexists(path):
        return
    LOG.info("No config at %s yet: waiting for push_waker.py --config-dir", path)
    while not os.path.lexists(path):
        time.sleep(WAIT)


def selfcheck() -> int:
    """`waker selfcheck`, which deploy.sh runs in a new release before the switch (#348): every
    module of the waker's runtime closure, those its functions import lazily included
    (`Waker.notify`'s poster, starter mode's client), from the release's tree and venv. The
    deploy tests hold this list equal to the static closure of `python -m zab.waker`."""
    import zab.config
    import zab.core.addressing
    import zab.core.metrics
    import zab.core.store
    import zab.core.types
    import zab.core.watchdog
    import zab.render.code
    import zab.waker.__main__
    import zab.waker.queues
    import zab.waker.service
    import zab.waker.starter_client
    import zab.zulip.budget
    import zab.zulip.client
    import zab.zulip.poster

    loaded = [zab.config, zab.core.metrics, zab.core.store, zab.core.types, zab.core.watchdog]
    loaded += [zab.core.addressing, zab.render.code]  # typed mentions (#683, #713)
    loaded += [zab.waker.__main__, zab.waker.queues, zab.waker.service, zab.waker.starter_client]
    loaded += [zab.zulip.budget, zab.zulip.client, zab.zulip.poster]
    print(f"waker selfcheck: OK, {len(loaded)} modules")
    return 0


def setup_logging() -> None:
    """The waker's own messages at INFO. httpx logs every Zulip long poll at INFO (about 70
    journal lines a minute for 18 VMs with two bots each), so it and httpcore log warnings only;
    the bridge's logging is its own."""
    logging.basicConfig(level=logging.INFO)
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="waker")
    parser.add_argument("command", choices=["run", "status", "selfcheck"])
    parser.add_argument("--config", type=Path, default=Path("/etc/zab-waker/config.toml"))
    parser.add_argument(
        "--wait-for-config", action="store_true", help="run: wait for a missing config"
    )
    args = parser.parse_args(argv)
    if args.command == "selfcheck":
        return selfcheck()
    if args.wait_for_config and args.command == "run":
        setup_logging()
        wait_for(args.config)
    config, entries = load(args.config)
    directory = Path(config.get("state_dir", "/var/lib/zab-waker"))
    store = Store(directory)
    try:
        if args.command == "status":
            print(json.dumps(report(config, entries, store), indent=2))
            return 0
        # The catch-up's state of bots, and starter mode's snapshots of workshops, no longer
        # opted in (#403); with no workshop left too, since the waker then idles.
        workshops = None
        if "starter" in config:
            workshops = {f"{key}{f.name}" for f in config["fleets"] for key in (STATES, READY)}
        forget(store, {bot.name for bot, _ in entries}, workshops)
        if "starter" in config:
            setup_logging()
            lock = BotLock(directory, "central-waker")
            try:
                run_fleets(args.config, config, entries, store)
            finally:
                lock.close()
            return 0
        # Import lazily: the SDK is not a dependency of zab on participant VMs.
        import boto3
        from botocore.config import Config

        from zab.waker.service import Target, Waker

        setup_logging()
        regions = {
            row["instance_id"]: row.get("region", config.get("region", "us-west-2"))
            for row in config["bots"]
        }
        ec2 = Regions(
            regions,
            {
                region: boto3.client(
                    "ec2",
                    region_name=region,
                    config=Config(
                        connect_timeout=10, read_timeout=20, retries={"total_max_attempts": 1}
                    ),
                )
                for region in set(regions.values())
            },
        )
        lock = BotLock(directory, "central-waker")
        try:
            targets = [
                Target(bot, instance, ZulipClient.from_zuliprc(bot.zuliprc))
                for bot, instance in entries
            ]
            organizers, workshop = config["organizers_channel"], config["workshop"]
            written = args.config.stat().st_mtime  # a bot's opt-in, at the latest (#403)
            asyncio.run(Waker(targets, ec2, store, organizers, workshop, opted_in=written).run())
        finally:
            lock.close()
            ec2.close()
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
