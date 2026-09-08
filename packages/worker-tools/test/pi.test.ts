import assert from 'node:assert/strict';
import test from 'node:test';
import type { ExtensionAPI } from '@earendil-works/pi-coding-agent';
import orchestratorWorker from '../pi-extension';

function harness(flags: Record<string, string> = {}) {
  const tools: string[] = [];
  const registeredFlags: string[] = [];
  const hooks: string[] = [];
  // Only registration is under test; there is deliberately no live Pi session.
  const pi = {
    registerFlag: (name: string) => { registeredFlags.push(name); },
    getFlag: (name: string) => flags[name],
    registerTool: (tool: { name: string }) => { tools.push(tool.name); },
    on: (name: string) => { hooks.push(name); },
  } as unknown as ExtensionAPI;
  return { pi, tools, registeredFlags, hooks };
}

test('Pi extension has no effect on unrelated sessions without credentials', async () => {
  const previous = { token: process.env.ORCHESTRATOR_WORKER_TOKEN, file: process.env.ORCHESTRATOR_WORKER_CREDENTIAL_FILE };
  delete process.env.ORCHESTRATOR_WORKER_TOKEN;
  delete process.env.ORCHESTRATOR_WORKER_CREDENTIAL_FILE;
  try {
    const mock = harness();
    await orchestratorWorker(mock.pi);
    assert.equal(mock.registeredFlags.length, 2);
    assert.deepEqual(mock.tools, []);
    assert.deepEqual(mock.hooks, []);
  } finally {
    if (previous.token !== undefined) process.env.ORCHESTRATOR_WORKER_TOKEN = previous.token;
    if (previous.file !== undefined) process.env.ORCHESTRATOR_WORKER_CREDENTIAL_FILE = previous.file;
  }
});

test('Pi registers scoped tools but defers network registration to session_start', async () => {
  const previous = { token: process.env.ORCHESTRATOR_WORKER_TOKEN, file: process.env.ORCHESTRATOR_WORKER_CREDENTIAL_FILE };
  process.env.ORCHESTRATOR_WORKER_TOKEN = 'test-session-credential-not-real';
  delete process.env.ORCHESTRATOR_WORKER_CREDENTIAL_FILE;
  try {
    const mock = harness({ 'orchestrator-url': 'http://127.0.0.1:1' });
    await orchestratorWorker(mock.pi); // Nothing contacts the deliberately unavailable port.
    assert.deepEqual(mock.tools, ['read_inbox', 'reply_to_orchestrator']);
    assert.deepEqual(mock.hooks, ['session_start']);
  } finally {
    if (previous.token === undefined) delete process.env.ORCHESTRATOR_WORKER_TOKEN;
    else process.env.ORCHESTRATOR_WORKER_TOKEN = previous.token;
    if (previous.file !== undefined) process.env.ORCHESTRATOR_WORKER_CREDENTIAL_FILE = previous.file;
  }
});
