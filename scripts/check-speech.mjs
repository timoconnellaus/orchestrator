#!/usr/bin/env node
// Explicit, billable model-access check. Run with node --env-file=.env.
// Uses synthetic speech only; never opens a microphone or prints credentials.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const key = process.env.OPENAI_API_KEY?.trim();
assert.ok(key, 'Set OPENAI_API_KEY locally before running this check.');
const stt = process.env.OPENAI_STT_MODEL || 'gpt-live-transcribe';
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
  body: JSON.stringify({ model: tts, voice, input: stt === 'gpt-live-transcribe'
    ? 'Orchestrator speech check. Please transcribe this sentence while it is being spoken.'
    : 'Orchestrator speech check.', response_format: 'wav' }),
});
await checkResponse(speech, 'Speech synthesis');
const audio = await speech.arrayBuffer();
assert.ok(audio.byteLength > 44 && audio.byteLength < 5 * 1024 * 1024, 'Unexpected synthetic audio size');
console.log(`PASS: ${tts} generated synthetic WAV audio (${audio.byteLength} bytes).`);
if (stt === 'gpt-live-transcribe') {
  // This model is realtime-only; don't send it to the file-transcription endpoint.
  const directory = await mkdtemp(join(tmpdir(), 'orchestrator-speech-'));
  try {
    const wav = join(directory, 'synthetic.wav');
    await writeFile(wav, new Uint8Array(audio), { mode: 0o600 });
    await new Promise((resolve, reject) => {
      const child = spawn('uv', [
        'run', '--frozen', 'python', fileURLToPath(new URL('./check-realtime-stt.py', import.meta.url)),
        '--wav', wav, '--expect', 'speech check',
      ], {
        cwd: fileURLToPath(new URL('../apps/voice/', import.meta.url)),
        stdio: 'inherit', timeout: 90_000,
      });
      child.once('error', reject);
      child.once('close', (code) => code === 0 ? resolve() : reject(new Error('Live transcription check failed')));
    });
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
} else {
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
}
console.log('Speech model access verified. This is not LiveKit, emulator microphone, or endurance validation.');
