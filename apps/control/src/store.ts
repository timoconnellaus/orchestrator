import { DatabaseSync } from 'node:sqlite';
import { createHash } from 'node:crypto';
import { chmodSync } from 'node:fs';
import { dirname } from 'node:path';
import { privateDirectory } from './config.js';
import { HttpError, now, type JournalEvent, type Message, type Operation, type Session } from './types.js';

export function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(',')}]`;
  if (value && typeof value === 'object') return `{${Object.entries(value).sort(([a], [b]) => a.localeCompare(b)).map(([k, v]) => `${JSON.stringify(k)}:${stableJson(v)}`).join(',')}}`;
  return JSON.stringify(value);
}
export const digest = (value: string) => createHash('sha256').update(value).digest('hex');
const parse = <T>(row: unknown): T | undefined => row ? JSON.parse((row as { json: string }).json) as T : undefined;
export class Store {
  readonly db: DatabaseSync;
  private listeners = new Set<(event: JournalEvent) => void>();
  private pendingEvents: JournalEvent[] | null = null;
  constructor(path: string) {
    if (path !== ':memory:') privateDirectory(dirname(path));
    this.db = new DatabaseSync(path);
    if (path !== ':memory:') chmodSync(path, 0o600);
    this.db.exec(`PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA busy_timeout=5000;
      CREATE TABLE IF NOT EXISTS operations (ordinal INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, fingerprint TEXT NOT NULL, json TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, json TEXT NOT NULL, native_id TEXT, token_hash TEXT UNIQUE);
      CREATE TABLE IF NOT EXISTS messages (ordinal INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, conversation_id TEXT NOT NULL, json TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL, json TEXT NOT NULL, created_at TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS reply_requests (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, message_id TEXT NOT NULL);`);
    this.transaction(() => {
      for (const op of this.operations()) if (op.status === 'running') this.updateOperation(op.id, { status: 'uncertain', error: 'Control restarted during execution; outcome unknown. Not replayed.' });
      for (const session of this.sessions()) if (session.status === 'starting' && !session.paneId) this.saveSession({ ...session, status: 'unknown', updatedAt: now() });
    });
  }
  transaction<T>(fn: () => T): T {
    if (this.pendingEvents) return fn();
    this.db.exec('BEGIN IMMEDIATE'); this.pendingEvents = [];
    let result: T; let events: JournalEvent[];
    try { result = fn(); events = this.pendingEvents; this.db.exec('COMMIT'); }
    catch (error) { this.db.exec('ROLLBACK'); this.pendingEvents = null; throw error; }
    this.pendingEvents = null;
    for (const event of events) for (const listener of this.listeners) {
      try { listener(event); } catch { this.listeners.delete(listener); }
    }
    return result;
  }
  private event(type: string, data: Session | Message | Operation): void {
    const createdAt = now();
    const row = this.db.prepare('INSERT INTO events(type,json,created_at) VALUES(?,?,?)').run(type, JSON.stringify(data), createdAt);
    const event = { seq: Number(row.lastInsertRowid), type, data, createdAt };
    if (!this.pendingEvents) throw new Error('Events must be committed atomically');
    this.pendingEvents.push(event);
  }
  metadata(key: string): string | undefined { return (this.db.prepare('SELECT value FROM metadata WHERE key=?').get(key) as { value: string } | undefined)?.value; }
  setMetadata(key: string, value: string): void { this.db.prepare('INSERT OR REPLACE INTO metadata VALUES(?,?)').run(key, value); }
  operation(id: string): Operation | undefined { return parse(this.db.prepare('SELECT json FROM operations WHERE id=?').get(id)); }
  operations(): Operation[] { return this.db.prepare('SELECT json FROM operations ORDER BY ordinal').all().map(row => parse<Operation>(row)!); }
  accept(id: string, kind: Operation['kind'], input: Record<string, unknown>, onNew?: () => void): Operation {
    return this.transaction(() => {
      const fingerprint = stableJson({ kind, input });
      const existing = this.db.prepare('SELECT fingerprint,json FROM operations WHERE id=?').get(id) as { fingerprint: string; json: string } | undefined;
      if (existing) {
        if (existing.fingerprint !== fingerprint) throw new HttpError(409, 'id_conflict', 'Request id already used with a different payload');
        return JSON.parse(existing.json) as Operation;
      }
      const date = now();
      const op: Operation = { id, kind, input, status: 'queued', output: null, error: null, createdAt: date, updatedAt: date };
      this.db.prepare('INSERT INTO operations(id,fingerprint,json) VALUES(?,?,?)').run(id, fingerprint, JSON.stringify(op));
      onNew?.(); this.event('operation.updated', op); return op;
    });
  }
  updateOperation(id: string, patch: Partial<Pick<Operation, 'status' | 'output' | 'error'>>): Operation {
    return this.transaction(() => {
      const op = this.operation(id); if (!op) throw new Error('Missing operation');
      Object.assign(op, patch, { updatedAt: now() });
      this.db.prepare('UPDATE operations SET json=? WHERE id=?').run(JSON.stringify(op), id);
      this.event('operation.updated', op); return op;
    });
  }
  sessions(): Session[] { return this.db.prepare('SELECT json FROM sessions ORDER BY rowid').all().map(row => parse<Session>(row)!); }
  session(id: string): Session | undefined { return parse(this.db.prepare('SELECT json FROM sessions WHERE id=?').get(id)); }
  saveSession(session: Session, nativeId?: string, tokenHash?: string): void {
    this.transaction(() => {
      this.db.prepare(`INSERT INTO sessions(id,json,native_id,token_hash) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET json=excluded.json, native_id=COALESCE(excluded.native_id,sessions.native_id), token_hash=COALESCE(excluded.token_hash,sessions.token_hash)`)
        .run(session.id, JSON.stringify(session), nativeId ?? null, tokenHash ?? null);
      this.event('session.updated', session);
    });
  }
  nativeId(id: string): string | undefined { return (this.db.prepare('SELECT native_id FROM sessions WHERE id=?').get(id) as { native_id: string } | undefined)?.native_id ?? undefined; }
  authenticate(token: string): Session {
    const session = parse<Session>(this.db.prepare('SELECT json FROM sessions WHERE token_hash=?').get(digest(token)));
    if (!session) throw new HttpError(401, 'invalid_worker_token', 'Invalid worker credential');
    return session;
  }
  addMessage(message: Message): void {
    this.transaction(() => {
      this.db.prepare('INSERT INTO messages(id,conversation_id,json) VALUES(?,?,?)').run(message.id, message.conversationId, JSON.stringify(message));
      this.event('message.created', message);
    });
  }
  message(id: string): Message | undefined { return parse(this.db.prepare('SELECT json FROM messages WHERE id=?').get(id)); }
  messages(conversationId: string, limit = 200): Message[] {
    return this.db.prepare('SELECT json FROM (SELECT ordinal,json FROM messages WHERE conversation_id=? ORDER BY ordinal DESC LIMIT ?) ORDER BY ordinal').all(conversationId, limit).map(row => parse<Message>(row)!);
  }
  messagesBefore(conversationId: string, messageId: string, limit = 200): Message[] {
    return this.db.prepare(`SELECT json FROM (
      SELECT ordinal,json FROM messages WHERE conversation_id=?
      AND ordinal < (SELECT ordinal FROM messages WHERE id=? AND conversation_id=?)
      ORDER BY ordinal DESC LIMIT ?) ORDER BY ordinal`)
      .all(conversationId, messageId, conversationId, limit).map(row => parse<Message>(row)!);
  }
  replyRequest(id: string, fingerprint: string): Message | undefined {
    const row = this.db.prepare('SELECT fingerprint,message_id FROM reply_requests WHERE id=?').get(id) as { fingerprint: string; message_id: string } | undefined;
    if (row && row.fingerprint !== fingerprint) throw new HttpError(409, 'id_conflict', 'Reply id already used with a different payload');
    return row ? this.message(row.message_id) : undefined;
  }
  saveReplyRequest(id: string, fingerprint: string, messageId: string): void { this.db.prepare('INSERT INTO reply_requests VALUES(?,?,?)').run(id, fingerprint, messageId); }
  events(after: number, limit = 500): JournalEvent[] {
    return this.db.prepare('SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT ?').all(after, limit).map(row => ({ seq: Number(row.seq), type: String(row.type), data: JSON.parse(String(row.json)), createdAt: String(row.created_at) }));
  }
  /** Synchronous replay and subscription share one JS turn: no await boundary can lose a committed event. */
  follow(after: number, listener: (event: JournalEvent) => void): () => void {
    let cursor = after;
    for (;;) {
      const batch = this.events(cursor); if (!batch.length) break;
      for (const event of batch) { listener(event); cursor = event.seq; }
    }
    const followListener = (event: JournalEvent) => { if (event.seq > cursor) { cursor = event.seq; listener(event); } };
    this.listeners.add(followListener);
    return () => this.listeners.delete(followListener);
  }
  close(): void { this.listeners.clear(); this.db.close(); }
}
