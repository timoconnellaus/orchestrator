import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { Store } from '../src/store.js';

test('Store accepts once, detects changed payload and commits event/message atomically', () => {
  const store = new Store(':memory:');
  try {
    const seen: number[] = []; const stop = store.follow(0, event => seen.push(event.seq));
    store.accept('a', 'chat', { text: 'hello', source: 'text' });
    store.accept('a', 'chat', { source: 'text', text: 'hello' });
    assert.equal(store.operations().length, 1); assert.deepEqual(seen, [1]);
    assert.throws(() => store.accept('a', 'chat', { text: 'changed' }), { status: 409 });
    assert.throws(() => store.accept('broken', 'chat', {}, () => { throw new Error('rollback'); }));
    assert.equal(store.operation('broken'), undefined); assert.deepEqual(seen, [1]);
    stop();
  } finally { store.close(); }
});
test('SQLite WAL restart marks running operations uncertain but preserves queued order', () => {
  const dir = mkdtempSync(join(tmpdir(), 'control-store-')); const path = join(dir, 'state.sqlite');
  try {
    let store = new Store(path);
    assert.equal(store.db.prepare('PRAGMA journal_mode').get()!.journal_mode, 'wal');
    for (const id of ['one', 'two', 'three']) store.accept(id, 'chat', { text: id });
    store.updateOperation('one', { status: 'running' }); store.close();
    store = new Store(path);
    assert.equal(store.operation('one')!.status, 'uncertain');
    assert.deepEqual(store.operations().filter(o => o.status === 'queued').map(o => o.id), ['two', 'three']);
    const replay: number[] = [];
    const stop = store.follow(0, event => replay.push(event.seq));
    store.accept('four', 'chat', {});
    assert.deepEqual(replay, [1, 2, 3, 4, 5, 6]); stop(); store.close();
  } finally { rmSync(dir, { recursive: true, force: true }); }
});
test('message context cutoff persists across restart and excludes future queued requests', () => {
  const dir = mkdtempSync(join(tmpdir(), 'control-context-')); const path = join(dir, 'state.sqlite');
  try {
    let store = new Store(path);
    for (const id of ['past', 'current', 'future']) store.addMessage({ id, conversationId: 'main', sessionId: null, role: 'user', text: id, replyTo: null, createdAt: '2026-09-08T00:00:00Z' });
    assert.deepEqual(store.messagesBefore('main', 'current').map(m => m.id), ['past']);
    store.close(); store = new Store(path);
    assert.deepEqual(store.messagesBefore('main', 'current').map(m => m.id), ['past']);
    assert.deepEqual(store.messagesBefore('main', 'future', 1).map(m => m.id), ['current']);
    assert.deepEqual(store.messagesBefore('main', 'missing'), []);
    store.close();
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test('replay pages then follows without a gap, respects future cursors and unsubscribe', () => {
  const store = new Store(':memory:');
  try {
    for (let index = 0; index < 1100; index++) store.accept(`op${index}`, 'chat', {});
    const seqs: number[] = []; const stop = store.follow(10, event => seqs.push(event.seq));
    store.accept('live', 'chat', {}); stop(); store.accept('not-live', 'chat', {});
    assert.equal(seqs.length, 1091); assert.equal(seqs[0], 11); assert.equal(seqs.at(-1), 1101);
    assert.equal(new Set(seqs).size, seqs.length);
    const future: number[] = []; const stopFuture = store.follow(1200, event => future.push(event.seq));
    store.accept('future', 'chat', {}); assert.deepEqual(future, []); stopFuture();
  } finally { store.close(); }
});
