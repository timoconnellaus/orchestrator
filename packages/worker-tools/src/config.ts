import { constants } from 'node:fs';
import { open } from 'node:fs/promises';

export interface WorkerConfig {
  url: string;
  token: string;
}

export interface ConfigInput {
  url?: string;
  credentialFile?: string;
  token?: string;
}

export function parseArguments(args: string[]): ConfigInput {
  const values: ConfigInput = {};
  for (let i = 0; i < args.length; i += 2) {
    const flag = args[i];
    const value = args[i + 1];
    if (!value || value.startsWith('--')) throw new Error('Expected --url URL or --credential-file PATH.');
    if (flag === '--url') values.url = value;
    else if (flag === '--credential-file') values.credentialFile = value;
    else throw new Error('Only --url and --credential-file arguments are supported.');
  }
  return values;
}

/** Only a small, owner-readable regular file may carry a session credential. */
async function readCredential(path: string): Promise<ConfigInput> {
  const file = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const stat = await file.stat();
    if (!stat.isFile() || stat.size > 8192 || (stat.mode & 0o077) !== 0) {
      throw new Error('Worker credential must be a regular file of at most 8 KiB with mode 0600.');
    }
    if (process.getuid && stat.uid !== process.getuid()) throw new Error('Worker credential must belong to the current user.');
    const text = (await file.readFile('utf8')).trim();
    if (!text.startsWith('{')) return { token: text };
    let value: unknown;
    try { value = JSON.parse(text); } catch { throw new Error('Worker credential file contains invalid JSON.'); }
    if (!value || typeof value !== 'object' || !('token' in value) || typeof value.token !== 'string') {
      throw new Error('Worker credential JSON must contain a token string.');
    }
    const url = 'url' in value && typeof value.url === 'string' ? value.url : undefined;
    return { token: value.token, url };
  } finally {
    await file.close();
  }
}

export function normalizeUrl(value: string): string {
  let url: URL;
  try { url = new URL(value); } catch { throw new Error('Orchestrator URL must be an absolute HTTP(S) URL.'); }
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.hash || url.search) {
    throw new Error('Orchestrator URL must use HTTP(S), without credentials, query, or fragment.');
  }
  return url.toString().replace(/\/+$/, '');
}

export async function loadConfig(
  input: ConfigInput = {},
  env: NodeJS.ProcessEnv = process.env,
): Promise<WorkerConfig> {
  const path = input.credentialFile ?? env.ORCHESTRATOR_WORKER_CREDENTIAL_FILE;
  const file = path ? await readCredential(path) : {};
  const token = file.token ?? input.token ?? env.ORCHESTRATOR_WORKER_TOKEN;
  if (!token || token.length < 16 || token.length > 4096 || /\s/.test(token)) {
    throw new Error('A valid scoped worker credential is required. Configure a credential file or ORCHESTRATOR_WORKER_TOKEN.');
  }
  return {
    url: normalizeUrl(input.url ?? env.ORCHESTRATOR_URL ?? file.url ?? 'http://127.0.0.1:8787'),
    token,
  };
}
