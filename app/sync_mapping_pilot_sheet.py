#!/usr/bin/env python3
import argparse
import itertools
import json
import os
import re
import sqlite3
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from io import BytesIO

from google_sheets_sync import DEFAULT_SPREADSHEET_ID, DEFAULT_TOKEN, DEFAULT_WEBHOOK_URL, post_payload


DEFAULT_DB = os.environ.get("OPS_DB_PATH", "data/ops_messages.sqlite3")
DEFAULT_SOURCE_SPREADSHEET_ID = os.environ.get(
    "MAPPING_PILOT_SOURCE_SPREADSHEET_ID",
    "1fAUbyfFrMw5VPK2hb_Ocg3-xjLjr_x6p",
)
DEFAULT_TARGET_SHEET_NAME = os.environ.get("GOOGLE_SHEETS_MAPPING_PILOT_TAB", "MAPPING_PILOT")
DEFAULT_OUTPUT = os.environ.get("OPS_PILOT_MAPPING_PATH", "data/reference/mapping_pilot.json")

XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
OFFICE_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

HEADERS = [
    "pilot_id",
    "pilot_name",
    "pilot_name_clean",
    "rank",
    "position",
    "rating",
    "mountain_qualification",
    "ccp",
    "fi",
    "gi",
    "notes",
    "initial_1",
    "initial_2",
    "initial_3",
    "initial_4",
    "match_keys",
    "observed_raw_values",
    "observed_raw_count",
    "initial_1_conflict_count",
    "mapping_status",
    "mapping_confidence",
    "source",
    "source_row_number",
]


def compact_space(value):
    text = str(value or "").replace("\u00a0", " ").replace("\r", " ").replace("\n", " ")
    return re.sub(r"\s+", " ", text).strip()


def clean_metadata(value):
    return compact_space(value)


def title_token(token):
    if len(token) == 1:
        return token.upper()
    if token.upper() in {"PIC", "SIC", "CCP", "FI", "GI", "PC", "EC"}:
        return token.upper()
    if token.isupper() or token.islower():
        return token.capitalize()
    return token[0].upper() + token[1:] if token else token


def title_text(value):
    text = compact_space(value)
    if not text or text == "-":
        return ""
    text = text.lower()
    return re.sub(r"[A-Za-zÀ-ÖØ-öø-ÿ]+", lambda match: title_token(match.group(0)), text)


def source_xlsx_url(spreadsheet_id):
    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export?format=xlsx"


def download_binary(url, timeout):
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def column_index(cell_ref):
    match = re.match(r"([A-Z]+)", cell_ref or "")
    if not match:
        return 0
    index = 0
    for char in match.group(1):
        index = index * 26 + ord(char) - 64
    return index - 1


def load_shared_strings(archive):
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    return ["".join(item.itertext()) for item in root.findall(f"{XLSX_NS}si")]


def cell_value(cell, shared_strings):
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        inline = cell.find(f"{XLSX_NS}is")
        return compact_space("".join(inline.itertext()) if inline is not None else "")

    value = cell.find(f"{XLSX_NS}v")
    if value is None:
        return ""
    raw = value.text or ""
    if cell_type == "s":
        try:
            return compact_space(shared_strings[int(raw)])
        except (IndexError, ValueError):
            return compact_space(raw)
    return compact_space(raw)


def workbook_sheets(archive):
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    relmap = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels.findall(f"{REL_NS}Relationship")}

    sheets = []
    for sheet in workbook.findall(f"{XLSX_NS}sheets/{XLSX_NS}sheet"):
        name = sheet.attrib["name"]
        rel_id = sheet.attrib[f"{OFFICE_REL_NS}id"]
        target = relmap[rel_id]
        path = target.lstrip("/") if target.startswith("/") else f"xl/{target}"
        sheets.append((name, path))
    return sheets


def read_xlsx_rows(content, sheet_name=None):
    with zipfile.ZipFile(BytesIO(content)) as archive:
        shared_strings = load_shared_strings(archive)
        sheets = workbook_sheets(archive)
        if not sheets:
            return []

        selected_path = sheets[0][1]
        if sheet_name:
            for name, path in sheets:
                if name == sheet_name:
                    selected_path = path
                    break

        root = ET.fromstring(archive.read(selected_path))
        rows = []
        for sheet_row in root.findall(f".//{XLSX_NS}sheetData/{XLSX_NS}row"):
            values = []
            for cell in sheet_row.findall(f"{XLSX_NS}c"):
                index = column_index(cell.attrib.get("r"))
                while len(values) <= index:
                    values.append("")
                values[index] = cell_value(cell, shared_strings)
            rows.append(values)
        return rows


def header_key(value):
    text = compact_space(value).lower()
    text = text.replace("&", "and")
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


def find_header_row(rows):
    for index, row in enumerate(rows):
        keys = {header_key(value) for value in row}
        if "name" in keys and ("position_pic_sic" in keys or "position_pic_and_sic" in keys):
            return index
    raise RuntimeError("Cannot find pilot master header row")


def row_value(row, positions, *keys):
    for key in keys:
        index = positions.get(key)
        if index is not None and index < len(row):
            return clean_metadata(row[index])
    return ""


def split_rank(name):
    text = compact_space(name)
    match = re.match(r"^(CAPT|CAPTAIN|FO|F/O|FIRST OFFICER)\.?\s*(.*)$", text, re.IGNORECASE)
    if not match:
        return "", text
    raw_rank = match.group(1).upper()
    rank = "CAPT" if raw_rank in {"CAPT", "CAPTAIN"} else "FO"
    return rank, compact_space(match.group(2))


def rank_display(rank):
    if rank == "CAPT":
        return "Capt."
    if rank == "FO":
        return "FO."
    return ""


def format_pilot_name(raw_name):
    rank, clean_name = split_rank(raw_name)
    display_name = title_text(clean_name)
    display_rank = rank_display(rank)
    return f"{display_rank} {display_name}".strip(), display_name, rank


def name_tokens(clean_name):
    text = re.sub(r"[^A-Za-z0-9]+", " ", clean_name.upper())
    return [token for token in text.split() if token]


def initials(tokens):
    return "".join(token[0] for token in tokens if token)


def first_last_initials(tokens):
    if len(tokens) < 2:
        return initials(tokens)
    return f"{tokens[0][0]}{tokens[-1][0]}"


def first_with_last_prefix(tokens):
    if len(tokens) < 2:
        return tokens[0][:3] if tokens else ""
    return f"{tokens[0][0]}{tokens[-1][:2]}"


def consonant_prefix(token, length=3):
    consonants = re.sub(r"[AEIOU]", "", token.upper())
    return consonants[:length]


def short_initial_variants(value):
    variants = []
    if 2 <= len(value) <= 4:
        variants.extend(value[:index] for index in range(2, len(value) + 1))
    if len(value) == 3:
        variants.extend("".join(chars) for chars in itertools.permutations(value, 3))
    elif len(value) == 4:
        variants.extend("".join(chars) for chars in itertools.permutations(value[:3], 3))
        variants.extend("".join(chars) for chars in itertools.permutations(value[1:], 3))
    return variants


def dedupe(values):
    result = []
    seen = set()
    for value in values:
        value = compact_space(value).upper()
        if not value or value in seen:
            continue
        result.append(value)
        seen.add(value)
    return result


def pilot_aliases(tokens):
    values = []
    if tokens:
        all_initials = initials(tokens)
        non_single_initials = initials([token for token in tokens if len(token) > 1])
        values.append(all_initials)
        values.extend(short_initial_variants(all_initials))
        values.append(non_single_initials)
        values.extend(short_initial_variants(non_single_initials))
        values.append(first_last_initials(tokens))
        values.append(first_with_last_prefix(tokens))
        values.append(tokens[0])
        values.append(tokens[-1])
        values.append(" ".join(tokens))
        values.append("".join(tokens))
        values.append(consonant_prefix(tokens[0]))
        values.append(consonant_prefix(tokens[-1]))

    if len(tokens) >= 2:
        values.append(f"{tokens[0]} {tokens[-1][0]}")
        values.append(f"{tokens[0]} {tokens[1]}")
        values.append(f"{tokens[0][0]} {tokens[1]}")
        values.append(f"{tokens[0][0]}{tokens[1]}")

    for token in tokens:
        if len(token) >= 4:
            values.append(token)
            values.append(token[:3])
            values.append(consonant_prefix(token))

    return dedupe(values)


def normalize_observed_crew(value):
    text = compact_space(value)
    rank, rest = split_rank(text)
    rest = re.sub(r"[^A-Za-z0-9]+", " ", rest.upper())
    return rank, compact_space(rest)


def normalize_rows(xlsx_rows):
    header_index = find_header_row(xlsx_rows)
    headers = [header_key(value) for value in xlsx_rows[header_index]]
    positions = {header: index for index, header in enumerate(headers)}

    rows = []
    for source_row_number, raw_row in enumerate(xlsx_rows[header_index + 1 :], start=header_index + 2):
        pilot_id = row_value(raw_row, positions, "no")
        raw_name = row_value(raw_row, positions, "name")
        if not pilot_id or not raw_name:
            continue

        pilot_name, pilot_name_clean, rank = format_pilot_name(raw_name)
        tokens = name_tokens(pilot_name_clean)
        aliases = pilot_aliases(tokens)
        initials_list = dedupe(
            [
                initials(tokens),
                initials([token for token in tokens if len(token) > 1]),
                first_last_initials(tokens),
                first_with_last_prefix(tokens),
            ]
        )
        while len(initials_list) < 4:
            initials_list.append("")

        rows.append(
            {
                "pilot_id": pilot_id,
                "pilot_name": pilot_name,
                "pilot_name_clean": pilot_name_clean,
                "rank": rank,
                "position": row_value(raw_row, positions, "position_pic_sic", "position_pic_and_sic"),
                "rating": row_value(raw_row, positions, "rating"),
                "mountain_qualification": row_value(raw_row, positions, "montain_qualification", "mountain_qualification"),
                "ccp": row_value(raw_row, positions, "ccp"),
                "fi": row_value(raw_row, positions, "fi"),
                "gi": row_value(raw_row, positions, "gi"),
                "notes": row_value(raw_row, positions, "notes"),
                "initial_1": initials_list[0],
                "initial_2": initials_list[1],
                "initial_3": initials_list[2],
                "initial_4": initials_list[3],
                "match_keys": ", ".join(aliases),
                "_match_keys": aliases,
                "observed_raw_values": "",
                "observed_raw_count": 0,
                "initial_1_conflict_count": 0,
                "mapping_status": "candidate",
                "mapping_confidence": 0.75,
                "source": "master_pilot_sheet",
                "source_row_number": source_row_number,
            }
        )
    return rows


def connect(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def observed_crew_values(db_path):
    if not os.path.exists(db_path):
        return []

    results = []
    with connect(db_path) as conn:
        for column_name in ("pic_name", "sic_name"):
            for row in conn.execute(
                f"""
                SELECT {column_name} AS value, COUNT(*) AS total
                FROM flight_movements
                WHERE {column_name} IS NOT NULL
                  AND TRIM({column_name}) != ''
                GROUP BY {column_name}
                ORDER BY COUNT(*) DESC
                """
            ):
                rank, key = normalize_observed_crew(row["value"])
                if not key:
                    continue
                results.append(
                    {
                        "value": compact_space(row["value"]),
                        "rank": rank,
                        "key": key,
                        "total": int(row["total"] or 0),
                    }
                )
    return results


def apply_conflict_metadata(rows):
    groups = {}
    for row in rows:
        initial = row["initial_1"]
        if not initial:
            continue
        groups.setdefault(initial, []).append(row)

    for group in groups.values():
        if len(group) <= 1:
            continue
        for row in group:
            row["initial_1_conflict_count"] = len(group)
            if row["mapping_status"] == "candidate":
                row["mapping_status"] = "needs_review"
                row["mapping_confidence"] = 0.5


def apply_observed_aliases(rows, observed):
    key_map = {}
    for index, row in enumerate(rows):
        for key in row["_match_keys"]:
            key_map.setdefault((row["rank"], key), []).append(index)
            key_map.setdefault(("", key), []).append(index)

    matched = {}
    unmatched = []
    ambiguous = []
    for item in observed:
        if item["rank"]:
            candidates = key_map.get((item["rank"], item["key"])) or []
        else:
            candidates = key_map.get(("", item["key"])) or []
        candidates = sorted(set(candidates))
        if len(candidates) == 1:
            matched.setdefault(candidates[0], []).append(item)
        elif len(candidates) > 1:
            ambiguous.append(item)
        else:
            unmatched.append(item)

    for index, items in matched.items():
        row = rows[index]
        total = sum(item["total"] for item in items)
        row["observed_raw_values"] = "; ".join(f"{item['value']} ({item['total']})" for item in items)
        row["observed_raw_count"] = total
        row["mapping_status"] = "matched_from_ops"
        row["mapping_confidence"] = 0.95 if row["initial_1_conflict_count"] == 0 else 0.7

    return {"matched": matched, "unmatched": unmatched, "ambiguous": ambiguous}


def sheet_rows(rows):
    return [{key: row.get(key, "") for key in HEADERS} for row in rows]


def save_json(path, rows):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    payload = []
    for row in rows:
        item = {key: value for key, value in row.items() if key != "_match_keys"}
        item["match_keys"] = [key.strip() for key in str(row.get("match_keys") or "").split(",") if key.strip()]
        payload.append(item)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
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
    output_rows = sheet_rows(rows)
    for start in range(0, len(output_rows), args.batch_size):
        batch = output_rows[start : start + args.batch_size]
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
    parser = argparse.ArgumentParser(description="Create a normalized MAPPING_PILOT sheet from the source pilot master")
    parser.add_argument("--source-spreadsheet-id", default=DEFAULT_SOURCE_SPREADSHEET_ID)
    parser.add_argument("--source-sheet-name", default=None)
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

    content = download_binary(source_xlsx_url(args.source_spreadsheet_id), args.timeout_seconds)
    rows = normalize_rows(read_xlsx_rows(content, args.source_sheet_name))
    apply_conflict_metadata(rows)
    observed_stats = apply_observed_aliases(rows, observed_crew_values(args.db))
    save_json(args.output, rows)

    appended = 0 if args.dry_run else post_rows(args, rows)
    print(
        json.dumps(
            {
                "ok": True,
                "source_spreadsheet_id": args.source_spreadsheet_id,
                "sheet_name": args.sheet_name,
                "output": args.output,
                "rows": len(rows),
                "appended": appended,
                "matched_observed_aliases": sum(len(items) for items in observed_stats["matched"].values()),
                "unmatched_observed_aliases": len(observed_stats["unmatched"]),
                "ambiguous_observed_aliases": len(observed_stats["ambiguous"]),
                "unmatched_sample": observed_stats["unmatched"][:20],
                "ambiguous_sample": observed_stats["ambiguous"][:20],
                "dry_run": args.dry_run,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
