import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync, statSync, symlinkSync, mkdirSync, realpathSync } from 'node:fs';
import { join } from 'node:path';
import { fixture, createWorker, terminal, until } from './helpers.js';
import type { MockWorkers } from '../src/mock.js';
import { projectDirectory } from '../src/config.js';
import { UncertainError, type Reasoner, type ToolHandler } from '../src/types.js';

test('deterministic full mock flow and scoped credentials/replies never enter journal', async () => {
  const f = fixture();
  try {
    const sessionId = await createWorker(f);
    const session = f.service.session(sessionId); assert.equal(session.status, 'done');
    assert.equal(session.messaging, 'connected');
    const file = join(f.config.credentialDir, readdirSync(f.config.credentialDir)[0]!);
    const token = readFileSync(file, 'utf8').trim(); assert.equal(statSync(file).mode & 0o777, 0o600);
    assert.deepEqual(f.service.register(token), { sessionId });
    assert.equal(f.service.inbox(token).length, 1);
    assert.throws(() => f.service.register('bad'), { status: 401 });
    f.service.send(sessionId, { id: 'send', text: 'Next task' });
    await terminal(f.service, 'send');
    const reply = { id: 'r1', text: 'A question', kind: 'question', replyTo: 'outbound:send' };
    const one = f.service.reply(token, reply); const two = f.service.reply(token, reply); assert.deepEqual(one, two);
    assert.throws(() => f.service.reply(token, { ...reply, text: 'Different' }), { status: 409 });
    const otherId = await createWorker(f, 'other');
    f.service.send(otherId, { id: 'other-send', text: 'Other task' });
    assert.throws(() => f.service.reply(token, { ...reply, id: 'invalid', replyTo: 'outbound:other-send' }), { status: 400 });
    assert.throws(() => f.service.reply(token, { ...reply, id: 'invalid2', sessionId: otherId }), /unrecognized_keys|Unrecognized key/);
    assert.equal(f.store.messages('main').filter(m => m.replyTo === one.id).length, 1);
    assert.ok(!JSON.stringify(f.store.events(0)).includes(token));
    assert.ok(!JSON.stringify(f.store.sessions()).includes(token));
    f.service.chat({ id: 'chat', text: 'Hello', source: 'voice' });
    const op = await terminal(f.service, 'chat');
    assert.equal(op.output!.text, 'Mock orchestrator: Hello');
    assert.equal(f.store.messages('main').at(-1)!.id, op.output!.messageId);
  } finally { await f.close(); }
});

test('working/blocked/unknown/offline sends remain queued; FIFO resumes only with matching native identity', async () => {
  const f = fixture();
  try {
    const id = await createWorker(f); const session = f.service.session(id); const workers = f.workers as MockWorkers;
    const worker = workers.workers.get(session.paneId!)!;
    const baseline = workers.deliveries.length;
    for (const state of ['working', 'blocked', 'unknown', 'offline'] as const) {
      worker.status = state;
      f.service.send(id, { id: `queued-${state}`, text: state });
      await new Promise(resolve => setTimeout(resolve, 35));
      assert.equal(f.service.operation(`queued-${state}`).status, 'queued');
    }
    f.service.chat({ id: 'unblocked-chat', text: 'Chat can continue' });
    assert.equal((await terminal(f.service, 'unblocked-chat')).status, 'succeeded');
    assert.equal(workers.deliveries.length, baseline);
    const nativeId = worker.nativeId; worker.nativeId = 'replacement'; worker.status = 'idle';
    await f.service.refresh(); f.service.kick(); await new Promise(resolve => setTimeout(resolve, 25));
    assert.equal(workers.deliveries.length, baseline); assert.equal(f.service.session(id).status, 'unknown');
    worker.nativeId = nativeId; worker.status = 'idle'; f.service.kick();
    await terminal(f.service, 'queued-offline');
    assert.deepEqual(workers.deliveries.slice(baseline).map(d => d.text), ['working', 'blocked', 'unknown', 'offline']);
  } finally { await f.close(); }
});

test('serialized producers and active-turn routing tools enqueue without deadlock or duplicate side effects', async () => {
  const turns: string[] = []; let running = 0; let max = 0; let cwd = '';
  const reasoner: Reasoner = { status: 'mock', close() {}, async turn(_text: string, id: string, tools: ToolHandler) {
    running++; max = Math.max(max, running); turns.push(id);
    if (id === 'a') {
      const call = { threadId: 't', turnId: 'turn', callId: 'create', tool: 'create_session', arguments: { agent: 'pi', cwd, name: 'routed', instructions: 'task' } };
      const first = await tools(call); assert.deepEqual(await tools(call), first);
      await assert.rejects(tools({ ...call, arguments: { ...call.arguments, name: 'changed' } }), { status: 409 });
    }
    await new Promise(resolve => setTimeout(resolve, 15)); running--; return `Answer ${id}`;
  } };
  const f = fixture({}, reasoner); cwd = f.dir;
  try {
    f.service.chat({ id: 'a', text: 'one', source: 'text' }); f.service.chat({ id: 'b', text: 'two', source: 'voice' });
    await terminal(f.service, 'b'); await until(() => f.store.sessions().length === 1 && f.store.sessions()[0]!.status === 'done');
    assert.equal(max, 1); assert.deepEqual(turns, ['a', 'b']);
    assert.equal(f.store.operations().filter(o => o.kind === 'create_session').length, 1);
    const sessionId = f.store.sessions()[0]!.id;
    const route = { threadId: 't', turnId: 'turn', callId: 'send', tool: 'send_message', arguments: { sessionId, text: 'routing send' } };
    const one = await f.service.route(route); const two = await f.service.route(route); assert.deepEqual(one, two);
    assert.equal((await terminal(f.service, String(one.operationId))).status, 'succeeded');
    await assert.rejects(f.service.route({ ...route, callId: 'shell', tool: 'shell' }), { code: 'unknown_tool' });
  } finally { await f.close(); }
});

test('later queued requests never enter an earlier reasoning turn', async () => {
  const prompts: string[] = [];
  const reasoner: Reasoner = { status: 'mock', close() {}, async turn(text) { prompts.push(text); return 'Acknowledged'; } };
  const f = fixture({}, reasoner);
  try {
    f.service.chat({ id: 'first', text: 'FIRST_REQUEST' });
    f.service.chat({ id: 'later', text: 'SECRET_FUTURE_REQUEST' });
    await terminal(f.service, 'later');
    assert.equal(prompts.length, 2);
    assert.ok(!prompts[0]!.includes('SECRET_FUTURE_REQUEST'));
    assert.ok(prompts[1]!.includes('SECRET_FUTURE_REQUEST'));
  } finally { await f.close(); }
});

test('unique native worker moves preserve session, credential, history and queued delivery', async () => {
  const f = fixture();
  try {
    const id = await createWorker(f);
    const session = f.service.session(id);
    const workers = f.workers as MockWorkers;
    const worker = workers.workers.get(session.paneId!)!;
    const token = readFileSync(join(f.config.credentialDir, readdirSync(f.config.credentialDir)[0]!), 'utf8').trim();
    worker.status = 'working';
    f.service.send(id, { id: 'after-move', text: 'Continue after moving' });
    await f.service.refresh();
    workers.workers.delete(worker.paneId);
    worker.paneId = 'moved:p1'; worker.workspaceId = 'moved'; worker.status = 'idle';
    workers.workers.set(worker.paneId, worker);
    await f.service.refresh();
    assert.equal(f.store.sessions().length, 1);
    assert.equal(f.service.session(id).paneId, 'moved:p1');
    assert.equal(f.service.session(id).workspaceId, 'moved');
    assert.equal(f.store.authenticate(token).id, id);
    f.service.kick();
    assert.equal((await terminal(f.service, 'after-move')).status, 'succeeded');
    assert.ok(f.service.inbox(token).some(m => m.id === 'outbound:after-move'));
    worker.workspaceId = 'another-workspace';
    await f.service.refresh();
    assert.equal(f.service.session(id).workspaceId, 'another-workspace');
  } finally { await f.close(); }
});

test('ambiguous duplicate native identities never rebind or deliver', async () => {
  const f = fixture();
  try {
    const id = await createWorker(f); const session = f.service.session(id);
    const workers = f.workers as MockWorkers;
    const worker = workers.workers.get(session.paneId!)!;
    workers.workers.set('duplicate:p1', { ...worker, paneId: 'duplicate:p1', workspaceId: 'duplicate' });
    await f.service.refresh();
    assert.equal(f.service.session(id).status, 'unknown');
    assert.equal(f.service.session(id).paneId, session.paneId);
    assert.equal(f.store.sessions().length, 1);
  } finally { await f.close(); }
});

test('ambiguous side effects become uncertain and are never retried by the queue', async () => {
  const f = fixture();
  try {
    const id = await createWorker(f); let attempts = 0;
    f.workers.send = async () => { attempts++; throw new UncertainError('process lost'); };
    f.service.send(id, { id: 'uncertain', text: 'task' });
    assert.equal((await terminal(f.service, 'uncertain')).status, 'uncertain');
    f.service.send(id, { id: 'uncertain', text: 'task' });
    await new Promise(resolve => setTimeout(resolve, 40)); assert.equal(attempts, 1);
  } finally { await f.close(); }
});

test('allowlisted real directories reject missing paths, sibling prefixes, symlink escapes and relative cwd', async () => {
  const f = fixture();
  try {
    const root = join(realpathSync(f.dir), 'root'); const outside = join(f.dir, 'root-other'); mkdirSync(root); mkdirSync(outside); symlinkSync(outside, join(root, 'escape'));
    assert.equal(projectDirectory(root, [root]), root);
    for (const path of [outside, join(root, 'escape'), join(root, 'missing'), '.']) assert.throws(() => projectDirectory(path, [root]));
    assert.throws(() => projectDirectory(root, []), { status: 403 });
    assert.throws(() => f.service.chat({ id: 'invalid', text: ' ', extra: true }));
  } finally { await f.close(); }
});
