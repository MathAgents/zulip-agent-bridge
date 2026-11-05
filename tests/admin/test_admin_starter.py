"""zab-admin-starter (docs/ADMIN.md §1.2; PR 21): a real Unix socket in a temporary directory, a
fake EC2 per region, and the waker's own client. Each guard has a test that fails without it
(the PR records the mutation run)."""

import ast
import asyncio
import copy
import json
import os
import select
import socket
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from zab.admin import __main__ as cli
from zab.admin import starter
from zab.admin.config import parse_admin_config
from zab.waker import starter_client
from zab.waker.starter_client import StarterClient, StarterError

pytestmark = pytest.mark.integration

CONFIG = """
provider = "aws"
admins = [101, 102]
deployers = [101]
[global]
scope = "mathagents"
regions = ["us-east-1", "us-west-2"]
instance_types = ["m7a.2xlarge"]
tags = ["zab-role=test"]
[[workshop]]
name = "alpha"
github_org = "MathAgents"
refserve_url = "https://files.example.org"
roster = "/var/lib/zab-admin/ws/alpha/roster.csv"
out_dir = "/var/lib/zab-admin/ws/alpha/out"
[workshop.bot]
zuliprc = "/etc/zab-admin/secrets/bots/alpha.zuliprc"
user_id = 901
channels = [42]
[workshop.aws]
region = "us-east-1"
ami = "ami-0123456789abcdef0"
security_group = "sg-0123456789abcdef0"
instance_types = ["m7a.2xlarge"]
key_name = "zab-admin-alpha"
admin_keys = ["/etc/zab-admin/break-glass/drew.pub"]
[[workshop]]
name = "beta"
github_org = "MathAgents"
refserve_url = "https://files.example.org"
roster = "/var/lib/zab-admin/ws/beta/roster.csv"
out_dir = "/var/lib/zab-admin/ws/beta/out"
[workshop.bot]
zuliprc = "/etc/zab-admin/secrets/bots/beta.zuliprc"
user_id = 902
channels = [43]
[workshop.aws]
region = "us-west-2"
ami = "ami-0123456789abcdef1"
security_group = "sg-0123456789abcdef1"
instance_types = ["m7a.2xlarge"]
key_name = "zab-admin-beta"
admin_keys = ["/etc/zab-admin/break-glass/drew.pub"]
"""
A, B = "i-0aaaaaaaaaaaaaaa1", "i-0bbbbbbbbbbbbbbb2"


def vm(instance, workshop="alpha", zone="us-east-1a", state="stopped", **tags):
    """A fleet VM as DescribeInstances returns it; `tags` add to, or with None remove, its own."""
    own = {"workshop": workshop, "participant": "alice", "Name": "ws-alice"}
    own |= {"zab-scope": "mathagents", "zab-role": "test"}  # the fleet's scope (#422)
    own.update(tags)
    return {
        "InstanceId": instance,
        "State": {"Name": state},
        "Placement": {"AvailabilityZone": zone},
        "Tags": [{"Key": k, "Value": v} for k, v in own.items() if v is not None],
    }


class FakeEc2:
    """boto3's EC2 client, with only the two methods the starter may call. It filters as EC2
    does (instance-id, and tag:KEY unless `tag_filters` is off, so that the starter's own check
    is what refuses) and pages `page` instances at a time."""

    def __init__(self, *vms, page=100):
        self.vms, self.page, self.tag_filters = list(vms), page, True
        self.calls: list[tuple[str, dict]] = []
        self.error: Exception | None = None

    def describe_instances(self, *, Filters, NextToken=None):
        self.calls.append(("describe_instances", {"Filters": Filters, "NextToken": NextToken}))
        found = [
            v
            for v in self.vms
            if all(
                (v["InstanceId"] if f["Name"] == "instance-id" else
                 {t["Key"]: t["Value"] for t in v["Tags"]}.get(f["Name"][4:])) in f["Values"]
                for f in Filters
                if self.tag_filters or f["Name"] == "instance-id"
            )
        ]  # fmt: skip
        at = int(NextToken or 0)
        page = {"Reservations": [{"Instances": copy.deepcopy(found[at : at + self.page])}]}
        if at + self.page < len(found):
            page["NextToken"] = str(at + self.page)
        return page

    def start_instances(self, *, InstanceIds):
        self.calls.append(("start_instances", {"InstanceIds": InstanceIds}))
        if self.error:
            raise self.error
        for v in self.vms:
            if v["InstanceId"] in InstanceIds:
                v["State"]["Name"] = "pending"
        return {}

    def starts(self):
        return [kw["InstanceIds"] for name, kw in self.calls if name == "start_instances"]


@pytest.fixture
def sockdir():
    """AF_UNIX paths are short: a directory of our own under /tmp."""
    with tempfile.TemporaryDirectory(prefix="zs-", dir="/tmp") as directory:
        yield Path(directory)


@pytest.fixture
async def rig(sockdir, tmp_path):
    """The starter on a real socket, with a fake EC2 per region and a settable clock."""
    east, west = FakeEc2(vm(A)), FakeEc2(vm(B, "beta", "us-west-2b"))
    clock = [1000.0]
    path = sockdir / "start.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen()
    config = parse_admin_config(CONFIG)
    (tmp_path / starter.LOG_DIR).mkdir()  # tmpfiles.conf's, on the server
    ec2 = {"us-east-1": starter.Ec2(east), "us-west-2": starter.Ec2(west)}
    service = starter.Starter(config, ec2, tmp_path, {os.getuid()}, lambda: clock[0])
    task = asyncio.create_task(service.serve(listener))
    client = StarterClient(str(path), {A: "alpha", B: "beta"}, timeout=5)
    yield SimpleNamespace(
        starter=service, east=east, west=west, clock=clock, path=path, client=client, state=tmp_path
    )
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def ask(rig, payload) -> dict:
    """One raw request, as any peer could send it."""
    try:
        return await asyncio.to_thread(rig.client.request, payload)
    except StarterError as exc:
        return {"ok": False, "error": str(exc), "code": exc.response["Error"]["Code"]}


async def raw(rig, data: bytes) -> bytes:
    def send() -> bytes:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(5)
            sock.connect(str(rig.path))
            sock.sendall(data)
            sock.shutdown(socket.SHUT_WR)
            return sock.makefile("rb").read()

    return await asyncio.to_thread(send)


async def test_describe_answers_with_the_workshops_fleet_vms_only(rig):
    """The waker's call shape; the starter filters on every scope tag, in the workshop's region."""
    stray = vm("i-0ccccccccccccccc3", participant=None)  # alpha's tags, but no participant
    rig.east.vms.append(stray)
    filters = [
        {"Name": "tag:workshop", "Values": ["alpha"]},
        {"Name": "instance-id", "Values": [A, stray["InstanceId"], B]},
    ]
    reply = await asyncio.to_thread(rig.client.describe_instances, Filters=filters)
    (reservation,) = reply["Reservations"]
    assert reservation["Instances"] == [
        {
            "InstanceId": A,
            "State": {"Name": "stopped"},
            "Tags": [{"Key": "workshop", "Value": "alpha"}],
        }
    ]
    sent = rig.east.calls[0][1]["Filters"]
    assert {"Name": "tag:zab-role", "Values": ["test"]} in sent
    assert {"Name": "tag:zab-scope", "Values": ["mathagents"]} in sent
    assert {"Name": "tag:workshop", "Values": ["alpha"]} in sent
    assert not rig.west.calls  # beta's region is not asked for alpha's VMs


async def test_a_stopped_fleet_vm_starts(rig):
    assert await asyncio.to_thread(rig.client.start_instances, InstanceIds=[A]) == {}
    assert rig.east.starts() == [[A]] and not rig.west.starts()
    assert json.loads((rig.state / "starts.json").read_text()) == {A: 1000.0}
    assert await asyncio.to_thread(rig.client.start_instances, InstanceIds=[B]) == {}
    assert rig.west.starts() == [[B]]
    lines = (rig.state / starter.LOG_DIR / starter.LOG_NAME).read_text().splitlines()
    me = starter.peer_name(os.getuid())  # the peer that asked: here this test, as the waker
    assert [json.loads(line) for line in lines] == [
        {"t": 1000.0, "workshop": w, "participant": "alice", "instance": i, "peer": me}
        for w, i in (("alpha", A), ("beta", B))
    ]


async def test_a_start_that_cant_be_logged_is_not_made(rig):
    """#359: every start the starter makes is in its start log, for the wake monitor."""
    (rig.state / starter.LOG_DIR).rmdir()
    reply = await ask(rig, {"op": "start", "workshop": "alpha", "instance": A})
    assert reply["error"] == "unlogged" and not rig.east.starts()
    assert "starts.json" not in os.listdir(rig.state)  # nor counted against the VM's limit
    (rig.state / starter.LOG_DIR).mkdir()
    assert (await ask(rig, {"op": "start", "workshop": "alpha", "instance": A}))["ok"] is True


def test_the_peer_is_named(monkeypatch):
    assert (starter.peer_name(0), starter.peer_name(None)) == ("root", None)

    def unknown(uid):
        raise KeyError(uid)

    monkeypatch.setattr(starter.pwd, "getpwuid", unknown)
    assert starter.peer_name(4321) == "4321"


@pytest.mark.parametrize(
    "change",
    [
        {"zab-role": None},  # a scope tag missing
        {"zab-role": "prod"},  # a scope tag with another value
        {"zab-scope": None},  # #422: a VM of v0.2.4, before the v0.2.5 policy change
        {"zab-scope": "other"},  # another server's fleet
        {"workshop": "beta"},  # another workshop's VM
        {"participant": None},
        {"participant": "  "},  # blank
        {"zab-admin": "server"},
        {"ZAB-Admin": "server"},  # IAM matches tag keys in any case
        {"zone": "us-west-2a"},  # another region's zone
        {"zone": "us-east-10a"},  # a region whose name extends this one's
        {"zone": None},
        {"state": None},  # an answer without a state
    ],
    ids=str,
)
async def test_a_vm_outside_the_fleet_is_refused(rig, change):
    """EC2 here ignores the tag filters the starter sends: its own check refuses."""
    change = dict(change)
    zone, state = change.pop("zone", "us-east-1a"), change.pop("state", "stopped")
    rig.east.vms[:] = [vm(A, zone=zone, **change)]
    rig.east.tag_filters = False
    if zone is None:
        del rig.east.vms[0]["Placement"]
    if state is None:
        del rig.east.vms[0]["State"]
    assert await ask(rig, {"op": "start", "workshop": "alpha", "instance": A}) == {
        "ok": False,
        "error": "refused",
        "code": "refused",
    }
    reply = await ask(rig, {"op": "describe", "workshop": "alpha", "instances": [A]})
    assert reply == {"ok": True, "states": {}}
    assert not rig.east.starts()


async def test_the_workshop_named_is_the_one_checked(rig):
    """beta's VM asked for as alpha's is not found in alpha's region and scope."""
    assert (await ask(rig, {"op": "start", "workshop": "alpha", "instance": B}))["error"] == (
        "refused"
    )
    assert (await ask(rig, {"op": "start", "workshop": "gamma", "instance": A}))["error"] == (
        "unknown-workshop"
    )
    assert not rig.east.starts() and not rig.west.starts()


@pytest.mark.parametrize("state", ["running", "pending", "stopping", "terminated"])
async def test_only_a_stopped_vm_starts(rig, state):
    rig.east.vms[0]["State"]["Name"] = state
    reply = await ask(rig, {"op": "start", "workshop": "alpha", "instance": A})
    assert (reply["error"], rig.east.starts()) == ("not-stopped", [])
    raw_reply = json.loads(
        await raw(
            rig, json.dumps({"op": "start", "workshop": "alpha", "instance": A}).encode() + b"\n"
        )
    )
    assert raw_reply == {"ok": False, "error": "not-stopped", "state": state}


async def test_starts_are_rate_limited_per_vm_across_restarts(rig):
    start = {"op": "start", "workshop": "alpha", "instance": A}
    assert (await ask(rig, start))["ok"] is True
    rig.east.vms[0]["State"]["Name"] = "stopped"  # it failed to start, say
    rig.clock[0] += starter.INTERVAL - 1
    limited = json.loads(await raw(rig, json.dumps(start).encode() + b"\n"))
    assert limited == {"ok": False, "error": "rate-limited", "retry_after": 1.0}
    restarted = starter.Starter(
        rig.starter.config, rig.starter.ec2, rig.state, {0}, lambda: rig.clock[0]
    )
    assert restarted.starts == {A: 1000.0}
    assert (await restarted.answer(start))["error"] == "rate-limited"
    assert (await ask(rig, {**start, "instance": A}))["error"] == "rate-limited"
    assert (await ask(rig, {"op": "start", "workshop": "beta", "instance": B}))["ok"] is True
    rig.clock[0] += 1  # INTERVAL after the first start
    assert (await ask(rig, start))["ok"] is True
    assert rig.east.starts() == [[A], [A]]
    rig.east.vms[0]["State"]["Name"] = "stopped"
    rig.clock[0] -= 3600  # the clock went back: a future record is no limit
    assert (await ask(rig, start))["ok"] is True


async def test_concurrent_starts_of_one_vm_call_start_once(rig, monkeypatch):
    """Five requests all see the VM stopped: one StartInstances, four refusals."""
    barrier, describe = threading.Barrier(5, timeout=5), rig.east.describe_instances

    def together(**kwargs):
        page = describe(**kwargs)  # every request's view, before any start
        barrier.wait()
        return page

    monkeypatch.setattr(rig.east, "describe_instances", together)
    start = {"op": "start", "workshop": "alpha", "instance": A}
    replies = await asyncio.gather(*(rig.starter.answer(start) for _ in range(5)))
    assert sorted(r.get("error", "ok") for r in replies) == ["ok"] + ["rate-limited"] * 4
    assert rig.east.starts() == [[A]]


@pytest.mark.parametrize("order", ["closed-first", "sent-first"])
async def test_a_peer_neither_root_nor_the_waker_is_refused(rig, monkeypatch, order):
    """The starter answers a peer it refuses without reading its request, and closes. The client
    reports `forbidden` whether that close comes before it sends or after (#387), each order
    made certain rather than left to timing: `closed-first` sends only once the starter has
    closed (POLLRDHUP), `sent-first` lets the starter check the peer only once the request is
    sent."""
    rig.starter.allowed = {os.getuid() + 1}
    sent, failed, got = threading.Event(), [], []

    class Ordered(socket.socket):
        def sendall(self, data, *args):
            if order == "closed-first":
                hangup = select.poll()
                hangup.register(self, select.POLLRDHUP)
                assert hangup.poll(5000), "the starter never closed"
            try:
                return super().sendall(data, *args)
            except OSError as exc:
                failed.append(type(exc))
                raise
            finally:
                sent.set()

        def recv(self, *args):
            got.append(super().recv(*args))
            return got[-1]

    def peer_uid(sock):
        if order == "sent-first":
            assert sent.wait(5), "the client never sent"
        return checked(sock)

    checked = starter.peer_uid
    monkeypatch.setattr(starter, "peer_uid", peer_uid)
    names = SimpleNamespace(socket=Ordered, AF_UNIX=socket.AF_UNIX, SOCK_STREAM=socket.SOCK_STREAM)
    monkeypatch.setattr(starter_client, "socket", names)
    reply = await ask(rig, {"op": "start", "workshop": "alpha", "instance": A})
    assert reply == {"ok": False, "error": "forbidden", "code": "forbidden"}
    assert json.loads(b"".join(got)) == {"ok": False, "error": "forbidden"}
    assert failed == ([BrokenPipeError] if order == "closed-first" else [])
    assert not rig.east.calls


def test_a_reset_ends_the_answer(sockdir):
    """A starter that closes with the request unread and no whole answer: `no-reply`, as when
    it closes having read it, not the reset that follows what it wrote (#387)."""
    path = sockdir / "start.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(path))
        listener.listen()

        def serve() -> None:
            conn, _ = listener.accept()
            with conn:
                conn.recv(1, socket.MSG_PEEK)  # the request has come, and stays unread
                conn.sendall(b'{"ok": fal')

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            with pytest.raises(StarterError, match="no-reply"):
                StarterClient(str(path), {A: "alpha"}, timeout=5).request({"op": "describe"})
        finally:
            thread.join(5)


def test_the_peers_are_root_and_zab_waker(monkeypatch):
    monkeypatch.setattr(starter.pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=997))
    assert starter.peers() == {0, 997}

    def missing(name):
        raise KeyError(name)

    monkeypatch.setattr(starter.pwd, "getpwnam", missing)
    assert starter.peers() == {0}


@pytest.mark.parametrize(
    "payload",
    [
        b"not json\n",
        b"[]\n",
        b'{"op": [], "workshop": "alpha", "instance": "i-0aaaaaaaaaaaaaaa1"}\n',
        b'{"op": "stop", "workshop": "alpha", "instance": "i-0aaaaaaaaaaaaaaa1"}\n',
        b'{"op": "start", "workshop": "alpha"}\n',
        b'{"op": "start", "workshop": "alpha", "instance": "i-0aaaaaaaaaaaaaaa1", "x": 1}\n',
        b'{"op": "start", "workshop": "alpha", "instance": "i-0AAAAAAAAAAAAAAA1"}\n',
        b'{"op": "start", "workshop": "alpha", "instance": "i-0aaaa"}\n',
        b'{"op": "start", "workshop": "alpha", "instance": ["i-0aaaaaaaaaaaaaaa1"]}\n',
        b'{"op": "describe", "workshop": "alpha", "instances": []}\n',
        b'{"op": "describe", "workshop": "alpha", "instances": "i-0aaaaaaaaaaaaaaa1"}\n',
        b'{"op": "describe", "workshop": "alpha", "instances": ["i-0aaaaaaaaaaaaaaa1", "x"]}\n',
        b'{"op": "describe", "workshop": "alpha", "instances": '
        b'["i-0aaaaaaaaaaaaaaa1", "i-0aaaaaaaaaaaaaaa1"]}\n',
        b'{"op": "describe", "workshop": "alpha", "instance": "i-0aaaaaaaaaaaaaaa1"}\n',
    ],
)
async def test_malformed_requests_are_refused(rig, payload):
    assert json.loads(await raw(rig, payload)) == {"ok": False, "error": "bad-request"}
    assert not rig.east.calls


async def test_limits_on_size_count_and_time(rig, monkeypatch):
    many = [f"i-{n:017x}" for n in range(starter.MAX_INSTANCES + 1)]
    payload = {"op": "describe", "workshop": "alpha", "instances": many}
    assert (await ask(rig, payload))["error"] == "bad-request"
    start = json.dumps({"op": "start", "workshop": "alpha", "instance": A}).encode()
    padded = start[:-1] + b" " * starter.LIMIT + b"}\n"  # valid, but too long
    assert json.loads(await raw(rig, padded))["error"] == "bad-request"
    assert json.loads(await raw(rig, b'{"op": 1, "workshop": "alpha"}\n'))["error"] == "bad-request"
    assert (await ask(rig, {"op": "start", "workshop": 7, "instance": A}))["error"] == (
        "unknown-workshop"
    )
    monkeypatch.setattr(starter, "TIMEOUT", 0.05)

    def slow() -> bytes:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(5)
            sock.connect(str(rig.path))
            sock.sendall(b'{"op": "start"')  # and never the end of the line
            return sock.makefile("rb").read()

    assert json.loads(await asyncio.to_thread(slow))["error"] == "bad-request"
    assert not rig.east.calls


async def test_describe_pages_and_splits_large_requests(rig):
    rig.east.page = 7
    ids = [f"i-{n:017x}" for n in range(250)]
    rig.east.vms[:] = [vm(i) for i in ids]
    reply = await ask(rig, {"op": "describe", "workshop": "alpha", "instances": ids})
    assert reply["states"] == dict.fromkeys(ids, "stopped")
    chunks = [kw["Filters"][0]["Values"] for name, kw in rig.east.calls if kw["NextToken"] is None]
    assert [len(c) for c in chunks] == [100, 100, 50]
    assert sum(1 for _, kw in rig.east.calls if kw["NextToken"]) == 14 + 14 + 7


async def test_only_the_requested_vms_are_answered(rig, monkeypatch):
    """Should EC2 return more than the instance-id filter asked for, only those asked count."""
    other = vm("i-0ccccccccccccccc3")
    rig.east.vms.append(other)
    every = rig.east.vms

    def unfiltered(**kwargs):
        return {"Reservations": [{"Instances": copy.deepcopy(every)}]}

    monkeypatch.setattr(rig.east, "describe_instances", unfiltered)
    reply = await ask(rig, {"op": "describe", "workshop": "alpha", "instances": [A]})
    assert reply == {"ok": True, "states": {A: "stopped"}}


async def test_describe_waits_for_the_bucket(rig, monkeypatch):
    monkeypatch.setattr(rig.starter.bucket, "wait", lambda: 0.2)
    started = time.monotonic()
    assert (await ask(rig, {"op": "describe", "workshop": "alpha", "instances": [A]}))["ok"]
    assert time.monotonic() - started >= 0.2


async def test_aws_errors_return_their_code_for_the_wakers_retry(rig):
    """InsufficientInstanceCapacity reaches the waker as botocore's error shape, which
    `Waker.wake` retries on; an unexpected code is never echoed raw."""
    error = Exception()
    error.response = {"Error": {"Code": "InsufficientInstanceCapacity"}}
    rig.east.error = error
    with pytest.raises(StarterError) as caught:
        await asyncio.to_thread(rig.client.start_instances, InstanceIds=[A])
    assert caught.value.response == {"Error": {"Code": "InsufficientInstanceCapacity"}}
    rig.clock[0] += starter.INTERVAL
    rig.east.error = Exception()
    rig.east.error.response = {"Error": {"Code": "Bad code\n{}"}}
    assert (await ask(rig, {"op": "start", "workshop": "alpha", "instance": A}))["code"] == "error"
    rig.clock[0] += starter.INTERVAL
    rig.east.error = Exception()
    rig.east.error.response = "not botocore's shape"  # #348: any shape is answered
    assert (await ask(rig, {"op": "start", "workshop": "alpha", "instance": A}))["code"] == (
        "Exception"
    )
    rig.clock[0] += starter.INTERVAL
    rig.east.error = Exception()
    rig.east.error.response = {"Error": "not a dict"}
    assert (await ask(rig, {"op": "start", "workshop": "alpha", "instance": A}))["code"] == (
        "Exception"
    )
    rig.clock[0] += starter.INTERVAL
    rig.east.error = TimeoutError()
    assert (await ask(rig, {"op": "start", "workshop": "alpha", "instance": A}))["code"] == (
        "TimeoutError"
    )


def test_the_starter_makes_two_ec2_calls_only():
    """Statically: the EC2 client is used through `Ec2` alone, for these two calls."""
    tree = ast.parse(Path(starter.__file__).read_text())
    used = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "_client"
    }
    assert used == {"describe_instances", "start_instances"}
    clients = [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == "_client"]
    assert len(clients) == 4  # assigned once, and the three calls


def test_the_bucket_allows_a_burst_then_its_rate():
    clock = [0.0]
    bucket = starter.Bucket(3, 2.0, lambda: clock[0])
    assert [bucket.wait() for _ in range(5)] == [0.0, 0.0, 0.0, 0.5, 1.0]
    clock[0] = 10.0
    assert bucket.wait() == 0.0 and bucket.tokens == 2.0


def test_a_damaged_rate_limit_file_is_ignored(tmp_path):
    (tmp_path / "starts.json").write_text("[1, 2")
    config = parse_admin_config(CONFIG)
    assert starter.Starter(config, {}, tmp_path, {0}).starts == {}
    (tmp_path / "starts.json").write_text(json.dumps({A: 5.0, B: "x"}))
    assert starter.Starter(config, {}, tmp_path, {0}, lambda: 10.0).starts == {A: 5.0}
    assert starter.Starter(config, {}, tmp_path, {0}, lambda: 10.0 + 86400).starts == {}


def test_the_socket_comes_from_systemd(sockdir):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(sockdir / "s"))
        listener.listen()
        fd = listener.fileno()
        env = {"LISTEN_PID": "42", "LISTEN_FDS": "1"}
        found = starter.systemd_socket(env, 42, os.dup(fd))
        assert found.family == socket.AF_UNIX and found.type == socket.SOCK_STREAM
        found.close()
        assert starter.systemd_socket(env, 43, fd) is None  # another process's
        assert starter.systemd_socket({**env, "LISTEN_FDS": "2"}, 42, fd) is None
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as other:
        with pytest.raises(ValueError, match="Unix stream"):
            starter.systemd_socket(env, 42, os.dup(other.fileno()))


async def test_the_starter_exits_when_admin_toml_changes(sockdir, tmp_path, monkeypatch):
    monkeypatch.setattr(starter, "WATCH", 0.01)
    config = tmp_path / "admin.toml"
    config.write_text(CONFIG)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(sockdir / "s"))
    listener.listen()
    service = starter.Starter(parse_admin_config(CONFIG), {}, tmp_path, {0})
    task = asyncio.create_task(service.serve(listener, config))
    await asyncio.sleep(0.05)
    assert not task.done()
    config.write_text(CONFIG + "\n")
    await asyncio.wait_for(task, 2)


def test_main_wires_regions_socket_and_state(sockdir, tmp_path, monkeypatch):
    made, served = [], []

    class Boto3:
        @staticmethod
        def client(service, region_name, config):
            made.append((service, region_name, config))
            return FakeEc2()

    monkeypatch.setattr(starter, "sdk", lambda: (Boto3, lambda **kw: kw))

    async def serve(self, sock, config):
        served.append((self, sock.getsockname(), config))

    monkeypatch.setattr(starter.Starter, "serve", serve)
    monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path))
    config = tmp_path / "admin.toml"
    config.write_text(CONFIG)
    with pytest.raises(SystemExit, match="no socket"):
        starter.main(config)
    assert starter.main(config, listen=sockdir / "s") == 0
    assert [(s, r) for s, r, _ in made] == [("ec2", "us-east-1"), ("ec2", "us-west-2")]
    assert made[0][2] == {
        "connect_timeout": 10,
        "read_timeout": 20,
        "retries": {"total_max_attempts": 1},
    }
    ((service, name, path),) = served
    assert (name, path, service.state) == (str(sockdir / "s"), config, tmp_path)


def test_the_entry_point_and_the_selfcheck(tmp_path, monkeypatch, capsys):
    config = tmp_path / "admin.toml"
    config.write_text(CONFIG)
    monkeypatch.setattr(starter, "sdk", lambda: (None, None))
    assert cli.main(["selfcheck", "--config", str(config)]) == 0
    assert "selfcheck: OK" in capsys.readouterr().out
    config.write_text(CONFIG.replace('provider = "aws"', 'provider = "gcp"'))
    assert cli.main(["selfcheck", "--config", str(config)]) == 2
    assert cli.main(["starter", "--config", str(config)]) == 2
    calls = []
    monkeypatch.setattr(starter, "main", lambda *args: calls.append(args) or 0)
    assert cli.main(["starter", "--config", "c", "--socket", "s", "--state-dir", "d"]) == 0
    assert calls == [(Path("c"), Path("d"), Path("s"))]

    def no_sdk():
        raise ImportError("boto3")

    config.write_text(CONFIG)
    monkeypatch.setattr(starter, "sdk", no_sdk)
    with pytest.raises(ImportError):  # a venv without the admin group fails the selfcheck
        cli.main(["selfcheck", "--config", str(config)])
