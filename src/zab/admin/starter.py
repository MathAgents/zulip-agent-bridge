"""zab-admin-starter: start a fleet VM for the waker, after checks (docs/ADMIN.md §1.2, §5).

The waker handles any Zulip user's messages, so on the admin server it holds no AWS credential
(#329). It asks this service, on `/run/zab-admin-starter/start.sock` (root:zab-waker 0660,
socket-activated), for its opted-in VMs' states and for starts. The starter runs as its own
user, holds the instance role and no key, and makes two EC2 calls only: DescribeInstances and
StartInstances. It lives in `zab.admin` because it reads `admin.toml` and holds the role: its
code is in the diff Drew reviews before signing a release (§1.6).

The protocol: one JSON object on one line per connection, one JSON line back.

    {"op": "describe", "workshop": W, "instances": [ID, ...]}
        -> {"ok": true, "states": {ID: STATE, ...}}     only W's fleet VMs; the rest left out
    {"op": "start", "workshop": W, "instance": ID}
        -> {"ok": true, "result": "started"}
        -> {"ok": false, "error": E, ...}, E one of: forbidden, bad-request, unknown-workshop,
           refused (not W's fleet VM), not-stopped (+ state), rate-limited (+ retry_after),
           unlogged (the start log could not be written), aws (+ code, such as
           InsufficientInstanceCapacity)

A VM is W's fleet VM when DescribeInstances, in W's region, returns it with every scope tag of
W (`workshop=W`, then `[global]`'s `zab-scope` and extra tags), a non-blank `participant` tag,
no `zab-admin` tag in any case, and an availability zone of W's region. Only root and
`zab-waker` are answered (SO_PEERCRED). A start needs the VM stopped, and at most one
StartInstances per VM every `INTERVAL` seconds, kept across restarts; DescribeInstances calls
draw from one token bucket. Requests carry no message text, and the starter logs instance IDs
and workshops only.

**The start log** (#359). Before each StartInstances, the starter appends one JSON line to
`log/starts.jsonl` in its state directory: the time, the workshop, the VM's `participant` tag, the
instance and the peer that asked (root or `zab-waker`). A start that can't be logged is not made
(`unlogged`), and a write that failed part way is cut back, so no fragment joins the next line.
`log/` is setgid `zab-admin`, 2750 (`tmpfiles.conf`), and each file is set 0640: the admin
bots' process reads it, checks every start against the participant's Zulip messages
(`zab.admin.wakes`), and nothing of the waker's can write it. Past `LOG_MAX` bytes the file is
renamed `starts.jsonl.1`, replacing the one before, so the log stays under twice that.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import pwd
import re
import socket
import struct
import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from zab.admin.config import AdminConfig, Workshop, load_admin_config

LOG = logging.getLogger("zab.admin.starter")
STATE = Path("/var/lib/zab-starter")
WAKER = "zab-waker"
INTERVAL = 30.0  # seconds between two StartInstances of one VM; the waker's own is 60
LIMIT = 64 * 1024  # bytes in one request
TIMEOUT = 10.0  # seconds to send it
MAX_INSTANCES = 500
CHUNK = 100  # instance IDs per DescribeInstances call
BURST, RATE = 10, 1.0  # DescribeInstances: a burst of calls, then this many a second
WATCH = 30.0  # seconds between checks of admin.toml
LOG_DIR, LOG_NAME = "log", "starts.jsonl"  # the start log, in the state directory (#359)
LOG_MAX = 1024 * 1024  # bytes in starts.jsonl before it is renamed starts.jsonl.1
BAD = {"ok": False, "error": "bad-request"}
KEYS = {"describe": {"op", "workshop", "instances"}, "start": {"op", "workshop", "instance"}}
_INSTANCE = re.compile(r"i-(?:[0-9a-f]{8}|[0-9a-f]{17})", re.ASCII)
_CODE = re.compile(r"[A-Za-z0-9.]{1,64}", re.ASCII)


class Ec2:
    """One region's EC2 client, as the starter uses it: DescribeInstances and StartInstances."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def describe(self, filters: list[dict]) -> list[dict]:
        instances: list[dict] = []
        page = self._client.describe_instances(Filters=filters)
        while True:
            instances += [i for r in page.get("Reservations", []) for i in r.get("Instances", [])]
            if not page.get("NextToken"):
                return instances
            page = self._client.describe_instances(Filters=filters, NextToken=page["NextToken"])

    def start(self, instance: str) -> None:
        self._client.start_instances(InstanceIds=[instance])


def fleet_vm(vm: dict, workshop: Workshop) -> bool:
    """Whether DescribeInstances' `vm` is one of `workshop`'s participant VMs (module doc)."""
    tags = {t.get("Key"): t.get("Value") for t in vm.get("Tags") or [] if isinstance(t, dict)}
    participant = tags.get("participant")
    zone = (vm.get("Placement") or {}).get("AvailabilityZone")
    return (
        all(tags.get(key) == value for key, value in workshop.tags)
        and isinstance(participant, str)
        and participant.strip() != ""
        and not any(isinstance(key, str) and key.lower() == "zab-admin" for key in tags)
        and isinstance(zone, str)
        and re.fullmatch(re.escape(workshop.aws.region) + "[a-z]", zone) is not None
        and isinstance((vm.get("State") or {}).get("Name"), str)
    )


def aws_code(exc: Exception) -> str:
    """botocore's error code (a ClientError's), else the exception's class, made safe."""
    response = getattr(exc, "response", None)
    error = response.get("Error") if isinstance(response, dict) else None  # any shape (#348)
    code = (error.get("Code") if isinstance(error, dict) else None) or type(exc).__name__
    return code if isinstance(code, str) and _CODE.fullmatch(code) else "error"


def peer_uid(sock: Any) -> int:
    _, uid, _ = struct.unpack(
        "3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    )
    return uid


class Bucket:
    """A token bucket: `burst` calls at once, then `rate` a second."""

    def __init__(self, burst: int, rate: float, clock: Callable[[], float] = time.monotonic):
        self.tokens, self.burst, self.rate, self.clock = float(burst), burst, rate, clock
        self.at = clock()

    def wait(self) -> float:
        """Take a token now; the seconds to wait before using it."""
        now = self.clock()
        self.tokens = min(self.burst, self.tokens + (now - self.at) * self.rate) - 1
        self.at = now
        return max(0.0, -self.tokens / self.rate)


class Starter:
    def __init__(
        self,
        config: AdminConfig,
        ec2: Mapping[str, Ec2],
        state: Path,
        allowed: set[int],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config, self.ec2, self.state, self.allowed = config, ec2, state, allowed
        self.clock = clock
        self.bucket = Bucket(BURST, RATE)
        self.starts = self.load()

    def load(self) -> dict[str, float]:
        """The last StartInstances per VM, from the last day."""
        try:
            data = json.loads((self.state / "starts.json").read_text())
            now = self.clock()
            return {
                k: float(v)
                for k, v in data.items()
                if isinstance(v, int | float) and abs(now - v) < 86400
            }
        except FileNotFoundError:
            return {}
        except (ValueError, AttributeError) as exc:
            LOG.warning("Ignoring a damaged starts.json (%s)", type(exc).__name__)
            return {}

    def save(self) -> None:
        fd, temporary = tempfile.mkstemp(dir=self.state, prefix=".starts-")
        try:
            with os.fdopen(fd, "w") as file:
                json.dump(self.starts, file)
            os.replace(temporary, self.state / "starts.json")
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise

    def log_start(self, workshop: Workshop, vm: dict, peer: int | None, now: float) -> None:
        """One line in the start log (module doc), written and synced before the start."""
        tags = {t.get("Key"): t.get("Value") for t in vm.get("Tags") or [] if isinstance(t, dict)}
        record = {"t": round(now, 3), "workshop": workshop.name,
                  "participant": tags.get("participant"), "instance": vm["InstanceId"],
                  "peer": peer_name(peer)}  # fmt: skip
        line = json.dumps(record, ensure_ascii=True, separators=(",", ":")).encode() + b"\n"
        path = self.state / LOG_DIR / LOG_NAME
        with contextlib.suppress(FileNotFoundError):
            if path.stat().st_size >= LOG_MAX:
                os.replace(path, path.with_name(LOG_NAME + ".1"))
        flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open(path, flags, 0o640)
        try:
            os.fchmod(fd, 0o640)  # UMask=0077 took the group's read; the directory gave the group
            size = os.fstat(fd).st_size
            if size and os.pread(fd, 1, size - 1) != b"\n":  # a fragment, should one be left
                line = b"\n" + line
            try:
                view = memoryview(line)
                while view:
                    view = view[os.write(fd, view) :]
                os.fsync(fd)
            except BaseException:  # no fragment may join the next line (review of #391)
                with contextlib.suppress(OSError):
                    os.ftruncate(fd, size)
                raise
        finally:
            os.close(fd)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            uid = peer_uid(writer.get_extra_info("socket"))
            if uid not in self.allowed:
                LOG.warning("Refused a connection: not root or %s", WAKER)
                reply: dict = {"ok": False, "error": "forbidden"}
            else:
                try:
                    async with asyncio.timeout(TIMEOUT):  # not wait_for (#491)
                        line = await reader.readline()
                    request = json.loads(line)
                except (ValueError, TimeoutError):  # too long, not JSON, or too slow
                    request = None
                reply = await self.answer(request, uid)
            writer.write(json.dumps(reply).encode() + b"\n")
            await writer.drain()
        except Exception as exc:
            LOG.warning("Request failed (%s)", type(exc).__name__)
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def answer(self, request: Any, peer: int | None = None) -> dict:
        op = request.get("op") if isinstance(request, dict) else None
        if not isinstance(op, str) or set(request) != KEYS.get(op, ()):
            return BAD
        name = request["workshop"]
        workshop = self.config.workshop(name) if isinstance(name, str) else None
        if workshop is None:
            return {"ok": False, "error": "unknown-workshop"}
        if op == "describe":
            ids = request["instances"]
            if (
                not isinstance(ids, list)
                or not 0 < len(ids) <= MAX_INSTANCES
                or not all(isinstance(i, str) and _INSTANCE.fullmatch(i) for i in ids)
                or len(set(ids)) != len(ids)
            ):
                return BAD
            vms = await self.describe(workshop, ids)
            return {"ok": True, "states": {i: vm["State"]["Name"] for i, vm in vms.items()}}
        instance = request["instance"]
        if not isinstance(instance, str) or not _INSTANCE.fullmatch(instance):
            return BAD
        return await self.start(workshop, instance, peer)

    async def describe(self, workshop: Workshop, ids: list[str]) -> dict[str, dict]:
        """`workshop`'s fleet VMs among `ids`, by ID."""
        ec2, wanted, found = self.ec2[workshop.aws.region], set(ids), {}
        scope = [{"Name": f"tag:{key}", "Values": [value]} for key, value in workshop.tags]
        for at in range(0, len(ids), CHUNK):
            await asyncio.sleep(self.bucket.wait())
            filters = [{"Name": "instance-id", "Values": ids[at : at + CHUNK]}, *scope]
            for vm in await asyncio.to_thread(ec2.describe, filters):
                if vm.get("InstanceId") in wanted and fleet_vm(vm, workshop):
                    found[vm["InstanceId"]] = vm
        return found

    async def start(self, workshop: Workshop, instance: str, peer: int | None = None) -> dict:
        vm = (await self.describe(workshop, [instance])).get(instance)
        if vm is None:
            LOG.warning("Refused to start %s: not a fleet VM of %s", instance, workshop.name)
            return {"ok": False, "error": "refused"}
        if (state := vm["State"]["Name"]) != "stopped":
            return {"ok": False, "error": "not-stopped", "state": state}
        # No awaits from the check to the record: one start per VM per INTERVAL.
        now, last = self.clock(), self.starts.get(instance)
        if last is not None and 0 <= now - last < INTERVAL:
            return {"ok": False, "error": "rate-limited", "retry_after": INTERVAL - (now - last)}
        try:
            self.log_start(workshop, vm, peer, now)
        except OSError as exc:  # no start without its record (#359)
            LOG.warning("Refused to start %s: the start log failed (%s)", instance, exc.strerror)
            return {"ok": False, "error": "unlogged"}
        self.starts[instance] = now
        self.save()
        try:
            await asyncio.to_thread(self.ec2[workshop.aws.region].start, instance)
        except Exception as exc:
            code = aws_code(exc)
            LOG.warning("StartInstances %s (%s) failed: %s", instance, workshop.name, code)
            return {"ok": False, "error": "aws", "code": code}
        LOG.info("Started %s (%s) for the waker", instance, workshop.name)
        return {"ok": True, "result": "started"}

    async def serve(self, sock: socket.socket, config: Path | None = None) -> None:
        """Answer on `sock` until `config` (admin.toml) changes: the starter then exits, and the
        socket unit starts it again, with the new workshops, at the next request."""
        seen = fingerprint(config)
        server = await asyncio.start_unix_server(self.handle, sock=sock, limit=LIMIT)
        async with server:
            while True:
                await asyncio.sleep(WATCH)
                if fingerprint(config) != seen:
                    break
        LOG.info("admin.toml changed: exiting until the next request")


def fingerprint(path: Path | None) -> tuple | None:
    if path is None:
        return None
    try:
        info = path.stat()
    except FileNotFoundError:
        return ()
    return info.st_ino, info.st_mtime_ns, info.st_size


def systemd_socket(env: Mapping[str, str], pid: int, fd: int = 3) -> socket.socket | None:
    """The listening socket systemd passes (`zab-admin-starter.socket`, `Accept=no`)."""
    if env.get("LISTEN_PID") != str(pid) or env.get("LISTEN_FDS") != "1":
        return None
    sock = socket.socket(fileno=fd)
    if sock.family != socket.AF_UNIX or sock.type != socket.SOCK_STREAM:
        raise ValueError("systemd passed a socket that is not a Unix stream socket")
    return sock


def peer_name(uid: int | None) -> str | None:
    """The start log's `peer`: a user name, else the UID as a string; None when unknown."""
    if uid is None:
        return None
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def peers() -> set[int]:
    try:
        return {0, pwd.getpwnam(WAKER).pw_uid}
    except KeyError:
        return {0}


def sdk() -> tuple[Any, Any]:
    """boto3 and botocore's Config: the admin group's (§1.6), imported only here."""
    import boto3
    from botocore.config import Config

    return boto3, Config


def main(config: Path, state: Path | None = None, listen: Path | None = None) -> int:
    logging.basicConfig(level=logging.INFO)
    for name in ("botocore", "boto3", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)
    admin = load_admin_config(config)
    sock = systemd_socket(os.environ, os.getpid())
    if sock is None:
        if listen is None:
            raise SystemExit("no socket from systemd (zab-admin-starter.socket) and no --socket")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(listen))
        sock.listen()
    for name in ("LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES"):
        os.environ.pop(name, None)
    boto3, Config = sdk()
    settings = Config(connect_timeout=10, read_timeout=20, retries={"total_max_attempts": 1})
    ec2 = {
        region: Ec2(boto3.client("ec2", region_name=region, config=settings))
        for region in sorted({ws.aws.region for ws in admin.workshops})
    }
    directory = state or Path(os.environ.get("STATE_DIRECTORY", str(STATE)).split(":")[0])
    asyncio.run(Starter(admin, ec2, directory, peers()).serve(sock, config))
    return 0
