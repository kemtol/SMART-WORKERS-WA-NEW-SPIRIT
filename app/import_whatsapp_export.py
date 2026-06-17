#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import re
import tempfile
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from ingest_service import Store


DEFAULT_DB = os.environ.get("OPS_DB_PATH", "data/ops_messages.sqlite3")
DEFAULT_GROUP_NAME = "New Spirit"
DEFAULT_GROUP_JID = "6282114137183-1490316198@g.us"
DEFAULT_TIMEZONE = "Asia/Jakarta"
DEFAULT_FROM_ISO = "2026-06-15T07:30:07Z"
DEFAULT_TO_ISO = "2026-06-17T04:07:00Z"

MESSAGE_START_RE = re.compile(
    r"^(\d{1,2}/\d{1,2}/\d{2,4}),\s+(\d{1,2}:\d{2})(?:[:.]\d{2})?\s*([AP]M)\s+-\s+(.*)$",
    re.IGNORECASE,
)


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


def parse_iso(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def parse_export_datetime(date_text, time_text, am_pm, tz):
    year_len = len(date_text.rsplit("/", 1)[-1])
    fmt = "%m/%d/%Y %I:%M %p" if year_len == 4 else "%m/%d/%y %I:%M %p"
    naive = datetime.strptime(f"{date_text} {time_text} {am_pm.upper()}", fmt)
    return naive.replace(tzinfo=tz).astimezone(timezone.utc)


def normalize_line(line):
    return line.replace("\u202f", " ").replace("\u00a0", " ")


def stable_message_id(timestamp_iso, sender, text):
    digest = hashlib.sha1(f"{timestamp_iso}\n{sender or ''}\n{text}".encode("utf-8")).hexdigest()[:20]
    return f"export-{digest}"


def sender_to_export_jid(sender):
    if not sender:
        return "system@export"
    digits = re.sub(r"\D", "", sender)
    if digits:
        return f"{digits}@export"
    slug = re.sub(r"[^a-z0-9]+", "-", sender.lower()).strip("-") or "unknown"
    return f"{slug}@export"


def read_export_text(path):
    path = Path(path)
    data = path.read_bytes()
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            names = [name for name in archive.namelist() if name.lower().endswith(".txt")]
            if not names:
                raise RuntimeError(f"No .txt chat export found in {path}")
            data = archive.read(names[0])
    for encoding in ("utf-8-sig", "utf-8", "utf-16", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def download_drive_file(file_id, output_path):
    url = f"https://drive.google.com/uc?export=download&id={file_id}"
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=60) as response, open(output_path, "wb") as handle:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            handle.write(chunk)
    return output_path


def parse_messages(text, tz):
    messages = []
    current = None
    for raw_line in text.splitlines():
        line = normalize_line(raw_line)
        match = MESSAGE_START_RE.match(line)
        if match:
            if current:
                current["text"] = "\n".join(current.pop("lines")).strip()
                messages.append(current)
            date_text, time_text, am_pm, body = match.groups()
            timestamp = parse_export_datetime(date_text, time_text, am_pm, tz)
            sender = None
            message = body
            if ": " in body:
                sender, message = body.split(": ", 1)
            current = {"timestamp": timestamp, "sender": sender, "lines": [message]}
        elif current:
            current["lines"].append(line)
    if current:
        current["text"] = "\n".join(current.pop("lines")).strip()
        messages.append(current)
    return messages


def should_import(message, from_dt, to_dt):
    timestamp = message["timestamp"]
    if from_dt and timestamp <= from_dt:
        return False
    if to_dt and timestamp >= to_dt:
        return False
    return True


def import_messages(args, messages):
    store = Store(args.db)
    from_dt = parse_iso(args.from_iso)
    to_dt = parse_iso(args.to_iso)
    imported = 0
    duplicates = 0
    skipped = 0
    parsed_movements = 0
    first_iso = None
    last_iso = None

    for message in messages:
        if not should_import(message, from_dt, to_dt):
            skipped += 1
            continue

        timestamp = int(message["timestamp"].timestamp())
        timestamp_iso = message["timestamp"].isoformat().replace("+00:00", "Z")
        sender = message["sender"] or ""
        text = message["text"]
        payload = {
            "source": args.source,
            "id": stable_message_id(timestamp_iso, sender, text),
            "remoteJid": args.group_jid,
            "senderJid": sender_to_export_jid(sender),
            "fromMe": False,
            "timestamp": timestamp,
            "timestampIso": timestamp_iso,
            "type": "whatsapp_export",
            "text": text,
            "groupName": args.group_name,
            "receivedAt": timestamp_iso,
            "raw": {"exportSender": sender, "exportTimestampIso": timestamp_iso},
        }
        result = store.insert_message(payload)
        if result.get("duplicate"):
            duplicates += 1
        else:
            imported += 1
            parsed_movements += int(result.get("movements") or 0)
            first_iso = first_iso or timestamp_iso
            last_iso = timestamp_iso

    return {
        "messages_seen": len(messages),
        "messages_imported": imported,
        "duplicates": duplicates,
        "skipped_outside_range": skipped,
        "movements_parsed": parsed_movements,
        "first_imported_at": first_iso,
        "last_imported_at": last_iso,
    }


def main():
    load_local_env()
    parser = argparse.ArgumentParser(description="Import WhatsApp group chat export ZIP/TXT into local SQLite")
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--file")
    parser.add_argument("--drive-file-id")
    parser.add_argument("--group-name", default=DEFAULT_GROUP_NAME)
    parser.add_argument("--group-jid", default=DEFAULT_GROUP_JID)
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    parser.add_argument("--from-iso", default=DEFAULT_FROM_ISO)
    parser.add_argument("--to-iso", default=DEFAULT_TO_ISO)
    parser.add_argument("--source", default="whatsapp_export")
    args = parser.parse_args()

    source_file = args.file
    with tempfile.TemporaryDirectory() as temp_dir:
        if args.drive_file_id:
            source_file = download_drive_file(args.drive_file_id, os.path.join(temp_dir, "whatsapp-export.zip"))
        if not source_file:
            raise SystemExit("Provide --file or --drive-file-id")

        text = read_export_text(source_file)
        messages = parse_messages(text, ZoneInfo(args.timezone))
        result = import_messages(args, messages)
        print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
