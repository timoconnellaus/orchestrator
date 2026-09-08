#!/usr/bin/env node
// Explicit, billable model-access check. Run with node --env-file=.env.
// Uses synthetic speech only; never opens a microphone or prints credentials.
import assert from 'node:assert/strict';

const key = process.env.OPENAI_API_KEY?.trim();
assert.ok(key, 'Set OPENAI_API_KEY locally before running this check.');
const stt = process.env.OPENAI_STT_MODEL || 'gpt-4o-mini-transcribe';
const tts = process.env.OPENAI_TTS_MODEL || 'gpt-4o-mini-tts';
const voice = process.env.OPENAI_TTS_VOICE || 'coral';
const base = 'https://api.openai.com/v1';
async function request(path, options = {}) {
  return fetch(`${base}${path}`, {
    ...options,
    headers: { ...options.headers, Authorization: `Bearer ${key}` },
    redirect: 'error', signal: AbortSignal.timeout(60000),
  });
}
async function checkResponse(response, label) {
  if (response.ok) return;
  let code = 'unknown';
  try {
    const body = await response.json();
    const value = body.error?.code || body.error?.type;
    if (typeof value === 'string' && /^[a-z_]{1,80}$/.test(value)) code = value;
  } catch { /* Never print raw provider errors or authentication material. */ }
  throw new Error(`${label}: HTTP ${response.status} (${code})`);
}
for (const model of new Set([stt, tts])) {
  const response = await request(`/models/${encodeURIComponent(model)}`);
  await checkResponse(response, `Model ${model}`);
  await response.arrayBuffer();
  console.log(`PASS: model accessible: ${model}`);
}
const speech = await request('/audio/speech', {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ model: tts, voice, input: 'Orchestrator speech check.', response_format: 'wav' }),
});
await checkResponse(speech, 'Speech synthesis');
const audio = await speech.arrayBuffer();
assert.ok(audio.byteLength > 44 && audio.byteLength < 5 * 1024 * 1024, 'Unexpected synthetic audio size');
console.log(`PASS: ${tts} generated synthetic WAV audio (${audio.byteLength} bytes).`);
const form = new FormData();
form.append('model', stt);
form.append('file', new Blob([audio], { type: 'audio/wav' }), 'speech-check.wav');
form.append('language', 'en');
const transcription = await request('/audio/transcriptions', { method: 'POST', body: form });
await checkResponse(transcription, 'Transcription');
const result = await transcription.json();
console.log('Synthetic transcript:', typeof result.text === 'string' ? JSON.stringify(result.text.slice(0, 240)) : '<missing text>');
assert.ok(typeof result.text === 'string' && /speech check/i.test(result.text), 'Synthetic phrase was not transcribed as expected');
console.log(`PASS: ${stt} transcribed the synthetic phrase correctly.`);
console.log('Speech model access verified. This is not LiveKit, emulator microphone, or endurance validation.');
