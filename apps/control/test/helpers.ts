import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { loadConfig, type Config } from '../src/config.js';
import { Store } from '../src/store.js';
import { ControlService } from '../src/service.js';
import { MockReasoner, MockWorkers } from '../src/mock.js';
import type { Operation, Reasoner, WorkerAdapter } from '../src/types.js';
export function fixture(overrides: Partial<Config> = {}, reasoner?: Reasoner, factory?: (store: Store) => WorkerAdapter) {
  const dir = mkdtempSync(join(tmpdir(), 'orchestrator-control-test-'));
  const config = { ...loadConfig({ ORCHESTRATOR_MODE: 'mock', ORCHESTRATOR_DB: join(dir, 'state.sqlite'), ORCHESTRATOR_PROJECT_ROOTS: dir }), pollMs: 10, ...overrides };
  const store = new Store(config.db); const workers = factory?.(store) ?? new MockWorkers(store);
  const service = new ControlService(config, store, workers, reasoner ?? new MockReasoner()); service.start();
  return { dir, config, store, workers, service, async close() { await service.close(); store.close(); rmSync(dir, { recursive: true, force: true }); } };
}
export async function until<T>(fn: () => T | undefined | false, timeout = 3000): Promise<T> {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) { const result = fn(); if (result) return result; await new Promise(resolve => setTimeout(resolve, 5)); }
  throw new Error('Timed out waiting for test condition');
}
export async function terminal(service: ControlService, id: string): Promise<Operation> {
  return until(() => { const op = service.operation(id); return ['succeeded', 'failed', 'uncertain'].includes(op.status) && op; });
}
export async function createWorker(f: ReturnType<typeof fixture>, id = 'create-1'): Promise<string> {
  f.service.create({ id, agent: 'pi', cwd: f.dir, name: id, instructions: 'Start task' });
  const op = await terminal(f.service, id);
  if (op.status !== 'succeeded') throw new Error(op.error!);
  await until(() => f.store.operations().every(o => o.status === 'succeeded'));
  return String(op.output!.sessionId);
}
