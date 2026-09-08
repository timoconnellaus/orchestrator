import { z } from 'zod';

const itemSchema = z.object({ type: z.literal('agentMessage'), id: z.string().min(1).max(256), text: z.string(), phase: z.enum(['commentary', 'final_answer']).nullish() });
type Item = { phase: 'commentary' | 'final_answer' | null; text: string; complete: boolean };

/** Only identified final-answer text can seal execution and leave an unfinished turn.
 * Commentary is deliberately muted; legacy unphased items are final-only fallback. */
export class TurnSpeech {
  private items = new Map<string, Item>();
  private firstFinal?: string;
  private emitted = '';
  private corrected = false;
  private bytes = 0;
  mutated = false;
  inFlight = 0;
  sealed = false;
  constructor(private emit?: (text: string) => void) {}
  item(value: unknown, complete: boolean): void {
    const parsed = itemSchema.safeParse(value); if (!parsed.success) return;
    const { id, text, phase } = parsed.data;
    const previous = this.items.get(id);
    if (this.items.size >= 128 && !previous) throw new Error('Too many answer items');
    if (previous && previous.phase !== null && phase != null && previous.phase !== phase) throw new Error('Answer phase changed');
    if (previous?.complete && !complete) return;
    this.bytes += text.length - (previous?.text.length ?? 0);
    if (this.bytes > 32768) throw new Error('Answer exceeded limit');
    this.items.set(id, { phase: phase ?? previous?.phase ?? null, text, complete });
    if (phase === 'final_answer') this.firstFinal ??= id;
    this.flush();
  }
  delta(itemId: unknown, delta: unknown): void {
    if (typeof itemId !== 'string' || typeof delta !== 'string') return;
    const item = this.items.get(itemId);
    if (!item || item.complete) return;
    if (this.bytes + delta.length > 32768) throw new Error('Answer exceeded limit');
    this.bytes += delta.length; item.text += delta;
    this.flush();
  }
  flush(): void {
    if (!this.emit || this.mutated || this.inFlight || this.corrected || !this.firstFinal) return;
    const text = this.items.get(this.firstFinal)!.text;
    if (!text.startsWith(this.emitted)) { this.corrected = true; return; }
    const tail = text.slice(this.emitted.length);
    if (!tail) return;
    this.sealed = true; this.emitted = text;
    // Playback observers are never allowed to fail durable reasoning work.
    try { this.emit(tail); } catch { this.emit = undefined; }
  }
  end(): void { this.emit = undefined; }
  answer(): string {
    const completed = [...this.items.values()].filter(item => item.complete);
    const finals = completed.filter(item => item.phase === 'final_answer');
    return (finals.length ? finals : completed.filter(item => item.phase === null)).map(item => item.text).join('\n\n');
  }
}
