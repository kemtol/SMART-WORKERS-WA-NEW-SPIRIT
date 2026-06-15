#!/usr/bin/env python3
import argparse
import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from movement_parser import load_mapping, parse_movements


DEFAULT_HOST = os.environ.get("OPS_INGEST_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("OPS_INGEST_PORT", "8088"))
DEFAULT_DB = os.environ.get("OPS_DB_PATH", "data/ops_messages.sqlite3")


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def json_response(handler, status, payload):
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("content-type", "application/json; charset=utf-8")
    handler.send_header("content-length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def normalize_time(value):
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 3:
        digits = "0" + digits
    if len(digits) != 4:
        return None
    hour = int(digits[:2])
    minute = int(digits[2:])
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def parse_schedule(text):
    normalized = (text or "").upper()
    if not normalized.strip():
        return None

    flight = None
    for match in re.finditer(r"\b([A-Z0-9]{2,3})[\s-]?(\d{2,4}[A-Z]?)\b", normalized):
        candidate = f"{match.group(1)}{match.group(2)}"
        if match.group(1) in {"STD", "ETD", "ATD", "STA", "ETA", "DEP", "ARR"}:
            continue
        flight = candidate
        break

    route = re.search(r"\b([A-Z]{3})\s*[-/]\s*([A-Z]{3})\b", normalized)
    origin = route.group(1) if route else None
    destination = route.group(2) if route else None

    times = {}
    for label, value in re.findall(r"\b(STD|ETD|ATD|STA|ETA)\s*[:=]?\s*([0-2]?\d[:.]?[0-5]\d)\b", normalized):
        times[label.lower()] = normalize_time(value)

    reg_match = re.search(r"\b(PK)[-\s]?([A-Z]{3})\b", normalized)
    registration = f"PK-{reg_match.group(2)}" if reg_match else None

    status = None
    for token in ("DELAY", "CANCEL", "CANCELLED", "DIVERT", "ON TIME", "BOARDING", "DEPARTED", "ARRIVED"):
        if token in normalized:
            status = token
            break

    confidence = 0.0
    confidence += 0.35 if flight else 0.0
    confidence += 0.25 if origin and destination else 0.0
    confidence += min(0.25, 0.08 * len([v for v in times.values() if v]))
    confidence += 0.10 if registration else 0.0
    confidence += 0.05 if status else 0.0

    if confidence < 0.35:
        return None

    return {
        "flight_number": flight,
        "origin": origin,
        "destination": destination,
        "std": times.get("std"),
        "etd": times.get("etd"),
        "atd": times.get("atd"),
        "sta": times.get("sta"),
        "eta": times.get("eta"),
        "registration": registration,
        "status": status,
        "parse_confidence": round(confidence, 2),
    }


class Store:
    def __init__(self, db_path):
        self.db_path = db_path
        self.write_lock = threading.Lock()
        self.airport_mapping = load_mapping()
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.init_db()

    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def init_db(self):
        with self.connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS raw_messages (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  dedupe_key TEXT NOT NULL UNIQUE,
                  message_id TEXT,
                  remote_jid TEXT,
                  group_name TEXT,
                  sender_jid TEXT,
                  from_me INTEGER NOT NULL DEFAULT 0,
                  message_timestamp INTEGER,
                  message_timestamp_iso TEXT,
                  message_type TEXT,
                  text TEXT,
                  source TEXT,
                  received_at TEXT NOT NULL,
                  payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS flight_schedules (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  raw_message_id INTEGER NOT NULL UNIQUE,
                  flight_number TEXT,
                  origin TEXT,
                  destination TEXT,
                  std TEXT,
                  etd TEXT,
                  atd TEXT,
                  sta TEXT,
                  eta TEXT,
                  registration TEXT,
                  status TEXT,
                  parse_confidence REAL NOT NULL DEFAULT 0,
                  created_at TEXT NOT NULL,
                  FOREIGN KEY(raw_message_id) REFERENCES raw_messages(id)
                );

                CREATE TABLE IF NOT EXISTS parse_errors (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  raw_message_id INTEGER NOT NULL UNIQUE,
                  reason TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  FOREIGN KEY(raw_message_id) REFERENCES raw_messages(id)
                );

                CREATE TABLE IF NOT EXISTS flight_movements (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  raw_message_id INTEGER NOT NULL,
                  movement_index INTEGER NOT NULL,
                  movement_type TEXT,
                  operation_date TEXT,
                  registration TEXT,
                  aircraft_type TEXT,
                  flight_seq TEXT,
                  leg_index INTEGER,
                  route_full TEXT,
                  leg_origin_code TEXT,
                  leg_origin_name TEXT,
                  leg_origin_icao TEXT,
                  leg_origin_iata TEXT,
                  leg_destination_code TEXT,
                  leg_destination_name TEXT,
                  leg_destination_icao TEXT,
                  leg_destination_iata TEXT,
                  from_place TEXT,
                  from_code TEXT,
                  from_name TEXT,
                  from_icao TEXT,
                  from_iata TEXT,
                  arrival_airport_code TEXT,
                  arrival_airport_name TEXT,
                  arrival_airport_icao TEXT,
                  arrival_airport_iata TEXT,
                  next_route TEXT,
                  next_text TEXT,
                  engine_start_time TEXT,
                  takeoff_time TEXT,
                  eta_airport_code TEXT,
                  eta_airport_name TEXT,
                  eta_airport_icao TEXT,
                  eta_airport_iata TEXT,
                  eta_time TEXT,
                  ata_airport_code TEXT,
                  ata_airport_name TEXT,
                  ata_airport_icao TEXT,
                  ata_airport_iata TEXT,
                  ata_time TEXT,
                  pax TEXT,
                  pax_weight_kg REAL,
                  baggage_kg REAL,
                  cargo_text TEXT,
                  cargo_kg REAL,
                  total_load_kg REAL,
                  remark TEXT,
                  parse_confidence REAL NOT NULL DEFAULT 0,
                  created_at TEXT NOT NULL,
                  UNIQUE(raw_message_id, movement_index),
                  FOREIGN KEY(raw_message_id) REFERENCES raw_messages(id)
                );

                CREATE INDEX IF NOT EXISTS idx_raw_messages_received_at ON raw_messages(received_at);
                CREATE INDEX IF NOT EXISTS idx_raw_messages_remote_jid ON raw_messages(remote_jid);
                CREATE INDEX IF NOT EXISTS idx_flight_schedules_flight_number ON flight_schedules(flight_number);
                CREATE INDEX IF NOT EXISTS idx_flight_movements_raw_message_id ON flight_movements(raw_message_id);
                CREATE INDEX IF NOT EXISTS idx_flight_movements_registration ON flight_movements(registration);
                CREATE INDEX IF NOT EXISTS idx_flight_movements_operation_date ON flight_movements(operation_date);
                """
            )

    def insert_movements(self, conn, raw_message_id, text, now):
        movements = parse_movements(text, self.airport_mapping)
        for index, movement in enumerate(movements, start=1):
            conn.execute(
                """
                INSERT OR IGNORE INTO flight_movements (
                  raw_message_id, movement_index, movement_type, operation_date, registration,
                  aircraft_type, flight_seq, leg_index, route_full,
                  leg_origin_code, leg_origin_name, leg_origin_icao, leg_origin_iata,
                  leg_destination_code, leg_destination_name, leg_destination_icao, leg_destination_iata,
                  from_place, from_code, from_name, from_icao, from_iata,
                  arrival_airport_code, arrival_airport_name, arrival_airport_icao, arrival_airport_iata,
                  next_route, next_text, engine_start_time, takeoff_time,
                  eta_airport_code, eta_airport_name, eta_airport_icao, eta_airport_iata, eta_time,
                  ata_airport_code, ata_airport_name, ata_airport_icao, ata_airport_iata, ata_time,
                  pax, pax_weight_kg, baggage_kg, cargo_text, cargo_kg, total_load_kg,
                  remark, parse_confidence, created_at
                ) VALUES (
                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    raw_message_id,
                    index,
                    movement.get("movement_type"),
                    movement.get("operation_date"),
                    movement.get("registration"),
                    movement.get("aircraft_type"),
                    movement.get("flight_seq"),
                    movement.get("leg_index"),
                    movement.get("route_full"),
                    movement.get("leg_origin_code"),
                    movement.get("leg_origin_name"),
                    movement.get("leg_origin_icao"),
                    movement.get("leg_origin_iata"),
                    movement.get("leg_destination_code"),
                    movement.get("leg_destination_name"),
                    movement.get("leg_destination_icao"),
                    movement.get("leg_destination_iata"),
                    movement.get("from_place"),
                    movement.get("from_code"),
                    movement.get("from_name"),
                    movement.get("from_icao"),
                    movement.get("from_iata"),
                    movement.get("arrival_airport_code"),
                    movement.get("arrival_airport_name"),
                    movement.get("arrival_airport_icao"),
                    movement.get("arrival_airport_iata"),
                    movement.get("next_route"),
                    movement.get("next_text"),
                    movement.get("engine_start_time"),
                    movement.get("takeoff_time"),
                    movement.get("eta_airport_code"),
                    movement.get("eta_airport_name"),
                    movement.get("eta_airport_icao"),
                    movement.get("eta_airport_iata"),
                    movement.get("eta_time"),
                    movement.get("ata_airport_code"),
                    movement.get("ata_airport_name"),
                    movement.get("ata_airport_icao"),
                    movement.get("ata_airport_iata"),
                    movement.get("ata_time"),
                    movement.get("pax"),
                    movement.get("pax_weight_kg"),
                    movement.get("baggage_kg"),
                    movement.get("cargo_text"),
                    movement.get("cargo_kg"),
                    movement.get("total_load_kg"),
                    movement.get("remark"),
                    movement.get("parse_confidence") or 0,
                    now,
                ),
            )
        return len(movements)

    def insert_message(self, payload):
        now = utc_now()
        remote_jid = payload.get("remoteJid")
        sender_jid = payload.get("senderJid")
        message_id = payload.get("id")
        timestamp = payload.get("timestamp")
        dedupe_key = "|".join(str(x or "") for x in [remote_jid, sender_jid, message_id, timestamp])
        text = payload.get("text") or ""

        with self.write_lock:
            with self.connect() as conn:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO raw_messages (
                      dedupe_key, message_id, remote_jid, group_name, sender_jid, from_me,
                      message_timestamp, message_timestamp_iso, message_type, text, source,
                      received_at, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        dedupe_key,
                        message_id,
                        remote_jid,
                        payload.get("groupName"),
                        sender_jid,
                        1 if payload.get("fromMe") else 0,
                        timestamp,
                        payload.get("timestampIso"),
                        payload.get("type"),
                        text,
                        payload.get("source"),
                        payload.get("receivedAt") or now,
                        json.dumps(payload, ensure_ascii=False),
                    ),
                )
                if cur.rowcount == 0:
                    row = conn.execute("SELECT id FROM raw_messages WHERE dedupe_key = ?", (dedupe_key,)).fetchone()
                    return {"duplicate": True, "raw_message_id": row["id"] if row else None}

                raw_message_id = cur.lastrowid
                schedule = parse_schedule(text)
                if schedule:
                    conn.execute(
                        """
                        INSERT INTO flight_schedules (
                          raw_message_id, flight_number, origin, destination, std, etd, atd, sta, eta,
                          registration, status, parse_confidence, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            raw_message_id,
                            schedule["flight_number"],
                            schedule["origin"],
                            schedule["destination"],
                            schedule["std"],
                            schedule["etd"],
                            schedule["atd"],
                            schedule["sta"],
                            schedule["eta"],
                            schedule["registration"],
                            schedule["status"],
                            schedule["parse_confidence"],
                            now,
                        ),
                    )
                else:
                    conn.execute(
                        "INSERT INTO parse_errors (raw_message_id, reason, created_at) VALUES (?, ?, ?)",
                        (raw_message_id, "no_schedule_pattern", now),
                    )
                movement_count = self.insert_movements(conn, raw_message_id, text, now)

        return {"duplicate": False, "raw_message_id": raw_message_id, "parsed": bool(schedule), "movements": movement_count}

    def list_rows(self, table, limit):
        if table not in {"raw_messages", "flight_schedules", "flight_movements", "parse_errors"}:
            raise ValueError("unsupported table")
        limit = max(1, min(int(limit), 200))
        with self.connect() as conn:
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def counts(self):
        with self.connect() as conn:
            return {
                "raw_messages": conn.execute("SELECT COUNT(*) AS c FROM raw_messages").fetchone()["c"],
                "flight_schedules": conn.execute("SELECT COUNT(*) AS c FROM flight_schedules").fetchone()["c"],
                "flight_movements": conn.execute("SELECT COUNT(*) AS c FROM flight_movements").fetchone()["c"],
                "parse_errors": conn.execute("SELECT COUNT(*) AS c FROM parse_errors").fetchone()["c"],
            }


class Handler(BaseHTTPRequestHandler):
    store = None

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}")

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        limit = int(query.get("limit", ["50"])[0])
        try:
            if parsed.path == "/health":
                json_response(self, 200, {"ok": True, "db": self.store.db_path, "counts": self.store.counts()})
            elif parsed.path == "/messages":
                json_response(self, 200, {"ok": True, "messages": self.store.list_rows("raw_messages", limit)})
            elif parsed.path == "/schedules":
                json_response(self, 200, {"ok": True, "schedules": self.store.list_rows("flight_schedules", limit)})
            elif parsed.path == "/movements":
                json_response(self, 200, {"ok": True, "movements": self.store.list_rows("flight_movements", limit)})
            elif parsed.path == "/parse-errors":
                json_response(self, 200, {"ok": True, "errors": self.store.list_rows("parse_errors", limit)})
            else:
                json_response(self, 404, {"ok": False, "error": "not found"})
        except Exception as exc:
            json_response(self, 500, {"ok": False, "error": str(exc)})

    def do_POST(self):
        if self.path != "/ingest/whatsapp":
            json_response(self, 404, {"ok": False, "error": "not found"})
            return

        try:
            length = int(self.headers.get("content-length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            result = self.store.insert_message(payload)
            json_response(self, 200, {"ok": True, **result})
        except json.JSONDecodeError:
            json_response(self, 400, {"ok": False, "error": "invalid JSON"})
        except Exception as exc:
            json_response(self, 500, {"ok": False, "error": str(exc)})


def main():
    parser = argparse.ArgumentParser(description="WhatsApp Ops ingest service")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--db", default=DEFAULT_DB)
    args = parser.parse_args()

    Handler.store = Store(args.db)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(json.dumps({"ok": True, "status": "listening", "host": args.host, "port": args.port, "db": args.db}))
    server.serve_forever()


if __name__ == "__main__":
    main()
