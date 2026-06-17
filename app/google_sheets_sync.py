#!/usr/bin/env python3
import argparse
import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone


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


load_local_env()

DEFAULT_DB = os.environ.get("OPS_DB_PATH", "data/ops_messages.sqlite3")
DEFAULT_STATE = os.environ.get("OPS_SHEETS_STATE", "data/google-sheets-movement-sync-state.json")
DEFAULT_RAW_SHEET_NAME = os.environ.get("GOOGLE_SHEETS_RAW_TAB", "RAW")
DEFAULT_FLIGHT_RAW_SHEET_NAME = os.environ.get("GOOGLE_SHEETS_FLIGHT_RAW_TAB", "FLIGHT_RAW")
DEFAULT_FLIGHT_OPS_SHEET_NAME = os.environ.get("GOOGLE_SHEETS_FLIGHT_OPS_TAB", "FLIGHT_OPS")
DEFAULT_WEBHOOK_URL = os.environ.get("GOOGLE_SHEETS_WEBHOOK_URL")
DEFAULT_TOKEN = os.environ.get("GOOGLE_SHEETS_WEBHOOK_TOKEN")
DEFAULT_SPREADSHEET_ID = os.environ.get("GOOGLE_SHEETS_SPREADSHEET_ID")

RAW_HEADERS = [
    "raw_message_id",
    "dedupe_key",
    "message_id",
    "remote_jid",
    "group_name",
    "sender_jid",
    "from_me",
    "message_timestamp",
    "message_timestamp_iso",
    "message_type",
    "text",
    "source",
    "received_at",
    "payload_json",
]

FLIGHT_RAW_HEADERS = [
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
    "deepclean_status",
    "deepclean_force_check",
    "deepclean_requested_at",
    "deepcleaned_at",
    "deepclean_prompt_version",
    "deepclean_model",
    "deepclean_error",
    "flight_ops_id",
    "source_text",
]

FLIGHT_OPS_HEADERS = [
    "schema_version",
    "prompt_version",
    "movement_id",
    "raw_message_id",
    "operation_date",
    "movement_type",
    "registration",
    "aircraft_type",
    "flight_seq",
    "leg_origin_code",
    "leg_destination_code",
    "route_full",
    "takeoff_time",
    "eta_time",
    "ata_time",
    "pax",
    "pax_weight_kg",
    "baggage_kg",
    "cargo_kg",
    "total_load_kg",
    "remark",
    "ops_status",
    "ai_confidence",
    "review_notes",
    "source_trace",
    "source_text",
    "deepcleaned_at",
    "deepclean_model",
]


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_state(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
    except FileNotFoundError:
        state = {}
    return {
        "last_raw_message_id": int(state.get("last_raw_message_id", 0) or 0),
        "last_movement_id": int(state.get("last_movement_id", 0) or 0),
    }


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


def get_raw_rows(db_path, after_id, limit):
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT
              id AS raw_message_id,
              dedupe_key,
              message_id,
              remote_jid,
              group_name,
              sender_jid,
              from_me,
              message_timestamp,
              message_timestamp_iso,
              message_type,
              text,
              source,
              received_at,
              payload_json
            FROM raw_messages
            WHERE id > ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (after_id, limit),
        ).fetchall()
    return [{key: row[key] for key in RAW_HEADERS} for row in rows]


def get_flight_raw_rows(db_path, after_id, limit):
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

    result = []
    for row in rows:
        item = {key: row[key] for key in FLIGHT_RAW_HEADERS if key in row.keys()}
        item.update(
            {
                "deepclean_status": "pending",
                "deepclean_force_check": False,
                "deepclean_requested_at": "",
                "deepcleaned_at": "",
                "deepclean_prompt_version": "",
                "deepclean_model": "",
                "deepclean_error": "",
                "flight_ops_id": "",
            }
        )
        result.append({key: item.get(key) for key in FLIGHT_RAW_HEADERS})
    return result


def post_payload(webhook_url, payload, timeout, spreadsheet_id=None):
    if spreadsheet_id and "spreadsheetId" not in payload:
        payload = {**payload, "spreadsheetId": spreadsheet_id}
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


def post_rows(webhook_url, token, spreadsheet_id, sheet_name, headers, rows, timeout):
    return post_payload(
        webhook_url,
        {
            "token": token,
            "sheetName": sheet_name,
            "headers": headers,
            "rows": rows,
        },
        timeout,
        spreadsheet_id,
    )


def require_webhook_url(args):
    if not args.webhook_url:
        raise RuntimeError("GOOGLE_SHEETS_WEBHOOK_URL is required unless --dry-run is used")


def ensure_sheets(args):
    require_webhook_url(args)
    result = post_payload(
        args.webhook_url,
        {
            "token": args.token,
            "action": "ensureSheets",
            "sheets": [
                {"name": args.raw_sheet_name, "headers": RAW_HEADERS},
                {"name": args.flight_raw_sheet_name, "headers": FLIGHT_RAW_HEADERS},
                {"name": args.flight_ops_sheet_name, "headers": FLIGHT_OPS_HEADERS},
            ],
        },
        args.timeout_seconds,
        args.spreadsheet_id,
    )
    print(json.dumps({"ok": True, "status": "ensured", "webhook": result}, ensure_ascii=False))


def delete_legacy_sheets(args):
    require_webhook_url(args)
    result = post_payload(
        args.webhook_url,
        {
            "token": args.token,
            "action": "deleteSheets",
            "deleteSheets": ["Movements_Internal", "Movements", "Schedules"],
            "keepSheetName": args.raw_sheet_name,
        },
        args.timeout_seconds,
        args.spreadsheet_id,
    )
    print(json.dumps({"ok": True, "status": "legacy_deleted", "webhook": result}, ensure_ascii=False))


def sync_dataset(args, state, state_key, sheet_name, headers, rows, id_key):
    if not rows:
        return {"sheet": sheet_name, "rows": 0, state_key: state[state_key], "status": "idle"}

    result = post_rows(args.webhook_url, args.token, args.spreadsheet_id, sheet_name, headers, rows, args.timeout_seconds)
    last_id = max(int(row[id_key]) for row in rows)
    state[state_key] = last_id
    save_state(args.state, state)
    return {"sheet": sheet_name, "rows": len(rows), state_key: last_id, "webhook": result}


def sync_once(args):
    state = load_state(args.state)
    raw_after_id = int(args.from_raw_id if args.from_raw_id is not None else state["last_raw_message_id"])
    movement_after_id = int(
        args.from_movement_id
        if args.from_movement_id is not None
        else args.from_id
        if args.from_id is not None
        else state["last_movement_id"]
    )

    raw_rows = [] if args.skip_raw else get_raw_rows(args.db, raw_after_id, args.batch_size)
    flight_raw_rows = [] if args.skip_flight_raw else get_flight_raw_rows(args.db, movement_after_id, args.batch_size)

    if args.dry_run:
        print(
            json.dumps(
                {
                    "ok": True,
                    "status": "dry_run",
                    "raw": {
                        "sheet": args.raw_sheet_name,
                        "rows": len(raw_rows),
                        "first": raw_rows[0] if raw_rows else None,
                        "last": raw_rows[-1] if raw_rows else None,
                    },
                    "flight_raw": {
                        "sheet": args.flight_raw_sheet_name,
                        "rows": len(flight_raw_rows),
                        "first": flight_raw_rows[0] if flight_raw_rows else None,
                        "last": flight_raw_rows[-1] if flight_raw_rows else None,
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return len(raw_rows) + len(flight_raw_rows)

    if not raw_rows and not flight_raw_rows:
        print(
            json.dumps(
                {
                    "ok": True,
                    "status": "idle",
                    "last_raw_message_id": raw_after_id,
                    "last_movement_id": movement_after_id,
                }
            )
        )
        return 0

    require_webhook_url(args)

    synced = {}
    if raw_rows:
        synced["raw"] = sync_dataset(
            args,
            state,
            "last_raw_message_id",
            args.raw_sheet_name,
            RAW_HEADERS,
            raw_rows,
            "raw_message_id",
        )
    else:
        synced["raw"] = {"sheet": args.raw_sheet_name, "rows": 0, "last_raw_message_id": state["last_raw_message_id"], "status": "idle"}

    if flight_raw_rows:
        synced["flight_raw"] = sync_dataset(
            args,
            state,
            "last_movement_id",
            args.flight_raw_sheet_name,
            FLIGHT_RAW_HEADERS,
            flight_raw_rows,
            "movement_id",
        )
    else:
        synced["flight_raw"] = {
            "sheet": args.flight_raw_sheet_name,
            "rows": 0,
            "last_movement_id": state["last_movement_id"],
            "status": "idle",
        }

    total = synced["raw"]["rows"] + synced["flight_raw"]["rows"]
    print(json.dumps({"ok": True, "status": "synced", "rows": total, **synced}, ensure_ascii=False))
    return total


def main():
    parser = argparse.ArgumentParser(description="Sync WhatsApp bronze/silver datasets to Google Sheets")
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--state", default=DEFAULT_STATE)
    parser.add_argument("--webhook-url", default=DEFAULT_WEBHOOK_URL)
    parser.add_argument("--token", default=DEFAULT_TOKEN)
    parser.add_argument("--spreadsheet-id", default=DEFAULT_SPREADSHEET_ID)
    parser.add_argument("--raw-sheet-name", default=DEFAULT_RAW_SHEET_NAME)
    parser.add_argument("--flight-raw-sheet-name", default=DEFAULT_FLIGHT_RAW_SHEET_NAME)
    parser.add_argument("--flight-ops-sheet-name", default=DEFAULT_FLIGHT_OPS_SHEET_NAME)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--interval-seconds", type=int, default=15)
    parser.add_argument("--timeout-seconds", type=int, default=30)
    parser.add_argument("--from-raw-id", type=int, default=None)
    parser.add_argument("--from-movement-id", type=int, default=None)
    parser.add_argument("--from-id", type=int, default=None, help="Legacy alias for --from-movement-id")
    parser.add_argument("--skip-raw", action="store_true")
    parser.add_argument("--skip-flight-raw", action="store_true")
    parser.add_argument("--ensure-sheets", action="store_true")
    parser.add_argument("--delete-legacy-sheets", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.ensure_sheets:
        ensure_sheets(args)
        return

    if args.delete_legacy_sheets:
        delete_legacy_sheets(args)
        return

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
