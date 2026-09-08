import type { Store } from './store.js';
import { UnsafeDeliveryError, type CreateInput, type NativeSession, type Reasoner, type Session, type WorkerAdapter } from './types.js';

export class MockWorkers implements WorkerAdapter {
  readonly workers = new Map<string, NativeSession>();
  readonly deliveries: { sessionId: string; text: string }[] = [];
  constructor(store: Store) {
    for (const session of store.sessions()) {
      const nativeId = store.nativeId(session.id);
      if (nativeId && session.paneId) this.workers.set(session.paneId, { nativeId, paneId: session.paneId, workspaceId: session.workspaceId, name: session.name, agent: session.agent, cwd: session.cwd, status: session.status });
    }
  }
  async list(): Promise<NativeSession[]> { return [...this.workers.values()].map(s => ({ ...s })); }
  async create(id: string, input: CreateInput): Promise<NativeSession> {
    const session: NativeSession = { nativeId: `mock-native:${id}`, paneId: `mock-pane:${id}`, workspaceId: `mock-workspace:${id}`, name: input.name, agent: input.agent, cwd: input.cwd, status: 'idle' };
    this.workers.set(session.paneId, session); return session;
  }
  async send(session: Session, nativeId: string, text: string): Promise<void> {
    const worker = this.workers.get(session.paneId!);
    if (!worker || worker.nativeId !== nativeId || !['idle', 'done'].includes(worker.status)) throw new UnsafeDeliveryError('Mock worker not safe');
    this.deliveries.push({ sessionId: session.id, text }); worker.status = 'working';
  }
  complete(paneId: string): void { const worker = this.workers.get(paneId); if (worker) worker.status = 'done'; }
}
export class MockReasoner implements Reasoner {
  readonly status = 'mock' as const;
  readonly turns: string[] = [];
  async turn(text: string): Promise<string> {
    this.turns.push(text);
    // The user text is last; context remains available to live reasoning without echoing all history in UI.
    return `Mock orchestrator: ${text.split('\nUSER REQUEST:\n').at(-1)}`;
  }
  close(): void {}
}
