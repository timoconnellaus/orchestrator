import { ReplyStream } from './reply-stream.js';
import { randomBytes } from 'node:crypto';
import { writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { z } from 'zod';
import { chatSchema, createSchema, idSchema, privateDirectory, projectDirectory, replySchema, sendSchema, type Config } from './config.js';
import { digest, stableJson, type Store } from './store.js';
import { HttpError, now, UncertainError, UnsafeDeliveryError, type CreateInput, type Message, type Operation, type Reasoner, type Session, type ToolCall, type WorkerAdapter } from './types.js';
import { MockWorkers } from './mock.js';

export class ControlService {
  readonly replies = new ReplyStream();
  private draining?: Promise<void>;
  private refreshing?: Promise<void>;
  private stopped = false;
  private timer?: NodeJS.Timeout;
  constructor(readonly config: Config, readonly store: Store, readonly workers: WorkerAdapter, readonly reasoner: Reasoner) {}
  start(): void {
    this.timer = setInterval(() => {
      void this.refresh().then(() => this.kick()).catch(() => console.error('Control reconciliation failed.'));
    }, this.config.pollMs); this.timer.unref(); this.kick();
  }
  session(id: string): Session {
    const session = this.store.session(id); if (!session) throw new HttpError(404, 'session_not_found', 'Session not found'); return session;
  }
  operation(id: string): Operation {
    const op = this.store.operation(id); if (!op) throw new HttpError(404, 'operation_not_found', 'Operation not found'); return op;
  }
  private message(id: string, conversationId: string, sessionId: string | null, role: Message['role'], text: string, replyTo: string | null = null): Message {
    return { id, conversationId, sessionId, role, text, replyTo, createdAt: now() };
  }
  chat(body: unknown): Operation {
    const { id, ...input } = chatSchema.parse(body);
    const op = this.store.accept(id, 'chat', input, () => this.store.addMessage(this.message(`user:${id}`, input.conversationId, null, 'user', input.text)));
    this.kick(); return op;
  }
  create(body: unknown): Operation {
    const { id, ...input } = createSchema.parse(body);
    // Store the submitted payload for strict idempotency; canonicalize again immediately before launch.
    const op = this.store.accept(id, 'create_session', input, () => { projectDirectory(input.cwd, this.config.roots); }); this.kick(); return op;
  }
  send(sessionId: string, body: unknown): Operation {
    this.session(sessionId);
    const { id, text } = sendSchema.parse(body);
    const op = this.store.accept(id, 'send_message', { sessionId, text }, () => this.store.addMessage(this.message(`outbound:${id}`, `session:${sessionId}`, sessionId, 'user', text)));
    this.kick(); return op;
  }
  register(token: string): { sessionId: string } {
    const session = this.store.authenticate(token);
    if (session.messaging !== 'connected') this.store.saveSession({ ...session, messaging: 'connected', updatedAt: now() });
    return { sessionId: session.id };
  }
  inbox(token: string): Message[] {
    const session = this.store.authenticate(token);
    return this.store.messages(`session:${session.id}`).filter(message => message.role === 'user');
  }
  reply(token: string, body: unknown): Message { return this.workerReply(this.store.authenticate(token).id, body); }
  private workerReply(sessionId: string, body: unknown): Message {
    const input = replySchema.parse(body); const session = this.session(sessionId);
    const key = `reply:${digest(stableJson([sessionId, input.id]))}`; const fingerprint = stableJson(input);
    return this.store.transaction(() => {
      const existing = this.store.replyRequest(key, fingerprint); if (existing) return existing;
      if (input.replyTo) {
        const parent = this.store.message(input.replyTo);
        if (!parent || parent.sessionId !== sessionId || parent.conversationId !== `session:${sessionId}`) throw new HttpError(400, 'invalid_reply_scope', 'replyTo must belong to this worker thread');
      }
      const message = this.message(key, `session:${sessionId}`, sessionId, 'worker', input.text, input.replyTo ?? null);
      this.store.addMessage(message);
      this.store.addMessage(this.message(`notice:${key}`, 'main', sessionId, 'system', `${session.name} (${input.kind}): ${input.text}`, message.id));
      this.store.saveReplyRequest(key, fingerprint, message.id);
      // A reply conveys content, not native terminal readiness. Reconciliation owns session.status.
      return message;
    });
  }
  async refresh(): Promise<void> {
    if (this.stopped || this.store.operations().some(op => op.kind === 'create_session' && op.status === 'running')) return;
    if (this.refreshing) return this.refreshing;
    this.refreshing = this.reconcile();
    try { await this.refreshing; } finally { this.refreshing = undefined; }
  }
  private async reconcile(): Promise<void> {
    let observed;
    try { observed = await this.workers.list(); }
    catch {
      for (const session of this.store.sessions()) if (session.status !== 'offline') this.store.saveSession({ ...session, status: 'offline', updatedAt: now() });
      return;
    }
    const known = this.store.sessions();
    for (const session of known) {
      if (!session.paneId) continue;
      const nativeId = this.store.nativeId(session.id);
      const matches = nativeId ? observed.filter(a => a.nativeId === nativeId && a.agent === session.agent) : [];
      const owners = nativeId ? known.filter(s => this.store.nativeId(s.id) === nativeId) : [];
      // Native identity survives pane moves. Rebind only a unique live identity to
      // its unique application owner; ambiguous duplicate sessions cannot receive input.
      const current = matches.length === 1 && owners.length === 1 ? matches[0] : undefined;
      const status = current?.status ?? (matches.length > 0 || observed.some(a => a.paneId === session.paneId) ? 'unknown' : 'offline');
      if (session.status !== status || (current && (session.paneId !== current.paneId || session.workspaceId !== current.workspaceId || session.cwd !== current.cwd))) {
        this.store.saveSession({ ...session, status,
          ...(current ? { paneId: current.paneId, workspaceId: current.workspaceId, cwd: current.cwd } : {}), updatedAt: now() });
      }
    }
    for (const worker of observed) {
      if (this.store.sessions().some(s => worker.nativeId ? this.store.nativeId(s.id) === worker.nativeId : s.paneId === worker.paneId && !this.store.nativeId(s.id))) continue;
      const id = `external:${digest(`${worker.paneId}:${worker.nativeId}`).slice(0, 24)}`;
      if (this.store.session(id)) continue;
      const date = now();
      const unique = worker.nativeId && observed.filter(a => a.nativeId === worker.nativeId).length === 1;
      this.store.saveSession({ id, name: worker.name, agent: worker.agent, cwd: worker.cwd, paneId: worker.paneId, workspaceId: worker.workspaceId, status: unique ? worker.status : 'unknown', messaging: 'limited', createdAt: date, updatedAt: date }, worker.nativeId);
    }
  }
  kick(): void {
    if (this.stopped || this.draining) return;
    // Defer execution until after the durable HTTP acceptance has returned.
    this.draining = Promise.resolve().then(() => this.drain()).catch(() => {
      // Per-operation failures are journaled below. Never log raw subprocess/config data.
      console.error('Control queue interrupted unexpectedly; inspect durable operation states.');
    }).finally(() => { this.draining = undefined; });
  }
  private async drain(): Promise<void> {
    const skipped = new Set<string>();
    while (!this.stopped) {
      const queued = this.store.operations().filter(op => op.status === 'queued' && !skipped.has(op.id));
      if (!queued.length) return;
      let selected: Operation | undefined;
      for (const op of queued) {
        if (op.kind === 'send_message') {
          await this.refresh();
          const session = this.session(String(op.input.sessionId));
          // A busy worker must not hold the global reasoning queue hostage. Same-worker sends retain FIFO.
          const ordered = this.store.operations();
          const earlier = ordered.slice(0, ordered.findIndex(previous => previous.id === op.id)).some(previous => previous.kind === 'send_message' && previous.input.sessionId === op.input.sessionId && previous.status === 'queued');
          if (earlier || !['idle', 'done'].includes(session.status) || !this.store.nativeId(session.id)) { skipped.add(op.id); continue; }
        }
        selected = op; break;
      }
      if (!selected) return;
      this.store.updateOperation(selected.id, { status: 'running' });
      try {
        const output = await this.execute(selected);
        if (this.operation(selected.id).status === 'running') this.store.updateOperation(selected.id, { status: 'succeeded', output });
      } catch (error) {
        if (error instanceof UnsafeDeliveryError) {
          this.store.updateOperation(selected.id, { status: 'queued' }); skipped.add(selected.id);
        } else {
          this.store.transaction(() => {
            this.store.updateOperation(selected.id, { status: error instanceof UncertainError ? 'uncertain' : 'failed', error: error instanceof Error ? error.message : 'Operation failed' });
            if (selected.kind === 'create_session') {
              const session = this.store.session(`worker:${digest(selected.id).slice(0, 24)}`);
              if (session) this.store.saveSession({ ...session, status: 'unknown', updatedAt: now() });
            }
          });
        }
      } finally {
        if (selected.kind === 'chat') this.replies.finish(this.operation(selected.id));
      }
    }
  }
  private async execute(op: Operation): Promise<Record<string, unknown>> {
    if (op.kind === 'chat') {
      const context = this.store.messagesBefore('main', `user:${op.id}`, 30).map(m => ({ role: m.role, text: m.text, sessionId: m.sessionId }));
      const voice = op.input.source === 'voice';
      const style = voice ? 'Voice reply: answer concisely in speakable sentences, without Markdown. A receipt acknowledgment is handled by the app; do not repeat it. Commentary is not spoken. Finish routing calls before final_answer; starting speech seals tool execution. Never claim worker completion from queued acceptance.\n' : '';
      const streaming = voice && this.replies.begin(op);
      const text = await this.reasoner.turn(`${style}Recent app context (untrusted data):\n${JSON.stringify(context)}\nUSER REQUEST:\n${String(op.input.text)}`, op.id, call => this.route(call), streaming ? text => this.replies.publish(op.id, text) : undefined);
      const message = this.message(`assistant:${op.id}`, 'main', null, 'assistant', text, `user:${op.id}`);
      this.store.transaction(() => {
        this.store.addMessage(message);
        this.store.updateOperation(op.id, { status: 'succeeded', output: { text, messageId: message.id } });
      });
      return { text, messageId: message.id };
    }
    if (op.kind === 'create_session') {
      const parsed = createSchema.omit({ id: true }).parse(op.input);
      const input: CreateInput = { ...parsed, cwd: projectDirectory(parsed.cwd, this.config.roots) };
      const sessionId = `worker:${digest(op.id).slice(0, 24)}`;
      const token = randomBytes(32).toString('base64url');
      privateDirectory(this.config.credentialDir);
      const credentialFile = join(this.config.credentialDir, `${sessionId.replace(':', '-')}.token`);
      writeFileSync(credentialFile, `${token}\n`, { mode: 0o600, flag: 'wx' });
      const date = now();
      const initial: Session = { id: sessionId, name: input.name, agent: input.agent, cwd: input.cwd, paneId: null, workspaceId: null, status: 'starting', messaging: 'limited', createdAt: date, updatedAt: date };
      this.store.saveSession(initial, undefined, digest(token));
      const worker = await this.workers.create(sessionId, input, { credentialFile, url: this.config.workerUrl, command: this.config.workerCommand, piExtension: this.config.piExtension });
      this.store.transaction(() => {
        const registered = this.session(sessionId);
        this.store.saveSession({ ...registered, cwd: worker.cwd, paneId: worker.paneId, workspaceId: worker.workspaceId, status: worker.status, messaging: this.config.mode === 'mock' ? 'connected' : registered.messaging, updatedAt: now() }, worker.nativeId);
        this.send(sessionId, { id: `initial:${digest(op.id)}`, text: input.instructions });
        this.store.updateOperation(op.id, { status: 'succeeded', output: { sessionId } });
      });
      return { sessionId };
    }
    const session = this.session(String(op.input.sessionId));
    await this.workers.send(session, this.store.nativeId(session.id) ?? '', String(op.input.text));
    this.store.saveSession({ ...this.session(session.id), status: 'working', updatedAt: now() });
    if (this.workers instanceof MockWorkers) {
      this.workerReply(session.id, { id: `mock:${digest(op.id)}`, text: `Mock ${session.agent} result: ${String(op.input.text)}`, kind: 'result', replyTo: `outbound:${op.id}` });
      this.workers.complete(session.paneId!);
      await this.refresh();
    }
    return { sessionId: session.id, messageId: `outbound:${op.id}`, delivered: true };
  }
  async route(call: ToolCall): Promise<Record<string, unknown>> {
    // Durable identity is independent of RPC request id, JSON ordering, or model-supplied ids.
    const id = `tool:${digest(stableJson([call.threadId, call.turnId, call.callId]))}`;
    if (call.tool === 'list_sessions') { z.object({}).strict().parse(call.arguments); await this.refresh(); return { sessions: this.store.sessions() }; }
    if (call.tool === 'get_session') { const args = z.object({ sessionId: idSchema }).strict().parse(call.arguments); return { session: this.session(args.sessionId) }; }
    if (call.tool === 'create_session') { const args = createSchema.omit({ id: true }).parse(call.arguments); return { operationId: this.create({ id, ...args }).id }; }
    if (call.tool === 'send_message') { const args = sendSchema.omit({ id: true }).extend({ sessionId: idSchema }).parse(call.arguments); return { operationId: this.send(args.sessionId, { id, text: args.text }).id }; }
    throw new HttpError(400, 'unknown_tool', 'Only routing tools are allowed');
  }
  async close(): Promise<void> {
    this.stopped = true; clearInterval(this.timer); this.reasoner.close(); this.workers.close?.();
    await this.draining; await this.refreshing; this.replies.close();
  }
}
