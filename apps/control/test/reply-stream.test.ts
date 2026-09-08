import test from 'node:test';
import assert from 'node:assert/strict';
import { once } from 'node:events';
import { Readable, Writable } from 'node:stream';
import { ReplyStream, type ReplyEvent } from '../src/reply-stream.js';
import { createHttpServer } from '../src/http.js';
import type { Operation, Reasoner } from '../src/types.js';
import { fixture, terminal } from './helpers.js';

function operation(id = 'op', status: Operation['status'] = 'running'): Operation {
  return { id, status, kind: 'chat', input: {}, output: { text: 'First. Second.', messageId: 'm' }, error: null, createdAt: new Date().toISOString(), updatedAt: new Date().toISOString() };
}

test('ReplyStream replays first-attachment prefix, pushes committed terminal, and handles immediate completion', () => {
  const replies = new ReplyStream(); const events: ReplyEvent[] = [];
  try {
    assert.equal(replies.begin(operation()), true);
    replies.publish('op', 'First. ');
    const off = replies.follow(operation(), event => events.push(event));
    replies.publish('op', 'Second.'); replies.finish(operation('op', 'succeeded'));
    assert.deepEqual(events.map(e => e.seq), [1, 2, 3]);
    assert.deepEqual(events.map(e => e.type), ['text', 'text', 'terminal']);
    off();
    const late: ReplyEvent[] = [];
    replies.follow(operation('op', 'succeeded'), event => late.push(event));
    assert.deepEqual(late.map(e => e.type), ['terminal']);
  } finally { replies.close(); }
});

test('ReplyStream removes interrupted/failing listeners without affecting work', () => {
  const replies = new ReplyStream(); let received = 0;
  try {
    const off = replies.follow(operation(), () => { received++; });
    replies.follow(operation(), () => { throw new Error('gone'); });
    replies.publish('op', 'First. '); off(); replies.publish('op', 'Second.');
    replies.finish(operation('op', 'succeeded'));
    assert.equal(received, 1);
  } finally { replies.close(); }
});

test('ReplyStream capacity and text overflow fail speech closed with bounded history', () => {
  const replies = new ReplyStream(); const events: ReplyEvent[] = [];
  try {
    replies.follow(operation(), event => events.push(event));
    replies.publish('op', 'x'.repeat(131073)); replies.publish('op', 'ignored');
    assert.deepEqual(events.map(e => e.type), ['unavailable']);
    for (let i = 0; i < 15; i++) replies.follow(operation(`op-${i}`), () => {});
    assert.throws(() => replies.follow(operation('over'), () => {}), /capacity/);
    replies.finish(operation('op', 'succeeded'));
    assert.doesNotThrow(() => replies.follow(operation('new'), () => {}));
  } finally { replies.close(); }
});

test('expired queued streams release capacity and cannot restart old speech', context => {
  context.mock.timers.enable({ apis: ['setTimeout', 'Date'], now: Date.now() });
  const replies = new ReplyStream(); const expired: ReplyEvent[] = [];
  const queued = Array.from({ length: 16 }, (_, i) => operation(`queued-${i}`, 'queued'));
  try {
    for (const op of queued) replies.follow(op, event => expired.push(event));
    context.mock.timers.tick(300000);
    assert.equal(expired.filter(event => event.type === 'unavailable').length, 16);
    const fresh: ReplyEvent[] = [];
    assert.doesNotThrow(() => replies.follow(operation('fresh'), event => fresh.push(event)));
    assert.equal(replies.begin(queued[0]!), false);
    replies.publish('queued-0', 'Must not revive.');
    const late: ReplyEvent[] = [];
    replies.follow(queued[0]!, event => late.push(event));
    assert.deepEqual(late.map(event => event.type), ['unavailable']);
    replies.publish('fresh', 'Fresh answer.');
    assert.deepEqual(fresh.map(event => event.type), ['text']);
  } finally { replies.close(); context.mock.timers.reset(); }
});

/** Invoke the actual HTTP request handler without a network socket or loopback. */
class MemoryResponse extends Writable {
  status = 0; headers: Record<string, string> = {}; headersSent = false; body = '';
  writeHead(status: number, headers: Record<string, string>) { this.status = status; this.headers = headers; this.headersSent = true; return this; }
  flushHeaders() {}
  override _write(chunk: Buffer, _encoding: BufferEncoding, callback: () => void) { this.body += chunk.toString(); callback(); }
}
function get(server: ReturnType<typeof createHttpServer>['server'], path: string, headers = {}) {
  const request = Object.assign(Readable.from([]), { method: 'GET', url: path, headers });
  const response = new MemoryResponse();
  server.emit('request', request, response);
  return response;
}

test('reply HTTP handler preserves guards and snapshots completed operations without network', async () => {
  const f = fixture(); const { server, closeStreams } = createHttpServer(f.service);
  try {
    f.service.chat({ id: 'done', text: 'hi', source: 'voice' }); await terminal(f.service, 'done');
    for (const [path, headers, status] of [
      ['/v1/operations/done/reply', {}, 200],
      ['/v1/operations/done/reply', { Origin: 'http://browser.test', origin: 'http://browser.test' }, 403],
      ['/v1/operations/done/reply', { authorization: 'Bearer worker' }, 403],
      ['/v1/operations/done/reply', { 'last-event-id': '1' }, 400],
      ['/v1/operations/missing/reply', {}, 404],
    ] as const) {
      const response = get(server, path, headers); await once(response, 'close');
      assert.equal(response.status, status);
      if (status === 200) {
        assert.equal(response.headers['Content-Type'], 'text/event-stream');
        const event = JSON.parse(response.body.slice(6).trim());
        assert.equal(event.type, 'terminal'); assert.equal(event.operation.output.messageId, 'assistant:done');
      }
    }
  } finally { closeStreams(); await f.close(); }
});

test('service sends live text before durable final, detach does not cancel accepted reasoning', async () => {
  let publish!: (text: string) => void; let finish!: (text: string) => void;
  let started!: () => void; const start = new Promise<void>(resolve => { started = resolve; });
  const reasoner: Reasoner = {
    status: 'mock', close() {},
    turn(prompt, _id, _tools, onText) {
      assert.match(prompt, /Voice reply:/); publish = onText!; started();
      return new Promise<string>(resolve => { finish = resolve; });
    },
  };
  const f = fixture({}, reasoner); const { server, closeStreams } = createHttpServer(f.service);
  try {
    f.service.chat({ id: 'live', text: 'status', source: 'voice' }); await start;
    publish('First. ');
    const response = get(server, '/v1/operations/live/reply');
    assert.match(response.body, /First/); assert.equal(f.store.message('assistant:live'), undefined);
    response.destroy(); await once(response, 'close');
    publish('Second.'); finish('First. Second.'); await terminal(f.service, 'live');
    assert.equal(f.store.message('assistant:live')!.text, 'First. Second.');
    assert.equal(f.store.messages('main').filter(m => m.role === 'assistant').length, 1);
    assert.ok(!f.store.events(0).some(e => e.type === 'text'));
  } finally { finish?.('First. Second.'); closeStreams(); await f.close(); }
});

test('unadmitted generation stays final-only after capacity becomes available', { timeout: 2000 }, async () => {
  let finish!: (text: string) => void; let wasStreaming: boolean | undefined;
  let started!: () => void; const start = new Promise<void>(resolve => { started = resolve; });
  const reasoner: Reasoner = {
    status: 'mock', close() {},
    turn(_prompt, _id, _tools, onText) {
      wasStreaming = onText !== undefined; started();
      return new Promise<string>(resolve => { finish = resolve; });
    },
  };
  const f = fixture({}, reasoner);
  try {
    for (let i = 0; i < 16; i++) f.service.replies.follow(operation(`occupied-${i}`), () => {});
    f.service.chat({ id: 'final-only', text: 'status', source: 'voice' }); await start;
    assert.equal(wasStreaming, false);
    f.service.replies.finish(operation('occupied-0', 'succeeded'));
    const events: ReplyEvent[] = [];
    f.service.replies.follow(f.service.operation('final-only'), event => events.push(event));
    finish('Complete answer.'); await terminal(f.service, 'final-only');
    assert.deepEqual(events.map(event => event.type), ['terminal']);
    assert.equal(f.store.message('assistant:final-only')!.text, 'Complete answer.');
  } finally { finish?.('Complete answer.'); await f.close(); }
});

test('overflowed streams reclaim their slot at the original acceptance deadline', context => {
  context.mock.timers.enable({ apis: ['setTimeout', 'Date'], now: Date.now() });
  const replies = new ReplyStream(); const overflowed = operation('overflowed');
  try {
    replies.follow(overflowed, () => {});
    replies.publish(overflowed.id, 'x'.repeat(131073));
    for (let i = 0; i < 15; i++) replies.follow(operation(`busy-${i}`), () => {});
    context.mock.timers.tick(300000);
    for (let i = 0; i < 16; i++) assert.equal(replies.begin(operation(`fresh-${i}`)), true);
    assert.equal(replies.begin(overflowed), false);
  } finally { replies.close(); context.mock.timers.reset(); }
});

test('ReplyStream deadline ends subscribers and suppresses later deltas without retry', context => {
  context.mock.timers.enable({ apis: ['setTimeout'] });
  const replies = new ReplyStream(); const events: ReplyEvent[] = [];
  try {
    replies.follow(operation(), event => events.push(event));
    context.mock.timers.tick(300000);
    replies.publish('op', 'Too late.');
    assert.deepEqual(events.map(e => e.type), ['unavailable']);
    replies.finish(operation('op', 'succeeded'));
  } finally { replies.close(); context.mock.timers.reset(); }
});
