"""The waker on the admin server (docs/ADMIN.md §1.2; PR 21): the admin layout, several
workshops, wakes through the real starter on a real Unix socket, the status file, and no AWS
SDK. Direct mode's tests are test_service.py and test_config.py, unchanged."""

import ast
import asyncio
import importlib.util
import json
import logging
import os
import socket
import stat
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

from zab.admin import starter
from zab.admin.config import parse_admin_config
from zab.config import BotConfig
from zab.core.store import Store
from zab.testing.fake_zulip import FakeZulip
from zab.waker import __main__ as cli
from zab.waker import service
from zab.waker.service import Target, Waker
from zab.waker.starter_client import StarterClient
from zab.zulip.client import ZulipClient

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "admin_starter_fakes", ROOT / "tests/admin/test_admin_starter.py"
)
fakes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fakes)
A, B, G = fakes.A, fakes.B, "i-0ccccccccccccccc3"
ZULIPRC = "[api]\nemail=bot@example.test\nkey=DECOY-KEY\nsite=https://zulip.example\n"


@pytest.fixture(autouse=True)
def short_grace(monkeypatch):
    monkeypatch.setattr(service, "GRACE", 0.2)


@pytest.fixture
def admin_ids(monkeypatch):
    """This test's user stands for zab-admin and its group for zab-waker."""
    monkeypatch.setattr(cli.pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=os.getuid()))
    monkeypatch.setattr(cli.grp, "getgrnam", lambda name: SimpleNamespace(gr_gid=os.getgid()))


def write(path: Path, text: str, mode: int = 0o640) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)
    return path


def admin_config(home: Path, *, status: Path | None = None, extra: str = "") -> Path:
    """The layout push_waker.py --config-dir writes: two workshops, one VM each."""
    rows = []
    for name, channel, slug, instance in (("alpha", 42, "alice", A), ("beta", 43, "bob", B)):
        rows.append(f'[[workshop]]\nname = "{name}"\norganizers_channel = {channel}\n')
        for kind in ("codex", "claude"):
            key = write(home / name / slug / f"{kind}.zuliprc", ZULIPRC)
            rows.append(
                f'[[workshop.bots]]\nname = "{kind}-{slug}"\ninstance_id = "{instance}"\n'
                f'zuliprc = "{key}"\nallow_user_ids = [20]\nallow_emails = []\n'
            )
    head = f'starter = "/run/zab-admin-starter/start.sock"\nstate_dir = "{home / "state"}"\n'
    if status:
        head += f'status_file = "{status}"\n'
    return write(home / "config.toml", head + extra + "\n".join(rows))


@pytest.mark.parametrize(
    "mode,uid,gid,ok",
    [
        (0o100640, 1, 2, True),
        (0o100640, 0, 2, True),  # root may write it too
        (0o100600, 1, 2, False),
        (0o100644, 1, 2, False),
        (0o100660, 1, 2, False),
        (0o120640, 1, 2, False),  # a symlink
        (0o100640, 3, 2, False),  # another owner
        (0o100640, 1, 3, False),  # another group
    ],
)
def test_the_admin_layouts_file_rule(monkeypatch, mode, uid, gid, ok):
    monkeypatch.setattr(cli.pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=1))
    monkeypatch.setattr(cli.grp, "getgrnam", lambda name: SimpleNamespace(gr_gid=2))
    monkeypatch.setattr(
        Path, "lstat", lambda _: SimpleNamespace(st_mode=mode, st_uid=uid, st_gid=gid)
    )
    if ok:
        cli.shared_file(Path("decoy"))
    else:
        with pytest.raises(ValueError, match="zab-admin or root, group zab-waker, mode 0640"):
            cli.shared_file(Path("decoy"))


def test_without_the_admin_users_only_direct_mode_loads(tmp_path, monkeypatch):
    def missing(name):
        raise KeyError(name)

    monkeypatch.setattr(cli.pwd, "getpwnam", missing)
    config = admin_config(tmp_path)
    with pytest.raises(ValueError, match=r"root-owned regular.*no user zab-admin"):
        cli.load(config)


def test_the_admin_layout_loads_every_workshop(tmp_path, admin_ids):
    config, entries = cli.load(admin_config(tmp_path))
    assert [
        (f.name, f.organizers, [(b.name, i) for b, i in f.entries]) for f in config["fleets"]
    ] == [
        ("alpha", 42, [("codex-alice", A), ("claude-alice", A)]),
        ("beta", 43, [("codex-bob", B), ("claude-bob", B)]),
    ]
    assert [b.name for b, _ in entries] == [
        "codex-alice",
        "claude-alice",
        "codex-bob",
        "claude-bob",
    ]
    assert entries[0][0].allow_user_ids == [20]


def test_the_starter_key_and_the_layout_go_together(tmp_path, admin_ids, monkeypatch):
    """A root 0600 config naming a starter, or an admin-layout one without, is refused."""
    path = admin_config(tmp_path)
    path.write_text(path.read_text().replace('starter = "/run/zab-admin-starter/start.sock"', ""))
    with pytest.raises(ValueError, match="`starter` goes with"):
        cli.load(path)
    monkeypatch.setattr(cli, "private_file", lambda _: None)  # as if root's, 0600
    with pytest.raises(ValueError, match="`starter` goes with"):
        cli.load(admin_config(tmp_path))


@pytest.mark.parametrize(
    "edit,error",
    [
        (("state_dir =", "colour = 1\nstate_dir ="), "unknown key colour"),
        (('starter = "/run', 'starter = "run'), "starter must be an absolute path"),
        (('name = "alpha"', 'name = "Alpha"'), "its name in admin.toml"),
        (('name = "beta"', 'name = "alpha"'), "alpha appears twice"),
        (("organizers_channel = 42", "organizers_channel = 0"), "positive channel"),
        (("organizers_channel = 42", "organizers_channel = 42\nregion = 'x'"), "a \\[\\[workshop"),
        (("allow_emails = []", "allow_emails = []\nregion = 'us-east-1'"), "a bot has"),
        (("/alpha/alice/codex.zuliprc", "/alpha/../../codex.zuliprc"), "must lie under"),
        (('instance_id = "' + B, 'instance_id = "' + A), "is in two workshops"),
        (("allow_user_ids = [20]", 'allow_user_ids = ["20"]'), "positive integers"),
        (('name = "codex-bob"', 'name = "codex-alice"'), "unique"),
    ],
    ids=lambda v: v if isinstance(v, str) else v[1][:24],
)
def test_the_admin_config_is_strict(tmp_path, admin_ids, edit, error):
    path = admin_config(tmp_path)
    old, new = edit
    assert old in path.read_text()
    path.write_text(path.read_text().replace(old, new, 1))
    with pytest.raises(ValueError, match=error):
        cli.load(path)


def test_a_zuliprc_outside_the_layout_is_refused(tmp_path, admin_ids):
    path = admin_config(tmp_path / "waker")
    (tmp_path / "waker" / "alpha" / "alice" / "codex.zuliprc").chmod(0o644)
    with pytest.raises(ValueError, match=r"codex\.zuliprc: expected a regular file"):
        cli.load(path)
    elsewhere = write(tmp_path / "elsewhere.zuliprc", ZULIPRC)
    path.write_text(
        path.read_text().replace(str(tmp_path / "waker/alpha/alice/codex.zuliprc"), str(elsewhere))
    )
    with pytest.raises(ValueError, match="must lie under"):
        cli.load(path)
    text = path.read_text()
    start = text.index("organizers_channel = 42\n") + len("organizers_channel = 42\n")
    path.write_text(text[:start] + text[text.index("[[workshop]]", start) :])
    with pytest.raises(ValueError, match="alpha: configure at least one"):
        cli.load(path)


# ------------------------------------------------- wakes through the starter, two workshops


@pytest.fixture
async def rig(tmp_path):
    """The real starter on a real socket, with alpha's VM in us-east-1 (scope workshop=alpha and
    zab-role=test) and beta's in us-west-2; a waker per workshop sharing one store and one
    client, as `run_fleets` builds them."""
    east, west = fakes.FakeEc2(fakes.vm(A)), fakes.FakeEc2(fakes.vm(B, "beta", "us-west-2b"))
    clock = [1000.0]
    with tempfile.TemporaryDirectory(prefix="zs-", dir="/tmp") as directory:
        path = Path(directory) / "start.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path))
        listener.listen()
        ec2 = {"us-east-1": starter.Ec2(east), "us-west-2": starter.Ec2(west)}
        config = parse_admin_config(fakes.CONFIG)
        (tmp_path / "starter" / starter.LOG_DIR).mkdir(parents=True)  # tmpfiles.conf's
        service_ = starter.Starter(
            config, ec2, tmp_path / "starter", {os.getuid()}, lambda: clock[0]
        )
        task = asyncio.create_task(service_.serve(listener))
        fake = FakeZulip()
        fake.members += [
            {"user_id": 20, "full_name": "Owner", "email": "o@example.test", "is_bot": False}
        ]
        client = ZulipClient("https://zulip.example", "bot", "decoy", transport=fake.transport)
        store = Store(tmp_path / "waker")
        sc = StarterClient(str(path), {A: "alpha", B: "beta"}, timeout=5)
        fleets, wakers = [], {}
        for name, channel, slug, instance in (("alpha", 42, "alice", A), ("beta", 43, "bob", B)):
            target = Target(BotConfig(name=f"codex-{slug}", allow_user_ids=[20]), instance, client)
            fleets.append(cli.Fleet(name, channel, [(target.bot, instance)]))
            wakers[name] = Waker(
                [target], sc, store, channel, name, states_key=f"waker_states:{name}"
            )
            await wakers[name].refresh(force=True)
        yield SimpleNamespace(
            east=east, west=west, clock=clock, fake=fake, store=store, wakers=wakers,
            config={"starter": str(path), "fleets": fleets}, starter=service_, path=path,
        )  # fmt: skip
        for waker in wakers.values():
            await asyncio.gather(*waker.tasks.values())
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await client.close()
        store.close()


async def wake(waker: Waker, message: dict, flags=("mentioned",)) -> None:
    await waker.message(waker.targets[0], message, list(flags))
    await asyncio.gather(*waker.tasks.values())


async def test_each_workshop_wakes_its_own_vm_through_the_starter(rig, caplog):
    caplog.set_level(logging.DEBUG)
    alpha, beta = rig.wakers["alpha"], rig.wakers["beta"]
    assert (alpha.states, beta.states) == ({A: "stopped"}, {B: "stopped"})
    message = rig.fake.post_as(20, "DECOY wake me", "wake")
    await wake(alpha, message)
    assert rig.east.starts() == [[A]] and not rig.west.starts()
    assert rig.fake.messages[message["id"]]["reactions"][0]["emoji_code"] == "23f0"
    dm = rig.fake.post_as(20, "DECOY !status")
    await wake(beta, dm, ())
    assert rig.west.starts() == [[B]]
    assert any(m["content"] == service.STARTING for m in rig.fake.messages.values())
    for waker in (alpha, beta):  # the next poll's snapshot, which the report shows
        await waker.refresh(force=True)
    report = cli.report(rig.config, [], rig.store)
    assert [
        (w["name"], [(b["bot"], b["state"]) for b in w["bots"]]) for w in report["workshops"]
    ] == [
        ("alpha", [("codex-alice", "pending")]),
        ("beta", [("codex-bob", "pending")]),
    ]
    assert all(w["bots"][0]["last_wake_attempt"] for w in report["workshops"])
    kept = caplog.text + "\n".join(rig.store.db.iterdump())
    assert "DECOY" not in kept and "DECOY" not in json.dumps(rig.starter.starts)


async def test_a_running_vm_is_left_alone(rig):
    rig.east.vms[0]["State"]["Name"] = "running"
    await rig.wakers["alpha"].refresh(force=True)
    rig.east.calls.clear()
    await wake(rig.wakers["alpha"], rig.fake.post_as(20, "hi", "wake"))
    assert not rig.east.calls  # not even a describe: the cached state said running


async def test_a_vm_the_starter_does_not_vouch_for_is_missing(rig, caplog):
    """Its participant tag removed: the starter leaves it out, the waker logs it once and never
    asks to start it."""
    rig.east.vms[0]["Tags"] = [t for t in rig.east.vms[0]["Tags"] if t["Key"] != "participant"]
    await rig.wakers["alpha"].refresh(force=True)
    await rig.wakers["alpha"].refresh(force=True)
    assert rig.wakers["alpha"].missing == {A} and caplog.text.count(A) == 1
    await wake(rig.wakers["alpha"], rig.fake.post_as(20, "hi", "wake"))
    assert not rig.east.starts()


async def test_a_starter_refusal_is_a_failed_wake(rig, caplog):
    rig.starter.starts[A] = rig.clock[0]  # rate-limited
    message = rig.fake.post_as(20, "hi", "wake")
    await wake(rig.wakers["alpha"], message)
    assert "Wake failed (StarterError)" in caplog.text
    assert not rig.east.starts() and not rig.fake.messages[message["id"]].get("reactions")


async def test_capacity_failures_retry_through_the_starter(rig, monkeypatch):
    """InsufficientInstanceCapacity three times: the waker's retries after 60 and 120 s pass the
    starter's own limit, then the owner and alpha's organizers channel are told."""
    error = Exception()
    error.response = {"Error": {"Code": "InsufficientInstanceCapacity"}}
    rig.east.error = error
    monkeypatch.setattr(
        service, "time", SimpleNamespace(time=lambda: rig.clock[0], monotonic=lambda: rig.clock[0])
    )
    sleep = asyncio.sleep

    async def advance(delay):
        rig.clock[0] += delay
        await sleep(0)

    monkeypatch.setattr(service.asyncio, "sleep", advance)
    await wake(rig.wakers["alpha"], rig.fake.post_as(20, "hi", "wake"))
    assert rig.east.starts() == [[A]] * 3
    notices = [
        m for m in rig.fake.messages.values() if "InsufficientInstanceCapacity" in m["content"]
    ]
    assert [(m["stream_id"], m["subject"]) for m in notices] == [(42, "VM wake failures")]
    assert any("Could not start your VM" in m["content"] for m in rig.fake.messages.values())


async def test_the_queue_source_is_the_wakers_only_input(rig):
    """ADMIN §11.4: replacing `BotQueues` changes where messages come from, nothing else."""
    alpha = rig.wakers["alpha"]
    message = rig.fake.post_as(20, "hi", "wake")

    class Source:
        async def run(self, deliver):
            await deliver(alpha.targets[0], message, ["mentioned"])
            await asyncio.gather(*alpha.tasks.values())
            raise LookupError("delivered")  # ends run(): the TaskGroup cancels the poll

    alpha.source = Source()
    with pytest.raises(ExceptionGroup) as ended:
        await alpha.run()
    assert ended.group_contains(LookupError, match="delivered")
    assert rig.east.starts() == [[A]]


# ------------------------------------------------- one workshop's failure stays its own


async def test_a_workshop_the_starter_does_not_know_leaves_the_others_running(
    rig, monkeypatch, caplog, tmp_path
):
    """Review on #343: gamma is not in admin.toml (retired, or not loaded yet). Its waker logs
    once and retries with a backoff, not in a hot loop; alpha's runs and wakes its VM, and the
    status file shows gamma never checked."""
    monkeypatch.setattr(cli, "PUBLISH", 0.01)
    monkeypatch.setattr(cli, "RETRY", 0.01)
    monkeypatch.setattr(cli, "RETRY_MAX", 0.04)
    rig.fake.poll_timeout = 5.0  # alpha's real queue long-polls, as Zulip does
    client = StarterClient(str(rig.path), {A: "alpha", G: "gamma"}, timeout=5)
    asked, request = [], client.request

    def counting(payload):
        asked.append((payload["workshop"], time.monotonic()))
        return request(payload)

    monkeypatch.setattr(client, "request", counting)
    built, zulips = {}, []

    def maker(name, instance):
        def make():
            zulip = ZulipClient("https://zulip.example", "b", "d", transport=rig.fake.transport)
            zulips.append((name, zulip))
            bot = BotConfig(name=f"codex-{name}", allow_user_ids=[20])
            key = f"waker_states:{name}"
            built[name] = Waker([Target(bot, instance, zulip)], client, rig.store, 42, name, key)
            return built[name]

        return make

    status = tmp_path / "waker.json"
    fleets = [
        cli.Fleet(name, 42, [(BotConfig(name=f"codex-{name}"), instance)])
        for name, instance in (("alpha", A), ("gamma", G))
    ]
    config = {"starter": str(rig.path), "status_file": str(status), "fleets": fleets}
    path = tmp_path / "config.toml"
    path.write_text("unchanged")
    makers = [("alpha", maker("alpha", A)), ("gamma", maker("gamma", G))]
    task = asyncio.create_task(cli.serve(makers, path, config, [], rig.store))
    for _ in range(500):
        if sum(w == "gamma" for w, _ in asked) >= 10 and built.get("alpha") and status.exists():
            break
        await asyncio.sleep(0.01)
    message = rig.fake.post_as(20, "hi", "wake")
    await wake(built["alpha"], message)
    assert rig.east.starts() == [[A]] and not task.done()
    report = json.loads(status.read_text())
    assert [(w["name"], w["checked_at"] is None) for w in report["workshops"]] == [
        ("alpha", False),
        ("gamma", True),
    ]
    assert caplog.text.count("Workshop gamma: no waker (unknown-workshop)") == 1
    gamma = [t for w, t in asked if w == "gamma"]
    assert len(gamma) <= (gamma[-1] - gamma[0]) / 0.04 + 3  # backed off to RETRY_MAX
    closed = [z.http.is_closed for name, z in zulips if name == "gamma"]
    assert all(closed[:-1])  # each failed waker's Zulip clients closed before the next
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_a_failing_waker_is_rebuilt_and_never_stops_another(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(cli, "RETRY", 0.01)
    monkeypatch.setattr(cli, "RETRY_MAX", 0.02)
    builds, running = [], asyncio.Event()

    class Broken:
        targets: ClassVar[list] = []

        def __init__(self):
            builds.append(self)

        async def refresh(self, force=False):
            pass

        async def run(self):
            raise RuntimeError("down")

    class Healthy(Broken):
        async def run(self):
            running.set()
            await asyncio.Event().wait()

    path = tmp_path / "config.toml"
    path.write_text("unchanged")
    task = asyncio.create_task(cli.serve([("b", Broken), ("h", Healthy)], path, {}, [], None))
    for _ in range(300):
        if sum(type(b) is Broken for b in builds) >= 4:
            break
        await asyncio.sleep(0.01)
    assert running.is_set() and not task.done()
    assert sum(type(b) is Healthy for b in builds) == 1  # never rebuilt: it never failed
    assert caplog.text.count("Workshop b: no waker (RuntimeError)") == 1
    assert caplog.text.count("Workshop h:") == 0  # healthy: nothing to say
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def test_ready_at_moves_only_once_a_waker_has_run_a_minute(tmp_path, monkeypatch):
    """#717: a workshop's `ready_at` is the first start's until its waker has run `UP_AFTER`,
    then moves with each write; a waker killed at every start (Nov 1) never moves it, and the
    store keeps it across restarts. `forget` drops those of workshops opted out."""
    clock = [1000.0]
    monkeypatch.setattr(cli, "time", SimpleNamespace(time=lambda: clock[0]))
    store, names = Store(tmp_path), ["alpha", "beta"]
    cli.ready(store, names, {})  # the first start: no ready_at yet
    assert [store.get(f"{service.READY}{n}") for n in names] == [1000.0, 1000.0]
    clock[0] = 1030.0
    cli.ready(store, names, {"alpha": 1000.0})  # alpha has run 30 s
    assert store.get(f"{service.READY}alpha") == 1000.0
    clock[0] = 1000.0 + cli.UP_AFTER
    cli.ready(store, names, {"alpha": 1000.0})
    assert store.get(f"{service.READY}alpha") == clock[0]
    clock[0] = 5000.0  # beta killed at every start since: each start writes, none moves it
    for _ in range(3):
        cli.ready(store, names, {"beta": clock[0]})
        clock[0] += 15
    assert store.get(f"{service.READY}beta") == 1000.0
    store.set(f"{service.READY}alpha", "damaged")  # damaged: a fresh start's
    cli.ready(store, ["alpha"], {})
    assert store.get(f"{service.READY}alpha") == clock[0]
    fleets = [cli.Fleet(n, 42, []) for n in names]
    report = cli.report({"starter": "s", "fleets": fleets}, [], store)
    assert [(w["name"], w["ready_at"]) for w in report["workshops"]] == [
        ("alpha", clock[0]),
        ("beta", 1000.0),
    ]
    service.forget(store, set(), {f"{k}alpha" for k in (service.STATES, service.READY)})
    assert store.get(f"{service.READY}alpha") == clock[0]
    assert store.get(f"{service.READY}beta") is None
    store.close()


async def test_the_status_file_shows_a_waker_that_never_stays_up(tmp_path, monkeypatch):
    """#717: the status file is written at once and every `PUBLISH`, a failing waker's too, but
    only the waker that runs moves its workshop's `ready_at`."""
    monkeypatch.setattr(cli, "PUBLISH", 0.01)
    monkeypatch.setattr(cli, "RETRY", 0.01)
    monkeypatch.setattr(cli, "RETRY_MAX", 0.02)
    monkeypatch.setattr(cli, "UP_AFTER", 0.05)

    class Broken:
        targets: ClassVar[list] = []

        async def refresh(self, force=False):
            pass

        async def run(self):
            await asyncio.sleep(0.01)
            raise MemoryError  # killed before it has run `UP_AFTER`

    class Healthy(Broken):
        async def run(self):
            await asyncio.Event().wait()

    store, status = Store(tmp_path / "state"), tmp_path / "waker.json"
    fleets = [cli.Fleet(n, 42, []) for n in ("b", "h")]
    config = {"starter": "s", "status_file": str(status), "fleets": fleets}
    path = tmp_path / "config.toml"
    path.write_text("unchanged")
    serve = cli.serve([("b", Broken), ("h", Healthy)], path, config, [], store)
    task = asyncio.create_task(serve)
    up: dict = {}
    for _ in range(300):
        await asyncio.sleep(0.01)
        report = json.loads(status.read_text()) if status.exists() else {"workshops": []}
        up = {w["name"]: w["ready_at"] for w in report["workshops"]}
        if up and up["h"] > up["b"] + 0.2:
            break
    assert up["h"] > up["b"] + 0.2 and report["written_at"] > up["b"] + 0.2 and not task.done()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    store.close()


@pytest.mark.parametrize("response", ["odd", None, {"Error": "odd"}, {"Error": {"Code": 7}}])
async def test_an_odd_error_never_escapes_the_supervisor(tmp_path, monkeypatch, caplog, response):
    """#348: an exception whose `response` is not botocore's shape is logged by its class, and
    the other workshop's waker runs on."""
    monkeypatch.setattr(cli, "RETRY", 0.01)
    monkeypatch.setattr(cli, "RETRY_MAX", 0.02)
    running, tries = asyncio.Event(), []

    class Odd(Exception):
        pass

    class Failing:
        targets: ClassVar[list] = []

        async def refresh(self, force=False):
            tries.append(1)
            error = Odd()
            error.response = response
            raise error

    class Healthy(Failing):
        async def refresh(self, force=False):
            pass

        async def run(self):
            running.set()
            await asyncio.Event().wait()

    path = tmp_path / "config.toml"
    path.write_text("unchanged")
    task = asyncio.create_task(cli.serve([("o", Failing), ("h", Healthy)], path, {}, [], None))
    for _ in range(300):
        if len(tries) >= 3 and running.is_set():
            break
        await asyncio.sleep(0.01)
    assert len(tries) >= 3 and running.is_set() and not task.done()
    assert caplog.text.count("Workshop o: no waker (Odd)") == 1
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert cli.error_code(Odd()) == "Odd"


def test_run_waits_for_a_missing_config(tmp_path, admin_ids, monkeypatch, caplog):
    """A fresh server: `--wait-for-config` (the admin server's unit) waits and logs once; without
    it a missing config fails as it always has."""
    caplog.set_level(logging.INFO)
    path, sleeps, ran = tmp_path / "waker" / "config.toml", [], []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            admin_config(tmp_path / "waker")  # push_waker.py --config-dir writes it

    monkeypatch.setattr(cli.time, "sleep", sleep)
    monkeypatch.setattr(cli, "run_fleets", lambda *args: ran.append(args))
    monkeypatch.setattr(cli, "setup_logging", lambda: None)
    assert cli.main(["run", "--wait-for-config", "--config", str(path)]) == 0
    assert sleeps == [cli.WAIT, cli.WAIT] and len(ran) == 1
    assert caplog.text.count("No config at") == 1
    with pytest.raises(FileNotFoundError):
        cli.main(["run", "--config", str(tmp_path / "missing.toml")])


# ------------------------------------------------- the process: no SDK, status file, restart


def test_starter_mode_runs_without_an_aws_sdk(tmp_path, admin_ids, monkeypatch):
    """boto3 and botocore can't even be imported: starter mode never tries."""
    for name in ("boto3", "botocore", "botocore.config"):
        monkeypatch.setitem(sys.modules, name, None)
    seen = []

    async def serve(fleets, path, config, entries, store):
        for name, make in fleets:
            waker = make()
            seen.append((name, waker.organizers, waker.states_key, type(waker.ec2).__name__))
            assert waker.tag["Value"] == name
            assert waker.opted_in == path.stat().st_mtime  # the catch-up's start (#403)
            for target in waker.targets:
                await target.client.close()

    monkeypatch.setattr(cli, "serve", serve)
    monkeypatch.setattr(cli, "setup_logging", lambda: None)
    path = admin_config(tmp_path)
    assert cli.main(["run", "--config", str(path)]) == 0
    assert seen == [
        ("alpha", 42, "waker_states:alpha", "StarterClient"),
        ("beta", 43, "waker_states:beta", "StarterClient"),
    ]
    with pytest.raises(ImportError):
        import boto3  # noqa: F401


@pytest.mark.parametrize("workshops", [True, False], ids=["two workshops", "none left"])
def test_the_start_forgets_what_is_no_longer_opted_in(tmp_path, admin_ids, monkeypatch, workshops):
    """#403, reviewer on #405: `waker run` drops the catch-up marks of the bots its config doesn't
    list and the snapshots of the workshops it doesn't, after the last opt-out too (the waker
    then idles); the rest of its store stays."""
    path = admin_config(tmp_path)
    if not workshops:
        path.write_text(path.read_text().split("[[workshop]]")[0])
    store = Store(tmp_path / "state")
    for name in ("codex-alice", "claude-bob", "codex-gone"):
        store.set(f"{service.MARK}{name}", 7)
    for name in ("alpha", "gamma"):
        store.set(f"{service.STATES}{name}", {"at": 1.0, "states": {}})
    store.set(f"wake:{A}", 5.0)
    store.close()

    async def serve(fleets, path, config, entries, store):
        pass

    monkeypatch.setattr(cli, "serve", serve)
    monkeypatch.setattr(cli, "setup_logging", lambda: None)
    assert cli.main(["run", "--config", str(path)]) == 0
    store = Store(tmp_path / "state")
    kept = {row["key"] for row in store.rows("kv")}
    store.close()
    listed = {"waker_mark:codex-alice", "waker_mark:claude-bob", "waker_states:alpha"}
    assert kept == {f"wake:{A}"} | (listed if workshops else set())


def test_the_report_follows_the_mode(tmp_path):
    """Direct mode accepts unknown keys: one named `fleets` must not change its report."""
    store = Store(tmp_path)
    entries = [(BotConfig(name="codex-a"), A)]
    assert set(cli.report({"fleets": "stray"}, entries, store)) == {"checked_at", "bots"}
    store.close()


def test_waker_selfcheck_needs_no_config(capsys):
    """deploy.sh's check of a new release (#348): no config, no state, no network."""
    assert cli.main(["selfcheck", "--config", "/nonexistent/config.toml"]) == 0
    assert capsys.readouterr().out == "waker selfcheck: OK, 14 modules\n"


def test_waker_status_by_workshop(tmp_path, admin_ids, capsys):
    path = admin_config(tmp_path)
    store = Store(tmp_path / "state")
    store.set("waker_states:beta", {"at": 5, "states": {B: "stopped"}})
    store.set(f"wake:{B}", 4)
    store.close()
    assert cli.main(["status", "--config", str(path)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert [w["name"] for w in report["workshops"]] == ["alpha", "beta"]
    assert report["workshops"][0]["checked_at"] is None
    assert report["workshops"][1]["bots"][1] == {
        "bot": "claude-bob",
        "instance": B,
        "state": "stopped",
        "last_wake_attempt": 4,
    }


async def test_the_status_file_and_the_exit_on_a_new_config(
    tmp_path, admin_ids, monkeypatch, caplog
):
    """The status file is 0640 and takes its setgid directory's group (zab-admin on the server);
    a new config ends the process for systemd to restart it, the wakers cancelled."""
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(cli, "PUBLISH", 0.01)
    status = tmp_path / "status"
    status.mkdir()
    other = [g for g in os.getgroups() if g != os.getegid()]
    if other:
        os.chown(status, -1, other[0])
    status.chmod(0o2750)
    path = admin_config(tmp_path, status=status / "waker.json")
    config, entries = cli.load(path)
    store = Store(tmp_path / "state")
    cancelled = asyncio.Event()

    class Idle:
        targets: ClassVar[list] = []

        async def refresh(self, force=False):
            pass

        async def run(self):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    fleets = [("alpha", Idle), ("beta", Idle)]
    task = asyncio.create_task(cli.serve(fleets, path, config, entries, store))
    for _ in range(200):
        if (status / "waker.json").exists():
            break
        await asyncio.sleep(0.01)
    info = (status / "waker.json").stat()
    assert stat.S_IMODE(info.st_mode) == 0o640 and info.st_gid == status.stat().st_gid
    assert [w["name"] for w in json.loads((status / "waker.json").read_text())["workshops"]] == [
        "alpha",
        "beta",
    ]
    assert [p.name for p in status.iterdir()] == ["waker.json"]  # no temporary left
    path.write_text(path.read_text() + "\n")
    await asyncio.wait_for(task, 2)
    assert cancelled.is_set() and "Config changed" in caplog.text
    config["status_file"] = str(tmp_path / "missing" / "waker.json")
    path.write_text(path.read_text() + "\n")
    task = asyncio.create_task(cli.serve(fleets[:1], path, config, entries, store))
    await asyncio.sleep(0.05)
    path.write_text(path.read_text() + "\n")
    await asyncio.wait_for(task, 2)
    assert "Status file not written (FileNotFoundError)" in caplog.text
    store.close()


def test_only_direct_mode_imports_the_aws_sdk():
    """Every import of boto3 or botocore in zab.waker is in `main`, after the starter-mode
    branch has returned."""
    found = []
    for path in sorted((ROOT / "src/zab/waker").glob("*.py")):
        tree = ast.parse(path.read_text())
        for function in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)] + [tree]:
            body = function.body if function is not tree else tree.body
            for node in body if function is tree else ast.walk(function):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                if any(n.split(".")[0] in ("boto3", "botocore") for n in names):
                    found.append((path.name, getattr(function, "name", "<module>"), node.lineno))
    assert sorted({(f, fn) for f, fn, _ in found}) == [("__main__.py", "main")]
    source = (ROOT / "src/zab/waker/__main__.py").read_text().splitlines()
    branch = next(i for i, line in enumerate(source, 1) if 'if "starter" in config:' in line)
    assert all(line > branch for _, _, line in found)


def test_the_wakers_import_closure_is_pinned():
    """ADMIN §1.6: once the waker runs on the admin server, its import closure joins the diff
    Drew reviews before he signs a release (and deploy.sh's release paths). A change here is
    deliberate."""
    spec = importlib.util.spec_from_file_location(
        "admin_imports", ROOT / "tests/admin/test_admin_imports.py"
    )
    imports = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(imports)
    modules, third, dynamic = imports.closure(ROOT / "src", "zab.waker")
    assert modules == {
        "zab", "zab.config", "zab.core", "zab.core.metrics", "zab.core.store", "zab.core.types",
        "zab.core.watchdog", "zab.waker", "zab.waker.__main__", "zab.waker.queues",
        "zab.waker.service", "zab.waker.starter_client", "zab.zulip", "zab.zulip.budget",
        "zab.zulip.client", "zab.zulip.poster",
        # #683: how a message addresses bots, the bridge's own reading, and its code protection
        "zab.core.addressing", "zab.render", "zab.render.code",
    }  # fmt: skip
    assert third == {"boto3", "botocore", "httpx"} and dynamic == []
