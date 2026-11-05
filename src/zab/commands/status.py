"""Read bridge state without taking bot locks or opening credentials (SPEC §13.1)."""

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

from zab.config import Config, load_config

#: The newest activity (epoch seconds), as `workshop/admin/idle_stop.py` counts it: a message
#: taken in for the agents (not `ignored`), one held for a busy session, a turn started or ended.
LAST_ACTIVITY_SQL = (
    "SELECT max(t) FROM ("
    " SELECT max(handled_at) AS t FROM inbound WHERE disposition != 'ignored'"
    " UNION ALL SELECT max(received_at) FROM held"
    " UNION ALL SELECT max(started_at) FROM turns"
    " UNION ALL SELECT max(ended_at) FROM turns)"
)


def read_status(config: Config) -> dict[str, Any]:
    path = Path(config.bridge.state_dir) / "zab.db"
    if not path.exists():
        return {"sessions": [], "running_turns": [], "last_activity": None}
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        sessions = [
            dict(r)
            for r in db.execute(
                "SELECT id, bot, backend, native_id, stream_id, topic, state, card_message_id "
                "FROM sessions ORDER BY updated_at DESC"
            )
        ]
        sites = {
            r["key"]: json.loads(r["value"])
            for r in db.execute("SELECT key, value FROM kv WHERE key LIKE 'site:%'")
        }
        for session in sessions:
            site = sites.get("site:" + session["bot"])
            session["topic_url"] = (
                f"{site}/#narrow/channel/{session['stream_id']}/near/{session['card_message_id']}"
                if site
                else None
            )
        turns = [
            dict(r)
            for r in db.execute(
                "SELECT session_id, native_turn_id, state, started_at, status_message_id "
                "FROM turns WHERE state='running'"
            )
        ]
        last = db.execute(LAST_ACTIVITY_SQL).fetchone()[0]
        return {"sessions": sessions, "running_turns": turns, "last_activity": last}
    finally:
        db.close()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="zab status")
    parser.add_argument("--config")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = read_status(load_config(args.config))
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({"error": type(exc).__name__}))
        return 1
    if args.json:
        print(json.dumps(result))
    else:
        for row in result["sessions"]:
            print(f"{row['bot']} {row['state']}: {row['topic']} ({row['native_id']})")
            if row["topic_url"]:
                print("  " + row["topic_url"])
        print(f"{len(result['running_turns'])} running turns")
    return 0
