#!/usr/bin/env node
import process from 'node:process';
import {
  appendJsonl,
  closeSocket,
  connectWhatsApp,
  includesName,
  messageDedupeKey,
  parseArgs,
  prepareAuthDir,
  serializeMessage,
  writeJson
} from './common.js';

const args = parseArgs();
if (args.help) {
  console.log('Usage: node src/scrape-history.js --group-name "New Spirit" [--wait-seconds 240] [--out data/new-spirit-history.jsonl]');
  console.log('   or: node src/scrape-history.js --group-name "New Spirit" --limit 10 --wait-seconds 180');
  console.log('   or: node src/scrape-history.js --group-jid "12345-678@g.us" [--paginate-rounds 2]');
  process.exit(0);
}
const groupName = args['group-name'] ? String(args['group-name']) : null;
const explicitGroupJid = args['group-jid'] ? String(args['group-jid']) : null;
const waitSeconds = Number(args['wait-seconds'] ?? 240);
const limit = Number(args.limit ?? 0);
const out = String(args.out ?? 'data/new-spirit-history.jsonl');
const metaOut = String(args['meta-out'] ?? 'data/scrape-meta.json');
const seen = new Set();
const targetJids = new Set(explicitGroupJid ? [explicitGroupJid] : []);
const oldestByJid = new Map();
const stats = {
  startedAt: new Date().toISOString(),
  groupName,
  explicitGroupJid,
  historyChunks: 0,
  historyMessagesSeen: 0,
  messagesWritten: 0,
  liveMessagesWritten: 0,
  targetJids: []
};
let sock;
let stopTimer;
let finishEarly;

function shouldKeep(message) {
  const jid = message?.key?.remoteJid;
  return Boolean(jid && targetJids.has(jid));
}

async function writeMessage(message, source) {
  if (limit > 0 && stats.messagesWritten >= limit) return;
  const key = messageDedupeKey(message);
  if (seen.has(key)) return;
  seen.add(key);
  const row = serializeMessage(message, source);
  await appendJsonl(out, row);
  stats.messagesWritten += 1;
  if (source === 'messages.upsert') stats.liveMessagesWritten += 1;
  const current = oldestByJid.get(row.remoteJid);
  if (row.remoteJid && row.timestamp && (!current || row.timestamp < current.timestamp)) {
    oldestByJid.set(row.remoteJid, { timestamp: row.timestamp, key: message.key });
  }
  if (limit > 0 && stats.messagesWritten >= limit) finishEarly?.();
}

async function maybePaginate() {
  const rounds = Number(args['paginate-rounds'] ?? 0);
  if (!rounds) return;
  for (let round = 0; round < rounds; round += 1) {
    for (const [jid, oldest] of oldestByJid.entries()) {
      if (!targetJids.has(jid)) continue;
      console.log(JSON.stringify({ status: 'fetchMessageHistory', round: round + 1, jid, timestamp: oldest.timestamp }));
      await sock.fetchMessageHistory(50, oldest.key, oldest.timestamp);
      await new Promise((resolve) => setTimeout(resolve, 5000));
    }
  }
}

try {
  if (!groupName && !explicitGroupJid) throw new Error('Provide --group-name or --group-jid');
  const authDir = await prepareAuthDir(args);
  sock = await connectWhatsApp({ authDir, syncFullHistory: true, loggerLevel: args.verbose ? 'info' : 'silent' });

  if (groupName && targetJids.size === 0) {
    const groups = await sock.groupFetchAllParticipating();
    const matches = Object.entries(groups ?? {})
      .filter(([, meta]) => includesName(meta?.subject, groupName))
      .map(([jid, meta]) => ({ jid, subject: meta?.subject ?? '' }));
    for (const match of matches) targetJids.add(match.jid);
    stats.groupMatches = matches;
    if (matches.length === 0) {
      console.warn(JSON.stringify({ warning: `No group matched "${groupName}" during groupFetchAllParticipating` }));
    }
  }

  sock.ev.on('messaging-history.set', async (event) => {
    stats.historyChunks += 1;
    for (const chat of event.chats ?? []) {
      if (groupName && chat?.id?.endsWith('@g.us') && includesName(chat?.name ?? chat?.subject, groupName)) {
        targetJids.add(chat.id);
      }
    }
    for (const message of event.messages ?? []) {
      stats.historyMessagesSeen += 1;
      if (shouldKeep(message)) await writeMessage(message, 'messaging-history.set');
    }
    stats.targetJids = [...targetJids];
    console.log(JSON.stringify({
      status: 'history-chunk',
      syncType: event.syncType ?? null,
      progress: event.progress ?? null,
      chunkOrder: event.chunkOrder ?? null,
      messagesInChunk: event.messages?.length ?? 0,
      targetJids: stats.targetJids,
      messagesWritten: stats.messagesWritten
    }));
  });

  sock.ev.on('messages.upsert', async (event) => {
    for (const message of event.messages ?? []) {
      if (shouldKeep(message)) await writeMessage(message, 'messages.upsert');
    }
  });

  sock.ev.on('messaging-history.status', (event) => {
    console.log(JSON.stringify({ status: 'messaging-history.status', ...event }));
  });

  stats.targetJids = [...targetJids];
  console.log(JSON.stringify({ ok: true, status: 'connected', waitSeconds, limit, out, targetJids: stats.targetJids }));
  await new Promise((resolve) => {
    finishEarly = resolve;
    stopTimer = setTimeout(resolve, waitSeconds * 1000);
  });
  await maybePaginate();
  stats.finishedAt = new Date().toISOString();
  stats.targetJids = [...targetJids];
  await writeJson(metaOut, stats);
  console.log(JSON.stringify({ ok: true, done: true, ...stats, out, metaOut }, null, 2));
} catch (error) {
  console.error(JSON.stringify({ ok: false, error: String(error?.message ?? error) }, null, 2));
  process.exitCode = 1;
} finally {
  if (stopTimer) clearTimeout(stopTimer);
  closeSocket(sock);
}
