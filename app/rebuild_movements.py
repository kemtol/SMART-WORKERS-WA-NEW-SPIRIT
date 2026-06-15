#!/usr/bin/env python3
import argparse
import json
import os
import sqlite3

from ingest_service import Store, utc_now


DEFAULT_DB = os.environ.get("OPS_DB_PATH", "data/ops_messages.sqlite3")


def rebuild(db_path):
    store = Store(db_path)
    now = utc_now()
    stats = {"raw_messages": 0, "messages_with_movements": 0, "movements": 0}
    with store.write_lock:
        with store.connect() as conn:
            conn.execute("DELETE FROM flight_movements")
            conn.execute("DELETE FROM sqlite_sequence WHERE name = 'flight_movements'")
            rows = conn.execute("SELECT id, text FROM raw_messages ORDER BY id ASC").fetchall()
            for row in rows:
                stats["raw_messages"] += 1
                count = store.insert_movements(conn, row["id"], row["text"] or "", now)
                if count:
                    stats["messages_with_movements"] += 1
                    stats["movements"] += count
    return stats


def main():
    parser = argparse.ArgumentParser(description="Rebuild parsed flight movements from raw WhatsApp messages")
    parser.add_argument("--db", default=DEFAULT_DB)
    args = parser.parse_args()
    print(json.dumps({"ok": True, **rebuild(args.db)}, indent=2))


if __name__ == "__main__":
    main()
