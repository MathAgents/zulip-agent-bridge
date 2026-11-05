import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from zab.commands import doctor
from zab.config import BackendConfig, BotConfig, BridgeConfig, Config, RenderConfig
from zab.core.types import Check
from zab.testing.fake_zulip import FakeZulip
from zab.zulip.client import ZulipClient

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.integration


def test_config_failure_is_machine_readable(tmp_path, capsys):
    assert doctor.main(["--config", str(tmp_path / "absent"), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)[0]["ok"] is False


@pytest.mark.parametrize("kind", ["codex", "claude"])
async def test_doctor_checks_permissions_and_exact_allowlist(tmp_path, monkeypatch, kind):
    rc = tmp_path / "bot.zuliprc"
    rc.write_text("decoy")
    rc.chmod(0o600)
    fake = FakeZulip()
    client = ZulipClient("https://zulip.example", "bot", "decoy", transport=fake.transport)
    monkeypatch.setattr(doctor.ZulipClient, "from_zuliprc", lambda _: client)
    config = Config(
        tmp_path / "config.toml",
        BridgeConfig(),
        [BotConfig("bot", str(rc), "codex", [123])],
        {"codex": BackendConfig(kind=kind)},
        [],
        RenderConfig(),
    )
    ready = asyncio.Event()
    ready.set()
    backend = SimpleNamespace(
        ready=ready,
        start=AsyncMock(),
        close=AsyncMock(),
        doctor=AsyncMock(return_value=[Check(name="codex:login", ok=True)]),
    )
    monkeypatch.setattr(doctor, "make_backend", lambda name, settings: backend)
    results = await doctor.checks(config)
    by_name = {c.name: c for c in results}
    assert by_name["bot:permissions"].ok
    assert by_name["bot:identity"].ok
    assert by_name["bot:allowlist"].detail == "123"
    assert ("GET", "users/123", {}) in fake.requests
    assert not any(r[1] == "users/2" for r in fake.requests)
    assert by_name["codex:login"].ok
    backend.doctor.assert_awaited_once()
    assert backend.start.await_count == (1 if kind == "codex" else 0)
    backend.close.assert_awaited_once()


async def allowlist_of(tmp_path, monkeypatch, bot_config, members=(), owner=2):
    """The doctor's `bot:allowlist` check against a fake realm with `members`, and the paths
    the doctor asked for."""
    rc = tmp_path / "bot.zuliprc"
    rc.write_text("decoy")
    rc.chmod(0o600)
    fake = FakeZulip()
    fake.members.extend(members)
    fake.user["bot_owner_id"] = owner
    client = ZulipClient("https://zulip.example", "bot", "decoy", transport=fake.transport)
    monkeypatch.setattr(doctor.ZulipClient, "from_zuliprc", lambda _: client)
    bot = BotConfig("bot", str(rc), "codex", *bot_config)
    config = Config(tmp_path / "c.toml", BridgeConfig(), [bot], {}, [], RenderConfig())
    check = {c.name: c for c in await doctor.checks(config)}["bot:allowlist"]
    return check, [r[1] for r in fake.requests]


def member(uid, email, **kw):
    return {"user_id": uid, "email": email, "is_bot": False, "is_active": True, **kw}


async def test_an_e_mail_nobody_has_yet_is_pending(tmp_path, monkeypatch):
    """#401: a workshop bot whose participant hasn't joined Zulip has the roster e-mail and no
    user ID. The doctor resolves it as the bridge does, finds nobody, and fails with a detail
    the admin scripts read as pending on an unbound row; the bot owner (2) is replaced."""
    check, asked = await allowlist_of(
        tmp_path, monkeypatch, ([], ["owner@example.org"]), [member(30, "else@example.org")]
    )
    assert not check.ok and check.detail.startswith(doctor.ALLOWLIST_PENDING)
    assert "hidden from members" in check.detail
    assert "users" in asked and not [p for p in asked if p.startswith("users/") and p != "users/me"]


@pytest.mark.parametrize(
    "seen",
    [
        member(20, "Owner@Example.org"),  # the bridge's casefold
        member(20, "user20@zulip.example", delivery_email="owner@example.org"),
    ],
)
async def test_an_e_mail_the_bot_can_see_passes_as_the_bridge_admits_it(
    tmp_path, monkeypatch, seen
):
    """#401 (reviewer 6205530228 on #402): the check passes exactly when the bridge admits
    someone, so a bound row's VM rolled out before the binding passes when the bot can read the
    address, and fails (above) when it is hidden from members."""
    check, asked = await allowlist_of(tmp_path, monkeypatch, ([], ["owner@example.org"]), [seen])
    assert check.ok and check.detail == "20" and "users/20" in asked


@pytest.mark.parametrize(
    "hidden",
    [
        member(20, "user20@zulip.example", delivery_email=None),  # hidden from members
        member(20, "owner@example.org", is_active=False),
        member(20, "owner@example.org", is_bot=True),
    ],
)
async def test_an_e_mail_the_bridge_would_not_admit_fails(tmp_path, monkeypatch, hidden):
    check, _ = await allowlist_of(tmp_path, monkeypatch, ([], ["owner@example.org"]), [hidden])
    assert not check.ok and check.detail.startswith(doctor.ALLOWLIST_PENDING)


async def test_nobody_allowed_through_the_checks(tmp_path, monkeypatch):
    """No lists and no bot owner: the hard failure, through `checks()` (the reviewer's mutant:
    the call site passing `True` for the e-mails)."""
    check, asked = await allowlist_of(tmp_path, monkeypatch, ([], []), owner=None)
    assert not check.ok and check.detail.startswith("nobody allowed: ")
    assert "users" not in asked  # no e-mail to resolve
    check, _ = await allowlist_of(tmp_path, monkeypatch, ([123], []))
    assert check.ok and check.detail == "123"


def test_the_allowlist_check_tells_waiting_from_nobody():
    """#401: IDs pass as before; e-mails only is pending (`ALLOWLIST_PENDING`); nothing at all,
    no owner either, is a hard failure with its own detail."""
    ok = doctor.allowlist_check("codex", frozenset({7, 3}), emails=True)
    assert ok == Check(name="codex:allowlist", ok=True, detail="3, 7")
    waiting = doctor.allowlist_check("codex", frozenset(), emails=True)
    assert not waiting.ok and waiting.detail == (
        "no allowed user yet: no Zulip user with the allowed e-mail visible to this bot "
        "(not joined yet, or the address is hidden from members)"
    )
    nobody = doctor.allowlist_check("claude", frozenset(), emails=False)
    assert nobody.name == "claude:allowlist" and not nobody.ok
    assert nobody.detail == "nobody allowed: no allow_user_ids, no allow_emails and no bot owner"
    assert not nobody.detail.startswith(doctor.ALLOWLIST_PENDING)


def test_warning_does_not_fail_m0_command(monkeypatch, capsys):
    async def checks(config):
        return [Check(name="partial", ok=True, warning=True)]

    monkeypatch.setattr(doctor, "load_config", lambda _: SimpleNamespace())
    monkeypatch.setattr(doctor, "checks", checks)
    assert doctor.main([]) == 0
    assert "WARN" in capsys.readouterr().out


async def test_malformed_credentials_never_expose_contents(tmp_path):
    rc = tmp_path / "malformed.zuliprc"
    rc.write_text("key=DECOY_SECRET\n")
    config = Config(
        tmp_path / "config.toml",
        BridgeConfig(),
        [BotConfig("bot", str(rc), "codex")],
        {},
        [],
        RenderConfig(),
    )
    results = await doctor.checks(config)
    serialized = json.dumps([c.detail for c in results])
    assert "DECOY_SECRET" not in serialized
    assert any(not c.ok and c.detail == "MissingSectionHeaderError" for c in results)


async def test_spawn_diagnostic_never_starts_when_bot_lock_held(tmp_path, monkeypatch):
    from zab.core.store import BotLock

    config = Config(
        tmp_path / "config.toml",
        BridgeConfig(state_dir=str(tmp_path)),
        [BotConfig("bot", str(tmp_path / "missing"), "codex")],
        {"codex": BackendConfig(kind="codex", connect="spawn")},
        [],
        RenderConfig(),
    )
    backend = SimpleNamespace(start=AsyncMock(), close=AsyncMock())
    monkeypatch.setattr(doctor, "make_backend", lambda *args: backend)
    lock = BotLock(tmp_path, "bot")
    try:
        results = await doctor.checks(config)
        assert any(c.warning and "unavailable" in c.detail for c in results)
        backend.start.assert_not_awaited()
    finally:
        lock.close()


@pytest.mark.parametrize("logged_in", [True, False])
async def test_doctor_uses_real_claude_adapter_and_hides_identity(tmp_path, monkeypatch, logged_in):
    binary = ROOT / "src/zab/testing/fake_claude.py"
    await asyncio.to_thread((tmp_path / "account").mkdir)
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_LOGGED_IN", "1" if logged_in else "0")
    config = Config(
        tmp_path / "config.toml",
        BridgeConfig(state_dir=str(tmp_path / "state")),
        [],
        {
            "claude": BackendConfig(
                kind="claude", binary=str(binary), accounts={"main": str(tmp_path / "account")}
            )
        },
        [],
        RenderConfig(),
    )
    checks = {c.name: c for c in await doctor.checks(config)}
    assert checks["claude version"].ok
    assert checks["claude account main"].ok == logged_in
    details = " ".join(c.detail for c in checks.values())
    assert "decoy-account" not in details and "Decoy Org" not in details
    assert "00000000-0000-4000-8000-0000000decoy" not in details
    if logged_in:
        assert "claude.ai" in details and "max" in details


@pytest.mark.parametrize(
    "used,warning", [(84, False), (84.01, True), (84.9, True), (85, True), (100, True)]
)
def test_disk_threshold_and_cli_warning(monkeypatch, capsys, used, warning):
    monkeypatch.setattr(
        doctor.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(
            total=100 * 1024**3, used=used * 1024**3, free=(100 - used) * 1024**3
        ),
    )
    check = doctor.disk_check(set())
    assert check.name == "disk" and check.ok and check.warning == warning
    assert f"{used:.1f}% used" in check.detail
    assert f"{100 - used:.1f} GiB free" in check.detail
    monkeypatch.setattr(doctor, "load_config", lambda _: None)
    monkeypatch.setattr(doctor, "checks", AsyncMock(return_value=[check]))
    assert doctor.main(["--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["warning"] == warning


def test_disk_covers_separate_project_mount_and_deduplicates(monkeypatch):
    monkeypatch.setattr(
        Path, "stat", lambda path: SimpleNamespace(st_dev=1 if str(path) == "/" else 2)
    )
    seen = []

    def usage(path):
        seen.append(path)
        return SimpleNamespace(total=100, used=40 if path == "/" else 90, free=10)

    monkeypatch.setattr(doctor.shutil, "disk_usage", usage)
    result = doctor.disk_check({"/", "/projects/a", "/projects/b"})
    assert seen == ["/", "/projects/a"]
    assert result.warning
    assert "/projects/a: 90.0% used" in result.detail


def test_disk_probe_failure_is_nonfatal_warning(monkeypatch):
    def denied(path):
        raise PermissionError("denied")

    monkeypatch.setattr(doctor.shutil, "disk_usage", denied)
    result = doctor.disk_check(set())
    assert result.ok and result.warning
    assert "unavailable (PermissionError)" in result.detail


def test_disk_reserved_blocks_count_as_full_like_df(monkeypatch):
    monkeypatch.setattr(
        doctor.shutil, "disk_usage", lambda _: SimpleNamespace(total=100, used=81, free=14)
    )
    result = doctor.disk_check(set())
    assert result.ok and result.warning
    assert "85.3% used" in result.detail


@pytest.mark.parametrize("value,warning", [(b"0\n", False), (b"1\n", True), (b"", True)])
async def test_service_reports_automatic_restarts_without_reset(monkeypatch, value, warning):
    monkeypatch.setattr(doctor.shutil, "which", lambda _: "/usr/bin/systemctl")
    process = SimpleNamespace(communicate=AsyncMock(return_value=(value, None)), returncode=0)
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(doctor.asyncio, "create_subprocess_exec", spawn)
    result = await doctor.service_restart_check()
    assert result.ok and result.warning == warning
    assert spawn.call_args.args == (
        "/usr/bin/systemctl",
        "--user",
        "show",
        "zab.service",
        "--property=NRestarts",
        "--value",
    )


async def test_the_sentinel_is_a_warning_for_each_bot_never_a_failure(tmp_path, monkeypatch):
    """#588: `<bot>:sentinel`, from the unit's file and state, then the bot's mark as the
    bridge reads it; none where no `zab.service` is installed (no sentinel runs there)."""
    import os

    from zab.tools import sentinel

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    units, state = tmp_path / "config" / "systemd" / "user", tmp_path / "state"
    state.mkdir()
    bots = [BotConfig("a", "rc", "codex", [1]), BotConfig("b", "rc", "claude", [1])]
    bridge = BridgeConfig(state_dir=str(state))
    config = Config(tmp_path / "c.toml", bridge, bots, {}, [], RenderConfig())
    asked, answer = [], ["active"]

    async def run(argv, seconds):
        asked.append(argv)
        assert seconds == 10  # as `service_restart_check`
        return 0, answer[0]

    monkeypatch.setattr(sentinel, "run_command", run)

    async def seen():
        found = await doctor.sentinel_checks(config, {"a": 100, "b": 200})
        assert all(c.ok for c in found)  # a warning at most
        return [(c.name, c.warning, c.detail) for c in found]

    assert await seen() == [] and not asked
    units.mkdir(parents=True)
    (units / "zab.service").write_text("unit")
    why = "; DM !restart is unavailable, and the bridge answers DM !vm itself"
    missing = "not installed: no zab-sentinel.service, which `zab service install` writes" + why
    assert await seen() == [("a:sentinel", True, missing), ("b:sentinel", True, missing)]
    assert not asked  # no unit file: systemd isn't asked
    (units / "zab-sentinel.service").write_text("unit")
    answer[0] = "failed"
    assert (await seen())[0] == ("a:sentinel", True, "down: zab-sentinel.service is failed" + why)
    assert asked == [["systemctl", "--user", "is-active", "zab-sentinel.service"]]
    answer[0] = ""  # no answer: the user's systemd out of reach (no XDG_RUNTIME_DIR)
    unknown = (await seen())[0]
    assert unknown[:2] == ("a:sentinel", True) and "DM !restart" not in unknown[2]
    assert unknown[2].startswith("unknown: can't reach the user's systemd: ")
    answer[0] = "active"
    mark = {"pid": os.getpid(), "boot": sentinel.boot_id(), "registered": 1.0, "allowed": {}}
    sentinel.mark_path(state, "a").write_text(json.dumps(mark | {"bot_id": 100}))
    sentinel.mark_path(state, "b").write_text(json.dumps(mark | {"bot_id": 999}))
    a, b = await seen()
    assert a[:2] == ("a:sentinel", False)
    assert a[2].startswith("zab-sentinel.service active, its mark is from this boot, ")
    assert b == ("b:sentinel", True, "stale: its mark is another bot's" + why)
