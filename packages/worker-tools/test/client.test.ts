import assert from 'node:assert/strict';
import test from 'node:test';
import { formatToolOutput, validateReply, WorkerClient } from '../src/client';

const token = 'test-session-credential-not-real';
const config = { url: 'http://127.0.0.1:8787', token };
const reply = { id: 'reply-1', kind: 'result', text: 'Tests pass.', replyTo: 'message-1' };

function fixture() {
  const calls: { url: string; init?: RequestInit }[] = [];
  const fetcher: typeof fetch = async (input, init) => {
    const url = String(input);
    calls.push({ url, init });
    if (url.endsWith('/register')) return Response.json({ sessionId: 'worker-1' });
    if (url.endsWith('/inbox')) return Response.json({ messages: [{ id: 'message-1', text: 'Run tests.' }] });
    return Response.json({ message: { ...JSON.parse(String(init?.body)), sessionId: 'worker-1' } });
  };
  return { client: new WorkerClient(config, fetcher), calls };
}

test('reply registers identity, keeps id and body, never puts credential in URL or body', async () => {
  const { client, calls } = fixture();
  await client.reply(reply);
  await client.reply(reply);
  assert.equal(calls.length, 4);
  for (const call of calls) {
    assert.equal(new Headers(call.init?.headers).get('Authorization'), `Bearer ${token}`);
    assert.equal(call.init?.redirect, 'error');
    assert.ok(!call.url.includes(token));
    assert.ok(!String(call.init?.body).includes(token));
  }
  assert.deepEqual(JSON.parse(String(calls[1]?.init?.body)), reply);
  assert.equal(calls[1]?.init?.body, calls[3]?.init?.body);
});

test('inbox has no sender override and uses GET after registration', async () => {
  const { client, calls } = fixture();
  assert.deepEqual(await client.inbox(), { messages: [{ id: 'message-1', text: 'Run tests.' }] });
  assert.equal(calls[1]?.init?.method, 'GET');
  assert.equal(calls[1]?.init?.body, undefined);
});

test('reply validation rejects impersonation, empty text, invalid kind and long payload', () => {
  for (const invalid of [null, {}, { ...reply, sessionId: 'other' }, { ...reply, text: ' ' }, { ...reply, kind: 'success' }, { ...reply, id: '../x' }, { ...reply, replyTo: 1 }, { ...reply, text: 'x'.repeat(12001) }]) {
    assert.throws(() => validateReply(invalid));
  }
  assert.deepEqual(validateReply(reply), reply);
});

test('rejected credentials do not leak backend response contents', async () => {
  const client = new WorkerClient(config, async () => new Response(`secret=${token}`, { status: 401 }));
  await assert.rejects(client.reply(reply), error => {
    assert.ok(error instanceof Error);
    assert.match(error.message, /HTTP 401/);
    assert.ok(!error.message.includes(token));
    return true;
  });
});

test('pre-aborted request never reaches backend', async () => {
  const { client, calls } = fixture();
  await assert.rejects(client.reply(reply, AbortSignal.abort()));
  assert.equal(calls.length, 0);
});

test('in-flight cancellation reaches fetch and explains uncertain acknowledgement', async () => {
  const signal = new AbortController();
  let entered!: () => void;
  const started = new Promise<void>(resolve => { entered = resolve; });
  const client = new WorkerClient(config, async (_input, init) => new Promise((_resolve, reject) => {
    entered();
    init?.signal?.addEventListener('abort', () => reject(new Error('network aborted')), { once: true });
  }));
  const pending = client.register(signal.signal);
  await started;
  signal.abort();
  await assert.rejects(pending, /Retry with the same id/);
});

test('network failure performs no automatic retry or payload logging', async () => {
  let calls = 0;
  const client = new WorkerClient(config, async () => { calls++; throw new Error(`secret ${token}`); });
  await assert.rejects(client.register(), /retry a reply with the same id/);
  assert.equal(calls, 1);
});

test('invalid or oversized JSON responses fail closed', async () => {
  await assert.rejects(new WorkerClient(config, async () => new Response('invalid')).register(), /invalid JSON/);
  await assert.rejects(new WorkerClient(config, async () => Response.json({ other: true })).register(), /Invalid registration/);
  await assert.rejects(new WorkerClient(config, async () => new Response('x'.repeat(600000))).register(), /exceeded 512 KiB/);
});

test('tool output cap is explicit and bounded for large multibyte inboxes', () => {
  const result = formatToolOutput({ messages: ['界'.repeat(100000)] });
  assert.ok(Buffer.byteLength(result) <= 40960);
  assert.match(result, /Output truncated/);
  assert.equal(formatToolOutput({ ok: true }), '{\n  "ok": true\n}');
});
