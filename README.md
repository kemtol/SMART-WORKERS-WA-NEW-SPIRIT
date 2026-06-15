# New Spirit Scrapper

WhatsApp Ops worker for reading movement messages from the `New Spirit` group, parsing MVT departure/arrival formats, storing them in SQLite, and syncing final movement rows to Google Sheets.

## Final Data Flow

```text
WhatsApp group
  -> Baileys listener
  -> Python ingest service
  -> SQLite raw_messages / flight_movements
  -> Google Sheets tab: Movements_Internal
```

`Movements_Internal` is the final tab to use. Older experimental tabs such as `Schedules` or `Movements` should not be used.

## Repository Contents

```text
src/                         Node/Baileys WhatsApp workers
app/                         Python ingest, movement parser, Sheets sync
bin/                         Restart-loop runners
config/airport_mappings.json Fallback airport mapping
config/google-sheets.env.example
integrations/google-sheets-webhook.gs
```

Runtime credentials, messages, databases, logs, process IDs, and internal master data are intentionally ignored by git.

## Prerequisites

- Node.js 22+
- Python 3.12+
- WhatsApp account/number that is allowed to join the target group
- Google Sheet with an Apps Script Web App deployment

Install Node dependencies:

```bash
npm install
```

## WhatsApp Listener

Use a dedicated WhatsApp bot number for this worker. Do not share the same linked-device session with another WhatsApp Web client.

Run once:

```bash
npm run listen
```

On first run, scan the QR with:

```text
WhatsApp -> Linked devices -> Link a device
```

For long-running operation, use the restart loop:

```bash
npm run listen:loop
```

The listener writes raw group messages to:

```text
data/live-messages.jsonl
```

## Ingest Service

Start the Python HTTP ingest service:

```bash
npm run ingest
```

For long-running operation:

```bash
npm run ingest:loop
```

Useful local checks:

```bash
curl http://127.0.0.1:8088/health
curl "http://127.0.0.1:8088/messages?limit=10"
curl "http://127.0.0.1:8088/movements?limit=10"
```

SQLite database path:

```text
data/ops_messages.sqlite3
```

## Movement Parser

The parser is in `app/movement_parser.py`. It understands the current Ops message patterns:

- `MVT Dept`
- `Mvt Arrival`
- aircraft registration, e.g. `PK-SNW`
- aircraft type, e.g. `C208B-EX`
- daily flight sequence, e.g. `Flight 04`
- multi-leg routes, e.g. `AAP-RTU-AAP`
- `Engine Start`, `Take Off`, `ETA`, `ATA`
- pax, pax weight, baggage, cargo, total load

Departure routes are split into one row per leg. For example:

```text
AAP-RTU-AAP
```

becomes:

```text
AAP -> RTU
RTU -> AAP
```

Rebuild parsed movement rows from existing raw messages:

```bash
npm run movements:rebuild
```

## Airport Master Data

If available, place the internal airport master JSON at:

```text
data/reference/master_iata.json
```

Expected fields include:

```text
code
icao_code
airport_name
location
province_name
timezone
```

Mapping priority:

1. `data/reference/master_iata.json`
2. `config/airport_mappings.json`
3. unmapped/free-text fallback

Because `data/` is ignored, the internal master file is not committed to GitHub.

## Google Sheets Sync

The final Sheets destination is:

```text
Movements_Internal
```

Set up Apps Script:

1. Open the Google Sheet.
2. Go to `Extensions` -> `Apps Script`.
3. Paste `integrations/google-sheets-webhook.gs`.
4. Add Script Properties:
   - `TOKEN`: private random token
   - `SPREADSHEET_ID`: target spreadsheet ID
5. Deploy as a Web App.
6. Set `Execute as` to `Me`.
7. Set access to `Anyone`.
8. Copy the `/exec` Web App URL.

Create local config:

```bash
cp config/google-sheets.env.example config/google-sheets.env
```

Set:

```bash
GOOGLE_SHEETS_WEBHOOK_URL=https://script.google.com/macros/s/YOUR_DEPLOYMENT_ID/exec
GOOGLE_SHEETS_WEBHOOK_TOKEN=the-same-token-from-apps-script
GOOGLE_SHEETS_TAB=Movements_Internal
```

Backfill movement rows once:

```bash
npm run sheets:sync
```

Run continuous sync:

```bash
npm run sheets:sync:loop
```

Sheets sync state is stored in:

```text
data/google-sheets-movement-sync-state.json
```

Delete this state file only if you intentionally want to append all movement rows again.

## Running All Workers

In separate terminals:

```bash
npm run ingest:loop
npm run listen:loop
npm run sheets:sync:loop
```

Detached/manual background runs usually write logs and PID files under `data/`.

## Export Parser

For manual WhatsApp chat exports:

```bash
npm run parse-export -- --input /path/to/chat.txt --out data/export.jsonl --csv data/export.csv
```

## Optional History Sync

`src/scrape-history.js` is experimental. WhatsApp decides how much old history a linked device receives, so manual export remains the more reliable archive path.

If using copied credentials from another WhatsApp Web integration, set:

```bash
OPENCLAW_WHATSAPP_AUTH_DIR=/path/to/source/auth
```

The project copies credentials into `.runtime-auth/` and does not need to modify the source credential directory.

## GitHub Hygiene

Do not commit:

- `.runtime-auth/`
- `data/`
- `config/google-sheets.env`
- SQLite databases
- WhatsApp JSONL exports
- logs and PID files
- `node_modules/`

These are already covered by `.gitignore`.
