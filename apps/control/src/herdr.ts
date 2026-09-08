import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { access, realpath, stat } from 'node:fs/promises';
import { constants } from 'node:fs';
import { z } from 'zod';
import { projectDirectory, type Config } from './config.js';
import { digest } from './store.js';
import { UncertainError, UnsafeDeliveryError, type Agent, type CreateInput, type NativeSession, type Session, type WorkerAdapter, type WorkerLaunch } from './types.js';
const execute = promisify(execFile);
const agentSchema = z.object({
  agent: z.string().nullable().optional(), pane_id: z.string(), workspace_id: z.string(),
  name: z.string().nullable().optional(), cwd: z.string().nullable().optional(), foreground_cwd: z.string().nullable().optional(),
  agent_status: z.enum(['idle', 'working', 'blocked', 'done', 'unknown']),
  agent_session: z.object({ agent: z.string(), kind: z.string(), value: z.string().min(1), source: z.string() }).nullable().optional(),
  interactive_ready: z.boolean().optional(), launch_pending: z.boolean().optional(),
});
function native(raw: z.infer<typeof agentSchema>): NativeSession | undefined {
  if (!['pi', 'codex', 'claude'].includes(raw.agent ?? '')) return;
  const identity = raw.agent_session;
  return { nativeId: identity && identity.agent === raw.agent ? `${identity.agent}:${identity.kind}:${identity.value}` : '',
    paneId: raw.pane_id, workspaceId: raw.workspace_id, name: raw.name ?? raw.pane_id,
    cwd: raw.foreground_cwd ?? raw.cwd ?? '', agent: raw.agent as Agent,
    status: raw.launch_pending ? 'starting' : raw.interactive_ready === false && ['idle', 'done'].includes(raw.agent_status) ? 'unknown' : raw.agent_status };
}
export function workerArguments(agent: Agent, launch: WorkerLaunch): string[] {
  const args = ['--credential-file', launch.credentialFile, '--url', launch.url];
  if (agent === 'pi') return ['-e', launch.piExtension, '--orchestrator-credential-file', launch.credentialFile, '--orchestrator-url', launch.url];
  if (agent === 'claude') return ['--strict-mcp-config', '--mcp-config', JSON.stringify({ mcpServers: { orchestrator: { command: launch.command, args } } })];
  return ['-c', `mcp_servers.orchestrator.command=${JSON.stringify(launch.command)}`, '-c', `mcp_servers.orchestrator.args=${JSON.stringify(args)}`];
}
/** CLI protocol 20, inspected using `herdr api schema --json`. No shell or focus-changing commands. */
export class HerdrAdapter implements WorkerAdapter {
  constructor(private config: Config) {}
  private async run(args: string[], mutating = false): Promise<unknown> {
    try {
      const { stdout } = await execute(this.config.herdrBin, args, { env: { ...process.env, ...(this.config.herdrSocket ? { HERDR_SOCKET_PATH: this.config.herdrSocket } : {}) }, timeout: this.config.requestTimeoutMs + 5000, maxBuffer: 4 * 1024 * 1024 });
      const envelope = z.object({ error: z.unknown().optional(), result: z.unknown().optional() }).parse(JSON.parse(stdout));
      if (envelope.error || !envelope.result) throw new Error('Herdr rejected request or returned no result');
      return envelope.result;
    } catch {
      // stderr may contain launch configuration. Do not journal raw process output.
      if (mutating) throw new UncertainError('Herdr side effect response unavailable; inspect workspace manually. Not retried.');
      throw new Error('Herdr observation unavailable');
    }
  }
  async list(): Promise<NativeSession[]> {
    const result = z.object({ agents: z.array(agentSchema) }).parse(await this.run(['agent', 'list']));
    return result.agents.flatMap(raw => { const entry = native(raw); return entry ? [entry] : []; });
  }
  async create(id: string, input: CreateInput, launch: WorkerLaunch): Promise<NativeSession> {
    await access(input.agent === 'pi' ? launch.piExtension : launch.command, input.agent === 'pi' ? constants.R_OK : constants.X_OK);
    // Reject non-Git directories before any Herdr mutation. No shared-cwd fallback.
    const repository = await execute('git', ['-C', input.cwd, 'rev-parse', '--show-toplevel'], { timeout: 5000 });
    projectDirectory(repository.stdout.trim(), this.config.roots);
    const result = await this.run(['worktree', 'create', '--cwd', input.cwd, '--branch', `orchestrator/${digest(id).slice(0, 20)}`, '--label', input.name, '--no-focus'], true);
    try {
      const created = z.object({ workspace: z.object({ workspace_id: z.string() }), root_pane: z.object({ pane_id: z.string(), agent: z.string().nullable().optional() }), worktree: z.object({ path: z.string() }) }).parse(result);
      const cwd = await realpath(created.worktree.path);
      if (!(await stat(cwd)).isDirectory() || cwd === await realpath(input.cwd)) throw new Error('Dedicated worktree missing');
      if (created.root_pane.agent || (await this.list()).some(a => a.paneId === created.root_pane.pane_id)) throw new Error('Workspace hook owns the returned pane');
      const start = z.object({ agent: agentSchema }).parse(await this.run(['agent', 'start', `orch-${digest(id).slice(0, 16)}`, '--kind', input.agent, '--pane', created.root_pane.pane_id, '--timeout', String(this.config.requestTimeoutMs), '--', ...workerArguments(input.agent, launch)], true));
      const session = native(start.agent);
      if (!session?.nativeId || session.agent !== input.agent || session.paneId !== created.root_pane.pane_id || session.workspaceId !== created.workspace.workspace_id) throw new Error('Native identity not confirmed');
      return { ...session, cwd };
    } catch {
      throw new UncertainError('Worktree created but safe worker readiness/native identity not confirmed; inspect Herdr manually. No retry or shell injection.');
    }
  }
  async send(session: Session, nativeId: string, text: string): Promise<void> {
    const observed = (await this.list()).find(a => a.paneId === session.paneId);
    if (!nativeId || !observed || observed.nativeId !== nativeId || observed.workspaceId !== session.workspaceId || observed.agent !== session.agent || !['idle', 'done'].includes(observed.status)) throw new UnsafeDeliveryError('Native worker identity or safe state not confirmed');
    // Require observed acceptance/state change, not a stale pre-submission idle screen.
    // This is delivery acknowledgement only, never a worker task result.
    const result = await this.run(['agent', 'prompt', observed.paneId, text, '--wait', '--until', 'working', '--until', 'done', '--until', 'blocked', '--until', 'idle', '--timeout', String(this.config.requestTimeoutMs)], true);
    try {
      const acknowledgement = native(z.object({ agent: agentSchema }).parse(result).agent);
      const after = (await this.list()).find(a => a.paneId === session.paneId);
      for (const current of [acknowledgement, after]) {
        if (!current || current.nativeId !== nativeId || current.agent !== session.agent || current.workspaceId !== session.workspaceId || current.paneId !== session.paneId) throw new Error('Worker changed during delivery');
      }
    } catch {
      throw new UncertainError('Prompt may have been delivered but native worker identity changed or became unavailable. Not retried.');
    }
  }
}
