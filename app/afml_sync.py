#!/usr/bin/env python3
import argparse
import base64
import fcntl
import gzip
import hashlib
import http.cookiejar
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

from google_sheets_sync import (
    DEFAULT_OPERATION_TIMEZONE,
    DEFAULT_SPREADSHEET_ID,
    DEFAULT_TOKEN,
    DEFAULT_WEBHOOK_URL,
    post_payload,
)


DEFAULT_BASE_URL = os.environ.get("AMS_BASE_URL", "https://ams.smartaviation.co.id").rstrip("/")
DEFAULT_DB = os.environ.get("OPS_DB_PATH", "data/ops_messages.sqlite3")
DEFAULT_LOOKBACK_DAYS = int(os.environ.get("AFML_LOOKBACK_DAYS", "14"))
DEFAULT_TIMEOUT_SECONDS = int(os.environ.get("AFML_HTTP_TIMEOUT_SECONDS", "30"))
DEFAULT_BATCH_SIZE = int(os.environ.get("AFML_SHEETS_BATCH_SIZE", "25"))
DEFAULT_RAW_SHEET = os.environ.get("GOOGLE_SHEETS_AFML_RAW_TAB", "AFML_RAW")
DEFAULT_LEGS_SHEET = os.environ.get("GOOGLE_SHEETS_AFML_LEGS_TAB", "AFML_LEGS")
DEFAULT_RECON_SHEET = os.environ.get("GOOGLE_SHEETS_AFML_RECON_TAB", "AFML_RECON")
DEFAULT_LOCK = os.environ.get("OPS_AFML_LOCK_PATH", "data/afml-sync.lock")
DEFAULT_STATE = os.environ.get("OPS_AFML_STATE_PATH", "data/afml-sync-state.json")
DEFAULT_PILOT_MAPPING = os.environ.get("OPS_PILOT_MAPPING_PATH", "data/reference/mapping_pilot.json")

AFML_RAW_HEADERS = [
    "afml_id",
    "page_no",
    "operation_date",
    "registration",
    "msn",
    "aircraft_type",
    "captain_name",
    "copilot_name",
    "list_pilot_name",
    "check_in_time",
    "check_out_time",
    "duty_time_text",
    "total_flight_time",
    "total_flight_minutes",
    "total_block_time",
    "total_block_minutes",
    "landing_cycles",
    "engine_cycles",
    "leg_count",
    "route_chain",
    "created_by",
    "created_date",
    "flight_type",
    "from_efb",
    "detail_status",
    "detail_hash",
    "source_url",
    "first_seen_at",
    "last_seen_at",
    "last_changed_at",
]

AFML_LEG_HEADERS = [
    "afml_leg_id",
    "afml_id",
    "page_no",
    "operation_date",
    "registration",
    "leg_index",
    "origin_code",
    "destination_code",
    "route_leg",
    "block_off_time",
    "block_on_time",
    "block_minutes",
    "takeoff_time",
    "landing_time",
    "flight_minutes",
    "landing_cycles",
    "engine_cycles",
    "fuel_remaining",
    "fuel_uplift",
    "fuel_total",
    "receipt_no",
    "oil_added",
    "hydraulic_added",
]

AFML_RECON_HEADERS = [
    "reconciliation_id",
    "operation_date",
    "registration",
    "flight_seq",
    "route_full",
    "wa_takeoff_time",
    "wa_pic_raw",
    "wa_pic_full",
    "wa_sic_raw",
    "wa_sic_full",
    "departure_raw_message_id",
    "departure_movement_id",
    "afml_id",
    "afml_page_no",
    "afml_leg_start",
    "afml_leg_end",
    "afml_route",
    "afml_block_off_time",
    "afml_block_on_time",
    "afml_takeoff_time",
    "afml_landing_time",
    "afml_block_minutes",
    "afml_flight_minutes",
    "afml_captain_name",
    "afml_copilot_name",
    "time_delta_minutes",
    "route_match",
    "pic_match",
    "sic_match",
    "match_status",
    "quality_score",
    "issue_notes",
    "updated_at",
]


def load_env_file(path):
    path = Path(path)
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def save_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def normalize_text(value):
    return re.sub(r"[ \t\r\f\v]+", " ", str(value or "")).strip()


def normalize_name(value):
    value = re.sub(r"\b(CAPT(?:AIN)?|PIC|FO|F/O|COPIL(?:OT)?|SIC)\b\.?", " ", str(value or "").upper())
    return re.sub(r"[^A-Z0-9]+", "", value)


def normalize_code(value):
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def parse_date(value):
    value = normalize_text(value)
    for fmt in ("%d/%m/%Y", "%d %b %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_hhmm_minutes(value):
    match = re.fullmatch(r"\s*(\d{1,3}):(\d{2})\s*", str(value or ""))
    if not match:
        return None
    return int(match.group(1)) * 60 + int(match.group(2))


def format_minutes(value):
    if value is None:
        return None
    return f"{int(value) // 60:02d}:{int(value) % 60:02d}"


def clock_minutes(value):
    parsed = parse_hhmm_minutes(value)
    if parsed is None:
        return None
    return parsed % (24 * 60)


def circular_time_delta(first, second):
    a = clock_minutes(first)
    b = clock_minutes(second)
    if a is None or b is None:
        return None
    delta = abs(a - b)
    return min(delta, 24 * 60 - delta)


class TableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.table_depth = 0
        self.current_row = None
        self.current_cell = None
        self.rows = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "table":
            self.table_depth += 1
        elif tag == "tr" and self.table_depth:
            self.current_row = {"cells": [], "hrefs": []}
        elif tag in ("td", "th") and self.current_row is not None:
            self.current_cell = []
        elif tag == "br" and self.current_cell is not None:
            self.current_cell.append("\n")
        elif tag == "a" and self.current_row is not None and attrs.get("href"):
            self.current_row["hrefs"].append(attrs["href"])

    def handle_data(self, data):
        if self.current_cell is not None:
            self.current_cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.current_cell is not None and self.current_row is not None:
            raw = "".join(self.current_cell)
            lines = [normalize_text(line) for line in raw.splitlines()]
            self.current_row["cells"].append("\n".join(line for line in lines if line))
            self.current_cell = None
        elif tag == "tr" and self.current_row is not None:
            if self.current_row["cells"]:
                self.rows.append(self.current_row)
            self.current_row = None
            self.current_cell = None
        elif tag == "table" and self.table_depth:
            self.table_depth -= 1


def parse_tables(html):
    parser = TableParser()
    parser.feed(html)
    return parser.rows


def decode_afml_id(encoded):
    padded = encoded + "=" * (-len(encoded) % 4)
    decoded = base64.b64decode(padded).decode("ascii")
    if not decoded.isdigit():
        raise ValueError(f"invalid AFML identifier: {encoded!r}")
    return int(decoded)


def encode_afml_id(afml_id):
    return base64.b64encode(str(int(afml_id)).encode("ascii")).decode("ascii")


def parse_afml_list_response(payload):
    if isinstance(payload, str):
        payload = json.loads(payload)
    if int(payload.get("result") or 0) != 1:
        raise RuntimeError(f"AFML list rejected request: {payload}")
    records = []
    for row in parse_tables(payload.get("div") or ""):
        detail_url = next((url for url in row["hrefs"] if "/AFML/detail/" in url), None)
        cells = row["cells"]
        if not detail_url or len(cells) < 9:
            continue
        encoded_id = detail_url.rstrip("/").split("/")[-1]
        created_parts = [part.strip() for part in cells[6].splitlines() if part.strip()]
        records.append(
            {
                "afml_id": decode_afml_id(encoded_id),
                "encoded_id": encoded_id,
                "page_no": cells[0],
                "list_pilot_name": cells[1],
                "operation_date": parse_date(cells[2]),
                "registration": cells[3].upper(),
                "total_flight_time": cells[4],
                "total_flight_minutes": parse_hhmm_minutes(cells[4]),
                "total_block_time": cells[5],
                "total_block_minutes": parse_hhmm_minutes(cells[5]),
                "created_by": created_parts[0] if created_parts else None,
                "created_date": parse_date(created_parts[1]) if len(created_parts) > 1 else None,
                "flight_type": cells[7] or None,
                "from_efb": cells[8] or None,
                "source_url": detail_url,
            }
        )
    return records


def value_after_label(cell, label):
    match = re.match(rf"\s*{re.escape(label)}\s*:\s*(.*)$", cell or "", flags=re.IGNORECASE)
    return normalize_text(match.group(1)) if match else None


def numeric(value):
    value = normalize_text(value)
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", value):
        return None
    number = float(value)
    return int(number) if number.is_integer() else number


def parse_afml_detail(html, afml_id=None, source_url=None):
    rows = parse_tables(html)
    detail = {
        "afml_id": int(afml_id) if afml_id is not None else None,
        "source_url": source_url,
        "legs": [],
        "detail_status": "parsed",
    }
    total_block_minutes = None
    total_flight_minutes = None
    total_landing_cycles = None
    total_engine_cycles = None

    for row in rows:
        cells = row["cells"]
        if len(cells) >= 5 and any(cell.upper().startswith("A/C REG") for cell in cells):
            labels = {
                "DATE": "operation_date",
                "A/C REG": "registration",
                "MSN": "msn",
                "TYPE": "aircraft_type",
                "PAGE": "page_no",
            }
            for cell in cells:
                for label, field in labels.items():
                    value = value_after_label(cell, label)
                    if value is not None:
                        detail[field] = parse_date(value) if field == "operation_date" else value
                        break
        elif len(cells) == 1:
            for label, field in (
                ("CAPT", "captain_name"),
                ("COPIL", "copilot_name"),
                ("EOB", "eob_name"),
                ("Flight Scientist 1", "flight_scientist_1"),
                ("Flight Scientist 2", "flight_scientist_2"),
            ):
                value = value_after_label(cells[0], label)
                if value is not None:
                    detail[field] = value or None
                    break
        if len(cells) == 3 and cells[0].upper().startswith("CHECK IN"):
            detail["check_in_time"] = value_after_label(cells[0], "Check In") or None
            detail["check_out_time"] = value_after_label(cells[1], "Check Out") or None
            detail["duty_time_text"] = value_after_label(cells[2], "Duty TIme") or None

        if len(cells) >= 18:
            origin = normalize_code(cells[0])
            destination = normalize_code(cells[1])
            times_valid = all(re.fullmatch(r"\d{1,2}:\d{2}", cells[index] or "") for index in (2, 3, 6, 7))
            if origin and destination and origin != "FROM" and times_valid:
                block_minutes = (numeric(cells[4]) or 0) * 60 + (numeric(cells[5]) or 0)
                flight_minutes = (numeric(cells[8]) or 0) * 60 + (numeric(cells[9]) or 0)
                detail["legs"].append(
                    {
                        "leg_index": len(detail["legs"]) + 1,
                        "origin_code": origin,
                        "destination_code": destination,
                        "block_off_time": cells[2],
                        "block_on_time": cells[3],
                        "block_minutes": int(block_minutes),
                        "takeoff_time": cells[6],
                        "landing_time": cells[7],
                        "flight_minutes": int(flight_minutes),
                        "landing_cycles": numeric(cells[10]),
                        "engine_cycles": numeric(cells[11]),
                        "fuel_remaining": numeric(cells[12]),
                        "fuel_uplift": numeric(cells[13]),
                        "fuel_total": numeric(cells[14]),
                        "receipt_no": cells[15] or None,
                        "oil_added": numeric(cells[16]),
                        "hydraulic_added": numeric(cells[17]),
                    }
                )

        if cells and cells[0].upper().startswith("TTL BLOCK TIME") and len(cells) >= 8:
            total_block_minutes = int((numeric(cells[1]) or 0) * 60 + (numeric(cells[2]) or 0))
            total_flight_minutes = int((numeric(cells[4]) or 0) * 60 + (numeric(cells[5]) or 0))
            total_landing_cycles = numeric(cells[6])
            total_engine_cycles = numeric(cells[7])

    detail["registration"] = (detail.get("registration") or "").upper() or None
    detail["total_block_minutes"] = total_block_minutes
    detail["total_block_time"] = format_minutes(total_block_minutes)
    detail["total_flight_minutes"] = total_flight_minutes
    detail["total_flight_time"] = format_minutes(total_flight_minutes)
    detail["landing_cycles"] = total_landing_cycles
    detail["engine_cycles"] = total_engine_cycles
    detail["leg_count"] = len(detail["legs"])
    detail["route_chain"] = route_chain(detail["legs"])
    if not detail.get("page_no") or not detail.get("operation_date") or not detail.get("registration"):
        detail["detail_status"] = "parse_incomplete"
    return detail


def route_chain(legs):
    usable = [leg for leg in legs if leg.get("origin_code") != "-" and leg.get("destination_code") != "-"]
    if not usable:
        return None
    parts = [usable[0]["origin_code"]]
    for leg in usable:
        if parts[-1] != leg["origin_code"]:
            parts.append(leg["origin_code"])
        parts.append(leg["destination_code"])
    return "-".join(parts)


class AfmlClient:
    def __init__(self, base_url, username, password, timeout=DEFAULT_TIMEOUT_SECONDS):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))
        self.opener.addheaders = [("User-Agent", "SMART-New-Spirit-AFML-ReadOnly/1.0")]

    def open(self, path, data=None):
        url = path if path.startswith("http") else self.base_url + path
        encoded = urllib.parse.urlencode(data or {}).encode("utf-8") if data is not None else None
        request = urllib.request.Request(url, data=encoded)
        with self.opener.open(request, timeout=self.timeout) as response:
            return response.read().decode("utf-8", errors="replace"), response.geturl()

    def login(self):
        body, final_url = self.open(
            "/v1/index/signin",
            {"username": self.username, "password": self.password},
        )
        if "SMART Aviation - Login" in body or "formLogin" in body:
            raise RuntimeError("AMS authentication failed")
        probe, probe_url = self.open("/v1/AFML")
        if "SMART Aviation - AFML" not in probe or "/v1/AFML" not in probe_url:
            raise RuntimeError("AMS authentication did not create an AFML session")
        return True

    def list_records(self, from_date, to_date, max_pages=100):
        result = []
        offset = 0
        form = {
            "filterOffset": "0",
            "filterSearch": "",
            "filterOption": "lock",
            "filterLock": "ALL",
            "filterDateBegin": datetime.strptime(from_date, "%Y-%m-%d").strftime("%d/%m/%Y"),
            "filterDateEnd": datetime.strptime(to_date, "%Y-%m-%d").strftime("%d/%m/%Y"),
        }
        for _ in range(max_pages):
            body, _ = self.open(f"/v1/AFML/afmlAjax/{offset}", form)
            try:
                page = parse_afml_list_response(body)
            except (json.JSONDecodeError, RuntimeError) as exc:
                raise RuntimeError("AMS AFML list returned an invalid or expired-session response") from exc
            result.extend(page)
            if len(page) < 10:
                break
            offset += 10
        else:
            raise RuntimeError(f"AFML pagination exceeded safety limit ({max_pages} pages)")
        unique = {row["afml_id"]: row for row in result}
        return sorted(unique.values(), key=lambda row: (row["operation_date"] or "", row["afml_id"]))

    def detail(self, afml_id):
        encoded_id = encode_afml_id(afml_id)
        path = f"/v1/AFML/detail/{urllib.parse.quote(encoded_id, safe='=')}"
        body, final_url = self.open(path)
        if "SMART Aviation - Login" in body or "AFML Detail" not in body:
            raise RuntimeError(f"AMS AFML detail unavailable for {afml_id}")
        return body, final_url


def init_schema(conn):
    conn.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE IF NOT EXISTS afml_records (
            afml_id INTEGER PRIMARY KEY,
            encoded_id TEXT NOT NULL,
            page_no TEXT,
            operation_date TEXT,
            registration TEXT,
            msn TEXT,
            aircraft_type TEXT,
            captain_name TEXT,
            copilot_name TEXT,
            eob_name TEXT,
            flight_scientist_1 TEXT,
            flight_scientist_2 TEXT,
            list_pilot_name TEXT,
            check_in_time TEXT,
            check_out_time TEXT,
            duty_time_text TEXT,
            total_flight_time TEXT,
            total_flight_minutes INTEGER,
            total_block_time TEXT,
            total_block_minutes INTEGER,
            landing_cycles INTEGER,
            engine_cycles INTEGER,
            leg_count INTEGER NOT NULL DEFAULT 0,
            route_chain TEXT,
            created_by TEXT,
            created_date TEXT,
            flight_type TEXT,
            from_efb TEXT,
            detail_status TEXT NOT NULL,
            list_hash TEXT NOT NULL,
            detail_hash TEXT,
            detail_fetched_at TEXT,
            source_url TEXT NOT NULL,
            raw_list_json TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            last_changed_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_afml_records_date_reg
            ON afml_records(operation_date, registration);

        CREATE TABLE IF NOT EXISTS afml_legs (
            afml_id INTEGER NOT NULL REFERENCES afml_records(afml_id) ON DELETE CASCADE,
            leg_index INTEGER NOT NULL,
            origin_code TEXT,
            destination_code TEXT,
            block_off_time TEXT,
            block_on_time TEXT,
            block_minutes INTEGER,
            takeoff_time TEXT,
            landing_time TEXT,
            flight_minutes INTEGER,
            landing_cycles INTEGER,
            engine_cycles INTEGER,
            fuel_remaining REAL,
            fuel_uplift REAL,
            fuel_total REAL,
            receipt_no TEXT,
            oil_added REAL,
            hydraulic_added REAL,
            PRIMARY KEY (afml_id, leg_index)
        );

        CREATE TABLE IF NOT EXISTS afml_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            afml_id INTEGER NOT NULL REFERENCES afml_records(afml_id) ON DELETE CASCADE,
            fetched_at TEXT NOT NULL,
            detail_hash TEXT NOT NULL,
            raw_html_gzip BLOB NOT NULL,
            UNIQUE (afml_id, detail_hash)
        );

        CREATE TABLE IF NOT EXISTS afml_reconciliation (
            reconciliation_id TEXT PRIMARY KEY,
            operation_date TEXT NOT NULL,
            registration TEXT NOT NULL,
            flight_seq TEXT,
            route_full TEXT,
            wa_takeoff_time TEXT,
            wa_pic_raw TEXT,
            wa_pic_full TEXT,
            wa_sic_raw TEXT,
            wa_sic_full TEXT,
            departure_raw_message_id INTEGER,
            departure_movement_id INTEGER,
            afml_id INTEGER,
            afml_page_no TEXT,
            afml_leg_start INTEGER,
            afml_leg_end INTEGER,
            afml_route TEXT,
            afml_block_off_time TEXT,
            afml_block_on_time TEXT,
            afml_takeoff_time TEXT,
            afml_landing_time TEXT,
            afml_block_minutes INTEGER,
            afml_flight_minutes INTEGER,
            afml_captain_name TEXT,
            afml_copilot_name TEXT,
            time_delta_minutes INTEGER,
            route_match TEXT,
            pic_match TEXT,
            sic_match TEXT,
            match_status TEXT NOT NULL,
            quality_score REAL NOT NULL,
            issue_notes TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_afml_recon_date_reg
            ON afml_reconciliation(operation_date, registration);
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(afml_records)")}
    if "detail_fetched_at" not in columns:
        conn.execute("ALTER TABLE afml_records ADD COLUMN detail_fetched_at TEXT")
    conn.execute(
        "UPDATE afml_records SET detail_fetched_at = last_seen_at WHERE detail_fetched_at IS NULL"
    )


def stable_hash(payload):
    if isinstance(payload, str):
        encoded = payload.encode("utf-8")
    else:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def save_record(conn, list_record, detail, raw_html, seen_at):
    afml_id = list_record["afml_id"]
    existing = conn.execute(
        "SELECT first_seen_at, detail_hash FROM afml_records WHERE afml_id = ?", (afml_id,)
    ).fetchone()
    list_hash = stable_hash(list_record)
    # Hash parsed operational content so rotating session/UI markup does not look like an AFML revision.
    detail_hash = stable_hash(detail)
    first_seen = existing["first_seen_at"] if existing else seen_at
    last_changed = seen_at if not existing or existing["detail_hash"] != detail_hash else conn.execute(
        "SELECT last_changed_at FROM afml_records WHERE afml_id = ?", (afml_id,)
    ).fetchone()[0]
    merged = dict(list_record)
    merged.update({key: value for key, value in detail.items() if key != "legs" and value is not None})
    conn.execute(
        """
        INSERT INTO afml_records (
            afml_id, encoded_id, page_no, operation_date, registration, msn, aircraft_type,
            captain_name, copilot_name, eob_name, flight_scientist_1, flight_scientist_2,
            list_pilot_name, check_in_time, check_out_time, duty_time_text,
            total_flight_time, total_flight_minutes, total_block_time, total_block_minutes,
            landing_cycles, engine_cycles, leg_count, route_chain, created_by, created_date,
            flight_type, from_efb, detail_status, list_hash, detail_hash, detail_fetched_at, source_url,
            raw_list_json, first_seen_at, last_seen_at, last_changed_at
        ) VALUES (
            :afml_id, :encoded_id, :page_no, :operation_date, :registration, :msn, :aircraft_type,
            :captain_name, :copilot_name, :eob_name, :flight_scientist_1, :flight_scientist_2,
            :list_pilot_name, :check_in_time, :check_out_time, :duty_time_text,
            :total_flight_time, :total_flight_minutes, :total_block_time, :total_block_minutes,
            :landing_cycles, :engine_cycles, :leg_count, :route_chain, :created_by, :created_date,
            :flight_type, :from_efb, :detail_status, :list_hash, :detail_hash, :detail_fetched_at, :source_url,
            :raw_list_json, :first_seen_at, :last_seen_at, :last_changed_at
        )
        ON CONFLICT(afml_id) DO UPDATE SET
            encoded_id=excluded.encoded_id, page_no=excluded.page_no,
            operation_date=excluded.operation_date, registration=excluded.registration,
            msn=excluded.msn, aircraft_type=excluded.aircraft_type,
            captain_name=excluded.captain_name, copilot_name=excluded.copilot_name,
            eob_name=excluded.eob_name, flight_scientist_1=excluded.flight_scientist_1,
            flight_scientist_2=excluded.flight_scientist_2, list_pilot_name=excluded.list_pilot_name,
            check_in_time=excluded.check_in_time, check_out_time=excluded.check_out_time,
            duty_time_text=excluded.duty_time_text, total_flight_time=excluded.total_flight_time,
            total_flight_minutes=excluded.total_flight_minutes, total_block_time=excluded.total_block_time,
            total_block_minutes=excluded.total_block_minutes, landing_cycles=excluded.landing_cycles,
            engine_cycles=excluded.engine_cycles, leg_count=excluded.leg_count,
            route_chain=excluded.route_chain, created_by=excluded.created_by,
            created_date=excluded.created_date, flight_type=excluded.flight_type,
            from_efb=excluded.from_efb, detail_status=excluded.detail_status,
            list_hash=excluded.list_hash, detail_hash=excluded.detail_hash,
            detail_fetched_at=excluded.detail_fetched_at,
            source_url=excluded.source_url, raw_list_json=excluded.raw_list_json,
            last_seen_at=excluded.last_seen_at, last_changed_at=excluded.last_changed_at
        """,
        {
            **{column: merged.get(column) for column in (
                "afml_id", "encoded_id", "page_no", "operation_date", "registration", "msn",
                "aircraft_type", "captain_name", "copilot_name", "eob_name", "flight_scientist_1",
                "flight_scientist_2", "list_pilot_name", "check_in_time", "check_out_time",
                "duty_time_text", "total_flight_time", "total_flight_minutes", "total_block_time",
                "total_block_minutes", "landing_cycles", "engine_cycles", "leg_count", "route_chain",
                "created_by", "created_date", "flight_type", "from_efb", "detail_status", "source_url"
            )},
            "list_hash": list_hash,
            "detail_hash": detail_hash,
            "detail_fetched_at": seen_at,
            "raw_list_json": json.dumps(list_record, ensure_ascii=False, sort_keys=True),
            "first_seen_at": first_seen,
            "last_seen_at": seen_at,
            "last_changed_at": last_changed,
        },
    )
    conn.execute("DELETE FROM afml_legs WHERE afml_id = ?", (afml_id,))
    for leg in detail["legs"]:
        conn.execute(
            """
            INSERT INTO afml_legs (
                afml_id, leg_index, origin_code, destination_code, block_off_time, block_on_time,
                block_minutes, takeoff_time, landing_time, flight_minutes, landing_cycles,
                engine_cycles, fuel_remaining, fuel_uplift, fuel_total, receipt_no,
                oil_added, hydraulic_added
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                afml_id, leg["leg_index"], leg["origin_code"], leg["destination_code"],
                leg["block_off_time"], leg["block_on_time"], leg["block_minutes"],
                leg["takeoff_time"], leg["landing_time"], leg["flight_minutes"],
                leg["landing_cycles"], leg["engine_cycles"], leg["fuel_remaining"],
                leg["fuel_uplift"], leg["fuel_total"], leg["receipt_no"],
                leg["oil_added"], leg["hydraulic_added"],
            ),
        )
    conn.execute(
        "INSERT OR IGNORE INTO afml_snapshots (afml_id, fetched_at, detail_hash, raw_html_gzip) VALUES (?, ?, ?, ?)",
        (afml_id, seen_at, detail_hash, gzip.compress(raw_html.encode("utf-8"))),
    )
    return not existing, not existing or existing["detail_hash"] != detail_hash


def load_pilot_aliases(path):
    path = Path(path)
    if not path.exists():
        return {}
    rows = json.loads(path.read_text(encoding="utf-8"))
    aliases = {}
    for row in rows:
        full = row.get("pilot_name_clean") or row.get("pilot_name")
        keys = list(row.get("match_keys") or [])
        keys.extend([row.get("pilot_name"), row.get("pilot_name_clean"), row.get("call_sign")])
        for key in keys:
            normalized = normalize_name(key)
            if normalized and normalized not in aliases:
                aliases[normalized] = full
    return aliases


def resolve_pilot(value, aliases):
    normalized = normalize_name(value)
    return aliases.get(normalized) or (normalize_text(value) if value else None)


def route_tokens(route):
    return [normalize_code(token) for token in re.split(r"\s*-\s*", route or "") if normalize_code(token)]


def sequence_candidates(legs, tokens):
    if len(tokens) < 2:
        return []
    required = len(tokens) - 1
    result = []
    for start in range(0, len(legs) - required + 1):
        segment = legs[start : start + required]
        actual = [segment[0]["origin_code"]] + [leg["destination_code"] for leg in segment]
        if actual == tokens:
            result.append(segment)
    return result


def name_tokens(value):
    cleaned = re.sub(
        r"\b(CAPT(?:AIN)?|PIC|FO|F/O|COPIL(?:OT)?|SIC)\b\.?",
        " ",
        str(value or "").upper(),
    )
    cleaned = re.sub(r"[^A-Z0-9 ]+", " ", cleaned)
    return [token for token in cleaned.split() if token]


def compare_name(source, target):
    if not source or not target:
        return "UNKNOWN"
    src = name_tokens(source)
    tgt = name_tokens(target)
    if not src or not tgt:
        return "UNKNOWN"
    if src == tgt:
        return "MATCH"
    si = 0
    for tgt_token in tgt:
        if si >= len(src):
            break
        src_token = src[si]
        if src_token == tgt_token:
            si += 1
        elif len(src_token) == 1 and tgt_token.startswith(src_token):
            si += 1
        elif len(tgt_token) == 1 and src_token.startswith(tgt_token):
            si += 1
    return "MATCH" if si == len(src) else "CONFLICT"


def reconcile(conn, from_date, to_date, pilot_mapping_path=DEFAULT_PILOT_MAPPING, max_time_delta=120):
    aliases = load_pilot_aliases(pilot_mapping_path)
    departures = conn.execute(
        """
        WITH ranked AS (
            SELECT m.id, m.raw_message_id, m.operation_date, m.registration, m.flight_seq,
                   m.route_full, m.takeoff_time, m.pic_name, m.sic_name,
                   row_number() OVER (
                       PARTITION BY m.operation_date, m.registration, coalesce(m.flight_seq, ''),
                                    m.route_full, m.takeoff_time
                       ORDER BY m.parse_confidence DESC, m.raw_message_id DESC, m.id DESC
                   ) AS duplicate_rank
            FROM flight_movements m
            WHERE m.movement_type = 'departure'
              AND m.takeoff_time IS NOT NULL AND trim(m.takeoff_time) <> ''
              AND m.route_full IS NOT NULL AND trim(m.route_full) <> ''
              AND m.operation_date BETWEEN ? AND ?
              AND m.leg_index = 1
        )
        SELECT id, raw_message_id, operation_date, registration, flight_seq,
               route_full, takeoff_time, pic_name, sic_name
        FROM ranked
        WHERE duplicate_rank = 1
        ORDER BY operation_date, registration, takeoff_time, id
        """,
        (from_date, to_date),
    ).fetchall()
    records = conn.execute(
        "SELECT * FROM afml_records WHERE operation_date BETWEEN ? AND ? ORDER BY operation_date, registration, afml_id",
        (from_date, to_date),
    ).fetchall()
    by_date_reg = {}
    for record in records:
        legs = conn.execute("SELECT * FROM afml_legs WHERE afml_id = ? ORDER BY leg_index", (record["afml_id"],)).fetchall()
        by_date_reg.setdefault((record["operation_date"], record["registration"]), []).append((record, legs))

    used_segments = set()
    output = []
    now = utc_now()
    for departure in departures:
        tokens = route_tokens(departure["route_full"])
        candidates = []
        for record, legs in by_date_reg.get((departure["operation_date"], departure["registration"]), []):
            for segment in sequence_candidates(legs, tokens):
                key = (record["afml_id"], segment[0]["leg_index"], segment[-1]["leg_index"])
                if key in used_segments:
                    continue
                delta = circular_time_delta(departure["takeoff_time"], segment[0]["takeoff_time"])
                candidates.append((delta if delta is not None else 99999, record, segment, key))
        candidates.sort(key=lambda item: (item[0], item[1]["afml_id"], item[2][0]["leg_index"]))

        pic_full = resolve_pilot(departure["pic_name"], aliases)
        sic_full = resolve_pilot(departure["sic_name"], aliases)
        notes = []
        if not candidates:
            row = {
                "afml_id": None,
                "afml_page_no": None,
                "afml_leg_start": None,
                "afml_leg_end": None,
                "afml_route": None,
                "afml_block_off_time": None,
                "afml_block_on_time": None,
                "afml_takeoff_time": None,
                "afml_landing_time": None,
                "afml_block_minutes": None,
                "afml_flight_minutes": None,
                "afml_captain_name": None,
                "afml_copilot_name": None,
                "time_delta_minutes": None,
                "route_match": "NO_MATCH",
                "pic_match": "UNKNOWN",
                "sic_match": "UNKNOWN",
                "match_status": "WA_ONLY",
                "quality_score": 0,
            }
            notes.append("Tidak ditemukan rangkaian leg AFML dengan tanggal, registrasi, dan rute yang sama")
        else:
            delta, record, segment, key = candidates[0]
            used_segments.add(key)
            block_minutes = sum(int(leg["block_minutes"] or 0) for leg in segment)
            flight_minutes = sum(int(leg["flight_minutes"] or 0) for leg in segment)
            pic_match = compare_name(pic_full, record["captain_name"])
            sic_match = compare_name(sic_full, record["copilot_name"])
            time_valid = delta <= max_time_delta
            has_actual_time = flight_minutes > 0 or block_minutes > 0
            if not has_actual_time:
                status = "AFML_INCOMPLETE"
                notes.append("AFML memiliki rute tetapi block/flight time masih nol")
            elif not time_valid:
                status = "TIME_CONFLICT"
                notes.append(f"Selisih takeoff WhatsApp dan AFML {delta} menit")
            elif pic_match == "CONFLICT" or sic_match == "CONFLICT":
                status = "CREW_CONFLICT"
                notes.append("Crew WhatsApp berbeda dengan AFML")
            else:
                status = "MATCHED"
            score = 50
            if has_actual_time:
                score += 10
            if delta <= 15:
                score += 25
            elif delta <= max_time_delta:
                score += 10
            if pic_match == "MATCH":
                score += 10
            if sic_match == "MATCH":
                score += 5
            row = {
                "afml_id": record["afml_id"],
                "afml_page_no": record["page_no"],
                "afml_leg_start": segment[0]["leg_index"],
                "afml_leg_end": segment[-1]["leg_index"],
                "afml_route": "-".join([segment[0]["origin_code"]] + [leg["destination_code"] for leg in segment]),
                "afml_block_off_time": segment[0]["block_off_time"],
                "afml_block_on_time": segment[-1]["block_on_time"],
                "afml_takeoff_time": segment[0]["takeoff_time"],
                "afml_landing_time": segment[-1]["landing_time"],
                "afml_block_minutes": block_minutes,
                "afml_flight_minutes": flight_minutes,
                "afml_captain_name": record["captain_name"],
                "afml_copilot_name": record["copilot_name"],
                "time_delta_minutes": delta if delta != 99999 else None,
                "route_match": "EXACT",
                "pic_match": pic_match,
                "sic_match": sic_match,
                "match_status": status,
                "quality_score": min(score, 100),
            }

        output.append(
            {
                "reconciliation_id": f"WA-{int(departure['raw_message_id']):08d}",
                "operation_date": departure["operation_date"],
                "registration": departure["registration"],
                "flight_seq": departure["flight_seq"],
                "route_full": departure["route_full"],
                "wa_takeoff_time": departure["takeoff_time"],
                "wa_pic_raw": departure["pic_name"],
                "wa_pic_full": pic_full,
                "wa_sic_raw": departure["sic_name"],
                "wa_sic_full": sic_full,
                "departure_raw_message_id": departure["raw_message_id"],
                "departure_movement_id": departure["id"],
                **row,
                "issue_notes": "; ".join(notes) or None,
                "updated_at": now,
            }
        )

    conn.execute("DELETE FROM afml_reconciliation WHERE operation_date BETWEEN ? AND ?", (from_date, to_date))
    for row in output:
        conn.execute(
            f"INSERT INTO afml_reconciliation ({','.join(AFML_RECON_HEADERS)}) VALUES ({','.join('?' for _ in AFML_RECON_HEADERS)})",
            [row.get(header) for header in AFML_RECON_HEADERS],
        )
    return output


def raw_sheet_rows(conn, from_date, to_date):
    rows = conn.execute(
        "SELECT * FROM afml_records WHERE operation_date BETWEEN ? AND ? ORDER BY operation_date, registration, afml_id",
        (from_date, to_date),
    ).fetchall()
    return [{header: row[header] for header in AFML_RAW_HEADERS} for row in rows]


def leg_sheet_rows(conn, from_date, to_date):
    rows = conn.execute(
        """
        SELECT l.*, r.page_no, r.operation_date, r.registration
        FROM afml_legs l JOIN afml_records r ON r.afml_id = l.afml_id
        WHERE r.operation_date BETWEEN ? AND ?
        ORDER BY r.operation_date, r.registration, l.afml_id, l.leg_index
        """,
        (from_date, to_date),
    ).fetchall()
    result = []
    for row in rows:
        item = {header: row[header] if header in row.keys() else None for header in AFML_LEG_HEADERS}
        item["afml_leg_id"] = f"{row['afml_id']}-{int(row['leg_index']):02d}"
        item["route_leg"] = f"{row['origin_code']}-{row['destination_code']}"
        result.append(item)
    return result


def recon_sheet_rows(conn, from_date, to_date):
    rows = conn.execute(
        "SELECT * FROM afml_reconciliation WHERE operation_date BETWEEN ? AND ? ORDER BY operation_date, registration, wa_takeoff_time, departure_movement_id",
        (from_date, to_date),
    ).fetchall()
    return [{header: row[header] for header in AFML_RECON_HEADERS} for row in rows]


def replace_sheet(webhook_url, token, spreadsheet_id, sheet_name, headers, rows, timeout, batch_size):
    post_payload(
        webhook_url,
        {"token": token, "action": "deleteSheets", "deleteSheets": [sheet_name], "keepSheetName": "RAW"},
        timeout,
        spreadsheet_id,
    )
    post_payload(
        webhook_url,
        {"token": token, "action": "ensureSheets", "sheets": [{"name": sheet_name, "headers": headers}]},
        timeout,
        spreadsheet_id,
    )
    appended = 0
    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        result = post_payload(
            webhook_url,
            {"token": token, "sheetName": sheet_name, "headers": headers, "rows": batch},
            timeout,
            spreadsheet_id,
        )
        appended += int(result.get("appended") or len(batch))
    return appended


def sync_sheets(args, conn, from_date, to_date):
    if not args.webhook_url:
        raise RuntimeError("GOOGLE_SHEETS_WEBHOOK_URL belum dikonfigurasi")
    datasets = [
        (args.raw_sheet_name, AFML_RAW_HEADERS, raw_sheet_rows(conn, from_date, to_date)),
        (args.legs_sheet_name, AFML_LEG_HEADERS, leg_sheet_rows(conn, from_date, to_date)),
        (args.recon_sheet_name, AFML_RECON_HEADERS, recon_sheet_rows(conn, from_date, to_date)),
    ]
    return {
        name: replace_sheet(
            args.webhook_url,
            args.token,
            args.spreadsheet_id,
            name,
            headers,
            rows,
            args.timeout_seconds,
            args.batch_size,
        )
        for name, headers, rows in datasets
    }


def run(args):
    username = args.username or os.environ.get("AMS_USERNAME")
    password = args.password or os.environ.get("AMS_PASSWORD")
    if not username or not password:
        raise RuntimeError("AMS_USERNAME dan AMS_PASSWORD wajib dikonfigurasi")

    timezone_name = os.environ.get("OPS_OPERATION_TIMEZONE", DEFAULT_OPERATION_TIMEZONE)
    today = datetime.now(ZoneInfo(timezone_name)).date()
    from_date = args.from_date or (today - timedelta(days=args.lookback_days - 1)).isoformat()
    to_date = args.to_date or today.isoformat()
    if from_date > to_date:
        raise RuntimeError("from-date tidak boleh setelah to-date")

    client = AfmlClient(args.base_url, username, password, args.timeout_seconds)
    client.login()
    list_records = client.list_records(from_date, to_date, args.max_pages)
    conn = sqlite3.connect(args.db, timeout=30)
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    created = 0
    changed = 0
    fetched = 0
    skipped = 0
    errors = []
    seen_at = utc_now()
    for list_record in list_records:
        existing = conn.execute(
            "SELECT list_hash, detail_fetched_at FROM afml_records WHERE afml_id = ?",
            (list_record["afml_id"],),
        ).fetchone()
        list_hash = stable_hash(list_record)
        recent_threshold = (today - timedelta(days=1)).isoformat()
        stale_before = datetime.now(timezone.utc) - timedelta(hours=args.historical_refresh_hours)
        fetched_at = None
        if existing and existing["detail_fetched_at"]:
            try:
                fetched_at = datetime.fromisoformat(existing["detail_fetched_at"].replace("Z", "+00:00"))
            except ValueError:
                fetched_at = None
        should_fetch = (
            not existing
            or existing["list_hash"] != list_hash
            or (list_record.get("operation_date") or "") >= recent_threshold
            or fetched_at is None
            or fetched_at < stale_before
        )
        if not should_fetch:
            conn.execute(
                "UPDATE afml_records SET last_seen_at = ? WHERE afml_id = ?",
                (seen_at, list_record["afml_id"]),
            )
            conn.commit()
            skipped += 1
            continue
        try:
            raw_html, final_url = client.detail(list_record["afml_id"])
            detail = parse_afml_detail(raw_html, list_record["afml_id"], final_url)
            is_new, is_changed = save_record(conn, list_record, detail, raw_html, seen_at)
            created += int(is_new)
            changed += int(is_changed)
            fetched += 1
            conn.commit()
        except (RuntimeError, urllib.error.URLError, ValueError, sqlite3.Error) as exc:
            conn.rollback()
            errors.append({"afml_id": list_record["afml_id"], "error": str(exc)})

    reconciliation = reconcile(conn, from_date, to_date, args.pilot_mapping, args.max_time_delta)
    conn.commit()
    sheets = sync_sheets(args, conn, from_date, to_date) if args.sync_sheets else {}
    summary = {
        "ok": not errors,
        "from_date": from_date,
        "to_date": to_date,
        "list_records": len(list_records),
        "new_records": created,
        "changed_records": changed,
        "detail_fetched": fetched,
        "detail_skipped_unchanged": skipped,
        "detail_errors": errors,
        "legs": conn.execute(
            "SELECT count(*) FROM afml_legs l JOIN afml_records r ON r.afml_id=l.afml_id WHERE r.operation_date BETWEEN ? AND ?",
            (from_date, to_date),
        ).fetchone()[0],
        "reconciliation": len(reconciliation),
        "reconciliation_status": dict(
            conn.execute(
                "SELECT match_status, count(*) FROM afml_reconciliation WHERE operation_date BETWEEN ? AND ? GROUP BY match_status",
                (from_date, to_date),
            ).fetchall()
        ),
        "sheets": sheets,
        "completed_at": utc_now(),
    }
    conn.close()
    return summary


def build_parser():
    parser = argparse.ArgumentParser(description="Sinkronisasi read-only AFML dan rekonsiliasi WhatsApp")
    parser.add_argument("--base-url", default=os.environ.get("AMS_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--username")
    parser.add_argument("--password")
    parser.add_argument("--db", default=os.environ.get("OPS_DB_PATH", DEFAULT_DB))
    parser.add_argument("--from-date")
    parser.add_argument("--to-date")
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=int(os.environ.get("AFML_LOOKBACK_DAYS", str(DEFAULT_LOOKBACK_DAYS))),
    )
    parser.add_argument("--max-pages", type=int, default=100)
    parser.add_argument("--max-time-delta", type=int, default=120)
    parser.add_argument(
        "--historical-refresh-hours",
        type=int,
        default=int(os.environ.get("AFML_HISTORICAL_REFRESH_HOURS", "24")),
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=int(os.environ.get("AFML_HTTP_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))),
    )
    parser.add_argument("--pilot-mapping", default=os.environ.get("OPS_PILOT_MAPPING_PATH", DEFAULT_PILOT_MAPPING))
    parser.add_argument("--lock-path", default=os.environ.get("OPS_AFML_LOCK_PATH", DEFAULT_LOCK))
    parser.add_argument("--state", default=os.environ.get("OPS_AFML_STATE_PATH", DEFAULT_STATE))
    parser.add_argument("--sync-sheets", action="store_true")
    parser.add_argument("--webhook-url", default=os.environ.get("GOOGLE_SHEETS_WEBHOOK_URL", DEFAULT_WEBHOOK_URL))
    parser.add_argument("--token", default=os.environ.get("GOOGLE_SHEETS_WEBHOOK_TOKEN", DEFAULT_TOKEN))
    parser.add_argument(
        "--spreadsheet-id",
        default=os.environ.get("GOOGLE_SHEETS_SPREADSHEET_ID", DEFAULT_SPREADSHEET_ID),
    )
    parser.add_argument("--raw-sheet-name", default=os.environ.get("GOOGLE_SHEETS_AFML_RAW_TAB", DEFAULT_RAW_SHEET))
    parser.add_argument("--legs-sheet-name", default=os.environ.get("GOOGLE_SHEETS_AFML_LEGS_TAB", DEFAULT_LEGS_SHEET))
    parser.add_argument("--recon-sheet-name", default=os.environ.get("GOOGLE_SHEETS_AFML_RECON_TAB", DEFAULT_RECON_SHEET))
    parser.add_argument(
        "--batch-size",
        type=int,
        default=int(os.environ.get("AFML_SHEETS_BATCH_SIZE", str(DEFAULT_BATCH_SIZE))),
    )
    return parser


def main():
    load_env_file("config/afml.env")
    load_env_file("config/google-sheets.env")
    args = build_parser().parse_args()
    Path(args.lock_path).parent.mkdir(parents=True, exist_ok=True)
    with Path(args.lock_path).open("w", encoding="utf-8") as lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"ok": True, "status": "skipped_already_running"}))
            return
        try:
            summary = run(args)
        except (RuntimeError, urllib.error.URLError, sqlite3.Error, ValueError) as exc:
            failure = {"ok": False, "error": str(exc), "completed_at": utc_now()}
            save_json(args.state, failure)
            print(json.dumps(failure, ensure_ascii=False), file=sys.stderr)
            raise SystemExit(1) from exc
        save_json(args.state, summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        if not summary["ok"]:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
