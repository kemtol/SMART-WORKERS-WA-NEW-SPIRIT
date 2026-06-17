const DEFAULT_SHEET_NAME = 'FLIGHT_RAW';
const DEFAULT_SPREADSHEET_ID = '';

const RAW_HEADERS = [
  'raw_message_id',
  'dedupe_key',
  'message_id',
  'remote_jid',
  'group_name',
  'sender_jid',
  'from_me',
  'message_timestamp',
  'message_timestamp_iso',
  'message_type',
  'text',
  'source',
  'received_at',
  'payload_json',
];

const FLIGHT_RAW_HEADERS = [
  'movement_id',
  'raw_message_id',
  'message_timestamp_iso',
  'received_at',
  'group_name',
  'sender_jid',
  'movement_type',
  'operation_date',
  'registration',
  'aircraft_type',
  'flight_seq',
  'leg_index',
  'route_full',
  'leg_origin_code',
  'leg_origin_name',
  'leg_origin_icao',
  'leg_origin_iata',
  'leg_destination_code',
  'leg_destination_name',
  'leg_destination_icao',
  'leg_destination_iata',
  'from_place',
  'from_code',
  'from_name',
  'from_icao',
  'from_iata',
  'arrival_airport_code',
  'arrival_airport_name',
  'arrival_airport_icao',
  'arrival_airport_iata',
  'next_route',
  'next_text',
  'engine_start_time',
  'takeoff_time',
  'eta_airport_code',
  'eta_airport_name',
  'eta_airport_icao',
  'eta_airport_iata',
  'eta_time',
  'ata_airport_code',
  'ata_airport_name',
  'ata_airport_icao',
  'ata_airport_iata',
  'ata_time',
  'pax',
  'pax_weight_kg',
  'baggage_kg',
  'cargo_text',
  'cargo_kg',
  'total_load_kg',
  'remark',
  'parse_confidence',
  'deepclean_status',
  'deepclean_force_check',
  'deepclean_requested_at',
  'deepcleaned_at',
  'deepclean_prompt_version',
  'deepclean_model',
  'deepclean_error',
  'flight_ops_id',
  'source_text',
];

const FLIGHT_OPS_HEADERS = [
  'operation_date',
  'movement_type',
  'registration',
  'aircraft_type',
  'flight_seq',
  'leg_origin_code',
  'leg_destination_code',
  'route_full',
  'takeoff_time',
  'eta_time',
  'ata_time',
  'pax',
  'pax_weight_kg',
  'baggage_kg',
  'cargo_kg',
  'total_load_kg',
  'remark',
  'ops_status',
  'ai_confidence',
  'review_notes',
  'movement_id',
  'raw_message_id',
  'source_text',
  'deepcleaned_at',
  'deepclean_prompt_version',
  'deepclean_model',
];

function doGet() {
  return json_({ ok: true, status: 'ready' });
}

function doPost(event) {
  const payload = JSON.parse(event.postData.contents || '{}');
  const expectedToken = PropertiesService.getScriptProperties().getProperty('TOKEN');
  if (expectedToken && payload.token !== expectedToken) {
    return json_({ ok: false, error: 'unauthorized' });
  }

  const spreadsheetId = PropertiesService.getScriptProperties().getProperty('SPREADSHEET_ID') || payload.spreadsheetId || DEFAULT_SPREADSHEET_ID;
  if (!spreadsheetId) {
    return json_({ ok: false, error: 'missing SPREADSHEET_ID script property' });
  }
  const spreadsheet = SpreadsheetApp.openById(spreadsheetId);

  if (payload.action === 'listSheets') {
    return json_({
      ok: true,
      sheets: spreadsheet.getSheets().map((sheet) => sheet.getName()),
    });
  }

  if (payload.action === 'ensureSheets') {
    return ensureSheets_(spreadsheet, payload.sheets || defaultSheets_());
  }

  if (payload.action === 'deleteSheets') {
    return deleteSheets_(spreadsheet, payload.deleteSheets || [], payload.keepSheetName || DEFAULT_SHEET_NAME);
  }

  const sheetName = payload.sheetName || DEFAULT_SHEET_NAME;
  const headers = payload.headers || defaultHeadersForSheet_(sheetName);
  const rows = payload.rows || [];
  const sheet = spreadsheet.getSheetByName(sheetName) || spreadsheet.insertSheet(sheetName);

  ensureHeaders_(sheet, headers);
  if (rows.length > 0) {
    const values = rows.map((row) => headers.map((header) => row[header] == null ? '' : row[header]));
    sheet.getRange(sheet.getLastRow() + 1, 1, values.length, headers.length).setValues(values);
  }

  return json_({ ok: true, sheetName, appended: rows.length });
}

function defaultSheets_() {
  return [
    { name: 'RAW', headers: RAW_HEADERS },
    { name: 'FLIGHT_RAW', headers: FLIGHT_RAW_HEADERS },
    { name: 'FLIGHT_OPS', headers: FLIGHT_OPS_HEADERS },
  ];
}

function defaultHeadersForSheet_(sheetName) {
  if (sheetName === 'RAW') {
    return RAW_HEADERS;
  }
  if (sheetName === 'FLIGHT_OPS') {
    return FLIGHT_OPS_HEADERS;
  }
  return FLIGHT_RAW_HEADERS;
}

function ensureSheets_(spreadsheet, sheets) {
  const ensured = [];
  sheets.forEach((spec) => {
    const sheetName = spec.name || spec.sheetName;
    if (!sheetName) {
      return;
    }
    const headers = spec.headers || defaultHeadersForSheet_(sheetName);
    const sheet = spreadsheet.getSheetByName(sheetName) || spreadsheet.insertSheet(sheetName);
    ensureHeaders_(sheet, headers);
    ensured.push(sheetName);
  });

  return json_({
    ok: true,
    ensured,
    remaining: spreadsheet.getSheets().map((sheet) => sheet.getName()),
  });
}

function deleteSheets_(spreadsheet, sheetNames, keepSheetName) {
  const deleted = [];
  const missing = [];
  const skipped = [];

  sheetNames.forEach((sheetName) => {
    if (sheetName === keepSheetName) {
      skipped.push({ sheetName, reason: 'keepSheetName' });
      return;
    }

    const sheet = spreadsheet.getSheetByName(sheetName);
    if (!sheet) {
      missing.push(sheetName);
      return;
    }

    if (spreadsheet.getSheets().length <= 1) {
      skipped.push({ sheetName, reason: 'cannotDeleteLastSheet' });
      return;
    }

    spreadsheet.deleteSheet(sheet);
    deleted.push(sheetName);
  });

  return json_({
    ok: true,
    kept: keepSheetName,
    deleted,
    missing,
    skipped,
    remaining: spreadsheet.getSheets().map((sheet) => sheet.getName()),
  });
}

function ensureHeaders_(sheet, headers) {
  if (sheet.getLastRow() === 0) {
    sheet.getRange(1, 1, 1, headers.length).setValues([headers]);
    return;
  }

  const current = sheet.getRange(1, 1, 1, headers.length).getValues()[0];
  const isEmpty = current.every((value) => value === '');
  if (isEmpty) {
    sheet.getRange(1, 1, 1, headers.length).setValues([headers]);
  }
}

function json_(payload) {
  return ContentService
    .createTextOutput(JSON.stringify(payload))
    .setMimeType(ContentService.MimeType.JSON);
}
