import { mkdir, cp, rm, stat, writeFile, appendFile } from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';
import {
  Browsers,
  fetchLatestBaileysVersion,
  makeCacheableSignalKeyStore,
  makeWASocket,
  useMultiFileAuthState
} from 'baileys';

export const DEFAULT_SOURCE_AUTH_DIR = process.env.OPENCLAW_WHATSAPP_AUTH_DIR ?? path.resolve('.runtime-auth/openclaw-source');
export const DEFAULT_RUNTIME_AUTH_DIR = path.resolve('.runtime-auth/default');

export function parseArgs(argv = process.argv.slice(2)) {
  const args = {};
  for (let i = 0; i < argv.length; i += 1) {
    const item = argv[i];
    if (!item.startsWith('--')) {
      (args._ ??= []).push(item);
      continue;
    }
    const key = item.slice(2);
    const next = argv[i + 1];
    if (!next || next.startsWith('--')) {
      args[key] = true;
      continue;
    }
    args[key] = next;
    i += 1;
  }
  return args;
}

export function makeLogger(level = 'silent') {
  const enabled = level !== 'silent';
  const log = (method) => (...items) => {
    if (enabled) console[method](...items);
  };
  const logger = {
    trace: log('debug'),
    debug: log('debug'),
    info: log('log'),
    warn: log('warn'),
    error: log('error'),
    fatal: log('error')
  };
  logger.child = () => logger;
  return logger;
}

export async function exists(filePath) {
  try {
    await stat(filePath);
    return true;
  } catch {
    return false;
  }
}

export function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export async function prepareAuthDir(args) {
  if (args['auth-dir']) return path.resolve(String(args['auth-dir']));

  const source = path.resolve(String(args['source-auth-dir'] ?? DEFAULT_SOURCE_AUTH_DIR));
  const target = path.resolve(String(args['runtime-auth-dir'] ?? DEFAULT_RUNTIME_AUTH_DIR));
  if (args['refresh-auth']) await rm(target, { recursive: true, force: true });
  if (!(await exists(target))) {
    await mkdir(path.dirname(target), { recursive: true });
    await cp(source, target, { recursive: true, preserveTimestamps: true });
  }
  return target;
}

export async function connectWhatsApp({
  authDir,
  syncFullHistory = false,
  loggerLevel = 'silent',
  printQRInTerminal = false,
  waitTimeoutMs = 60_000,
  pairingPhoneNumber = null,
  customPairingCode = null,
  onPairingCode = null,
  onConnectionUpdate = null
}) {
  const logger = makeLogger(loggerLevel);
  await mkdir(authDir, { recursive: true });
  const { state, saveCreds } = await useMultiFileAuthState(authDir);
  const { version } = await fetchLatestBaileysVersion();
  const sock = makeWASocket({
    auth: {
      creds: state.creds,
      keys: makeCacheableSignalKeyStore(state.keys, logger)
    },
    version,
    logger,
    browser: Browsers.macOS('Desktop'),
    printQRInTerminal,
    markOnlineOnConnect: false,
    syncFullHistory,
    connectTimeoutMs: 60_000,
    defaultQueryTimeoutMs: 60_000,
    keepAliveIntervalMs: 25_000
  });
  sock.ev.on('creds.update', saveCreds);
  if (onConnectionUpdate) sock.ev.on('connection.update', onConnectionUpdate);
  if (pairingPhoneNumber && !state.creds.registered) {
    await sleep(3000);
    const phoneNumber = String(pairingPhoneNumber).replace(/\D/g, '');
    const code = await sock.requestPairingCode(phoneNumber, customPairingCode || undefined);
    if (onPairingCode) await onPairingCode(code, phoneNumber);
  }
  await waitForOpen(sock, waitTimeoutMs);
  return sock;
}

export function waitForOpen(sock, timeoutMs) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      cleanup();
      reject(new Error(`Timed out after ${timeoutMs}ms waiting for WhatsApp connection`));
    }, timeoutMs);
    const onUpdate = (update) => {
      if (update.connection === 'open') {
        cleanup();
        resolve(update);
      } else if (update.connection === 'close') {
        cleanup();
        reject(new Error(`WhatsApp connection closed: ${String(update.lastDisconnect?.error ?? 'unknown')}`));
      }
    };
    const cleanup = () => {
      clearTimeout(timer);
      sock.ev.off?.('connection.update', onUpdate);
    };
    sock.ev.on('connection.update', onUpdate);
  });
}

export function closeSocket(sock) {
  if (!sock) return;
  if (typeof sock.end === 'function') sock.end();
  else if (typeof sock.ws?.close === 'function') sock.ws.close();
}

export function normalizeName(value) {
  return String(value ?? '').trim().toLowerCase();
}

export function includesName(value, query) {
  return normalizeName(value).includes(normalizeName(query));
}

export async function writeJson(filePath, value) {
  await mkdir(path.dirname(filePath), { recursive: true });
  await writeFile(filePath, `${JSON.stringify(value, null, 2)}\n`);
}

export async function appendJsonl(filePath, value) {
  await mkdir(path.dirname(filePath), { recursive: true });
  await appendFile(filePath, `${JSON.stringify(value)}\n`);
}

export function messageText(message) {
  const content = message?.message;
  if (!content) return '';
  const direct =
    content.conversation ??
    content.extendedTextMessage?.text ??
    content.imageMessage?.caption ??
    content.videoMessage?.caption ??
    content.documentMessage?.caption ??
    content.buttonsResponseMessage?.selectedDisplayText ??
    content.listResponseMessage?.title ??
    content.templateButtonReplyMessage?.selectedDisplayText;
  if (direct) return String(direct);
  const wrapped =
    content.ephemeralMessage?.message ??
    content.viewOnceMessage?.message ??
    content.viewOnceMessageV2?.message ??
    content.documentWithCaptionMessage?.message;
  return wrapped ? messageText({ message: wrapped }) : '';
}

export function senderId(message) {
  return message?.key?.participant ?? message?.participant ?? message?.key?.remoteJid ?? null;
}

export function messageTimestampSeconds(message) {
  const raw = message?.messageTimestamp;
  if (typeof raw === 'number') return raw;
  if (typeof raw?.toNumber === 'function') return raw.toNumber();
  if (typeof raw?.low === 'number') return raw.low;
  return null;
}

export function serializeMessage(message, source = 'unknown') {
  const ts = messageTimestampSeconds(message);
  const remoteJid = message?.key?.remoteJid ?? null;
  const id = message?.key?.id ?? null;
  return {
    source,
    id,
    remoteJid,
    senderJid: senderId(message),
    fromMe: Boolean(message?.key?.fromMe),
    timestamp: ts,
    timestampIso: ts ? new Date(ts * 1000).toISOString() : null,
    type: Object.keys(message?.message ?? {})[0] ?? null,
    text: messageText(message)
  };
}

export function messageDedupeKey(message) {
  return [
    message?.key?.remoteJid ?? '',
    message?.key?.participant ?? '',
    message?.key?.id ?? '',
    messageTimestampSeconds(message) ?? ''
  ].join('|');
}
