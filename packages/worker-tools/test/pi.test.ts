import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { ExtensionAPI } from '@earendil-works/pi-coding-agent';
import orchestratorWorker from '../pi-extension';

type Hook = (event: object, ctx: { hasUI: boolean }) => unknown;
function harness() {
  const flags: Record<string, string> = {};
  const tools: string[] = [];
  const registeredFlags: string[] = [];
  const hooks = new Map<string, Hook>();
  // Deliberately only the registration surface, not a live Pi session.
  const pi = {
    registerFlag: (name: string) => { registeredFlags.push(name); },
    getFlag: (name: string) => flags[name],
    registerTool: (tool: { name: string }) => { tools.push(tool.name); },
    on: (name: string, callback: Hook) => { hooks.set(name, callback); },
  } as unknown as ExtensionAPI;
  return { pi, flags, tools, registeredFlags, hooks, fire: async (name: string) => hooks.get(name)?.({}, { hasUI: false }) };
}

function cleanCredentials() {
  const keys = ['ORCHESTRATOR_WORKER_TOKEN', 'ORCHESTRATOR_WORKER_CREDENTIAL_FILE', 'ORCHESTRATOR_URL'];
  const previous = keys.map(key => process.env[key]);
  keys.forEach(key => { delete process.env[key]; });
  return () => keys.forEach((key, i) => {
    if (previous[i] === undefined) delete process.env[key];
    else process.env[key] = previous[i];
  });
}

test('Pi extension stays inert on unrelated sessions without credentials', async () => {
  const restore = cleanCredentials();
  try {
    const mock = harness();
    orchestratorWorker(mock.pi);
    assert.equal(mock.registeredFlags.length, 2);
    assert.deepEqual(mock.tools, []);
    await mock.fire('session_start');
    assert.deepEqual(mock.tools, []);
  } finally { restore(); }
});

test('flag-only Pi launch resolves flags after factory and honors explicit URL over env', async () => {
  const restore = cleanCredentials();
  const directory = await mkdtemp(join(tmpdir(), 'orchestrator-pi-flags-'));
  const file = join(directory, 'credential');
  const previousFetch = globalThis.fetch;
  const requests: { url: string; token: string | null }[] = [];
  globalThis.fetch = async (input, init) => {
    requests.push({ url: String(input), token: new Headers(init?.headers).get('authorization') });
    return Response.json({ sessionId: 'worker-1' });
  };
  try {
    await writeFile(file, 'flag-only-scoped-token-not-real', { mode: 0o600 });
    const mock = harness();
    orchestratorWorker(mock.pi);
    assert.deepEqual(mock.tools, []);
    assert.equal(requests.length, 0);
    // Match SDK startup order: CLI values arrive only AFTER factories run.
    mock.flags['orchestrator-credential-file'] = file;
    mock.flags['orchestrator-url'] = 'http://127.0.0.1:9999';
    process.env.ORCHESTRATOR_URL = 'http://127.0.0.1:1111';
    await mock.fire('session_start');
    assert.deepEqual(mock.tools, ['read_inbox', 'reply_to_orchestrator']);
    assert.deepEqual(requests, [{ url: 'http://127.0.0.1:9999/v1/worker/register', token: 'Bearer flag-only-scoped-token-not-real' }]);
    await mock.fire('session_shutdown');
  } finally {
    restore();
    globalThis.fetch = previousFetch;
    await rm(directory, { recursive: true, force: true });
  }
});

test('shutdown aborts a pending registration without keeping the old session alive', async () => {
  const restore = cleanCredentials();
  const previousFetch = globalThis.fetch;
  let ready!: () => void;
  const started = new Promise<void>(resolve => { ready = resolve; });
  let wasAborted = false;
  globalThis.fetch = async (_input, init) => new Promise((_resolve, reject) => {
    init?.signal?.addEventListener('abort', () => { wasAborted = true; reject(new Error('cancelled')); }, { once: true });
    ready();
  });
  try {
    process.env.ORCHESTRATOR_WORKER_TOKEN = 'test-session-token-not-real';
    const mock = harness();
    orchestratorWorker(mock.pi);
    const pending = mock.fire('session_start');
    await started;
    await mock.fire('session_shutdown');
    await pending;
    assert.equal(wasAborted, true);
  } finally { restore(); globalThis.fetch = previousFetch; }
});
