#!/usr/bin/env node
import { randomBytes } from 'node:crypto';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';

const root = new URL('../', import.meta.url);
const target = new URL('.env', root);
const example = await readFile(new URL('.env.example', root), 'utf8');
const content = example
  .replace(/^LIVEKIT_API_KEY=$/m, `LIVEKIT_API_KEY=orch_${randomBytes(12).toString('hex')}`)
  .replace(/^LIVEKIT_API_SECRET=$/m, `LIVEKIT_API_SECRET=${randomBytes(32).toString('hex')}`);
try {
  await writeFile(target, content, { mode: 0o600, flag: 'wx' });
  await mkdir(new URL('.data/', root), { mode: 0o700, recursive: true });
  console.log(`Created ${fileURLToPath(target)} with local LiveKit credentials (not printed).`);
  console.log('Add OPENAI_API_KEY for voice and ORCHESTRATOR_PROJECT_ROOTS for live worker creation.');
} catch (error) {
  if (error?.code !== 'EEXIST') throw error;
  console.log('Existing .env preserved; no credentials changed.');
}
