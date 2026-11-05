from types import SimpleNamespace

import pytest

from zab.commands.status import read_status
from zab.core.store import BotLock, Store

pytestmark = pytest.mark.integration


def test_read_status_while_bridge_lock_is_held(tmp_path):
    config = SimpleNamespace(bridge=SimpleNamespace(state_dir=str(tmp_path)))
    assert read_status(config) == {"sessions": [], "running_turns": [], "last_activity": None}
    assert not (tmp_path / "zab.db").exists()
    store = Store(tmp_path)
    lock = BotLock(tmp_path, "bot")
    try:
        store.set("site:bot", "https://zulip.example")
        store.insert(
            "sessions",
            id="s",
            bot="bot",
            backend="codex",
            native_id="n",
            card_message_id=42,
            stream_id=1,
            topic="topic",
            workspace="/work",
            workspace_mode="shared",
            repo_root="/work",
            settings_json="{}",
            state="running",
            created_at=0,
            updated_at=0,
        )
        store.insert(
            "turns",
            session_id="s",
            native_turn_id="t",
            trigger_msg_ids="[]",
            state="running",
            started_at=5,
        )
        result = read_status(config)
        assert (
            result["sessions"][0]["topic_url"] == "https://zulip.example/#narrow/channel/1/near/42"
        )
        assert result["running_turns"][0]["native_turn_id"] == "t"
        assert result["last_activity"] == 5  # the turn's start (ADMIN.md §9 V7)
    finally:
        lock.close()
        store.close()
