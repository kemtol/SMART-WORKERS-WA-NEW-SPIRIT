#!/usr/bin/env python3
import argparse
import csv
import json
import os
import re
import sqlite3
import urllib.request
from io import StringIO

from google_sheets_sync import DEFAULT_SPREADSHEET_ID, DEFAULT_TOKEN, DEFAULT_WEBHOOK_URL, post_payload


DEFAULT_DB = os.environ.get("OPS_DB_PATH", "data/ops_messages.sqlite3")
DEFAULT_SOURCE_SPREADSHEET_ID = os.environ.get("MASTER_IATA_SOURCE_SPREADSHEET_ID", "1nLd3kkkSWJFCUjjR3kph7Wjsv0v_gN8VvXWptGmS5NE")
DEFAULT_SOURCE_GID = os.environ.get("MASTER_IATA_SOURCE_GID", "980038686")
DEFAULT_TARGET_SHEET_NAME = os.environ.get("GOOGLE_SHEETS_MASTER_IATA_TAB", "MASTER_IATA")
DEFAULT_OUTPUT = os.environ.get("OPS_INTERNAL_AIRPORT_MASTER_PATH", "data/reference/master_iata.json")

HEADERS = [
    "id",
    "code",
    "icao_code",
    "airport_name",
    "location",
    "province_id",
    "province_name",
    "timezone",
    "latitude_deg",
    "longitude_deg",
    "coordinate_source",
    "coordinate_confidence",
    "total_departure",
    "total_arrival",
    "total_flight",
    "is_hidden",
    "source",
    "source_row_number",
    "create_date",
    "create_user",
    "update_date",
    "update_user",
]

ACRONYMS = {"DKI", "DIY", "NTB", "NTT", "NAD"}


def compact_space(value):
    text = str(value or "").replace("\u00a0", " ").replace("\r", " ").replace("\n", " ")
    return re.sub(r"\s+", " ", text).strip()


def clean_code(value):
    text = compact_space(value).upper()
    return "" if text in {"", "-"} else text


def title_token(token):
    if token.upper() in ACRONYMS:
        return token.upper()
    if token.isupper() or token.islower():
        return token.capitalize()
    return token[0].upper() + token[1:] if token else token


def title_text(value):
    text = compact_space(value)
    if text in {"", "-"}:
        return ""
    text = text.lower()

    def repl(match):
        return title_token(match.group(0))

    return re.sub(r"[A-Za-zÀ-ÖØ-öø-ÿ]+", repl, text)


def clean_metadata(value):
    return compact_space(value)


def source_csv_url(spreadsheet_id, gid):
    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export?format=csv&gid={gid}"


def download_csv(url, timeout):
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8-sig")


def normalize_rows(csv_text):
    reader = csv.DictReader(StringIO(csv_text))
    rows = []
    for index, row in enumerate(reader, start=2):
        normalized = {
            "id": clean_metadata(row.get("id")),
            "code": clean_code(row.get("code")),
            "icao_code": clean_code(row.get("icao_code")),
            "airport_name": title_text(row.get("airport_name")),
            "location": title_text(row.get("location")),
            "province_id": clean_metadata(row.get("province_id")),
            "province_name": title_text(row.get("province_name")),
            "timezone": clean_metadata(row.get("timezone")),
            "latitude_deg": "",
            "longitude_deg": "",
            "coordinate_source": "",
            "coordinate_confidence": "",
            "total_departure": 0,
            "total_arrival": 0,
            "total_flight": 0,
            "is_hidden": clean_metadata(row.get("is_hidden") or "0"),
            "source": "master_iata_sheet",
            "source_row_number": index,
            "create_date": clean_metadata(row.get("create_date")),
            "create_user": clean_metadata(row.get("create_user")),
            "update_date": clean_metadata(row.get("update_date")),
            "update_user": clean_metadata(row.get("update_user")),
        }
        rows.append(normalized)
    return rows


def connect(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def movement_counts(db_path):
    if not os.path.exists(db_path):
        return {}, {}

    departures = {}
    arrivals = {}
    with connect(db_path) as conn:
        for row in conn.execute(
            """
            SELECT leg_origin_code AS code, COUNT(*) AS total
            FROM flight_movements
            WHERE movement_type = 'departure'
              AND takeoff_time IS NOT NULL
              AND takeoff_time != ''
              AND leg_origin_code IS NOT NULL
              AND leg_origin_code != ''
            GROUP BY leg_origin_code
            """
        ):
            departures[clean_code(row["code"])] = int(row["total"] or 0)

        for row in conn.execute(
            """
            SELECT arrival_airport_code AS code, COUNT(*) AS total
            FROM flight_movements
            WHERE movement_type = 'arrival'
              AND ata_time IS NOT NULL
              AND ata_time != ''
              AND arrival_airport_code IS NOT NULL
              AND arrival_airport_code != ''
            GROUP BY arrival_airport_code
            """
        ):
            arrivals[clean_code(row["code"])] = int(row["total"] or 0)

    return departures, arrivals


def apply_movement_counts(rows, departures, arrivals):
    for row in rows:
        code = clean_code(row.get("code"))
        total_departure = departures.get(code, 0)
        total_arrival = arrivals.get(code, 0)
        row["total_departure"] = total_departure
        row["total_arrival"] = total_arrival
        row["total_flight"] = total_departure + total_arrival


def save_json(path, rows):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(rows, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def post_rows(args, rows):
    if not args.webhook_url:
        raise RuntimeError("GOOGLE_SHEETS_WEBHOOK_URL is required")
    if not args.token:
        raise RuntimeError("GOOGLE_SHEETS_WEBHOOK_TOKEN is required")

    if args.replace:
        post_payload(
            args.webhook_url,
            {
                "token": args.token,
                "action": "deleteSheets",
                "deleteSheets": [args.sheet_name],
                "keepSheetName": "RAW",
            },
            args.timeout_seconds,
            args.spreadsheet_id,
        )

    post_payload(
        args.webhook_url,
        {
            "token": args.token,
            "action": "ensureSheets",
            "sheets": [{"name": args.sheet_name, "headers": HEADERS}],
        },
        args.timeout_seconds,
        args.spreadsheet_id,
    )

    appended = 0
    for start in range(0, len(rows), args.batch_size):
        batch = rows[start : start + args.batch_size]
        if not batch:
            continue
        result = post_payload(
            args.webhook_url,
            {
                "token": args.token,
                "sheetName": args.sheet_name,
                "headers": HEADERS,
                "rows": batch,
            },
            args.timeout_seconds,
            args.spreadsheet_id,
        )
        appended += int(result.get("appended") or len(batch))
    return appended


def main():
    parser = argparse.ArgumentParser(description="Create a normalized MASTER_IATA sheet from the source Google Sheet CSV")
    parser.add_argument("--source-spreadsheet-id", default=DEFAULT_SOURCE_SPREADSHEET_ID)
    parser.add_argument("--source-gid", default=DEFAULT_SOURCE_GID)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--sheet-name", default=DEFAULT_TARGET_SHEET_NAME)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--webhook-url", default=DEFAULT_WEBHOOK_URL)
    parser.add_argument("--token", default=DEFAULT_TOKEN)
    parser.add_argument("--spreadsheet-id", default=DEFAULT_SPREADSHEET_ID)
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument("--timeout-seconds", type=int, default=60)
    parser.add_argument("--replace", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    csv_text = download_csv(source_csv_url(args.source_spreadsheet_id, args.source_gid), args.timeout_seconds)
    rows = normalize_rows(csv_text)
    departures, arrivals = movement_counts(args.db)
    apply_movement_counts(rows, departures, arrivals)
    save_json(args.output, rows)

    appended = 0 if args.dry_run else post_rows(args, rows)
    print(
        json.dumps(
            {
                "ok": True,
                "source_spreadsheet_id": args.source_spreadsheet_id,
                "source_gid": args.source_gid,
                "sheet_name": args.sheet_name,
                "output": args.output,
                "rows": len(rows),
                "appended": appended,
                "total_departure_events": sum(departures.values()),
                "total_arrival_events": sum(arrivals.values()),
                "dry_run": args.dry_run,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
