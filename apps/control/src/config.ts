import { delimiter, dirname, isAbsolute, relative, resolve, sep } from 'node:path';
import { chmodSync, mkdirSync, realpathSync, statSync } from 'node:fs';
import { z } from 'zod';
import { HttpError } from './types.js';

export interface Config {
  mode: 'live' | 'mock'; host: string; port: number; db: string; roots: string[];
  herdrBin: string; herdrSocket?: string; codexBin: string; codexModel?: string;
  workerCommand: string; piExtension: string; workerUrl: string; credentialDir: string;
  livekitUrl?: string; livekitKey?: string; livekitSecret?: string;
  pollMs: number; requestTimeoutMs: number; turnTimeoutMs: number;
}
export function loadConfig(env: NodeJS.ProcessEnv = process.env): Config {
  const mode = z.enum(['live', 'mock']).parse(env.ORCHESTRATOR_MODE ?? 'live');
  const host = env.ORCHESTRATOR_HOST ?? '127.0.0.1';
  const port = z.coerce.number().int().min(1).max(65535).parse(env.ORCHESTRATOR_PORT ?? 8787);
  const db = resolve(env.ORCHESTRATOR_DB ?? '.data/orchestrator.sqlite');
  const roots = (env.ORCHESTRATOR_PROJECT_ROOTS ?? '').split(delimiter).filter(Boolean).map(directory);
  const workerHost = host === '0.0.0.0' || host === '::' ? '127.0.0.1' : host.includes(':') ? `[${host}]` : host;
  return { mode, host, port, db, roots, herdrBin: env.HERDR_BIN_PATH ?? 'herdr', herdrSocket: env.HERDR_SOCKET_PATH,
    codexBin: env.CODEX_BIN ?? 'codex', codexModel: env.CODEX_MODEL,
    workerCommand: env.ORCHESTRATOR_WORKER_COMMAND ?? '/Users/tim/repos/orchestrator/packages/worker-tools/bin/mcp',
    piExtension: env.ORCHESTRATOR_PI_EXTENSION ?? '/Users/tim/repos/orchestrator/packages/worker-tools/pi-extension.ts',
    workerUrl: env.ORCHESTRATOR_URL ?? `http://${workerHost}:${port}`, credentialDir: resolve(dirname(db), 'worker-credentials'),
    livekitUrl: env.LIVEKIT_URL, livekitKey: env.LIVEKIT_API_KEY, livekitSecret: env.LIVEKIT_API_SECRET,
    pollMs: 2000, requestTimeoutMs: 30000, turnTimeoutMs: 300000 };
}
function directory(path: string): string {
  const canonical = realpathSync(path);
  if (!statSync(canonical).isDirectory()) throw new Error(`Not a directory: ${path}`);
  return canonical;
}
export function projectDirectory(cwd: string, roots: string[]): string {
  let canonical: string;
  try { canonical = directory(cwd); } catch { throw new HttpError(400, 'invalid_cwd', 'cwd must be an existing directory'); }
  if (!isAbsolute(cwd) || !roots.some(root => {
    const part = relative(root, canonical);
    return part === '' || (!isAbsolute(part) && part !== '..' && !part.startsWith(`..${sep}`));
  })) throw new HttpError(403, 'project_not_allowed', 'cwd must be under an explicitly configured project root');
  return canonical;
}
export function privateDirectory(path: string): void {
  mkdirSync(path, { recursive: true, mode: 0o700 }); chmodSync(path, 0o700);
}
const opaqueIdSchema = z.string().min(1).max(256).regex(/^[A-Za-z0-9][A-Za-z0-9_.:-]*$/);
export const idSchema = opaqueIdSchema.max(160);
const text = z.string().min(1).max(32000).refine(s => s.trim().length > 0, 'Must not be blank');
export const createSchema = z.object({ id: idSchema, agent: z.enum(['pi', 'codex', 'claude']), cwd: z.string().min(1).max(4096), name: z.string().min(1).max(120), instructions: text }).strict();
export const sendSchema = z.object({ id: idSchema, text }).strict();
export const chatSchema = z.object({ id: idSchema, text, conversationId: z.literal('main').default('main'), source: z.enum(['text', 'voice']).default('text') }).strict();
export const replySchema = z.object({ id: idSchema, text, replyTo: opaqueIdSchema.optional(), kind: z.enum(['progress', 'question', 'result', 'error']) }).strict();
export const voiceSchema = z.object({ conversationId: z.literal('main') }).strict();
