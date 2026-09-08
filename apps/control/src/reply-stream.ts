import { HttpError, type Operation } from './types.js';

export type ReplyEvent = { operationId: string; seq: number } & (
  { type: 'text'; text: string } | { type: 'terminal'; operation: Operation } | { type: 'unavailable' }
);
type Listener = (event: ReplyEvent) => void;
type Entry = { seq: number; size: number; history: ReplyEvent[]; listeners: Set<Listener>; unavailable: boolean; deadline: number; timer: NodeJS.Timeout };
const LIFETIME = 300000;

/** Transient, bounded, first-attachment catch-up. Not a resumable audio journal.
 * Terminal snapshots come from SQLite, so immediate completion needs no retention. */
export class ReplyStream {
  private entries = new Map<string, Entry>();
  private remaining(operation: Operation): number {
    const remaining = Date.parse(operation.createdAt) + LIFETIME - Date.now();
    return Number.isFinite(remaining) && remaining > 0 && remaining <= LIFETIME ? remaining : 0;
  }
  private entry(operation: Operation): Entry | undefined {
    const remaining = this.remaining(operation); if (!remaining) return;
    let entry = this.entries.get(operation.id);
    if (!entry && this.entries.size < 16) {
      const timer = setTimeout(() => this.expire(operation.id), remaining); timer.unref();
      entry = { seq: 0, size: 0, history: [], listeners: new Set(), unavailable: false, deadline: Date.parse(operation.createdAt) + LIFETIME, timer };
      this.entries.set(operation.id, entry);
    }
    return entry;
  }
  /** Reserve before generation. If rejected, the caller must use final-only output. */
  begin(operation: Operation): boolean {
    const entry = this.entry(operation);
    return entry !== undefined && !entry.unavailable;
  }
  private expire(id: string): void {
    const entry = this.entries.get(id); if (!entry) return;
    clearTimeout(entry.timer); this.unavailable(id); this.entries.delete(id);
  }
  private deliver(entry: Entry, event: ReplyEvent): void {
    for (const listener of [...entry.listeners]) {
      try { listener(event); } catch { entry.listeners.delete(listener); }
    }
  }
  private unavailable(id: string): void {
    const entry = this.entries.get(id); if (!entry || entry.unavailable) return;
    // Overflow stays closed until its original deadline; expiry reclaims the slot.
    entry.unavailable = true; entry.history = [];
    this.deliver(entry, { operationId: id, seq: ++entry.seq, type: 'unavailable' });
    entry.listeners.clear();
  }
  publish(id: string, text: string): void {
    // Publishing can never recreate an expired or unadmitted stream.
    const entry = this.entries.get(id); if (!entry || entry.unavailable) return;
    if (Date.now() >= entry.deadline) { this.expire(id); return; }
    if (entry.history.length >= 2048 || entry.size + Buffer.byteLength(text) > 131072) { this.unavailable(id); return; }
    const event: ReplyEvent = { operationId: id, seq: ++entry.seq, type: 'text', text };
    entry.size += Buffer.byteLength(text); entry.history.push(event); this.deliver(entry, event);
  }
  finish(operation: Operation): void {
    const entry = this.entries.get(operation.id); if (!entry) return;
    clearTimeout(entry.timer);
    this.deliver(entry, { operationId: operation.id, seq: ++entry.seq, type: 'terminal', operation });
    entry.listeners.clear(); this.entries.delete(operation.id);
  }
  follow(operation: Operation, listener: Listener): () => void {
    if (operation.kind !== 'chat') throw new HttpError(400, 'not_chat', 'Only chat operations have reply streams');
    if (!['queued', 'running'].includes(operation.status)) {
      listener({ operationId: operation.id, seq: 1, type: 'terminal', operation }); return () => {};
    }
    // Admission is bounded by durable acceptance time, not a retained tombstone.
    if (!this.remaining(operation)) {
      listener({ operationId: operation.id, seq: 1, type: 'unavailable' }); return () => {};
    }
    const entry = this.entry(operation);
    if (!entry || entry.listeners.size >= 8) throw new HttpError(503, 'reply_capacity', 'Reply stream capacity exceeded');
    if (entry.unavailable) {
      listener({ operationId: operation.id, seq: 1, type: 'unavailable' }); return () => {};
    }
    // No await between snapshot and registration: no publish can fall through the seam.
    for (const event of entry.history) listener(event);
    entry.listeners.add(listener);
    return () => { entry.listeners.delete(listener); };
  }
  close(): void {
    for (const id of this.entries.keys()) this.expire(id);
    this.entries.clear();
  }
}
