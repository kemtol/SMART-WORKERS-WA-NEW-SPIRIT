#!/usr/bin/env node
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';
import QRCode from 'qrcode';
import qrcode from 'qrcode-terminal';
import {
  appendJsonl,
  closeSocket,
  connectWhatsApp,
  includesName,
  messageDedupeKey,
  parseArgs,
  serializeMessage,
  writeJson
} from './common.js';

const args = parseArgs();
if (args.help) {
  console.log('Usage: node src/listen-new-messages.js --group-name "New Spirit" [--auth-dir .runtime-auth/listener]');
  console.log('       node src/listen-new-messages.js --group-jid "12345-678@g.us" --ingest-url http://127.0.0.1:8088/ingest/whatsapp');
  console.log('');
  console.log('Options:');
  console.log('  --no-post              Only write local JSONL, do not POST to Python ingest');
  console.log('  --no-qr                Do not print QR in terminal');
  console.log('  --pair-phone <number>  Link using WhatsApp phone pairing code, e.g. 628123456789');
  console.log('  --pair-code <code>     Optional custom 8-character pairing code');
  console.log('  --discover-only        Resolve group and exit');
  console.log('  --out <path>           JSONL output path (default: data/live-messages.jsonl)');
  console.log('  --qr-png-out <path>    QR PNG output path (default: data/listener-qr.png)');
  console.log('  --status-out <path>    status JSON path (default: data/listener-status.json)');
  process.exit(0);
}

const groupName = args['group-name'] ? String(args['group-name']) : 'New Spirit';
const explicitGroupJid = args['group-jid'] ? String(args['group-jid']) : null;
const authDir = path.resolve(String(args['auth-dir'] ?? '.runtime-auth/listener'));
const out = String(args.out ?? 'data/live-messages.jsonl');
const statusOut = String(args['status-out'] ?? 'data/listener-status.json');
const ingestUrl = String(args['ingest-url'] ?? 'http://127.0.0.1:8088/ingest/whatsapp');
const qrOut = String(args['qr-out'] ?? 'data/listener-qr.txt');
const qrPngOut = String(args['qr-png-out'] ?? 'data/listener-qr.png');
const pairingPhoneNumber = args['pair-phone'] === true
  ? String(args._?.[0] ?? process.env.WHATSAPP_PAIR_PHONE ?? '')
  : args['pair-phone']
    ? String(args['pair-phone'])
    : process.env.WHATSAPP_PAIR_PHONE || null;
const customPairingCode = args['pair-code'] ? String(args['pair-code']) : null;
const shouldPost = args['no-post'] !== true;
const targetJids = new Set(explicitGroupJid ? [explicitGroupJid] : []);
const seen = new Set();
const stats = {
  startedAt: new Date().toISOString(),
  connectedAt: null,
  stoppedAt: null,
  groupName,
  explicitGroupJid,
  targetJids: [],
  groupMatches: [],
  messagesSeen: 0,
  messagesKept: 0,
  messagesWritten: 0,
  messagesPosted: 0,
  postErrors: 0,
  lastMessageAt: null,
  lastPostError: null,
  lastError: null
};

let sock;
let stopping = false;
let lastQr = null;

async function writeStatus(extra = {}) {
  await writeJson(statusOut, {
    ...stats,
    ...extra,
    targetJids: [...targetJids],
    updatedAt: new Date().toISOString()
  });
}

async function writeQr(qr) {
  lastQr = qr;
  await mkdir(path.dirname(qrOut), { recursive: true });
  await writeFile(qrOut, `${qr}\n`);
  await mkdir(path.dirname(qrPngOut), { recursive: true });
  await QRCode.toFile(qrPngOut, qr, { width: 720, margin: 2 });
  await writeStatus({ state: 'qr', qrOut, qrPngOut });
  console.log('\nScan this QR in WhatsApp -> Linked Devices:\n');
  qrcode.generate(qr, { small: true });
  console.log(`\nQR raw string saved to ${qrOut}. QR PNG saved to ${qrPngOut}. Waiting for scan...\n`);
}

async function resolveTargetGroups() {
  if (targetJids.size > 0) return;
  const groups = await sock.groupFetchAllParticipating();
  const matches = Object.entries(groups ?? {})
    .filter(([, meta]) => includesName(meta?.subject, groupName))
    .map(([jid, meta]) => ({
      jid,
      subject: meta?.subject ?? '',
      size: Array.isArray(meta?.participants) ? meta.participants.length : null,
      owner: meta?.owner ?? null,
      creation: meta?.creation ?? null
    }));

  for (const match of matches) targetJids.add(match.jid);
  stats.groupMatches = matches;
  await writeJson('data/listener-groups.json', { query: groupName, matches });
  if (matches.length === 0) throw new Error(`No WhatsApp group matched "${groupName}"`);
}

async function postToIngest(row) {
  if (!shouldPost) return false;
  const response = await fetch(ingestUrl, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(row)
  });
  if (!response.ok) {
    const text = await response.text().catch(() => '');
    throw new Error(`ingest POST failed ${response.status}: ${text.slice(0, 300)}`);
  }
  return true;
}

async function handleMessage(message, source) {
  stats.messagesSeen += 1;
  const remoteJid = message?.key?.remoteJid;
  if (!remoteJid || !targetJids.has(remoteJid)) return;

  const key = messageDedupeKey(message);
  if (seen.has(key)) return;
  seen.add(key);

  const row = {
    ...serializeMessage(message, source),
    groupName,
    receivedAt: new Date().toISOString(),
    raw: message
  };

  stats.messagesKept += 1;
  stats.lastMessageAt = row.timestampIso ?? row.receivedAt;
  await appendJsonl(out, row);
  stats.messagesWritten += 1;

  let posted = false;
  try {
    posted = await postToIngest(row);
    if (posted) {
      stats.messagesPosted += 1;
      stats.lastPostError = null;
    }
  } catch (error) {
    stats.postErrors += 1;
    stats.lastPostError = String(error?.message ?? error);
    await appendJsonl('data/ingest-post-errors.jsonl', {
      at: new Date().toISOString(),
      error: stats.lastPostError,
      row
    });
  }

  await writeStatus();
  console.log(JSON.stringify({
    status: 'message',
    timestampIso: row.timestampIso,
    remoteJid: row.remoteJid,
    senderJid: row.senderJid,
    type: row.type,
    textPreview: row.text.slice(0, 120),
    posted
  }));
}

async function stop(exitCode = 0) {
  if (stopping) return;
  stopping = true;
  stats.stoppedAt = new Date().toISOString();
  await writeStatus({ state: 'stopped' }).catch(() => {});
  closeSocket(sock);
  process.exit(exitCode);
}

process.on('SIGINT', () => stop(0));
process.on('SIGTERM', () => stop(0));

try {
  await writeStatus({ state: 'starting' });
  sock = await connectWhatsApp({
    authDir,
    syncFullHistory: false,
    loggerLevel: args.verbose ? 'info' : 'silent',
    printQRInTerminal: false,
    waitTimeoutMs: Number(args['wait-timeout-ms'] ?? 300_000),
    pairingPhoneNumber,
    customPairingCode,
    onPairingCode: async (code, phoneNumber) => {
      await writeStatus({ state: 'pairing-code', pairingPhoneNumber: phoneNumber, pairingCode: code });
      console.log('\nWhatsApp pairing code:\n');
      console.log(`  ${code}\n`);
      console.log('On your phone: WhatsApp -> Linked devices -> Link a device -> Link with phone number instead\n');
    },
    onConnectionUpdate: async (update) => {
      if (!pairingPhoneNumber && update.qr && args['no-qr'] !== true && update.qr !== lastQr) {
        await writeQr(update.qr);
      }
    }
  });
  stats.connectedAt = new Date().toISOString();
  await resolveTargetGroups();
  await writeStatus({ state: 'connected' });

  console.log(JSON.stringify({
    ok: true,
    status: args['discover-only'] ? 'discovered' : 'listening',
    groupName,
    targetJids: [...targetJids],
    out,
    ingestUrl: shouldPost ? ingestUrl : null,
    authDir
  }, null, 2));

  if (args['discover-only']) await stop(0);

  sock.ev.on('messages.upsert', async (event) => {
    for (const message of event.messages ?? []) {
      await handleMessage(message, 'messages.upsert');
    }
  });

  sock.ev.on('connection.update', async (update) => {
    if (update.connection === 'close') {
      stats.lastError = String(update.lastDisconnect?.error ?? 'connection closed');
      await writeStatus({ state: 'closed' });
      console.error(JSON.stringify({ ok: false, status: 'closed', error: stats.lastError }));
    }
  });
} catch (error) {
  stats.lastError = String(error?.message ?? error);
  await writeStatus({ state: 'error' }).catch(() => {});
  console.error(JSON.stringify({ ok: false, error: stats.lastError }, null, 2));
  closeSocket(sock);
  process.exitCode = 1;
}
