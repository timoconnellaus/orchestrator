#!/usr/bin/env node
// Live ChatGPT-login smoke in an isolated control server. Worker mutations are
// rejected in code, not merely discouraged in a prompt. Build apps/control first.
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { CodexAdapter } from '../apps/control/dist/src/codex.js';
import { loadConfig } from '../apps/control/dist/src/config.js';
import { HerdrAdapter } from '../apps/control/dist/src/herdr.js';
import { createHttpServer } from '../apps/control/dist/src/http.js';
import { ControlService } from '../apps/control/dist/src/service.js';
import { Store } from '../apps/control/dist/src/store.js';

const directory = mkdtempSync(join(tmpdir(), 'orchestrator-codex-smoke-'));
const config = loadConfig({ ...process.env, ORCHESTRATOR_MODE: 'live', ORCHESTRATOR_DB: join(directory, 'state.sqlite'), ORCHESTRATOR_PROJECT_ROOTS: '' });
const store = new Store(config.db);
const herdr = new HerdrAdapter(config);
let adapter = new CodexAdapter(config, store);
const calls = [];
const denied = [];
const reasoner = {
  get status() { return adapter.status; },
  close() { adapter.close(); },
  turn(text, id, tools) {
    return adapter.turn(text, id, call => {
      if (call.tool !== 'list_sessions') {
        denied.push(call.tool);
        throw new Error('Read-only smoke permits only list_sessions');
      }
      calls.push(call.tool);
      return tools(call);
    });
  },
};
const rejectMutation = async () => { throw new Error('Worker mutations disabled by smoke harness'); };
const service = new ControlService(config, store, { list: () => herdr.list(), create: rejectMutation, send: rejectMutation }, reasoner);
const { server } = createHttpServer(service);
let url;
async function request(path, body) {
  const response = await fetch(`${url}${path}`, {
    method: body ? 'POST' : 'GET', headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined, signal: AbortSignal.timeout(15000),
  });
  const value = await response.json();
  assert.ok(response.ok, `HTTP ${response.status}`);
  return value;
}
async function turn(text) {
  const input = { id: `smoke-${randomUUID()}`, text, conversationId: 'main' };
  const accepted = await request('/v1/chat', input);
  const duplicate = await request('/v1/chat', input);
  assert.equal(accepted.operationId, duplicate.operationId);
  const deadline = Date.now() + 180000;
  while (Date.now() < deadline) {
    const { operation } = await request(`/v1/operations/${encodeURIComponent(accepted.operationId)}`);
    if (operation.status === 'succeeded') return operation.output.text;
    assert.ok(!['failed', 'uncertain'].includes(operation.status), `Operation ${operation.status}: ${operation.error}`);
    await delay(300);
  }
  throw new Error('Codex smoke deadline exceeded; no automatic resubmission.');
}
try {
  // Fail rather than mistaking an unavailable Herdr connection for an empty list.
  await herdr.list();
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve); });
  url = `http://127.0.0.1:${server.address().port}`;
  service.start();
  assert.equal((await request('/health')).mode, 'live');
  const answer = await turn('Reply with exactly ORCHESTRATOR_SMOKE_OK. Do not use tools, run commands, inspect files, create sessions, or send any messages to workers.');
  assert.equal(answer.trim(), 'ORCHESTRATOR_SMOKE_OK');
  assert.equal(calls.length, 0);
  console.log('PASS: real authenticated Codex answer through HTTP; duplicate request kept one operation.');
  const threadId = store.metadata('codex.threadId');
  adapter.close(); adapter = new CodexAdapter(config, store);
  const observed = await turn('Use list_sessions exactly once and report only how many sessions it returns. This is a read-only integration check. Do not create sessions, send worker messages, read files, or execute commands.');
  assert.ok(observed.trim());
  assert.deepEqual(calls, ['list_sessions']);
  assert.deepEqual(denied, []);
  assert.equal(store.metadata('codex.threadId'), threadId);
  assert.equal(store.operations().length, 2);
  assert.ok(store.operations().every(op => op.kind === 'chat' && op.status === 'succeeded'));
  console.log('PASS: persisted thread resumed after app-server restart; exactly one real list_sessions tool callback, no worker mutations.');
} finally {
  await service.close();
  await new Promise(resolve => server.close(resolve));
  store.close();
  rmSync(directory, { recursive: true, force: true });
}
