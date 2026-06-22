import json
import os
import re
from datetime import datetime


DEFAULT_MAPPING_PATH = os.environ.get("OPS_AIRPORT_MAPPING_PATH", "config/airport_mappings.json")
DEFAULT_INTERNAL_MASTER_PATH = os.environ.get("OPS_INTERNAL_AIRPORT_MASTER_PATH", "data/reference/master_iata.json")

MONTHS_ID = {
    "JANUARI": 1,
    "FEBRUARI": 2,
    "MARET": 3,
    "APRIL": 4,
    "MEI": 5,
    "JUNI": 6,
    "JULI": 7,
    "AGUSTUS": 8,
    "SEPTEMBER": 9,
    "OKTOBER": 10,
    "NOVEMBER": 11,
    "DESEMBER": 12,
}


def clean_text(value):
    text = value or ""
    text = text.replace("```", "")
    text = text.replace("*", "")
    return "\n".join(line.strip() for line in text.splitlines())


def normalize_time(value):
    if not value:
        return None
    match = re.search(r"([0-2]?\d)[ \t]*[:.][ \t]*([0-5]\d)", value)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    if hour > 23:
        return None
    return f"{hour:02d}:{minute:02d}"


def parse_number_kg(value):
    raw = (value or "").strip()
    if not raw or raw in {"-", "-KG", "- KG"}:
        return None
    match = re.search(r"(\d[\d.,]*)", raw)
    if not match:
        return None
    number = match.group(1)
    if "," in number and "." in number:
        number = number.replace(".", "").replace(",", ".")
    elif "." in number:
        parts = number.split(".")
        if len(parts[-1]) == 3:
            number = "".join(parts)
    else:
        number = number.replace(",", ".")
    try:
        parsed = float(number)
    except ValueError:
        return None
    return int(parsed) if parsed.is_integer() else parsed


def first_match(pattern, text, flags=re.IGNORECASE):
    match = re.search(pattern, text, flags)
    return match.group(1).strip() if match else None


def empty_to_none(value):
    if value is None:
        return None
    cleaned = value.strip().strip("`").strip()
    return None if cleaned in {"", "-", ":", ":-"} else cleaned


def extract_crew(text):
    pic = first_match(r"(?im)^PIC[ \t]*:?[ \t]*([^\n]+)$", text)
    sic = first_match(r"(?im)^(?:SIC|FIRST[ \t]+OFFICER|F/O)[ \t]*:?[ \t]*([^\n]+)$", text)
    pic = empty_to_none(pic)
    sic = empty_to_none(sic)

    lines = []
    if pic:
        lines.append(f"PIC: {pic}")
    if sic:
        lines.append(f"SIC: {sic}")

    return {
        "pic_name": pic,
        "sic_name": sic,
        "crew_text": "\n".join(lines) if lines else None,
    }


def parse_operation_date(text):
    match = re.search(
        r"\b(\d{1,2})\s+(JANUARI|FEBRUARI|MARET|APRIL|MEI|JUNI|JULI|AGUSTUS|SEPTEMBER|OKTOBER|NOVEMBER|DESEMBER)\s+(\d{4})\b",
        text.upper(),
    )
    if not match:
        return None
    day = int(match.group(1))
    month = MONTHS_ID[match.group(2)]
    year = int(match.group(3))
    return datetime(year, month, day).date().isoformat()


def normalize_aircraft_type(value):
    if not value:
        return None
    aircraft = re.sub(r"\s+", "", value.upper())
    aircraft = aircraft.replace("BEX", "B-EX")
    aircraft = aircraft.replace("208BEX", "208B-EX")
    return aircraft


def normalize_code(value):
    if not value:
        return None
    code = re.sub(r"[^A-Z0-9]", "", value.upper())
    return code or None


def clean_master_value(value):
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text in {"", "-"} else text


def load_mapping(path=DEFAULT_MAPPING_PATH, internal_master_path=DEFAULT_INTERNAL_MASTER_PATH):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            mapping = json.load(handle)
    except FileNotFoundError:
        mapping = {"airports": {}, "aliases": {}}

    airports = mapping.setdefault("airports", {})
    try:
        with open(internal_master_path, "r", encoding="utf-8") as handle:
            rows = json.load(handle)
    except FileNotFoundError:
        return mapping

    for row in rows:
        code = clean_master_value(row.get("code")).upper()
        if not code:
            continue
        airports[code] = {
            "iata": code,
            "icao": clean_master_value(row.get("icao_code")).upper(),
            "name": clean_master_value(row.get("airport_name")),
            "municipality": clean_master_value(row.get("location")),
            "province": clean_master_value(row.get("province_name")),
            "timezone": clean_master_value(row.get("timezone")),
            "source": "internal_master",
        }

    return mapping


def airport_for_token(token, mapping):
    if not token:
        return None
    raw = token.strip()
    code = normalize_code(raw)
    airports = mapping.get("airports", {})
    aliases = mapping.get("aliases", {})

    if code in airports:
        return {**airports[code], "ops_code": code}

    alias_code = aliases.get(raw.upper()) or aliases.get(code)
    if alias_code and alias_code in airports:
        return {**airports[alias_code], "ops_code": alias_code}

    if code and len(code) == 3:
        return {"ops_code": code, "iata": "", "icao": "", "name": raw, "municipality": "", "source": "unmapped"}

    return {"ops_code": "", "iata": "", "icao": "", "name": raw, "municipality": "", "source": "free_text"}


def airport_fields(prefix, airport):
    airport = airport or {}
    return {
        f"{prefix}_code": airport.get("ops_code"),
        f"{prefix}_name": airport.get("name"),
        f"{prefix}_icao": airport.get("icao"),
        f"{prefix}_iata": airport.get("iata"),
    }


def split_route(route):
    if not route:
        return []
    route = re.sub(r"\s+", " ", route.strip())
    return [part.strip() for part in re.split(r"\s*[-/–—]\s*", route) if part.strip()]


def extract_route(text):
    match = re.search(r"\bRUT[EA]\s*:\s*([^\n]+)", text, re.IGNORECASE)
    if not match:
        return None
    return match.group(1).strip()


def extract_next(text):
    match = re.search(r"\bNEXT\s*:?\s*([^\n]+)", text, re.IGNORECASE)
    return match.group(1).strip() if match else None


def extract_load(pattern, text):
    value = first_match(pattern, text)
    return value, parse_number_kg(value)


def base_fields(text, mapping):
    registration = first_match(r"\bPK[-\s]?([A-Z]{3})\b", text)
    aircraft = first_match(r"\b(C\s*208\s*B?(?:\s*[- ]?\s*EX)?)\b", text)
    flight_seq = first_match(r"\b(?:FLIGHT|SORTIE)\s*0*([0-9]+)\b", text)
    pax = first_match(r"(?im)^PAX[ \t]*:?[ \t]*([^\n]+)$", text)
    pax_weight_raw, pax_weight_kg = extract_load(r"(?im)^PAX[ \t]+(?:WEIGHT|LOAD)[ \t]*:?[ \t]*([^\n]+)$", text)
    baggage_raw, baggage_kg = extract_load(r"(?im)^(?:BGE|BAG)[ \t]*:?[ \t]*([^\n]+)$", text)
    cargo_text, cargo_kg = extract_load(r"(?im)^(?:CGO|CARGO)[ \t]*:?[ \t]*([^\n]+)$", text)
    total_raw, total_load_kg = extract_load(r"(?im)^(?:TOTAL|TTL)[ \t]+LOAD[ \t]*:?[ \t]*([^\n]+)$", text)

    result = {
        "operation_date": parse_operation_date(text),
        "registration": f"PK-{registration.upper()}" if registration else None,
        "aircraft_type": normalize_aircraft_type(aircraft),
        "flight_seq": flight_seq.zfill(2) if flight_seq else None,
        "pax": empty_to_none(pax),
        "pax_weight_kg": pax_weight_kg,
        "baggage_kg": baggage_kg,
        "cargo_text": empty_to_none(cargo_text),
        "cargo_kg": cargo_kg,
        "total_load_kg": total_load_kg,
        "remark": empty_to_none(first_match(r"(?im)^REMARK[ \t]*:?[ \t]*([^\n]+)$", text)),
    }
    result.update(extract_crew(text))
    return result


def parse_departure(text, mapping):
    route_full = extract_route(text)
    route_parts = split_route(route_full)
    if len(route_parts) < 2:
        return []

    engine_start_time = normalize_time(
        first_match(
            r"\b(?:ENGINE[ \t]+START|ENG\.?[ \t]*ON)[ \t]*:?[ \t]*([0-2]?\d[ \t]*[:.][ \t]*[0-5]\d[ \t]*z?)",
            text,
        )
    )
    takeoff_time = normalize_time(
        first_match(
            r"\b(?:TAKE[ \t]*OFF|ATD(?:[ \t]+[A-Z]{3})?)[ \t]*:?[ \t]*([0-2]?\d[ \t]*[:.][ \t]*[0-5]\d[ \t]*z?)",
            text,
        )
    )
    eta_airport_code = first_match(r"\bETA[ \t]+([A-Z]{3})[ \t]*:?[ \t]*[0-2]?\d[ \t]*[:.][ \t]*[0-5]\d", text)
    eta_time = normalize_time(first_match(r"\bETA[ \t]+[A-Z]{3}[ \t]*:?[ \t]*([0-2]?\d[ \t]*[:.][ \t]*[0-5]\d[ \t]*z?)", text))
    eta_airport = airport_for_token(eta_airport_code, mapping) if eta_airport_code else None

    rows = []
    for index, (origin_token, destination_token) in enumerate(zip(route_parts, route_parts[1:]), start=1):
        origin = airport_for_token(origin_token, mapping)
        destination = airport_for_token(destination_token, mapping)
        row = {
            "movement_type": "departure",
            "leg_index": index,
            "route_full": route_full,
            "from_place": None,
            "next_route": None,
            "next_text": None,
            "engine_start_time": engine_start_time if index == 1 else None,
            "takeoff_time": takeoff_time if index == 1 else None,
            "eta_time": eta_time if eta_airport and destination.get("ops_code") == eta_airport.get("ops_code") else None,
            "ata_time": None,
            "parse_confidence": 0.9,
        }
        row.update(airport_fields("leg_origin", origin))
        row.update(airport_fields("leg_destination", destination))
        row.update(airport_fields("from", None))
        row.update(airport_fields("arrival_airport", None))
        row.update(airport_fields("eta_airport", eta_airport if row["eta_time"] else None))
        row.update(airport_fields("ata_airport", None))
        rows.append(row)

    return rows


def parse_arrival(text, mapping):
    is_sortie_movement = "ARRIVAL MOVEMENT SORTIE" in text.upper()
    from_place = None if is_sortie_movement else first_match(r"\bFROM\s*:?\s*([^\n]+)", text)
    from_airport = airport_for_token(from_place, mapping) if from_place else None
    ata_airport_code = first_match(r"\bATA[ \t]+([A-Z]{3})[ \t]*:?[ \t]*[0-2]?\d[ \t]*[:.][ \t]*[0-5]\d", text)
    ata_time = normalize_time(first_match(r"\bATA[ \t]+[A-Z]{3}[ \t]*:?[ \t]*([0-2]?\d[ \t]*[:.][ \t]*[0-5]\d[ \t]*z?)", text))
    ata_airport = airport_for_token(ata_airport_code, mapping) if ata_airport_code else None
    next_text = extract_next(text)
    next_route = None if not next_text or re.search(r"FULL\s*STOP", next_text, re.IGNORECASE) else next_text

    row = {
        "movement_type": "arrival",
        "leg_index": 1,
        "route_full": extract_route(text) if is_sortie_movement else None,
        "from_place": from_place,
        "next_route": next_route,
        "next_text": next_text,
        "engine_start_time": None,
        "takeoff_time": None,
        "eta_time": None,
        "ata_time": ata_time,
        "parse_confidence": 0.9 if ata_airport and ata_time else 0.75,
    }
    row.update(airport_fields("leg_origin", from_airport))
    row.update(airport_fields("leg_destination", ata_airport))
    row.update(airport_fields("from", from_airport))
    row.update(airport_fields("arrival_airport", ata_airport))
    row.update(airport_fields("eta_airport", None))
    row.update(airport_fields("ata_airport", ata_airport))
    return [row]


def parse_movements(text, mapping=None):
    mapping = mapping or load_mapping()
    cleaned = clean_text(text)
    upper = cleaned.upper()
    is_sortie_movement = "DEPARTURE MOVEMENT SORTIE" in upper or "ARRIVAL MOVEMENT SORTIE" in upper
    if "MVT" not in upper and not is_sortie_movement:
        return []

    base = base_fields(cleaned, mapping)
    if "ARRIVAL" in upper:
        rows = parse_arrival(cleaned, mapping)
    elif "DEPARTURE MOVEMENT SORTIE" in upper or "DEPT" in upper or "DEP" in upper:
        rows = parse_departure(cleaned, mapping)
    else:
        return []

    for row in rows:
        row.update(base)
    return rows
