import assert from 'node:assert/strict';
import test from 'node:test';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js';
import { WorkerClient } from '../src/client';
import { createWorkerServer } from '../src/mcp';

async function connect() {
  const calls: string[] = [];
  const worker = new WorkerClient({ url: 'http://localhost:8787', token: 'test-session-credential-not-real' }, async (url, options) => {
    calls.push(String(url));
    if (String(url).endsWith('/register')) return Response.json({ sessionId: 'worker-1' });
    if (String(url).endsWith('/inbox')) return Response.json({ messages: [{ id: 'msg-1', text: 'Run tests.' }] });
    return Response.json({ message: JSON.parse(String(options?.body)) });
  });
  const server = createWorkerServer(worker);
  const client = new Client({ name: 'worker-test', version: '0.1.0' });
  const [a, b] = InMemoryTransport.createLinkedPair();
  await server.connect(a);
  await client.connect(b);
  return { client, server, calls };
}

test('MCP exposes only scoped inbox and reply tools and completes a reply round-trip', async () => {
  const { client, server, calls } = await connect();
  try {
    const tools = await client.listTools();
    assert.deepEqual(tools.tools.map(t => t.name), ['read_inbox', 'reply_to_orchestrator']);
    const inbox = await client.callTool({ name: 'read_inbox', arguments: {} });
    assert.notEqual(inbox.isError, true);
    assert.match(JSON.stringify(inbox.content), /msg-1/);
    const reply = await client.callTool({ name: 'reply_to_orchestrator', arguments: { id: 'reply-1', text: 'Passed.', kind: 'result', replyTo: 'msg-1' } });
    assert.notEqual(reply.isError, true);
    assert.equal(calls.length, 4);
  } finally { await client.close(); await server.close(); }
});

test('MCP tool validation returns isError and performs no HTTP mutation', async () => {
  const { client, server, calls } = await connect();
  try {
    const result = await client.callTool({ name: 'reply_to_orchestrator', arguments: { id: 'x', text: 'bad', kind: 'result', sessionId: 'other' } });
    assert.equal(result.isError, true);
    assert.equal(calls.length, 0);
    const invalidInbox = await client.callTool({ name: 'read_inbox', arguments: { sessionId: 'other' } });
    assert.equal(invalidInbox.isError, true);
  } finally { await client.close(); await server.close(); }
});
