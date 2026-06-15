#!/usr/bin/env node
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';
import { parseArgs } from './common.js';

const args = parseArgs();
if (args.help) {
  console.log('Usage: node src/parse-export.js --input /path/to/chat.txt --out data/chat.jsonl --csv data/chat.csv');
  console.log('   or: node src/parse-export.js --input /path/to/chat.txt --last 10 --pretty');
  process.exit(0);
}
const input = args.input ? String(args.input) : null;
const out = String(args.out ?? 'data/whatsapp-export.jsonl');
const csvOut = args.csv ? String(args.csv) : null;
const last = Number(args.last ?? 0);
const pretty = Boolean(args.pretty);

const patterns = [
  /^\[(?<date>\d{1,2}[\/.-]\d{1,2}[\/.-]\d{2,4}),?\s+(?<time>\d{1,2}[:.]\d{2}(?::\d{2})?)\]\s(?<sender>[^:]+):\s(?<text>.*)$/,
  /^(?<date>\d{1,2}[\/.-]\d{1,2}[\/.-]\d{2,4}),?\s+(?<time>\d{1,2}[:.]\d{2}(?::\d{2})?)\s+-\s+(?<sender>[^:]+):\s(?<text>.*)$/
];

function parseLine(line) {
  for (const pattern of patterns) {
    const match = line.match(pattern);
    if (match?.groups) return match.groups;
  }
  return null;
}

function csvEscape(value) {
  const text = String(value ?? '');
  return /[",\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

function normalizeDate(date, time) {
  return { raw: `${date} ${time}` };
}

if (!input) {
  console.error('Usage: node src/parse-export.js --input /path/to/chat.txt --out data/chat.jsonl --csv data/chat.csv');
  process.exit(1);
}

const raw = await readFile(input, 'utf8');
const lines = raw.replace(/^\uFEFF/, '').split(/\r?\n/);
let messages = [];

for (const line of lines) {
  const parsed = parseLine(line);
  if (parsed) {
    messages.push({
      timestamp: normalizeDate(parsed.date, parsed.time),
      sender: parsed.sender.trim(),
      text: parsed.text
    });
  } else if (messages.length > 0) {
    messages[messages.length - 1].text += `\n${line}`;
  }
}

if (last > 0) messages = messages.slice(-last);

await mkdir(path.dirname(out), { recursive: true });
await writeFile(out, `${messages.map((message) => JSON.stringify(message)).join('\n')}\n`);

if (csvOut) {
  await mkdir(path.dirname(csvOut), { recursive: true });
  const csv = [
    ['timestamp_raw', 'sender', 'text'].join(','),
    ...messages.map((message) => [
      csvEscape(message.timestamp.raw),
      csvEscape(message.sender),
      csvEscape(message.text)
    ].join(','))
  ].join('\n');
  await writeFile(csvOut, `${csv}\n`);
}

if (pretty) {
  for (const [index, message] of messages.entries()) {
    console.log(`\n#${index + 1} ${message.timestamp.raw} - ${message.sender}`);
    console.log(message.text);
  }
}

console.log(JSON.stringify({ ok: true, input, messages: messages.length, out, csv: csvOut }, null, 2));
