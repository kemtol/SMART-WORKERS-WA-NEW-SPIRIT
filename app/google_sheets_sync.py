#!/usr/bin/env python3
import argparse
import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone


DEFAULT_DB = os.environ.get("OPS_DB_PATH", "data/ops_messages.sqlite3")
DEFAULT_STATE = os.environ.get("OPS_SHEETS_STATE", "data/google-sheets-movement-sync-state.json")
DEFAULT_SHEET_NAME = os.environ.get("GOOGLE_SHEETS_TAB", "Movements_Internal")
DEFAULT_WEBHOOK_URL = os.environ.get("GOOGLE_SHEETS_WEBHOOK_URL")
DEFAULT_TOKEN = os.environ.get("GOOGLE_SHEETS_WEBHOOK_TOKEN")

HEADERS = [
    "movement_id",
    "raw_message_id",
    "message_timestamp_iso",
    "received_at",
    "group_name",
    "sender_jid",
    "movement_type",
    "operation_date",
    "registration",
    "aircraft_type",
    "flight_seq",
    "leg_index",
    "route_full",
    "leg_origin_code",
    "leg_origin_name",
    "leg_origin_icao",
    "leg_origin_iata",
    "leg_destination_code",
    "leg_destination_name",
    "leg_destination_icao",
    "leg_destination_iata",
    "from_place",
    "from_code",
    "from_name",
    "from_icao",
    "from_iata",
    "arrival_airport_code",
    "arrival_airport_name",
    "arrival_airport_icao",
    "arrival_airport_iata",
    "next_route",
    "next_text",
    "engine_start_time",
    "takeoff_time",
    "eta_airport_code",
    "eta_airport_name",
    "eta_airport_icao",
    "eta_airport_iata",
    "eta_time",
    "ata_airport_code",
    "ata_airport_name",
    "ata_airport_icao",
    "ata_airport_iata",
    "ata_time",
    "pax",
    "pax_weight_kg",
    "baggage_kg",
    "cargo_text",
    "cargo_kg",
    "total_load_kg",
    "remark",
    "parse_confidence",
    "source_text",
]


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_state(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return {"last_movement_id": 0}


def save_state(path, state):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({**state, "updatedAt": utc_now()}, handle, indent=2)
        handle.write("\n")


def connect(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def get_movement_rows(db_path, after_id, limit):
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT
              fm.id AS movement_id,
              fm.raw_message_id,
              rm.message_timestamp_iso,
              rm.received_at,
              rm.group_name,
              rm.sender_jid,
              fm.movement_type,
              fm.operation_date,
              fm.registration,
              fm.aircraft_type,
              fm.flight_seq,
              fm.leg_index,
              fm.route_full,
              fm.leg_origin_code,
              fm.leg_origin_name,
              fm.leg_origin_icao,
              fm.leg_origin_iata,
              fm.leg_destination_code,
              fm.leg_destination_name,
              fm.leg_destination_icao,
              fm.leg_destination_iata,
              fm.from_place,
              fm.from_code,
              fm.from_name,
              fm.from_icao,
              fm.from_iata,
              fm.arrival_airport_code,
              fm.arrival_airport_name,
              fm.arrival_airport_icao,
              fm.arrival_airport_iata,
              fm.next_route,
              fm.next_text,
              fm.engine_start_time,
              fm.takeoff_time,
              fm.eta_airport_code,
              fm.eta_airport_name,
              fm.eta_airport_icao,
              fm.eta_airport_iata,
              fm.eta_time,
              fm.ata_airport_code,
              fm.ata_airport_name,
              fm.ata_airport_icao,
              fm.ata_airport_iata,
              fm.ata_time,
              fm.pax,
              fm.pax_weight_kg,
              fm.baggage_kg,
              fm.cargo_text,
              fm.cargo_kg,
              fm.total_load_kg,
              fm.remark,
              fm.parse_confidence,
              rm.text AS source_text
            FROM flight_movements fm
            JOIN raw_messages rm ON rm.id = fm.raw_message_id
            WHERE fm.id > ?
            ORDER BY fm.id ASC
            LIMIT ?
            """,
            (after_id, limit),
        ).fetchall()
    return [{key: row[key] for key in HEADERS} for row in rows]


def post_rows(webhook_url, token, sheet_name, rows, timeout):
    payload = {
        "token": token,
        "sheetName": sheet_name,
        "headers": HEADERS,
        "rows": rows,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=data,
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
        result = json.loads(body) if body else {}
    if not result.get("ok"):
        raise RuntimeError(f"Google Sheets webhook rejected payload: {result}")
    return result


def sync_once(args):
    state = load_state(args.state)
    after_id = int(args.from_id if args.from_id is not None else state.get("last_movement_id", 0))
    rows = get_movement_rows(args.db, after_id, args.batch_size)

    if not rows:
        print(json.dumps({"ok": True, "status": "idle", "last_movement_id": after_id}))
        return 0

    if args.dry_run:
        print(json.dumps({"ok": True, "status": "dry_run", "rows": len(rows), "first": rows[0], "last": rows[-1]}, ensure_ascii=False, indent=2))
        return len(rows)

    if not args.webhook_url:
        raise RuntimeError("GOOGLE_SHEETS_WEBHOOK_URL is required unless --dry-run is used")

    result = post_rows(args.webhook_url, args.token, args.sheet_name, rows, args.timeout_seconds)
    last_id = max(int(row["movement_id"]) for row in rows)
    save_state(args.state, {"last_movement_id": last_id})
    print(json.dumps({"ok": True, "status": "synced", "rows": len(rows), "last_movement_id": last_id, "webhook": result}, ensure_ascii=False))
    return len(rows)


def main():
    parser = argparse.ArgumentParser(description="Sync parsed WhatsApp flight movements to Google Sheets")
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--state", default=DEFAULT_STATE)
    parser.add_argument("--webhook-url", default=DEFAULT_WEBHOOK_URL)
    parser.add_argument("--token", default=DEFAULT_TOKEN)
    parser.add_argument("--sheet-name", default=DEFAULT_SHEET_NAME)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--interval-seconds", type=int, default=15)
    parser.add_argument("--timeout-seconds", type=int, default=30)
    parser.add_argument("--from-id", type=int, default=None)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.once:
        sync_once(args)
        return

    while True:
        try:
            synced = sync_once(args)
            if synced >= args.batch_size:
                continue
        except (urllib.error.URLError, TimeoutError, RuntimeError, OSError, sqlite3.Error) as exc:
            print(json.dumps({"ok": False, "error": str(exc), "at": utc_now()}, ensure_ascii=False))
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    main()
