#!/usr/bin/env python3
import argparse
import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


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
DEFAULT_FLIGHT_TIMELINE_SHEET_NAME = os.environ.get("GOOGLE_SHEETS_FLIGHT_TIMELINE_TAB", "FLIGHT_TIMELINE")
DEFAULT_WEBHOOK_URL = os.environ.get("GOOGLE_SHEETS_WEBHOOK_URL")
DEFAULT_TOKEN = os.environ.get("GOOGLE_SHEETS_WEBHOOK_TOKEN")
DEFAULT_SPREADSHEET_ID = os.environ.get("GOOGLE_SHEETS_SPREADSHEET_ID")
DEFAULT_OPERATION_TIMEZONE = os.environ.get("OPS_OPERATION_TIMEZONE", "Asia/Jakarta")

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
    "chronology_sort_key",
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
    "pic_name",
    "sic_name",
    "crew_text",
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
    "pic_name",
    "sic_name",
    "crew_text",
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

FLIGHT_TIMELINE_HEADERS = [
    "timeline_sort_key",
    "operation_date",
    "registration",
    "flight_seq",
    "timeline_kind",
    "movement_type",
    "leg_index",
    "event_datetime_local",
    "event_datetime_utc",
    "event_time",
    "event_time_source",
    "origin_code",
    "origin_name",
    "origin_icao",
    "origin_iata",
    "destination_code",
    "destination_name",
    "destination_icao",
    "destination_iata",
    "route_leg",
    "route_full",
    "next_route",
    "takeoff_time",
    "eta_time",
    "ata_time",
    "pic_name",
    "sic_name",
    "crew_text",
    "pax",
    "pax_weight_kg",
    "baggage_kg",
    "cargo_kg",
    "total_load_kg",
    "parse_confidence",
    "message_timestamp_iso",
    "raw_message_id",
    "movement_id",
]


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def operation_date_from_timestamp(timestamp_iso, timezone_name=DEFAULT_OPERATION_TIMEZONE):
    if not timestamp_iso:
        return None
    try:
        value = timestamp_iso.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(ZoneInfo(timezone_name)).date().isoformat()
    except (ValueError, TypeError, OSError):
        return timestamp_iso[:10] if len(timestamp_iso) >= 10 else None


def local_timestamp_from_timestamp(timestamp_iso, timezone_name=DEFAULT_OPERATION_TIMEZONE):
    if not timestamp_iso:
        return None
    try:
        value = timestamp_iso.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(ZoneInfo(timezone_name)).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError, OSError):
        return timestamp_iso


def parse_timestamp_utc(timestamp_iso):
    if not timestamp_iso:
        return None
    try:
        parsed = datetime.fromisoformat(timestamp_iso.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def event_datetime_utc_from_time(timestamp_iso, event_time):
    anchor = parse_timestamp_utc(timestamp_iso)
    if not anchor or not event_time:
        return None
    try:
        hour_text, minute_text = str(event_time).split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except (ValueError, TypeError):
        return None
    if hour > 23 or minute > 59:
        return None

    candidates = []
    for day_offset in (-1, 0, 1):
        day = (anchor + timedelta(days=day_offset)).date()
        candidates.append(datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc))
    return min(candidates, key=lambda value: abs((value - anchor).total_seconds()))


def format_datetime_utc(value):
    return value.strftime("%Y-%m-%d %H:%MZ") if value else None


def format_datetime_local(value, timezone_name=DEFAULT_OPERATION_TIMEZONE, include_seconds=False):
    if not value:
        return None
    fmt = "%Y-%m-%d %H:%M:%S" if include_seconds else "%Y-%m-%d %H:%M"
    return value.astimezone(ZoneInfo(timezone_name)).strftime(fmt)


def first_value(*values):
    for value in values:
        if value not in (None, ""):
            return value
    return None


def message_time(timestamp_iso):
    if not timestamp_iso:
        return None
    return timestamp_iso[11:16] if len(timestamp_iso) >= 16 else None


def numeric_text(value, width):
    try:
        return f"{int(value):0{width}d}"
    except (TypeError, ValueError):
        return "9" * width


def operation_date_for_row(row):
    return first_value(row["operation_date"], operation_date_from_timestamp(row["message_timestamp_iso"]))


def chronology_sort_key(row, operation_date=None):
    return "|".join(
        [
            operation_date or "9999-99-99",
            row["registration"] or "",
            numeric_text(row["flight_seq"], 3),
            row["message_timestamp_iso"] or "9999-99-99T99:99:99Z",
            numeric_text(row["movement_id"], 8),
            numeric_text(row["leg_index"], 3),
        ]
    )


def timeline_sort_key(row, event_datetime_utc=None):
    event_sort_time = format_datetime_local(event_datetime_utc, include_seconds=True)
    return "|".join(
        [
            event_sort_time or local_timestamp_from_timestamp(row["message_timestamp_iso"]) or "9999-99-99 99:99:99",
            row["registration"] or "",
            numeric_text(row["movement_id"], 8),
            numeric_text(row["leg_index"], 3),
        ]
    )


def load_state(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
    except FileNotFoundError:
        state = {}
    return {
        "last_raw_message_id": int(state.get("last_raw_message_id", 0) or 0),
        "last_movement_id": int(state.get("last_movement_id", 0) or 0),
        "last_timeline_movement_id": int(state.get("last_timeline_movement_id", 0) or 0),
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


def get_max_movement_id(db_path):
    with connect(db_path) as conn:
        row = conn.execute("SELECT MAX(id) AS max_id FROM flight_movements").fetchone()
    return int(row["max_id"] or 0)


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
              rm.text AS source_text,
              fm.pic_name,
              fm.sic_name,
              fm.crew_text
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
        operation_date = operation_date_for_row(row)
        item["operation_date"] = operation_date
        item["chronology_sort_key"] = chronology_sort_key(row, operation_date)
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


def timeline_row_from_movement(row):
    movement_type = row["movement_type"]
    operation_date = operation_date_for_row(row)

    if movement_type == "arrival":
        timeline_kind = "actual_arrival"
        event_time = first_value(row["ata_time"], message_time(row["message_timestamp_iso"]))
        event_time_source = "ata_time" if row["ata_time"] else "message_timestamp"
        origin_code = first_value(row["from_code"], row["leg_origin_code"])
        origin_name = first_value(row["from_name"], row["from_place"], row["leg_origin_name"])
        origin_icao = first_value(row["from_icao"], row["leg_origin_icao"])
        origin_iata = first_value(row["from_iata"], row["leg_origin_iata"])
        destination_code = first_value(row["arrival_airport_code"], row["leg_destination_code"])
        destination_name = first_value(row["arrival_airport_name"], row["leg_destination_name"])
        destination_icao = first_value(row["arrival_airport_icao"], row["leg_destination_icao"])
        destination_iata = first_value(row["arrival_airport_iata"], row["leg_destination_iata"])
    else:
        timeline_kind = "actual_departure"
        event_time = row["takeoff_time"]
        event_time_source = "takeoff_time"
        origin_code = row["leg_origin_code"]
        origin_name = row["leg_origin_name"]
        origin_icao = row["leg_origin_icao"]
        origin_iata = row["leg_origin_iata"]
        destination_code = row["leg_destination_code"]
        destination_name = row["leg_destination_name"]
        destination_icao = row["leg_destination_icao"]
        destination_iata = row["leg_destination_iata"]

    origin_display = first_value(origin_code, origin_name, "")
    destination_display = first_value(destination_code, destination_name, "")
    route_leg = f"{origin_display}-{destination_display}" if origin_display or destination_display else None
    event_datetime_utc = event_datetime_utc_from_time(row["message_timestamp_iso"], event_time)
    sort_key = timeline_sort_key(row, event_datetime_utc)

    item = {
        "timeline_sort_key": sort_key,
        "operation_date": operation_date,
        "registration": row["registration"],
        "flight_seq": row["flight_seq"],
        "timeline_kind": timeline_kind,
        "movement_type": movement_type,
        "leg_index": row["leg_index"],
        "event_datetime_local": format_datetime_local(event_datetime_utc),
        "event_datetime_utc": format_datetime_utc(event_datetime_utc),
        "event_time": event_time,
        "event_time_source": event_time_source,
        "origin_code": origin_code,
        "origin_name": origin_name,
        "origin_icao": origin_icao,
        "origin_iata": origin_iata,
        "destination_code": destination_code,
        "destination_name": destination_name,
        "destination_icao": destination_icao,
        "destination_iata": destination_iata,
        "route_leg": route_leg,
        "route_full": row["route_full"],
        "next_route": row["next_route"],
        "takeoff_time": row["takeoff_time"],
        "eta_time": row["eta_time"],
        "ata_time": row["ata_time"],
        "pic_name": row["pic_name"],
        "sic_name": row["sic_name"],
        "crew_text": row["crew_text"],
        "pax": row["pax"],
        "pax_weight_kg": row["pax_weight_kg"],
        "baggage_kg": row["baggage_kg"],
        "cargo_kg": row["cargo_kg"],
        "total_load_kg": row["total_load_kg"],
        "parse_confidence": row["parse_confidence"],
        "message_timestamp_iso": row["message_timestamp_iso"],
        "raw_message_id": row["raw_message_id"],
        "movement_id": row["movement_id"],
    }
    return {key: item.get(key) for key in FLIGHT_TIMELINE_HEADERS}


def get_flight_timeline_rows(db_path, after_id=0, limit=None, sort_by_timeline=True):
    with connect(db_path) as conn:
        limit_clause = "" if limit is None else "LIMIT ?"
        params = (after_id,) if limit is None else (after_id, limit)
        rows = conn.execute(
            f"""
            SELECT
              fm.id AS movement_id,
              fm.raw_message_id,
              rm.message_timestamp_iso,
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
              fm.takeoff_time,
              fm.eta_time,
              fm.ata_time,
              fm.pic_name,
              fm.sic_name,
              fm.crew_text,
              fm.pax,
              fm.pax_weight_kg,
              fm.baggage_kg,
              fm.cargo_kg,
              fm.total_load_kg,
              fm.parse_confidence
            FROM flight_movements fm
            JOIN raw_messages rm ON rm.id = fm.raw_message_id
            WHERE fm.registration IS NOT NULL
              AND fm.id > ?
              AND (
                (fm.movement_type = 'departure' AND fm.takeoff_time IS NOT NULL AND fm.takeoff_time != '')
                OR (fm.movement_type = 'arrival' AND fm.ata_time IS NOT NULL AND fm.ata_time != '')
              )
            ORDER BY fm.id ASC
            {limit_clause}
            """,
            params,
        ).fetchall()

    result = [timeline_row_from_movement(row) for row in rows]
    if sort_by_timeline:
        return sorted(result, key=lambda item: item["timeline_sort_key"])
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
                {"name": args.flight_timeline_sheet_name, "headers": FLIGHT_TIMELINE_HEADERS},
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


def replace_flight_raw_sheet(args):
    require_webhook_url(args)
    deleted = post_payload(
        args.webhook_url,
        {
            "token": args.token,
            "action": "deleteSheets",
            "deleteSheets": [args.flight_raw_sheet_name],
            "keepSheetName": args.raw_sheet_name,
        },
        args.timeout_seconds,
        args.spreadsheet_id,
    )
    ensured = post_payload(
        args.webhook_url,
        {
            "token": args.token,
            "action": "ensureSheets",
            "sheets": [
                {"name": args.flight_raw_sheet_name, "headers": FLIGHT_RAW_HEADERS},
            ],
        },
        args.timeout_seconds,
        args.spreadsheet_id,
    )

    state = load_state(args.state)
    state["last_movement_id"] = 0
    save_state(args.state, state)

    total = 0
    while True:
        state = load_state(args.state)
        rows = get_flight_raw_rows(args.db, state["last_movement_id"], args.batch_size)
        if not rows:
            break
        synced = sync_dataset(
            args,
            state,
            "last_movement_id",
            args.flight_raw_sheet_name,
            FLIGHT_RAW_HEADERS,
            rows,
            "movement_id",
        )
        total += synced["rows"]
        if synced["rows"] < args.batch_size:
            break

    print(
        json.dumps(
            {
                "ok": True,
                "status": "flight_raw_replaced",
                "rows": total,
                "deleted": deleted,
                "ensured": ensured,
                "last_movement_id": load_state(args.state)["last_movement_id"],
            },
            ensure_ascii=False,
        )
    )


def replace_flight_timeline_sheet(args):
    require_webhook_url(args)
    deleted = post_payload(
        args.webhook_url,
        {
            "token": args.token,
            "action": "deleteSheets",
            "deleteSheets": [args.flight_timeline_sheet_name],
            "keepSheetName": args.raw_sheet_name,
        },
        args.timeout_seconds,
        args.spreadsheet_id,
    )
    ensured = post_payload(
        args.webhook_url,
        {
            "token": args.token,
            "action": "ensureSheets",
            "sheets": [
                {"name": args.flight_timeline_sheet_name, "headers": FLIGHT_TIMELINE_HEADERS},
            ],
        },
        args.timeout_seconds,
        args.spreadsheet_id,
    )

    rows = get_flight_timeline_rows(args.db)
    total = 0
    for start in range(0, len(rows), args.batch_size):
        batch = rows[start : start + args.batch_size]
        if not batch:
            break
        result = post_rows(
            args.webhook_url,
            args.token,
            args.spreadsheet_id,
            args.flight_timeline_sheet_name,
            FLIGHT_TIMELINE_HEADERS,
            batch,
            args.timeout_seconds,
        )
        total += int(result.get("appended") or len(batch))

    state = load_state(args.state)
    state["last_timeline_movement_id"] = get_max_movement_id(args.db)
    save_state(args.state, state)

    print(
        json.dumps(
            {
                "ok": True,
                "status": "flight_timeline_replaced",
                "rows": total,
                "deleted": deleted,
                "ensured": ensured,
                "last_timeline_movement_id": state["last_timeline_movement_id"],
            },
            ensure_ascii=False,
        )
    )


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
    timeline_after_id = int(
        args.from_timeline_movement_id
        if args.from_timeline_movement_id is not None
        else state["last_timeline_movement_id"]
    )

    raw_rows = [] if args.skip_raw else get_raw_rows(args.db, raw_after_id, args.batch_size)
    flight_raw_rows = [] if args.skip_flight_raw else get_flight_raw_rows(args.db, movement_after_id, args.batch_size)
    flight_timeline_rows = (
        []
        if args.skip_flight_timeline
        else get_flight_timeline_rows(args.db, timeline_after_id, args.batch_size, sort_by_timeline=False)
    )

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
                    "flight_timeline": {
                        "sheet": args.flight_timeline_sheet_name,
                        "rows": len(flight_timeline_rows),
                        "first": flight_timeline_rows[0] if flight_timeline_rows else None,
                        "last": flight_timeline_rows[-1] if flight_timeline_rows else None,
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return len(raw_rows) + len(flight_raw_rows) + len(flight_timeline_rows)

    if (
        not args.skip_flight_timeline
        and not flight_timeline_rows
        and args.from_timeline_movement_id is None
    ):
        max_movement_id = get_max_movement_id(args.db)
        if max_movement_id > state["last_timeline_movement_id"]:
            state["last_timeline_movement_id"] = max_movement_id
            save_state(args.state, state)
            timeline_after_id = max_movement_id

    if not raw_rows and not flight_raw_rows and not flight_timeline_rows:
        print(
            json.dumps(
                {
                    "ok": True,
                    "status": "idle",
                    "last_raw_message_id": raw_after_id,
                    "last_movement_id": movement_after_id,
                    "last_timeline_movement_id": timeline_after_id,
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

    if flight_timeline_rows:
        synced["flight_timeline"] = sync_dataset(
            args,
            state,
            "last_timeline_movement_id",
            args.flight_timeline_sheet_name,
            FLIGHT_TIMELINE_HEADERS,
            flight_timeline_rows,
            "movement_id",
        )
    else:
        synced["flight_timeline"] = {
            "sheet": args.flight_timeline_sheet_name,
            "rows": 0,
            "last_timeline_movement_id": state["last_timeline_movement_id"],
            "status": "idle",
        }

    total = synced["raw"]["rows"] + synced["flight_raw"]["rows"] + synced["flight_timeline"]["rows"]
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
    parser.add_argument("--flight-timeline-sheet-name", default=DEFAULT_FLIGHT_TIMELINE_SHEET_NAME)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--interval-seconds", type=int, default=15)
    parser.add_argument("--timeout-seconds", type=int, default=30)
    parser.add_argument("--from-raw-id", type=int, default=None)
    parser.add_argument("--from-movement-id", type=int, default=None)
    parser.add_argument("--from-timeline-movement-id", type=int, default=None)
    parser.add_argument("--from-id", type=int, default=None, help="Legacy alias for --from-movement-id")
    parser.add_argument("--skip-raw", action="store_true")
    parser.add_argument("--skip-flight-raw", action="store_true")
    parser.add_argument("--skip-flight-timeline", action="store_true")
    parser.add_argument("--ensure-sheets", action="store_true")
    parser.add_argument("--delete-legacy-sheets", action="store_true")
    parser.add_argument("--replace-flight-raw", action="store_true")
    parser.add_argument("--replace-flight-timeline", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.ensure_sheets:
        ensure_sheets(args)
        return

    if args.delete_legacy_sheets:
        delete_legacy_sheets(args)
        return

    if args.replace_flight_raw:
        replace_flight_raw_sheet(args)
        return

    if args.replace_flight_timeline:
        replace_flight_timeline_sheet(args)
        return

    if args.once or args.dry_run:
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
