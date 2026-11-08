import importlib.util
import pathlib
import sqlite3

import pytest

from zab.core.store import BotLock, Store

pytestmark = pytest.mark.contract


def add_session(store, **overrides):
    row = dict(
        id="s",
        bot="b",
        backend="c",
        native_id="native",
        card_message_id=1,
        stream_id=1,
        topic="topic",
        workspace="/workspace",
        workspace_mode="shared",
        repo_root="/workspace",
        settings_json="{}",
        state="idle",
        created_at=0,
        updated_at=0,
    )
    store.insert("sessions", **(row | overrides))


def test_state_survives_restart_and_claims_are_per_bot(tmp_path):
    store = Store(tmp_path)
    add_session(store)
    assert store.claim("b", 10, "s")
    assert not store.claim("b", 10, "s")
    assert store.claim("other-bot", 10)
    store.hold("s", 12, "sender", "second")
    store.hold("s", 11, "sender", "first")
    store.set("last_event_id:b", 34)
    store.close()
    store = Store(tmp_path)
    assert not store.claim("b", 10)
    assert [r["message_id"] for r in store.held("s")] == [11, 12]
    store.clear_held("s", [11])
    assert len(store.held("s")) == 1
    assert store.get("last_event_id:b") == 34
    assert store.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    store.close()


def test_binding_uniqueness_and_detach(tmp_path):
    store = Store(tmp_path)
    add_session(store)
    with pytest.raises(sqlite3.IntegrityError):
        add_session(store, id="other", card_message_id=2, topic="other")
    with pytest.raises(sqlite3.IntegrityError):
        add_session(store, id="other", card_message_id=2, native_id="different")
    store.update("sessions", {"id": "s"}, state="detached")
    add_session(store, id="other", card_message_id=2)
    store.close()


OLD_ROW = (
    "INSERT INTO sessions (id, bot, backend, native_id, card_message_id, stream_id, topic,"
    " workspace, workspace_mode, repo_root, settings_json, state, created_at, updated_at)"
    " VALUES (?, 'b', 'c', ?, ?, 1, ?, '/w', 'shared', '/w', '{}', 'idle', 0, 0)"
)


def test_parent_session_id_is_added_to_an_existing_database(tmp_path):
    """#202: a VM keeps its database across the rollout; the column is added in place, the
    rows kept, and the schema version unchanged, so an older zab still opens it (a rollback)."""
    from zab.core.store import _migration_1

    db = sqlite3.connect(tmp_path / "zab.db")
    _migration_1(db)  # the schema every release so far created
    db.execute(OLD_ROW, ("old", "n", 1, "t"))
    db.commit()
    db.close()
    store = Store(tmp_path)
    (old,) = store.rows("sessions")
    assert old["id"] == "old" and old["parent_session_id"] is None
    add_session(store, id="fork", card_message_id=2, topic="t ⑂ 1", native_id="f")
    store.update("sessions", {"id": "fork"}, parent_session_id="old")
    assert store.rows("sessions", parent_session_id="old")[0]["id"] == "fork"
    assert store.db.execute("PRAGMA user_version").fetchone()[0] == 1
    store.close()
    again = Store(tmp_path)  # opened again: nothing left to add
    assert again.rows("sessions", id="fork")[0]["parent_session_id"] == "old"
    again.close()
    # what an older zab does with it: write and read by column name, unaware of the column
    db = sqlite3.connect(tmp_path / "zab.db")
    db.execute(OLD_ROW, ("older", "o", 3, "u"))
    db.commit()
    assert db.execute("SELECT parent_session_id FROM sessions WHERE id='older'").fetchone() == (
        None,
    )
    db.close()


def test_two_stores_opening_an_old_database_add_the_column_once(tmp_path, monkeypatch):
    """Both past the unlocked check, then one migrates while the other waits for the write
    lock: the second sees the column under the lock and adds nothing (review of #572)."""
    from zab.core import store as module

    db = sqlite3.connect(tmp_path / "zab.db")
    module._migration_1(db)
    db.close()
    check = module._missing_columns
    raced = []

    def racing(connection):
        found = check(connection)
        if found and not raced:  # the first store, past its unlocked check: another one wins
            raced.append(True)
            other = sqlite3.connect(tmp_path / "zab.db")
            module._add_columns(other)
            other.close()
        return found

    monkeypatch.setattr(module, "_missing_columns", racing)
    store = Store(tmp_path)  # would fail with "duplicate column name" without the re-check
    assert raced
    columns = [r[1] for r in store.db.execute("PRAGMA table_info(sessions)")]
    assert columns.count("parent_session_id") == 1
    store.close()


def test_message_edits_is_added_to_a_database_of_the_last_schema(tmp_path):
    """#757: a database as 0.2.18 left it, its rows kept, gets the table of each message's
    edits in place, `user_version` still 1, so that an older zab still opens it (a rollback)."""
    from zab.core.store import _migration_1

    db = sqlite3.connect(tmp_path / "zab.db")
    _migration_1(db)
    db.execute("ALTER TABLE sessions ADD COLUMN parent_session_id TEXT")  # 0.2.18's schema
    db.execute(OLD_ROW, ("old", "n", 1, "t"))
    db.execute("INSERT INTO kv VALUES ('last_event_id:b', '34')")
    db.commit()
    db.close()
    store = Store(tmp_path)
    assert [r["id"] for r in store.rows("sessions")] == ["old"]
    assert store.get("last_event_id:b") == 34 and store.edit_counts("b") == []
    assert store.db.execute("PRAGMA user_version").fetchone()[0] == 1
    store.keep_edit_count("b", 10, 3, 1000.0)
    store.keep_edit_count("b", 11, 1, 900.0)
    store.keep_edit_count("b", 10, 4, 1100.0, final=True)
    store.keep_edit_count("other", 10, 1, 1000.0)
    store.close()
    again = Store(tmp_path)  # opened again: nothing left to add, nothing lost
    assert again.db.total_changes == 0
    least_recent_first = [(11, 1, 900.0, False), (10, 4, 1100.0, True)]
    assert again.edit_counts("b") == least_recent_first
    again.forget_edit_counts("b", (10, 12))
    assert again.edit_counts("b") == [(11, 1, 900.0, False)] and again.edit_counts("other")
    again.close()
    # what an older zab does with it: never names the table
    db = sqlite3.connect(tmp_path / "zab.db")
    db.execute(OLD_ROW, ("older", "o", 3, "u"))
    db.commit()
    assert db.execute("PRAGMA user_version").fetchone()[0] == 1
    db.close()


def store_757():
    """`zab.core.store` as #757 shipped it (PR #795), verbatim: what a rollback to it runs."""
    path = pathlib.Path(__file__).parents[1] / "fixtures" / "store" / "store_757.py"
    spec = importlib.util.spec_from_file_location("store_757", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_rollback_to_757_still_keeps_edit_counts(tmp_path):
    """Codex round 1 on #813: the final marks (#806) are a table of their own. #757's zab writes
    `message_edits` rows by position, four values: a fifth column there failed every edit-count
    write after a rollback, and so every edit. Upgrade, roll back, upgrade again."""
    old = store_757()
    old.Store(tmp_path).close()  # #757's schema
    store = Store(tmp_path)
    columns = [r[1] for r in store.db.execute("PRAGMA table_info(message_edits)")]
    assert columns == ["bot", "message_id", "edits", "edited_at"]  # #757's, untouched
    store.keep_edit_count("b", 10, 20, 1000.0, final=True)
    store.keep_edit_count("b", 11, 3, 1010.0)
    store.close()
    rolled_back = old.Store(tmp_path)
    rolled_back.keep_edit_count("b", 12, 1, 1020.0)  # an edit, as #757's `EditBounds` keeps it
    rolled_back.keep_edit_count("b", 11, 4, 1030.0)
    rolled_back.forget_edit_counts("b", (10,))  # its turn delivered
    assert rolled_back.edit_counts("b") == [(12, 1, 1020.0), (11, 4, 1030.0)]
    rolled_back.close()
    again = Store(tmp_path)  # the final mark #757 left behind is gone
    assert again.edit_counts("b") == [(12, 1, 1020.0, False), (11, 4, 1030.0, False)]
    assert again.rows("message_finals") == []
    again.keep_edit_count("b", 11, 5, 1040.0, final=True)
    assert again.edit_counts("b")[-1] == (11, 5, 1040.0, True)
    again.forget_edit_counts("b", (11,))
    assert again.rows("message_finals") == []
    assert again.db.execute("PRAGMA user_version").fetchone()[0] == 1
    again.close()


def test_lock_exclusion_and_release(tmp_path):
    first = BotLock(tmp_path, "bot")
    with pytest.raises(RuntimeError, match="already running"):
        BotLock(tmp_path, "bot")
    other = BotLock(tmp_path, "other")
    first.close()
    second = BotLock(tmp_path, "bot")
    other.close()
    second.close()


def test_identifiers_are_validated(tmp_path):
    store = Store(tmp_path)
    with pytest.raises(ValueError):
        store.rows("sessions; DROP TABLE sessions")
    with pytest.raises(ValueError):
        store.update("sessions", {}, state="idle")
    store.close()


def test_null_predicates_match_and_update(tmp_path):
    store = Store(tmp_path)
    store.claim("bot", 1)
    assert len(store.rows("inbound", session_id=None)) == 1
    store.update("inbound", {"session_id": None}, disposition="command")
    assert store.rows("inbound", session_id=None)[0]["disposition"] == "command"
    store.close()


def test_consumed_or_dropped_messages_release_only_their_peer_keys(tmp_path):
    store = Store(tmp_path)
    add_session(store)
    for mid in (10, 11, 12):
        store.hold("s", mid, "owner", "input")
        store.set(f"addressed_peers:b:{mid}", ["Peer"])
        store.set(f"message_audience:b:{mid}", dict(addressed=True, peers=["Peer"]))
    store.set("addressed_peers:other-bot:10", ["Peer"])
    store.set("pending_delivery:b:12", dict(message_id=12))
    store.clear_held("s", [10, 12])
    assert store.get("addressed_peers:b:10") is None
    assert store.get("message_audience:b:10") is None
    assert store.get("addressed_peers:b:11") == ["Peer"]
    assert store.get("addressed_peers:b:12") == ["Peer"]
    assert store.get("addressed_peers:other-bot:10") == ["Peer"]
    store.close()


def test_audience_pruning_preserves_replay_windows_pending_work_and_legacy(tmp_path):
    store = Store(tmp_path)
    add_session(store)
    for mid in range(1, 151):
        store.set(
            f"message_audience:b:{mid}",
            dict(addressed=False, peers=["Peer"], stream_id=1, topic="topic"),
        )
    store.set("message_audience:b:1000", dict(addressed=False, peers=["Peer"]))  # legacy
    store.set("message_audience:other:1", dict(stream_id=1, topic="topic"))
    store.set("message_audience:b:1001", dict(stream_id=1, topic="different"))
    store.set("pending_delivery:b:15", dict(message_id=15))
    store.prune_audience("b", 1, "topic", 130)
    assert store.get("message_audience:b:1") is not None
    store.delete("pending_delivery:b:15")
    store.hold("s", 15, "Owner", "delayed")
    store.prune_audience("b", 1, "topic", 130)
    assert store.get("message_audience:b:1") is not None
    store.clear_held("s", [15])
    store.close()
    store = Store(tmp_path)
    store.prune_audience("b", 1, "topic", 130)
    for mid in range(1, 151):
        assert (store.get(f"message_audience:b:{mid}") is not None) == (mid > 30)
    for key in ("message_audience:b:1000", "message_audience:other:1", "message_audience:b:1001"):
        assert store.get(key) is not None
    store.close()
