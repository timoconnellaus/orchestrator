import type { WorkerConfig } from './config';

export const REPLY_KINDS = ['progress', 'question', 'result', 'error'] as const;
export interface Reply {
  id: string;
  text: string;
  replyTo?: string;
  kind: (typeof REPLY_KINDS)[number];
}

const MAX_RESPONSE_BYTES = 512 * 1024;
const MAX_TOOL_BYTES = 40 * 1024;
const ID_PATTERN = /^[a-zA-Z0-9:_-]{1,128}$/;

export function validateReply(value: unknown): Reply {
  if (!value || typeof value !== 'object') throw new Error('Reply must be an object.');
  const record = value as Record<string, unknown>;
  if (Object.keys(record).some(key => !['id', 'text', 'replyTo', 'kind'].includes(key))) {
    throw new Error('Reply accepts only id, text, replyTo, and kind.');
  }
  if (typeof record.id !== 'string' || !ID_PATTERN.test(record.id)) throw new Error('id must contain 1–128 letters, numbers, underscores, colons or hyphens.');
  if (typeof record.text !== 'string' || !record.text.trim() || record.text.length > 12000) throw new Error('text must contain 1–12000 characters.');
  if (typeof record.kind !== 'string' || !REPLY_KINDS.some(kind => kind === record.kind)) throw new Error('kind must be progress, question, result, or error.');
  if (record.replyTo !== undefined && (typeof record.replyTo !== 'string' || !ID_PATTERN.test(record.replyTo))) throw new Error('replyTo must be an inbox message id.');
  return { id: record.id, text: record.text, kind: record.kind as Reply['kind'], ...(record.replyTo === undefined ? {} : { replyTo: record.replyTo as string }) };
}

async function readJson(response: Response): Promise<unknown> {
  if (!response.body) throw new Error('Orchestrator returned an empty response.');
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    for (;;) {
      const result = await reader.read();
      if (result.done) break;
      size += result.value.byteLength;
      if (size > MAX_RESPONSE_BYTES) {
        await reader.cancel();
        throw new Error('Orchestrator response exceeded 512 KiB.');
      }
      chunks.push(result.value);
    }
  } finally { reader.releaseLock(); }
  try { return JSON.parse(Buffer.concat(chunks).toString('utf8')); }
  catch { throw new Error('Orchestrator returned invalid JSON.'); }
}

/** Tool output is explicitly bounded even when the backend returns a large inbox. */
export function formatToolOutput(value: unknown): string {
  const text = JSON.stringify(value, null, 2);
  const bytes = Buffer.from(text);
  if (bytes.length <= MAX_TOOL_BYTES) return text;
  return `${bytes.subarray(0, MAX_TOOL_BYTES - 200).toString('utf8')}\n\n[Output truncated to 40 KiB; the full durable messages remain in the Orchestrator app.]`;
}

/** No autonomous retry: callers retain the reply id if an acknowledgement is lost. */
export class WorkerClient {
  constructor(
    private readonly config: WorkerConfig,
    private readonly fetcher: typeof fetch = fetch,
    private readonly timeoutMs = 15000,
  ) {}

  private async request(path: string, body: unknown | undefined, signal?: AbortSignal): Promise<unknown> {
    signal?.throwIfAborted();
    const deadline = AbortSignal.timeout(this.timeoutMs);
    const combined = signal ? AbortSignal.any([signal, deadline]) : deadline;
    let response: Response;
    try {
      response = await this.fetcher(`${this.config.url}${path}`, {
        method: body === undefined ? 'GET' : 'POST',
        headers: { Authorization: `Bearer ${this.config.token}`, ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: combined,
        redirect: 'error',
      });
    } catch {
      if (signal?.aborted) throw new Error('Request cancelled; an already submitted reply may still be saved. Retry with the same id.');
      throw new Error('Orchestrator could not acknowledge the request. Check connectivity and retry a reply with the same id.');
    }
    if (!response.ok) {
      await response.body?.cancel();
      throw new Error(`Orchestrator rejected the request (HTTP ${response.status}). Do not change a reply id to bypass a conflict; check the original message.`);
    }
    return readJson(response);
  }

  async register(signal?: AbortSignal): Promise<{ sessionId: string }> {
    const result = await this.request('/v1/worker/register', {}, signal);
    if (!result || typeof result !== 'object' || !('sessionId' in result) || typeof result.sessionId !== 'string') throw new Error('Invalid registration response.');
    return { sessionId: result.sessionId };
  }

  async inbox(signal?: AbortSignal): Promise<{ messages: unknown[] }> {
    await this.register(signal);
    const result = await this.request('/v1/worker/inbox', undefined, signal);
    if (!result || typeof result !== 'object' || !('messages' in result) || !Array.isArray(result.messages)) throw new Error('Invalid inbox response.');
    return { messages: result.messages };
  }

  async reply(input: unknown, signal?: AbortSignal): Promise<unknown> {
    const reply = validateReply(input);
    await this.register(signal);
    const result = await this.request('/v1/worker/replies', reply, signal);
    if (!result || typeof result !== 'object' || !('message' in result)) throw new Error('Invalid reply acknowledgement; retry with the same id.');
    return result;
  }
}
