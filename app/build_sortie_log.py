#!/usr/bin/env python3
import argparse
import csv
import fcntl
import json
import os
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from google_sheets_sync import (
    DEFAULT_OPERATION_TIMEZONE,
    DEFAULT_SPREADSHEET_ID,
    DEFAULT_TOKEN,
    DEFAULT_WEBHOOK_URL,
    event_datetime_utc_from_time,
    format_datetime_local,
    post_payload,
)


DEFAULT_DB = os.environ.get("OPS_DB_PATH", "data/ops_messages.sqlite3")
DEFAULT_PILOT_MAPPING = os.environ.get("OPS_PILOT_MAPPING_PATH", "data/reference/mapping_pilot.json")
DEFAULT_OUTPUT_DIR = os.environ.get("OPS_SORTIE_OUTPUT_DIR", "data/derived")
DEFAULT_FROM_DATE = os.environ.get("SORTIE_LOG_FROM_DATE", "2026-06-12") or None
DEFAULT_TO_DATE = os.environ.get("SORTIE_LOG_TO_DATE") or None
DEFAULT_SORTIE_SHEET = os.environ.get("GOOGLE_SHEETS_SORTIE_LOG_TAB", "SORTIE_LOG")
DEFAULT_EXCEPTION_SHEET = os.environ.get(
    "GOOGLE_SHEETS_ARRIVAL_EXCEPTION_TAB", "ARRIVAL_ACK_EXCEPTIONS"
)
DEFAULT_ABNORMAL_SHEET = os.environ.get(
    "GOOGLE_SHEETS_ABNORMAL_AUDIT_TAB", "ABNORMAL_EVIDENCE_AUDIT"
)
DEFAULT_LOCK = os.environ.get("OPS_SORTIE_LOCK_PATH", "data/sortie-log-sync.lock")

SORTIE_HEADERS = [
    "mission_id",
    "operation_date",
    "registration",
    "aircraft_type",
    "aircraft_type_quality",
    "flight_seq",
    "route_full",
    "route_type",
    "from",
    "to",
    "via",
    "pic_raw",
    "pic_full",
    "sic_raw",
    "sic_full",
    "departure_time_z",
    "departure_datetime_local",
    "departure_datetime_utc",
    "pax",
    "pax_total",
    "pax_weight_kg",
    "baggage_kg",
    "cargo_text",
    "cargo_kg",
    "cargo_ton",
    "cargo_quality",
    "total_load_kg",
    "total_load_ton",
    "arrival_ack_status",
    "arrival_time_z",
    "arrival_datetime_local",
    "arrival_datetime_utc",
    "mission_status",
    "confidence_level",
    "completion_basis",
    "productivity_include_flag",
    "load_quality",
    "pax_quality",
    "abnormal_flag_same_aircraft_date",
    "abnormal_evidence_count",
    "abnormal_evidence_sample",
    "departure_raw_message_id",
    "arrival_raw_message_id",
    "departure_movement_id",
    "arrival_movement_id",
    "parse_confidence",
    "issue_notes",
]

ARRIVAL_EXCEPTION_HEADERS = [
    "exception_id",
    "operation_date",
    "registration",
    "aircraft_type",
    "flight_seq",
    "from_reported",
    "from_code",
    "arrival_airport_code",
    "arrival_time_z",
    "arrival_datetime_local",
    "arrival_datetime_utc",
    "pax",
    "total_load_kg",
    "arrival_raw_message_id",
    "arrival_movement_id",
    "exception_reason",
    "source_text",
]

ABNORMAL_AUDIT_HEADERS = [
    "evidence_id",
    "operation_date",
    "registration_list",
    "keyword_list",
    "raw_message_id",
    "message_timestamp_iso",
    "snippet",
    "source_text",
]

ABNORMAL_PATTERNS = {
    "ACCIDENT": r"\bACCIDENT\b",
    "INCIDENT": r"\bINCIDENT\b",
    "EMERGENCY": r"\bEMERGENCY\b|\bMAYDAY\b|\bPAN[ -]?PAN\b",
    "DIVERT": r"\bDIVERT(?:ED|ING|ION)?\b",
    "RTB": r"\bRTB\b|RETURN\s+TO\s+BASE",
    "ABORT": r"\bABORT(?:ED)?\b",
    "FORCED_LANDING": r"FORCED\s+LANDING",
    "ENGINE_FAILURE": r"ENGINE\s+FAIL(?:URE|ED)?",
    "AOG": r"\bAOG\b",
    "UNSERVICEABLE": r"\bUNSERVICEABLE\b",
    "TECHNICAL": r"\bTECH(?:NICAL)?(?:\s+(?:REASON|PROBLEM|ISSUE))?\b",
    "CANCEL": r"\bCANCEL(?:LED|ED)?\b",
}


def connect(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def parse_iso(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def operation_date_for_timestamp(value, timezone_name=DEFAULT_OPERATION_TIMEZONE):
    parsed = parse_iso(value)
    if not parsed:
        return str(value or "")[:10] or None
    try:
        from zoneinfo import ZoneInfo

        return parsed.astimezone(ZoneInfo(timezone_name)).date().isoformat()
    except (KeyError, OSError, ValueError):
        return parsed.date().isoformat()


def operation_date_for_row(row, timezone_name=DEFAULT_OPERATION_TIMEZONE):
    return row["operation_date"] or operation_date_for_timestamp(row["message_timestamp_iso"], timezone_name)


def normalize_code(value):
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper()) or None


def split_route(value):
    raw_parts = [part.strip() for part in re.split(r"\s*[-/]\s*", str(value or "").strip()) if part.strip()]
    parts = []
    for part in raw_parts:
        compact = normalize_code(part)
        if compact and len(compact) == 6 and re.fullmatch(r"[A-Z]{6}", compact):
            parts.extend((compact[:3], compact[3:]))
        else:
            parts.append(part)
    return parts


def route_metadata(route_full):
    parts = split_route(route_full)
    if len(parts) < 2:
        return {"route_type": None, "from": None, "to": None, "via": None}
    if len(parts) == 2:
        route_type = "direct"
    elif normalize_code(parts[0]) == normalize_code(parts[-1]):
        route_type = "out_and_back"
    else:
        route_type = "multi_leg"
    return {
        "route_type": route_type,
        "from": normalize_code(parts[0]) or parts[0],
        "to": normalize_code(parts[-1]) or parts[-1],
        "via": "-".join(normalize_code(part) or part for part in parts[1:-1]) or None,
    }


def parse_pax_total(value):
    text = str(value or "").strip()
    if not text:
        return None, "MISSING"
    match = re.search(r"\b(\d+)\s*/\s*(\d+)\s*/\s*(\d+)\b", text)
    if not match:
        return None, "NEEDS_REVIEW"
    total = sum(int(part) for part in match.groups())
    # FOC tetap orang yang diangkut dan baseline productivity internal memasukkannya.
    for note in re.findall(r"\(([^)]*\bFOC\b[^)]*)\)", text, re.IGNORECASE):
        total += sum(int(number) for number in re.findall(r"\b(\d+)\s*FOC\b", note, re.IGNORECASE))
    return total, "COMPLETE"


def numeric(value):
    if value in (None, ""):
        return None
    number = float(value)
    return int(number) if number.is_integer() else number


def metric_ton(value):
    value = numeric(value)
    return round(value / 1000, 3) if value is not None else None


def cargo_quality(cargo_text, cargo_kg):
    text = str(cargo_text or "").strip()
    if cargo_kg is not None:
        return "COMPLETE"
    if not text or re.fullmatch(r"-?\s*(?:kg)?", text, re.IGNORECASE):
        return "MISSING"
    return "NEEDS_PARSE"


def aircraft_type_from_source(value, source_text):
    if value:
        return value
    text = str(source_text or "").upper()
    if re.search(r"\bPAC\s*750(?:\s*XSTOL)?\b", text):
        return "PAC750-XSTOL"
    if re.search(r"\bEC\s*130\s*T2\b", text):
        return "EC130-T2"
    return None


def load_quality(row):
    if row["total_load_kg"] is not None:
        return "COMPLETE"
    if any(row[key] is not None for key in ("pax_weight_kg", "baggage_kg", "cargo_kg")):
        return "PARTIAL"
    return "MISSING"


def normalize_crew(value):
    text = re.sub(r"\s+", " ", str(value or "").replace(".", " ")).strip().upper()
    rank = ""
    rank_match = re.match(r"^(CAPT(?:AIN)?|FO|F/O|FIRST\s+OFFICER)\s+", text)
    if rank_match:
        rank = "CAPT" if rank_match.group(1).startswith("CAPT") else "FO"
        text = text[rank_match.end() :]
    return rank, re.sub(r"[^A-Z0-9]+", " ", text).strip()


def load_pilot_index(path):
    try:
        rows = json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    index = defaultdict(list)
    for row in rows:
        rank = str(row.get("rank") or "").upper()
        keys = set(row.get("match_keys") or [])
        keys.update(row.get(key) for key in ("initial_1", "initial_2", "initial_3", "initial_4"))
        keys.add(row.get("pilot_name_clean"))
        for key in keys:
            _, normalized = normalize_crew(key)
            if normalized:
                index[(rank, normalized)].append(row)
    return index


def match_pilot(value, expected_rank, pilot_index):
    observed_rank, key = normalize_crew(value)
    if not key:
        return None, "crew_missing"
    candidates = pilot_index.get((observed_rank or expected_rank, key), [])
    unique = {str(row.get("pilot_id")): row for row in candidates}
    if len(unique) == 1:
        return next(iter(unique.values())).get("pilot_name"), None
    if len(unique) > 1:
        return None, f"crew_mapping_ambiguous:{value}"
    return None, f"crew_mapping_unmatched:{value}"


def quality_score(row):
    fields = (
        "aircraft_type",
        "flight_seq",
        "route_full",
        "pic_name",
        "sic_name",
        "pax",
        "pax_weight_kg",
        "baggage_kg",
        "cargo_kg",
        "total_load_kg",
    )
    return sum(row[key] not in (None, "") for key in fields), int(row["raw_message_id"])


def load_movements(db_path, timezone_name):
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT
              fm.*,
              rm.message_timestamp_iso,
              rm.received_at,
              rm.text AS source_text
            FROM flight_movements fm
            JOIN raw_messages rm ON rm.id = fm.raw_message_id
            WHERE (
              fm.movement_type = 'departure'
              AND fm.takeoff_time IS NOT NULL
              AND TRIM(fm.takeoff_time) != ''
            ) OR (
              fm.movement_type = 'arrival'
              AND fm.ata_time IS NOT NULL
              AND TRIM(fm.ata_time) != ''
            )
            ORDER BY rm.message_timestamp_iso, fm.id
            """
        ).fetchall()

    departures = []
    arrivals = []
    for row in rows:
        item = dict(row)
        item["operation_date_resolved"] = operation_date_for_row(row, timezone_name)
        event_time = row["takeoff_time"] if row["movement_type"] == "departure" else row["ata_time"]
        item["event_datetime_utc"] = event_datetime_utc_from_time(row["message_timestamp_iso"], event_time)
        (departures if row["movement_type"] == "departure" else arrivals).append(item)
    return departures, arrivals


def filter_dates(rows, from_date=None, to_date=None):
    return [
        row
        for row in rows
        if (not from_date or row["operation_date_resolved"] >= from_date)
        and (not to_date or row["operation_date_resolved"] <= to_date)
    ]


def dedupe_departures(rows):
    grouped = defaultdict(list)
    for row in rows:
        key = (
            row["operation_date_resolved"],
            row["registration"],
            row["flight_seq"],
            re.sub(r"\s+", "", str(row["route_full"] or "").upper()),
            row["takeoff_time"],
        )
        grouped[key].append(row)
    deduped = [max(group, key=quality_score) for group in grouped.values()]
    return sorted(deduped, key=lambda row: (row["event_datetime_utc"] or datetime.max.replace(tzinfo=timezone.utc), row["id"]))


def format_utc(value):
    return value.strftime("%Y-%m-%dT%H:%M:00Z") if value else None


def arrival_destination(row):
    return normalize_code(row.get("arrival_airport_code") or row.get("ata_airport_code") or row.get("leg_destination_code"))


def match_arrivals(departures, arrivals):
    used = set()
    matches = {}
    arrivals_by_key = defaultdict(list)
    for arrival in arrivals:
        key = (arrival["operation_date_resolved"], arrival["registration"], arrival["flight_seq"])
        arrivals_by_key[key].append(arrival)

    for departure in departures:
        metadata = route_metadata(departure["route_full"])
        final_destination = normalize_code(metadata["to"])
        exact_key = (
            departure["operation_date_resolved"],
            departure["registration"],
            departure["flight_seq"],
        )
        if departure["flight_seq"]:
            pool = arrivals_by_key.get(exact_key, [])
            match_basis = "registration_date_flight_final_destination"
        else:
            pool = [
                arrival
                for key, keyed_arrivals in arrivals_by_key.items()
                if key[:2] == exact_key[:2]
                for arrival in keyed_arrivals
            ]
            match_basis = "registration_date_missing_flight_final_destination"
        candidates = []
        for arrival in pool:
            if arrival["id"] in used:
                continue
            dep_time = departure["event_datetime_utc"]
            arr_time = arrival["event_datetime_utc"]
            if dep_time and arr_time:
                delta = arr_time - dep_time
                if delta < timedelta(minutes=-30) or delta > timedelta(hours=18):
                    continue
                distance = abs(delta.total_seconds())
            else:
                distance = abs(int(arrival["raw_message_id"]) - int(departure["raw_message_id"])) * 60
            candidate = (distance, int(arrival["raw_message_id"]), arrival)
            if arrival_destination(arrival) == final_destination:
                candidates.append(candidate)
        if not candidates:
            continue
        matched = min(candidates, key=lambda item: (item[0], item[1]))[2]
        matched["_sortie_match_basis"] = match_basis
        matches[departure["id"]] = matched
        used.add(matched["id"])
    return matches, used


def abnormal_keywords(text):
    upper = str(text or "").upper()
    return [name for name, pattern in ABNORMAL_PATTERNS.items() if re.search(pattern, upper)]


def registration_near_abnormal(text):
    upper = str(text or "").upper()
    all_registrations = re.findall(r"\bPK[-\s]?([A-Z]{3})\b", upper)
    if not all_registrations:
        return []

    scoped = set()
    lines = upper.splitlines()
    current_registration = None
    for index, line in enumerate(lines):
        registrations = re.findall(r"\bPK[-\s]?([A-Z]{3})\b", line)
        if registrations:
            current_registration = registrations[-1]
        if not abnormal_keywords(line):
            continue
        for candidate_line in lines[max(0, index - 1) : min(len(lines), index + 2)]:
            scoped.update(re.findall(r"\bPK[-\s]?([A-Z]{3})\b", candidate_line))
        if current_registration:
            scoped.add(current_registration)

    if scoped:
        return sorted(f"PK-{registration}" for registration in scoped)
    if len(set(all_registrations)) == 1:
        return [f"PK-{all_registrations[0]}"]
    return []


def load_abnormal_evidence(db_path, timezone_name, from_date=None, to_date=None):
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id AS raw_message_id, message_timestamp_iso, text
            FROM raw_messages
            WHERE text IS NOT NULL AND TRIM(text) != ''
            ORDER BY id
            """
        ).fetchall()

    evidence = []
    for row in rows:
        keywords = abnormal_keywords(row["text"])
        if not keywords:
            continue
        operation_date = operation_date_for_timestamp(row["message_timestamp_iso"], timezone_name)
        if from_date and operation_date < from_date:
            continue
        if to_date and operation_date > to_date:
            continue
        registrations = registration_near_abnormal(row["text"])
        compact = re.sub(r"\s+", " ", row["text"]).strip()
        evidence.append(
            {
                "evidence_id": f"ABN-{int(row['raw_message_id']):08d}",
                "operation_date": operation_date,
                "registrations": registrations,
                "registration_list": ",".join(registrations),
                "keyword_list": ",".join(keywords),
                "raw_message_id": row["raw_message_id"],
                "message_timestamp_iso": row["message_timestamp_iso"],
                "snippet": compact[:300],
                "source_text": row["text"],
            }
        )
    return evidence


def build_sortie_rows(departures, matches, abnormal_evidence, pilot_index, timezone_name):
    evidence_by_aircraft_date = defaultdict(list)
    for evidence in abnormal_evidence:
        for registration in evidence["registrations"]:
            evidence_by_aircraft_date[(evidence["operation_date"], registration)].append(evidence)

    rows = []
    for departure in departures:
        operation_date = departure["operation_date_resolved"]
        metadata = route_metadata(departure["route_full"])
        matched = matches.get(departure["id"])
        evidence = evidence_by_aircraft_date.get((operation_date, departure["registration"]), [])
        notes = []
        pic_full, pic_issue = match_pilot(departure["pic_name"], "CAPT", pilot_index)
        sic_full, sic_issue = match_pilot(departure["sic_name"], "FO", pilot_index)
        notes.extend(issue for issue in (pic_issue, sic_issue) if issue)
        if matched and "missing_flight" in matched.get("_sortie_match_basis", ""):
            notes.append("arrival_ack_matched_with_missing_departure_flight_seq")
        if (
            matched
            and matched["event_datetime_utc"]
            and departure["event_datetime_utc"]
            and matched["event_datetime_utc"] < departure["event_datetime_utc"]
        ):
            notes.append("arrival_time_before_departure_within_30m")

        pax_total, pax_quality = parse_pax_total(departure["pax"])
        aircraft_type = aircraft_type_from_source(departure["aircraft_type"], departure["source_text"])
        if not aircraft_type:
            notes.append("aircraft_type_missing")
        for field in ("registration", "route_full", "pic_name", "takeoff_time"):
            if not departure[field]:
                notes.append(f"required_{field}_missing")

        if matched:
            mission_status = "completed_ack_received"
            confidence = "HIGH"
            completion_basis = "MVT Departure takeoff exists + MVT Arrival ACK matched"
        elif evidence:
            mission_status = "needs_review_departure_no_ack_abnormal_found"
            confidence = "REVIEW"
            completion_basis = "MVT Departure takeoff exists, no ACK, abnormal evidence found"
        else:
            mission_status = "assumed_completed_no_arrival_ack"
            confidence = "HIGH_ASSUMED"
            completion_basis = "MVT Departure takeoff exists, no abnormal evidence found"

        raw_flight_seq = str(departure["flight_seq"] or "").strip()
        flight_seq = raw_flight_seq.zfill(2) if raw_flight_seq else "NA"
        route_id = re.sub(r"\s+", "", str(departure["route_full"] or "UNKNOWN").upper())
        mission_id = (
            f"{operation_date}|{departure['registration']}|F{flight_seq}|"
            f"{route_id}|DEP{int(departure['raw_message_id']):08d}"
        )
        dep_datetime = departure["event_datetime_utc"]
        arr_datetime = matched["event_datetime_utc"] if matched else None
        row = {
            "mission_id": mission_id,
            "operation_date": operation_date,
            "registration": departure["registration"],
            "aircraft_type": aircraft_type,
            "aircraft_type_quality": "FROM_DEPARTURE_RAW" if aircraft_type else "MISSING",
            "flight_seq": departure["flight_seq"],
            "route_full": departure["route_full"],
            **metadata,
            "pic_raw": departure["pic_name"],
            "pic_full": pic_full,
            "sic_raw": departure["sic_name"],
            "sic_full": sic_full,
            "departure_time_z": departure["takeoff_time"],
            "departure_datetime_local": format_datetime_local(dep_datetime, timezone_name),
            "departure_datetime_utc": format_utc(dep_datetime),
            "pax": departure["pax"],
            "pax_total": pax_total,
            "pax_weight_kg": numeric(departure["pax_weight_kg"]),
            "baggage_kg": numeric(departure["baggage_kg"]),
            "cargo_text": departure["cargo_text"],
            "cargo_kg": numeric(departure["cargo_kg"]),
            "cargo_ton": metric_ton(departure["cargo_kg"]),
            "cargo_quality": cargo_quality(departure["cargo_text"], departure["cargo_kg"]),
            "total_load_kg": numeric(departure["total_load_kg"]),
            "total_load_ton": metric_ton(departure["total_load_kg"]),
            "arrival_ack_status": "ack_received" if matched else "ack_missing",
            "arrival_time_z": matched["ata_time"] if matched else None,
            "arrival_datetime_local": format_datetime_local(arr_datetime, timezone_name),
            "arrival_datetime_utc": format_utc(arr_datetime),
            "mission_status": mission_status,
            "confidence_level": confidence,
            "completion_basis": completion_basis,
            "productivity_include_flag": mission_status != "needs_review_departure_no_ack_abnormal_found",
            "load_quality": load_quality(departure),
            "pax_quality": pax_quality,
            "abnormal_flag_same_aircraft_date": "YES" if evidence else "NO",
            "abnormal_evidence_count": len(evidence),
            "abnormal_evidence_sample": evidence[0]["snippet"] if evidence else None,
            "departure_raw_message_id": departure["raw_message_id"],
            "arrival_raw_message_id": matched["raw_message_id"] if matched else None,
            "departure_movement_id": departure["id"],
            "arrival_movement_id": matched["id"] if matched else None,
            "parse_confidence": departure["parse_confidence"],
            "issue_notes": "; ".join(notes),
        }
        rows.append({header: row.get(header) for header in SORTIE_HEADERS})
    return sorted(rows, key=lambda row: (row["departure_datetime_utc"] or "", row["mission_id"]))


def build_arrival_exceptions(arrivals, used_arrival_ids, timezone_name):
    rows = []
    for arrival in arrivals:
        if arrival["id"] in used_arrival_ids:
            continue
        event_datetime = arrival["event_datetime_utc"]
        row = {
            "exception_id": f"ARR-{int(arrival['raw_message_id']):08d}",
            "operation_date": arrival["operation_date_resolved"],
            "registration": arrival["registration"],
            "aircraft_type": arrival["aircraft_type"],
            "flight_seq": arrival["flight_seq"],
            "from_reported": arrival["from_place"],
            "from_code": arrival["from_code"],
            "arrival_airport_code": arrival_destination(arrival),
            "arrival_time_z": arrival["ata_time"],
            "arrival_datetime_local": format_datetime_local(event_datetime, timezone_name),
            "arrival_datetime_utc": format_utc(event_datetime),
            "pax": arrival["pax"],
            "total_load_kg": numeric(arrival["total_load_kg"]),
            "arrival_raw_message_id": arrival["raw_message_id"],
            "arrival_movement_id": arrival["id"],
            "exception_reason": "arrival_only_no_matching_departure",
            "source_text": arrival["source_text"],
        }
        rows.append({header: row.get(header) for header in ARRIVAL_EXCEPTION_HEADERS})
    return sorted(rows, key=lambda row: (row["arrival_datetime_utc"] or "", row["exception_id"]))


def write_csv(path, headers, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


def replace_sheet(args, sheet_name, headers, rows):
    post_payload(
        args.webhook_url,
        {
            "token": args.token,
            "action": "deleteSheets",
            "deleteSheets": [sheet_name],
            "keepSheetName": os.environ.get("GOOGLE_SHEETS_RAW_TAB", "RAW"),
        },
        args.timeout_seconds,
        args.spreadsheet_id,
    )
    post_payload(
        args.webhook_url,
        {"token": args.token, "action": "ensureSheets", "sheets": [{"name": sheet_name, "headers": headers}]},
        args.timeout_seconds,
        args.spreadsheet_id,
    )
    appended = 0
    for start in range(0, len(rows), args.batch_size):
        batch = rows[start : start + args.batch_size]
        result = post_payload(
            args.webhook_url,
            {"token": args.token, "sheetName": sheet_name, "headers": headers, "rows": batch},
            args.timeout_seconds,
            args.spreadsheet_id,
        )
        appended += int(result.get("appended") or len(batch))
    return appended


def validate(rows, from_date=None, to_date=None):
    mission_ids = [row["mission_id"] for row in rows]
    failures = []
    if len(mission_ids) != len(set(mission_ids)):
        failures.append("duplicate_mission_id")
    for row in rows:
        if not row["departure_raw_message_id"]:
            failures.append(f"missing_departure_raw_message_id:{row['mission_id']}")
        if not row["departure_time_z"]:
            failures.append(f"missing_takeoff:{row['mission_id']}")
        if from_date and row["operation_date"] < from_date:
            failures.append(f"before_date_range:{row['mission_id']}")
        if to_date and row["operation_date"] > to_date:
            failures.append(f"after_date_range:{row['mission_id']}")
        for field in ("registration", "route_full", "from", "to", "pic_raw"):
            if not row[field]:
                failures.append(f"missing_{field}:{row['mission_id']}")
        if row["mission_status"] == "assumed_completed_no_arrival_ack" and row["confidence_level"] != "HIGH_ASSUMED":
            failures.append(f"invalid_assumed_confidence:{row['mission_id']}")
        if row["mission_status"] == "needs_review_departure_no_ack_abnormal_found" and row["confidence_level"] != "REVIEW":
            failures.append(f"invalid_review_confidence:{row['mission_id']}")
    return failures


def summary(rows, exceptions, abnormal, failures):
    status_counts = defaultdict(int)
    for row in rows:
        status_counts[row["mission_status"]] += 1
    return {
        "sortie_total": len(rows),
        "completed_ack_received": status_counts["completed_ack_received"],
        "assumed_completed_no_arrival_ack": status_counts["assumed_completed_no_arrival_ack"],
        "needs_review": status_counts["needs_review_departure_no_ack_abnormal_found"],
        "productivity_include": sum(bool(row["productivity_include_flag"]) for row in rows),
        "total_load_available_rows": sum(row["total_load_kg"] is not None for row in rows),
        "total_load_kg": round(sum(row["total_load_kg"] or 0 for row in rows), 3),
        "pax_total": sum(row["pax_total"] or 0 for row in rows),
        "cargo_kg": round(sum(row["cargo_kg"] or 0 for row in rows), 3),
        "arrival_only_exceptions": len(exceptions),
        "abnormal_evidence_rows": len(abnormal),
        "validation_failures": failures,
    }


def main():
    parser = argparse.ArgumentParser(description="Build the sortie-level gold dataset from WhatsApp movements")
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--pilot-mapping", default=DEFAULT_PILOT_MAPPING)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--from-date", default=DEFAULT_FROM_DATE)
    parser.add_argument("--to-date", default=DEFAULT_TO_DATE)
    parser.add_argument("--timezone", default=DEFAULT_OPERATION_TIMEZONE)
    parser.add_argument("--webhook-url", default=DEFAULT_WEBHOOK_URL)
    parser.add_argument("--token", default=DEFAULT_TOKEN)
    parser.add_argument("--spreadsheet-id", default=DEFAULT_SPREADSHEET_ID)
    parser.add_argument("--sortie-sheet", default=DEFAULT_SORTIE_SHEET)
    parser.add_argument("--exception-sheet", default=DEFAULT_EXCEPTION_SHEET)
    parser.add_argument("--abnormal-sheet", default=DEFAULT_ABNORMAL_SHEET)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--timeout-seconds", type=int, default=60)
    parser.add_argument("--lock-path", default=DEFAULT_LOCK)
    parser.add_argument("--sync", action="store_true")
    args = parser.parse_args()

    lock_path = Path(args.lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock_handle:
        try:
            fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SystemExit("SORTIE_LOG builder is already running") from exc

        departures, arrivals = load_movements(args.db, args.timezone)
        departures = dedupe_departures(filter_dates(departures, args.from_date, args.to_date))
        arrivals = filter_dates(arrivals, args.from_date, args.to_date)
        matches, used_arrivals = match_arrivals(departures, arrivals)
        abnormal = load_abnormal_evidence(args.db, args.timezone, args.from_date, args.to_date)
        pilot_index = load_pilot_index(args.pilot_mapping)
        sortie_rows = build_sortie_rows(departures, matches, abnormal, pilot_index, args.timezone)
        exception_rows = build_arrival_exceptions(arrivals, used_arrivals, args.timezone)
        abnormal_rows = [{header: row.get(header) for header in ABNORMAL_AUDIT_HEADERS} for row in abnormal]
        failures = validate(sortie_rows, args.from_date, args.to_date)

        output_dir = Path(args.output_dir)
        write_csv(output_dir / "SORTIE_LOG.csv", SORTIE_HEADERS, sortie_rows)
        write_csv(output_dir / "ARRIVAL_ACK_EXCEPTIONS.csv", ARRIVAL_EXCEPTION_HEADERS, exception_rows)
        write_csv(output_dir / "ABNORMAL_EVIDENCE_AUDIT.csv", ABNORMAL_AUDIT_HEADERS, abnormal_rows)

        result = summary(sortie_rows, exception_rows, abnormal_rows, failures)
        result.update({"ok": not failures, "status": "built", "output_dir": str(output_dir)})
        if args.sync:
            if failures:
                raise RuntimeError(f"SORTIE_LOG validation failed: {failures[:10]}")
            if not args.webhook_url or not args.token:
                raise RuntimeError("Google Sheets webhook URL and token are required for --sync")
            result["sheets"] = {
                args.sortie_sheet: replace_sheet(args, args.sortie_sheet, SORTIE_HEADERS, sortie_rows),
                args.exception_sheet: replace_sheet(
                    args, args.exception_sheet, ARRIVAL_EXCEPTION_HEADERS, exception_rows
                ),
                args.abnormal_sheet: replace_sheet(args, args.abnormal_sheet, ABNORMAL_AUDIT_HEADERS, abnormal_rows),
            }
            result["status"] = "synced"
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
