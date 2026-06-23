# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Hybrid Node.js + Python pipeline that reads operational messages from the "New Spirit" WhatsApp group (an Indonesian aviation ops channel), parses flight movements with rule-based regex, builds a sortie-level gold dataset, and reconciles against the AMS AFML (Aircraft Maintenance Log) read-only API. The data flows as **Bronze (RAW) → Silver (FLIGHT_RAW, FLIGHT_TIMELINE) → Gold (SORTIE_LOG)**, all mirrored to Google Sheets. `README.md` is the authoritative spec/PRD and is written in Indonesian; code, identifiers, and commit messages are in English.

## Common commands

**Lifecycle (primary entry points — supersede running workers individually):**
- `./connect.sh` — shows WhatsApp QR if needed, then starts all workers (listener, ingest, sheets-sync, afml-sync) under systemd user services (or detached fallback). `--reset` wipes Baileys auth and forces a fresh QR.
- `./status.sh` — health of systemd units, fallback processes, ingest, listener, and sheet sync.
- `./stop.sh` — graceful shutdown and disable auto-start.

**Tests** (Python `unittest`, no Node-side tests):
- `npm run test` → `python3 -m unittest discover -s tests -v`
- Single file: `python3 -m unittest tests.test_movement_parser -v`
- Single method: `python3 -m unittest tests.test_movement_parser.ClassName.test_method -v`

**Manual worker runs (debug only — production uses lifecycle scripts):**
- `npm run listen` (Baileys listener), `npm run ingest` (Python HTTP receiver on :8088)
- `npm run sheets:sync`, `npm run afml:sync`, `npm run sortie:build`

**Other useful scripts:**
- `npm run movements:rebuild` — re-parse all raw messages into `flight_movements` (silver).
- `npm run master:iata:sync`, `npm run mapping:pilot:sync` — refresh reference data from upstream Google Sheets.
- `npm run sheets:replace-flight-raw` / `:replace-raw` / `:replace-flight-timeline` — destructive sheet rewrites.

No build step. Python runs directly. Node uses ESM (`"type": "module"` in `package.json`, requires Node ≥22).

## Architecture

**Two runtimes, one SQLite:**
- `src/` — Node.js Baileys listener. `listen-new-messages.js` is the live process; it POSTs new messages to the Python ingest service at `localhost:8088`.
- `app/` — Python pipeline. Key modules:
  - `ingest_service.py` — HTTP receiver, writes to SQLite.
  - `movement_parser.py` — rule-based parsing for MVT departure/arrival and fire-patrol `DEPARTURE/ARRIVAL MOVEMENT SORTIE` formats.
  - `build_sortie_log.py` — gold builder. One row per mission: dedup-on-departure, match arrival ACK within a 30-min window, normalize crew via `mapping_pilot.json`.
  - `afml_sync.py` — authenticated AMS collector + reconciler.
  - `google_sheets_sync.py`, `sync_master_iata_sheet.py`, `sync_mapping_pilot_sheet.py`.
- Shared state: `data/ops_messages.sqlite3` (tables: `raw_messages`, `flight_movements`, `afml_records`, `afml_legs`, `afml_snapshots`, `afml_reconciliation`).

**Data layers:**
- Bronze — `raw_messages` table / `RAW` sheet tab.
- Silver — `flight_movements` table / `FLIGHT_RAW` + `FLIGHT_TIMELINE` tabs. Status field: `pending` / `needs_parse` / `cleaned`.
- Gold — `SORTIE_LOG` tab plus audit tabs `ARRIVAL_ACK_EXCEPTIONS` and `ABNORMAL_EVIDENCE_AUDIT`.
- AFML — `AFML_RAW`, `AFML_LEGS`, `AFML_RECON`. Matching key: date + registration + route + takeoff time (±120 min). Conflict flags: `TIME_CONFLICT`, `CREW_CONFLICT`, `WA_ONLY`, `AFML_INCOMPLETE`.

**Reference data (git-tracked):** `data/reference/master_iata.json`, `data/reference/mapping_pilot.json`, `data/reference/pilot_callsigns.json`. These JSON files are the runtime source of truth for parsers and crew normalization; the `master:iata:sync` and `mapping:pilot:sync` commands rebuild them from upstream Google Sheets.

**Worker orchestration:** `bin/worker-common.sh` provides shared systemd/fallback lifecycle helpers used by all `bin/run-*.sh` loop runners. `connect.sh` registers systemd user services (or detaches as fallback). Reboot survival requires `loginctl enable-linger "$USER"`.

**Google Sheets integration:** all writes go through an Apps Script Web App webhook (`integrations/google-sheets-webhook.gs`); Python code never calls the Sheets API directly. Webhook URL and token live in `config/google-sheets.env`.

## Configuration (not in git)

- `config/google-sheets.env` — webhook URL, token, spreadsheet ID, tab names, timezone (`Asia/Jakarta`), master sheet IDs.
- `config/afml.env` — AMS read-only credentials, mode 600.
- `.runtime-auth/listener/` — Baileys session storage. Wiped by `./connect.sh --reset`.

## Conventions worth knowing

- All dates/times are `Asia/Jakarta`.
- The `Movements_Internal` sheet tab is **legacy** — do not write to it.
- AI deep-clean enrichment for low-quality FLIGHT_RAW fields is specced in `prompts/flight_ops_deepclean_v1.md` but **not yet implemented**. Don't assume an AI cleaning pass runs.
- The Baileys listener and ingest service run as separate processes — when changing message contracts, update both sides.
