import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from zab.config import BotConfig
from zab.core.store import Store
from zab.waker import __main__ as cli

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "mode,uid", [(0o100600, 0), (0o100644, 0), (0o100600, 1000), (0o120600, 0)]
)
def test_private_files_require_root_0600_regular(monkeypatch, mode, uid):
    monkeypatch.setattr(Path, "lstat", lambda _: SimpleNamespace(st_mode=mode, st_uid=uid))
    if mode == 0o100600 and uid == 0:
        cli.private_file(Path("decoy"))
    else:
        with pytest.raises(ValueError, match="root-owned regular"):
            cli.private_file(Path("decoy"))


def test_status_uses_saved_state_without_sdk_or_credentials(tmp_path, monkeypatch, capsys):
    config = {"state_dir": str(tmp_path)}
    bot = BotConfig(name="codex-test", zuliprc="never-read")
    monkeypatch.setattr(cli, "load", lambda _: (config, [(bot, "i-01234567")]))
    with_store = Store(tmp_path)
    with_store.set("waker_states", {"at": 123, "states": {"i-01234567": "stopped"}})
    with_store.set("wake:i-01234567", 120)
    with_store.close()
    assert cli.main(["status"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report == {
        "checked_at": 123,
        "bots": [
            {
                "bot": "codex-test",
                "instance": "i-01234567",
                "state": "stopped",
                "last_wake_attempt": 120,
            }
        ],
    }


@pytest.mark.parametrize("ids", ['["20"]', "[true]", "[0]"])
def test_config_refuses_invalid_owner_ids(tmp_path, monkeypatch, ids):
    monkeypatch.setattr(cli, "private_file", lambda _: None)
    config = tmp_path / "waker.toml"
    config.write_text(
        'organizers_channel = 1\n[[bots]]\nname = "test"\n'
        'instance_id = "i-01234567"\nzuliprc = "decoy"\nallow_user_ids = ' + ids
    )
    with pytest.raises(ValueError, match="positive integers"):
        cli.load(config)


@pytest.mark.parametrize("value,ok", [(None, "nt-agents-2026"), ('"zab-test"', "zab-test"),
                                      ('"a b"', None), ("1", None)])  # fmt: skip
def test_config_workshop_tag(tmp_path, monkeypatch, value, ok):
    monkeypatch.setattr(cli, "private_file", lambda _: None)
    config = tmp_path / "waker.toml"
    head = "organizers_channel = 1\n" + (f"workshop = {value}\n" if value else "")
    config.write_text(
        head + '[[bots]]\nname = "test"\ninstance_id = "i-01234567"\nzuliprc = "decoy"\n'
        "allow_user_ids = [20]\n"
    )
    if ok is None:
        with pytest.raises(ValueError, match="workshop"):
            cli.load(config)
    else:
        assert cli.load(config)[0]["workshop"] == ok


def test_regions_route_starts_and_follow_describe_pages():
    from unittest.mock import Mock

    west, east = Mock(), Mock()
    west.describe_instances.side_effect = [
        {"Reservations": ["west1"], "NextToken": "next"},
        {"Reservations": ["west2"]},
    ]
    east.describe_instances.return_value = {"Reservations": ["east"]}
    adapter = cli.Regions({"i-west": "west", "i-east": "east"}, {"west": west, "east": east})
    result = adapter.describe_instances(
        Filters=[{"Name": "instance-id", "Values": ["i-west", "i-east"]}]
    )
    assert result["Reservations"] == ["west1", "west2", "east"]
    assert west.describe_instances.call_args.kwargs["NextToken"] == "next"
    adapter.start_instances(InstanceIds=["i-east"])
    east.start_instances.assert_called_once_with(InstanceIds=["i-east"])
    west.start_instances.assert_not_called()
