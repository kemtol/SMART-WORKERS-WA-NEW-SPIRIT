#!/usr/bin/env node
import process from 'node:process';
import {
  closeSocket,
  connectWhatsApp,
  includesName,
  parseArgs,
  prepareAuthDir,
  writeJson
} from './common.js';

const args = parseArgs();
if (args.help) {
  console.log('Usage: node src/discover-groups.js --name "New Spirit" [--out data/groups.json] [--refresh-auth]');
  process.exit(0);
}
const name = String(args.name ?? args._?.[0] ?? 'New Spirit');
const out = String(args.out ?? 'data/groups.json');
let sock;

try {
  const authDir = await prepareAuthDir(args);
  sock = await connectWhatsApp({ authDir, syncFullHistory: false, loggerLevel: args.verbose ? 'info' : 'silent' });
  const groups = await sock.groupFetchAllParticipating();
  const rows = Object.entries(groups ?? {}).map(([jid, meta]) => ({
    jid,
    subject: meta?.subject ?? '',
    owner: meta?.owner ?? null,
    size: Array.isArray(meta?.participants) ? meta.participants.length : null,
    creation: meta?.creation ?? null
  })).sort((a, b) => a.subject.localeCompare(b.subject));
  const matches = rows.filter((row) => includesName(row.subject, name));
  await writeJson(out, { query: name, count: rows.length, matches, groups: rows });
  console.log(JSON.stringify({ ok: true, query: name, count: rows.length, matches, out }, null, 2));
} catch (error) {
  console.error(JSON.stringify({ ok: false, error: String(error?.message ?? error) }, null, 2));
  process.exitCode = 1;
} finally {
  closeSocket(sock);
}
