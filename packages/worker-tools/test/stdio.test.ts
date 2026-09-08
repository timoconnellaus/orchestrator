import assert from 'node:assert/strict';
import test from 'node:test';
import { createServer } from 'node:http';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { fileURLToPath } from 'node:url';
import { createInterface } from 'node:readline';

function deadline<T>(promise: Promise<T>, milliseconds = 5000): Promise<T> {
  let timer: ReturnType<typeof setTimeout>;
  return Promise.race([
    promise,
    new Promise<never>((_resolve, reject) => { timer = setTimeout(() => reject(new Error('Subprocess did not settle before deadline')), milliseconds); }),
  ]).finally(() => clearTimeout(timer));
}

test('actual MCP stdio EOF cancels registration and pending tools before reply POST', async () => {
  let registrations = 0;
  let replies = 0;
  let pendingTool!: () => void;
  const toolRegistered = new Promise<void>(resolve => { pendingTool = resolve; });
  const http = createServer((request, response) => {
    if (request.url === '/v1/worker/register') {
      registrations++;
      if (registrations === 2) pendingTool();
      // Leave registrations unacknowledged to exercise shutdown cancellation.
      request.resume();
    } else {
      replies++;
      response.end('{}');
    }
  });
  http.listen(0, '127.0.0.1');
  await once(http, 'listening');
  const address = http.address();
  assert.ok(address && typeof address === 'object');
  const child = spawn(process.execPath, ['--import', 'tsx', 'src/main.ts'], {
    cwd: fileURLToPath(new URL('../', import.meta.url)),
    env: { ...process.env, ORCHESTRATOR_URL: `http://127.0.0.1:${address.port}`, ORCHESTRATOR_WORKER_TOKEN: 'stdio-test-token-not-real', ORCHESTRATOR_WORKER_CREDENTIAL_FILE: '' },
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  const exited = once(child, 'exit');
  child.stderr.resume();
  const lines = createInterface({ input: child.stdout });
  const initialized = new Promise<void>(resolve => {
    lines.on('line', line => { const result = JSON.parse(line); if (result.id === 1 && result.result) resolve(); });
  });
  try {
    child.stdin.write(JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2024-11-05', capabilities: {}, clientInfo: { name: 'stdio-test', version: '1' } } }) + '\n');
    await deadline(initialized);
    child.stdin.write(JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized' }) + '\n');
    child.stdin.write(JSON.stringify({ jsonrpc: '2.0', id: 2, method: 'tools/call', params: { name: 'reply_to_orchestrator', arguments: { id: 'reply-1', text: 'Should not be sent after EOF.', kind: 'result' } } }) + '\n');
    await deadline(toolRegistered);
    child.stdin.end();
    const [code] = await deadline(exited, 3000);
    assert.equal(code, 0);
    assert.equal(replies, 0);
  } finally {
    if (child.exitCode === null) child.kill('SIGKILL');
    lines.close();
    http.closeAllConnections();
    await new Promise<void>(resolve => http.close(() => resolve()));
  }
});
