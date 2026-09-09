import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { TokenVerifier } from 'livekit-server-sdk';
import { defaultVoiceTuning, voiceSchema, voiceTuningSchema } from '../src/config.js';
import { createHttpServer } from '../src/http.js';
import { voiceToken } from '../src/voice.js';
import { fixture } from './helpers.js';

const contract = JSON.parse(readFileSync(new URL('../../../docs/fixtures/voice-tuning-v1.json', import.meta.url), 'utf8'));
test('voice tuning cross-language defaults and strict validation', () => {
  assert.deepEqual(defaultVoiceTuning, contract.defaults);
  for (const patch of contract.validOverrides) assert.deepEqual(voiceTuningSchema.parse({ ...contract.defaults, ...patch }), { ...contract.defaults, ...patch });
  for (const patch of [...contract.invalidOverrides, { activationThreshold: NaN }, { activationThreshold: Infinity }]) {
    assert.throws(() => voiceTuningSchema.parse({ ...contract.defaults, ...patch }));
  }
  assert.throws(() => voiceTuningSchema.parse({}));
  assert.throws(() => voiceSchema.parse({ conversationId: 'main', voiceTuning: null }));
  assert.deepEqual(voiceSchema.parse({ conversationId: 'main' }), { conversationId: 'main' });
});

test('custom effective token tuning is signed and forwarded intact to named dispatch without durable events', async () => {
  const f = fixture({ livekitUrl: 'wss://offline.invalid', livekitKey: 'key', livekitSecret: 'a-secret-at-least-thirty-two-characters' });
  try {
    const tuning = voiceTuningSchema.parse({ ...defaultVoiceTuning, activationThreshold: .7, echoCancellation: false });
    const token = await voiceToken(f.config, tuning);
    const claims = await new TokenVerifier(f.config.livekitKey!, f.config.livekitSecret!).verify(token.token);
    assert.deepEqual(token.voiceTuning, tuning);
    const metadata = { conversationId: 'main', room: token.room, speaker: token.speaker, voiceTuning: tuning };
    assert.equal(claims.sub, token.speaker);
    assert.deepEqual(JSON.parse(claims.metadata!), metadata);
    assert.deepEqual(JSON.parse(claims.roomConfig!.agents[0]!.metadata!), metadata);
    assert.equal(claims.roomConfig!.agents[0]!.agentName, 'orchestrator-voice');
    assert.equal(f.store.events(0).length, 0);
  } finally { await f.close(); }
});


test('HTTP token handler forwards validated snapshots and legacy requests keep defaults', async () => {
  const f = fixture({ livekitUrl: 'wss://offline.invalid', livekitKey: 'key', livekitSecret: 'a-secret-at-least-thirty-two-characters' });
  const { server, closeStreams } = createHttpServer(f.service);
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
  const address = server.address(); assert.ok(address && typeof address === 'object');
  const post = (body: unknown) => fetch(`http://127.0.0.1:${address.port}/v1/voice/token`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  try {
    const custom = { ...defaultVoiceTuning, minSpeechMs: 250 };
    for (const requested of [undefined, custom]) {
      const response = await post({ conversationId: 'main', ...(requested ? { voiceTuning: requested } : {}) });
      assert.equal(response.status, 200);
      const body = await response.json() as { voiceTuning: unknown; token: string };
      assert.deepEqual(body.voiceTuning, requested ?? defaultVoiceTuning);
      const claims = await new TokenVerifier(f.config.livekitKey!, f.config.livekitSecret!).verify(body.token);
      assert.deepEqual(JSON.parse(claims.roomConfig!.agents[0]!.metadata!).voiceTuning, body.voiceTuning);
    }
    for (const patch of contract.invalidOverrides) {
      assert.equal((await post({ conversationId: 'main', voiceTuning: { ...defaultVoiceTuning, ...patch } })).status, 400);
    }
    assert.equal((await post({ conversationId: 'main', voiceTuning: { ...defaultVoiceTuning, unknown: 'x'.repeat(70000) } })).status, 413);
    assert.equal(f.store.events(0).length, 0);
  } finally {
    closeStreams(); server.closeAllConnections();
    await new Promise<void>(resolve => server.close(() => resolve())); await f.close();
  }
});
