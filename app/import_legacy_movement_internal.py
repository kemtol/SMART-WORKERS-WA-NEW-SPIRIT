#!/usr/bin/env python3
import argparse
import csv
import io
import json
import os
import sqlite3
import urllib.parse
import urllib.request
from datetime import datetime, timezone


DEFAULT_DB = os.environ.get("OPS_DB_PATH", "data/ops_messages.sqlite3")
DEFAULT_SHEET_NAME = "Movement_Internal"
DEFAULT_GROUP_JID = "6282114137183-1490316198@g.us"

MOVEMENT_COLUMNS = [
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
]


def load_local_env(path="config/google-sheets.env"):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))
    except FileNotFoundError:
        return


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def blank_to_none(value):
    value = value.strip() if isinstance(value, str) else value
    return None if value == "" else value


def to_int(value):
    value = blank_to_none(value)
    return int(value) if value is not None else None


def to_float(value):
    value = blank_to_none(value)
    if value is None:
        return None
    return float(str(value).replace(",", "."))


def timestamp_seconds(timestamp_iso):
    timestamp_iso = blank_to_none(timestamp_iso)
    if not timestamp_iso:
        return None
    value = timestamp_iso.replace("Z", "+00:00")
    try:
        return int(datetime.fromisoformat(value).timestamp())
    except ValueError:
        return None


def csv_url(spreadsheet_id, sheet_name):
    encoded_sheet = urllib.parse.quote(sheet_name)
    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/gviz/tq?tqx=out:csv&sheet={encoded_sheet}"


def fetch_rows(spreadsheet_id, sheet_name):
    with urllib.request.urlopen(csv_url(spreadsheet_id, sheet_name), timeout=30) as response:
        text = response.read().decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def ensure_schema(db_path):
    from ingest_service import Store

    Store(db_path)


def movement_value(row, column):
    if column in {"leg_index"}:
        return to_int(row.get(column))
    if column in {"pax_weight_kg", "baggage_kg", "cargo_kg", "total_load_kg", "parse_confidence"}:
        return to_float(row.get(column))
    return blank_to_none(row.get(column, ""))


def import_rows(db_path, rows, group_jid, source_sheet):
    rows = [row for row in rows if blank_to_none(row.get("movement_id")) and blank_to_none(row.get("raw_message_id"))]
    rows.sort(key=lambda row: int(row["movement_id"]))

    by_raw = {}
    for row in rows:
        raw_id = int(row["raw_message_id"])
        by_raw.setdefault(raw_id, row)

    movement_index = {}
    now = utc_now()

    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.execute("PRAGMA busy_timeout = 30000")
        raw_inserted = 0
        movement_inserted = 0

        for raw_id, row in sorted(by_raw.items()):
            text = blank_to_none(row.get("source_text")) or ""
            payload = {
                "source": source_sheet,
                "raw_message_id": raw_id,
                "groupName": blank_to_none(row.get("group_name")),
                "senderJid": blank_to_none(row.get("sender_jid")),
                "timestampIso": blank_to_none(row.get("message_timestamp_iso")),
                "receivedAt": blank_to_none(row.get("received_at")),
                "text": text,
            }
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO raw_messages (
                  id, dedupe_key, message_id, remote_jid, group_name, sender_jid, from_me,
                  message_timestamp, message_timestamp_iso, message_type, text, source,
                  received_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    raw_id,
                    f"legacy:{source_sheet}:{raw_id}",
                    f"legacy-{raw_id}",
                    group_jid,
                    blank_to_none(row.get("group_name")) or "New Spirit",
                    blank_to_none(row.get("sender_jid")),
                    timestamp_seconds(row.get("message_timestamp_iso")),
                    blank_to_none(row.get("message_timestamp_iso")),
                    "legacy_movement",
                    text,
                    source_sheet,
                    blank_to_none(row.get("received_at")) or now,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            raw_inserted += cur.rowcount

        for row in rows:
            raw_id = int(row["raw_message_id"])
            movement_id = int(row["movement_id"])
            movement_index[raw_id] = movement_index.get(raw_id, 0) + 1
            values = [movement_value(row, column) for column in MOVEMENT_COLUMNS]
            cur = conn.execute(
                f"""
                INSERT OR IGNORE INTO flight_movements (
                  id, raw_message_id, movement_index, {", ".join(MOVEMENT_COLUMNS)}, created_at
                ) VALUES (
                  ?, ?, ?, {", ".join("?" for _ in MOVEMENT_COLUMNS)}, ?
                )
                """,
                (movement_id, raw_id, movement_index[raw_id], *values, now),
            )
            movement_inserted += cur.rowcount

    return {
        "source_sheet": source_sheet,
        "rows_read": len(rows),
        "raw_inserted": raw_inserted,
        "movements_inserted": movement_inserted,
        "raw_min": min(by_raw) if by_raw else None,
        "raw_max": max(by_raw) if by_raw else None,
    }


def main():
    load_local_env()
    parser = argparse.ArgumentParser(description="Import legacy Movement_Internal rows from Google Sheets into SQLite")
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--spreadsheet-id", default=os.environ.get("GOOGLE_SHEETS_SPREADSHEET_ID"))
    parser.add_argument("--sheet-name", default=DEFAULT_SHEET_NAME)
    parser.add_argument("--group-jid", default=DEFAULT_GROUP_JID)
    args = parser.parse_args()

    if not args.spreadsheet_id:
        raise SystemExit("GOOGLE_SHEETS_SPREADSHEET_ID or --spreadsheet-id is required")

    ensure_schema(args.db)
    rows = fetch_rows(args.spreadsheet_id, args.sheet_name)
    result = import_rows(args.db, rows, args.group_jid, args.sheet_name)
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
