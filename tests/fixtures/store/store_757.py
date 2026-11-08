"""Durable bridge state and process locks (SPEC §6)."""

from __future__ import annotations

import fcntl
import json
import re
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE sessions (
 id TEXT PRIMARY KEY, bot TEXT NOT NULL, backend TEXT NOT NULL,
 native_id TEXT NOT NULL, card_message_id INTEGER NOT NULL UNIQUE,
 stream_id INTEGER NOT NULL, topic TEXT NOT NULL, workspace TEXT NOT NULL,
 workspace_mode TEXT NOT NULL, repo_root TEXT NOT NULL, branch TEXT,
 start_snapshot TEXT, settings_json TEXT NOT NULL, state TEXT NOT NULL,
 shared INTEGER NOT NULL DEFAULT 0, last_sender_id INTEGER,
 created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX sessions_native ON sessions(backend, native_id)
 WHERE state != 'detached';
CREATE UNIQUE INDEX sessions_topic ON sessions(bot, stream_id, topic)
 WHERE state != 'detached';
CREATE TABLE turns (
 id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
 native_turn_id TEXT, trigger_msg_ids TEXT NOT NULL, state TEXT NOT NULL,
 status_message_id INTEGER, final_message_ids TEXT, start_snapshot TEXT,
 pgid INTEGER, sender_id INTEGER, started_at REAL NOT NULL, ended_at REAL,
 UNIQUE(session_id, native_turn_id)
);
CREATE TABLE approvals (
 request_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
 message_id INTEGER, kind TEXT NOT NULL, state TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE inbound (
 message_id INTEGER NOT NULL, bot TEXT NOT NULL, session_id TEXT,
 disposition TEXT NOT NULL, handled_at REAL NOT NULL, PRIMARY KEY(message_id, bot)
);
CREATE TABLE held (
 message_id INTEGER NOT NULL, session_id TEXT NOT NULL REFERENCES sessions(id),
 sender TEXT NOT NULL, content TEXT NOT NULL, received_at REAL NOT NULL,
 PRIMARY KEY(message_id, session_id)
);
CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def _migration_1(db: sqlite3.Connection) -> None:
    db.executescript("BEGIN IMMEDIATE;\n" + _SCHEMA + "PRAGMA user_version=1; COMMIT;")


#: Nullable columns added to an existing database where they are missing, without a new
#: `user_version`: a zab that predates them still opens the database (a rollback on a VM,
#: whose state survives it), reading and writing rows by column name. `parent_session_id`
#: is the zab session a `!fork` copied (issue #202), NULL for every other session.
_ADDED_COLUMNS = (("sessions", "parent_session_id", "TEXT"),)


def _missing_columns(db: sqlite3.Connection) -> list[tuple[str, str, str]]:
    return [
        (table, name, kind)
        for table, name, kind in _ADDED_COLUMNS
        if name not in {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
    ]


def _add_columns(db: sqlite3.Connection) -> None:
    if not _missing_columns(db):
        return
    db.execute("BEGIN IMMEDIATE")  # a second process opening the store waits, then finds them
    try:
        for table, name, kind in _missing_columns(db):  # again, under the write lock
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
    except BaseException:
        db.rollback()
        raise
    db.commit()


#: Tables created in an existing database where they are missing, again without a new
#: `user_version`: an older zab ignores them. `message_edits` keeps the edits of each message a
#: bot edited, for their bounds (`EditBounds.keep`; SPEC §4.9, §6.2; #757): how many, and when
#: the last went out, on the wall clock, which a reboot doesn't start again.
_ADDED_TABLES = {
    "message_edits": """CREATE TABLE IF NOT EXISTS message_edits (
 bot TEXT NOT NULL, message_id INTEGER NOT NULL, edits INTEGER NOT NULL,
 edited_at REAL NOT NULL, PRIMARY KEY (bot, message_id)
)""",
}


def _add_tables(db: sqlite3.Connection) -> None:
    present = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if _ADDED_TABLES.keys() <= present:
        return
    db.execute("BEGIN IMMEDIATE")
    try:
        for schema in _ADDED_TABLES.values():  # IF NOT EXISTS: another store may have made it
            db.execute(schema)
    except BaseException:
        db.rollback()
        raise
    db.commit()


class Store:
    """Small synchronous transactions; callers must never await within one."""

    def __init__(self, state_dir: str | Path) -> None:
        root = self.state_dir = Path(state_dir)
        root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(root / "zab.db", timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version > 1:
            self.db.close()
            raise ValueError(f"unsupported database schema version {version}")
        if version == 0:
            _migration_1(self.db)
        _add_columns(self.db)
        _add_tables(self.db)
        self._columns = {
            name: {r[1] for r in self.db.execute(f"PRAGMA table_info({name})")}
            for name in ("sessions", "turns", "approvals", "inbound", "held", "kv", *_ADDED_TABLES)
        }

    def close(self) -> None:
        self.db.close()

    def rows(self, table: str, **where: Any) -> list[dict[str, Any]]:
        """Read by exact field equality; identifiers are restricted to the schema."""
        self._validate(table, where)
        clause = " AND ".join(f"{name} IS ?" for name in where) or "1"
        query = f"SELECT * FROM {table} WHERE {clause}"  # noqa: S608
        return [dict(row) for row in self.db.execute(query, tuple(where.values()))]

    def insert(self, table: str, **values: Any) -> int:
        self._validate(table, values)
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        with self.transaction():
            cursor = self.db.execute(
                f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",  # noqa: S608
                tuple(values.values()),
            )
        return cursor.lastrowid or 0

    def update(self, table: str, where: dict[str, Any], **values: Any) -> None:
        self._validate(table, where)
        self._validate(table, values)
        if not where or not values:
            raise ValueError("update requires predicates and values")
        assignments = ", ".join(f"{name} = ?" for name in values)
        clause = " AND ".join(f"{name} IS ?" for name in where)
        with self.transaction():
            self.db.execute(
                f"UPDATE {table} SET {assignments} WHERE {clause}",  # noqa: S608
                (*values.values(), *where.values()),
            )

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Group synchronous mutations atomically; nested store methods join the transaction."""
        # Never await inside this scope: other coroutines would join the transaction.
        outer = not self.db.in_transaction
        if outer:
            self.db.execute("BEGIN")
        try:
            yield
        except BaseException:
            if outer:
                self.db.rollback()
            raise
        else:
            if outer:
                self.db.commit()

    def pending_deliveries(self, bot: str) -> list[dict[str, Any]]:
        prefix = f"pending_delivery:{bot}:"
        result = [
            json.loads(r["value"])
            for r in self.db.execute(
                "SELECT value FROM kv WHERE substr(key, 1, ?) = ?", (len(prefix), prefix)
            )
        ]
        return sorted(result, key=lambda value: value["message_id"])

    def claim(self, bot: str, message_id: int, session_id: str | None = None) -> bool:
        """Durably claim a message before any side effect; duplicates return False."""
        with self.transaction():
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO inbound VALUES (?, ?, ?, 'ignored', ?)",
                (message_id, bot, session_id, time.time()),
            )
        return cursor.rowcount == 1

    def hold(self, session_id: str, message_id: int, sender: str, content: str) -> None:
        with self.transaction():
            self.db.execute(
                "INSERT OR IGNORE INTO held VALUES (?, ?, ?, ?, ?)",
                (message_id, session_id, sender, content, time.time()),
            )

    def held(self, session_id: str) -> list[dict[str, Any]]:
        return sorted(self.rows("held", session_id=session_id), key=lambda r: r["message_id"])

    def clear_held(self, session_id: str, message_ids: list[int]) -> None:
        """Remove only successfully delivered messages, preserving newer arrivals."""
        with self.transaction():
            self.db.executemany(
                "DELETE FROM held WHERE session_id=? AND message_id=?",
                [(session_id, mid) for mid in message_ids],
            )
            session = self.rows("sessions", id=session_id)
            if session:
                bot = session[0]["bot"]
                for mid in message_ids:
                    if self.get(f"pending_delivery:{bot}:{mid}"):
                        continue
                    self.delete(f"addressed_peers:{bot}:{mid}")
                    key = f"message_audience:{bot}:{mid}"
                    if self.get(key, {}).get("addressed") is True:
                        self.delete(key)  # Peer history only consumes unaddressed snapshots.

    def prune_audience(self, bot: str, stream_id: int, topic: str, through: int) -> None:
        """Keep 100 consumed snapshots per topic for fresh-session peer context.

        Defer while this bot has held/pending input: an older trigger may still
        need the snapshots before its anchor. Legacy snapshots lack topic data
        and cannot safely be aged out here.
        """
        if (
            self.pending_deliveries(bot)
            or self.db.execute(
                "SELECT 1 FROM held h JOIN sessions s ON s.id=h.session_id WHERE s.bot=? LIMIT 1",
                (bot,),
            ).fetchone()
        ):
            return
        prefix = f"message_audience:{bot}:"
        with self.transaction():
            self.db.execute(
                """DELETE FROM kv WHERE key IN (
                    SELECT key FROM kv
                    WHERE substr(key, 1, ?) = ?
                      AND json_extract(value, '$.stream_id') = ?
                      AND json_extract(value, '$.topic') = ?
                      AND CAST(substr(key, ?) AS INTEGER) <= ?
                    ORDER BY CAST(substr(key, ?) AS INTEGER) DESC
                    LIMIT -1 OFFSET 100
                )""",
                (len(prefix), prefix, stream_id, topic, len(prefix) + 1, through, len(prefix) + 1),
            )

    def overlapping_bots(self, session_id: str, start: float, end: float) -> list[str]:
        """Other sessions whose workspace-wide turn interval overlaps this diff."""
        rows = self.db.execute(
            """SELECT DISTINCT peer.bot FROM turns t
               JOIN sessions peer ON peer.id=t.session_id
               JOIN sessions own ON own.id=?
               WHERE peer.id != own.id AND peer.repo_root=own.repo_root
                 AND t.started_at < ? AND (t.ended_at IS NULL OR t.ended_at > ?)
               ORDER BY peer.bot""",
            (session_id, end, start),
        )
        return [row["bot"] for row in rows]

    def edit_counts(self, bot: str) -> list[tuple[int, int, float]]:
        """The edits kept of `bot`'s messages (`EditBounds.keep`, #757): (message id, edits, when
        the last went out), the least recently edited first."""
        rows = self.db.execute(
            "SELECT message_id, edits, edited_at FROM message_edits WHERE bot=?"
            " ORDER BY edited_at, message_id",
            (bot,),
        )
        return [(row[0], row[1], row[2]) for row in rows]

    def keep_edit_count(
        self, bot: str, message_id: int, edits: int, edited_at: float, forget: tuple[int, ...] = ()
    ) -> None:
        """Keep a message's edits, and forget those of the messages in `forget`, at once."""
        with self.transaction():
            self.db.execute(
                "INSERT INTO message_edits VALUES (?, ?, ?, ?) ON CONFLICT(bot, message_id)"
                " DO UPDATE SET edits=excluded.edits, edited_at=excluded.edited_at",
                (bot, message_id, edits, edited_at),
            )
            self.forget_edit_counts(bot, forget)

    def forget_edit_counts(self, bot: str, message_ids: tuple[int, ...]) -> None:
        with self.transaction():
            self.db.executemany(
                "DELETE FROM message_edits WHERE bot=? AND message_id=?",
                [(bot, mid) for mid in message_ids],
            )

    def get(self, key: str, default: Any = None) -> Any:
        rows = self.rows("kv", key=key)
        return json.loads(rows[0]["value"]) if rows else default

    def set(self, key: str, value: Any) -> None:
        with self.transaction():
            self.db.execute(
                "INSERT INTO kv VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )

    def delete(self, key: str) -> None:
        """Remove transient metadata after its durable operation completes."""
        with self.transaction():
            self.db.execute("DELETE FROM kv WHERE key=?", (key,))

    def _validate(self, table: str, values: dict[str, Any]) -> None:
        if table not in self._columns or not values.keys() <= self._columns[table]:
            raise ValueError("unknown store table or column")


class BotLock:
    """Lifetime flock; never unlink a lock file, which would permit split ownership."""

    def __init__(self, state_dir: str | Path, bot: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", bot):
            raise ValueError("invalid bot lock name")
        root = Path(state_dir)
        root.mkdir(parents=True, exist_ok=True)
        self._file = (root / f"{bot}.lock").open("a")
        try:
            fcntl.flock(self._file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._file.close()
            raise RuntimeError(f"bot {bot} is already running") from exc

    def close(self) -> None:
        self._file.close()
