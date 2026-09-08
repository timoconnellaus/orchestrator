#!/usr/bin/env node
import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { randomUUID } from 'node:crypto';
import { lstat, mkdir, rename, rm, writeFile } from 'node:fs/promises';
import { isIP } from 'node:net';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('../', import.meta.url));
if (existsSync(resolve(root, '.env'))) process.loadEnvFile(resolve(root, '.env'));
const env = { ...process.env };
const mode = process.argv[2];
let command;
let args;
let cwd = root;

if (mode === 'control' || mode === 'mock') {
  cwd = resolve(root, 'apps/control');
  command = process.execPath;
  args = ['--import', 'tsx', 'src/main.ts'];
  env.ORCHESTRATOR_DB = resolve(root, mode === 'mock' ? '.data/mock.sqlite' : env.ORCHESTRATOR_DB || '.data/orchestrator.sqlite');
  env.ORCHESTRATOR_MODE = mode === 'mock' ? 'mock' : 'live';
  if (mode === 'mock') env.ORCHESTRATOR_PROJECT_ROOTS ||= root;
  env.ORCHESTRATOR_WORKER_COMMAND = resolve(root, 'packages/worker-tools/bin/mcp');
  env.ORCHESTRATOR_PI_EXTENSION = resolve(root, 'packages/worker-tools/pi-extension.ts');
} else if (mode === 'voice' || mode === 'voice-download') {
  cwd = resolve(root, 'apps/voice');
  if (mode === 'voice' && !env.OPENAI_API_KEY) throw new Error('Set OPENAI_API_KEY in .env for speech. Text orchestration works without it.');
  env.LIVEKIT_URL = env.LIVEKIT_INTERNAL_URL || 'ws://127.0.0.1:7880';
  const controlHost = env.ORCHESTRATOR_HOST || '127.0.0.1';
  env.ORCHESTRATOR_URL ||= `http://${controlHost.includes(':') ? `[${controlHost}]` : controlHost}:${env.ORCHESTRATOR_PORT || '8787'}`;
  command = 'uv';
  args = ['run', '--frozen', 'orchestrator-voice', mode === 'voice-download' ? 'download-files' : 'start'];
} else if (mode === 'livekit') {
  if (!env.LIVEKIT_API_KEY || !env.LIVEKIT_API_SECRET) throw new Error('Run node scripts/init-local.mjs to create local LiveKit credentials.');
  const bind = env.ORCHESTRATOR_HOST || '127.0.0.1';
  const nodeIp = env.LIVEKIT_NODE_IP || bind;
  if (!isIP(bind) || !isIP(nodeIp) || bind === '0.0.0.0' || bind === '::') throw new Error('Choose explicit IP addresses for ORCHESTRATOR_HOST and LIVEKIT_NODE_IP, not a wildcard listener.');
  const runtime = resolve(root, '.data');
  await mkdir(runtime, { recursive: true, mode: 0o700 });
  const stat = await lstat(runtime);
  if (!stat.isDirectory() || stat.isSymbolicLink() || (stat.mode & 0o077) || (process.getuid && stat.uid !== process.getuid())) throw new Error('.data must be an owner-only directory, not a symlink.');
  const config = resolve(runtime, 'livekit.json');
  const temporary = resolve(runtime, `livekit-${randomUUID()}.tmp`);
  // Atomic replacement never follows an existing config symlink or preserves loose modes.
  const body = JSON.stringify({
    port: 7880,
    bind_addresses: [...new Set(['127.0.0.1', bind])],
    rtc: { tcp_port: 7881, use_external_ip: false, enable_loopback_candidate: nodeIp.startsWith('127.') || nodeIp === '::1' },
    keys: { [env.LIVEKIT_API_KEY]: env.LIVEKIT_API_SECRET },
    logging: { level: 'info' },
  });
  try {
    await writeFile(temporary, body, { mode: 0o600, flag: 'wx' });
    await rename(temporary, config);
  } finally { await rm(temporary, { force: true }); }
  command = 'livekit-server';
  args = ['--config', config, '--node-ip', nodeIp, '--udp-port', '7882'];
} else {
  console.error('Usage: node scripts/run.mjs control|mock|livekit|voice|voice-download');
  process.exit(2);
}

console.log(`Starting ${mode}. Press Ctrl+C to stop this module.`);
const child = spawn(command, args, { cwd, env, stdio: 'inherit' });
for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => { if (child.exitCode === null && !child.killed) child.kill(signal); });
}
child.on('error', error => { console.error(`Could not start ${mode}: ${error.code || 'process error'}. Check the setup instructions.`); process.exitCode = 1; });
child.on('exit', (code, signal) => { process.exitCode = code ?? (signal ? 1 : 0); });
