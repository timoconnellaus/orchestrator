import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { request as nodeRequest } from 'node:http';
import { TokenVerifier } from 'livekit-server-sdk';
import { createHttpServer } from '../src/http.js';
import { fixture, terminal, createWorker } from './helpers.js';
import { voiceToken } from '../src/voice.js';
import type { Message, Operation, Session } from '../src/types.js';
interface TestResponse { operation: Operation; session: Session; messages: Message[]; sessions: Session[]; error: { code: string } }

test('all HTTP endpoints, body/error validation, idempotency, worker authorization and voice unavailable', async () => {
  const f = fixture(); const { server, closeStreams } = createHttpServer(f.service);
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
  const address = server.address(); assert.ok(address && typeof address === 'object'); const url = `http://127.0.0.1:${address.port}`;
  const request = async (path: string, body?: unknown, headers: Record<string, string> = {}) => {
    const response = await fetch(url + path, { method: body === undefined ? 'GET' : 'POST', headers: { ...(body === undefined ? {} : { 'Content-Type': 'application/json' }), ...headers }, body: body === undefined ? undefined : JSON.stringify(body) });
    return { status: response.status, body: await response.json() as TestResponse };
  };
  try {
    assert.deepEqual((await request('/health')).body, { status: 'ok', mode: 'mock', codex: 'mock', voiceConfigured: false });
    assert.equal((await request('/health', undefined, { Origin: 'http://evil.test' })).status, 403);
    assert.equal((await request('/v1/chat', { id: 'a', text: '' })).status, 400);
    assert.equal((await request('/v1/chat', { id: 'a', text: 'hello', extra: 'bad' })).status, 400);
    assert.equal((await request('/v1/chat', { id: 'a', text: 'hello' })).status, 202);
    assert.equal((await request('/v1/chat', { id: 'a', text: 'hello' })).status, 202);
    assert.equal((await request('/v1/chat', { id: 'a', text: 'changed' })).status, 409);
    await terminal(f.service, 'a');
    assert.equal((await request('/v1/operations/a')).body.operation.status, 'succeeded');
    assert.equal((await request('/v1/messages')).body.messages.length, 2);
    assert.equal((await request('/v1/operations/missing')).status, 404);
    assert.equal((await request('/v1/sessions/missing')).status, 404);
    assert.equal((await request('/v1/sessions', { id: 'bad', agent: 'pi', cwd: '/', name: 'bad', instructions: 'task' })).status, 403);
    assert.equal((await request('/v1/sessions', { id: 'worker', agent: 'codex', cwd: f.dir, name: 'worker', instructions: 'task' })).status, 202);
    const created = await terminal(f.service, 'worker'); const sessionId = String(created.output!.sessionId);
    assert.equal((await request(`/v1/sessions/${sessionId}`)).body.session.id, sessionId);
    assert.equal((await request('/v1/sessions')).body.sessions.length, 1);
    assert.equal((await request(`/v1/sessions/${sessionId}/messages`, { id: 'message', text: 'next' })).status, 202);
    await terminal(f.service, 'message');
    assert.ok((await request(`/v1/messages?conversationId=session:${sessionId}`)).body.messages.length >= 2);
    assert.equal((await request('/v1/messages?conversationId=invalid')).status, 400);
    assert.equal((await request('/v1/events?after=-1')).status, 400);
    assert.equal((await request('/v1/voice/token', { conversationId: 'main' })).body.error.code, 'voice_not_configured');
    assert.equal((await request('/v1/worker/register', {})).status, 401);
    const token = readFileSync(join(f.config.credentialDir, readdirSync(f.config.credentialDir)[0]!), 'utf8').trim();
    const auth = { Authorization: `Bearer ${token}` };
    assert.deepEqual((await request('/v1/worker/register', {}, auth)).body, { sessionId });
    assert.equal((await request('/v1/worker/register', { sessionId: 'fake' }, auth)).status, 400);
    assert.equal((await request('/v1/worker/inbox', undefined, auth)).body.messages.length, 2);
    assert.equal((await request('/v1/worker/replies', { id: 'result', text: 'finished', kind: 'result', replyTo: 'outbound:message' }, auth)).status, 200);
    assert.equal((await request('/v1/worker/replies', { id: 'bad', text: 'x', kind: 'result', replyTo: 'user:a' }, auth)).status, 400);
    assert.equal((await request('/v1/chat', { id: 'scope', text: 'no' }, auth)).status, 403);
    assert.equal((await request('/not-found')).body.error.code, 'not_found');
    assert.equal((await fetch(url + '/v1/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{' })).status, 400);
    assert.equal((await fetch(url + '/v1/chat', { method: 'POST', body: '{}' })).status, 415);
    assert.equal((await request('/v1/chat', { id: 'large', text: 'x'.repeat(70000) })).status, 413);
    const streamedStatus = await new Promise<number | undefined>((resolve, reject) => {
      const upload = nodeRequest(url + '/v1/chat', { method: 'POST', headers: { 'Content-Type': 'application/json', 'Transfer-Encoding': 'chunked' } }, response => { response.resume(); resolve(response.statusCode); });
      upload.on('error', reject); upload.write('x'.repeat(70000)); upload.end();
    });
    assert.equal(streamedStatus, 413);
  } finally { closeStreams(); server.closeAllConnections(); await new Promise<void>(resolve => server.close(() => resolve())); await f.close(); }
});

test('SSE replays then follows with valid frames and Last-Event-ID resumption', async () => {
  const f = fixture(); const { server, closeStreams } = createHttpServer(f.service);
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
  const address = server.address(); assert.ok(address && typeof address === 'object'); const url = `http://127.0.0.1:${address.port}`;
  const controller = new AbortController();
  try {
    await createWorker(f);
    const before = f.store.events(0); const last = before.at(-1)!.seq;
    const response = await fetch(url + '/v1/events?after=0', { headers: { 'Last-Event-ID': String(last - 1) }, signal: controller.signal });
    assert.equal(response.headers.get('content-type'), 'text/event-stream');
    const reader = response.body!.getReader(); let text = new TextDecoder().decode((await reader.read()).value);
    assert.match(text, new RegExp(`id: ${last}\\nevent: `)); assert.ok(!text.includes(`id: ${last - 1}\n`));
    f.service.chat({ id: 'follow', text: 'live' }); await terminal(f.service, 'follow');
    while (!text.includes('assistant:follow')) text += new TextDecoder().decode((await reader.read()).value);
    const events = text.split('\n\n').filter(frame => frame.startsWith('id:')).map(frame => JSON.parse(frame.split('\ndata: ')[1]!));
    assert.deepEqual(events.map(e => e.seq), Array.from({ length: events.length }, (_, index) => last + index));
    assert.ok(events.some(e => e.type === 'message.created' && e.data.id === 'assistant:follow'));
    controller.abort();
  } finally { controller.abort(); closeStreams(); server.closeAllConnections(); await new Promise<void>(resolve => server.close(() => resolve())); await f.close(); }
});

test('LiveKit token is signed, room-scoped, uniquely identified and explicitly dispatches named voice agent', async () => {
  const f = fixture({ livekitUrl: 'wss://phone.example.test', livekitKey: 'key', livekitSecret: 'a-secret-at-least-thirty-two-characters' });
  try {
    const one = await voiceToken(f.config); const two = await voiceToken(f.config);
    assert.match(one.room, /^orchestrator-main-[0-9a-f-]{36}$/);
    assert.notEqual(one.room, two.room, 'an explicit new join must not reuse a closed room session');
    assert.equal(one.url, 'wss://phone.example.test');
    const verifier = new TokenVerifier(f.config.livekitKey!, f.config.livekitSecret!);
    const claims = await verifier.verify(one.token); const other = await verifier.verify(two.token);
    assert.notEqual(claims.sub, other.sub); assert.equal(claims.video!.room, one.room);
    assert.equal(other.video!.room, two.room);
    assert.deepEqual(JSON.parse(claims.metadata!), { conversationId: 'main', room: one.room, speaker: one.speaker, voiceTuning: one.voiceTuning });
    assert.deepEqual(JSON.parse(other.metadata!), { conversationId: 'main', room: two.room, speaker: two.speaker, voiceTuning: two.voiceTuning });
    assert.equal(claims.video!.roomJoin, true); assert.equal(claims.video!.roomAdmin, undefined);
    assert.match(JSON.stringify(claims.roomConfig), /orchestrator-voice/);
    assert.equal(f.store.events(0).length, 0);
  } finally { await f.close(); }
});
