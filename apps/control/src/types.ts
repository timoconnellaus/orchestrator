export type Agent = 'pi' | 'codex' | 'claude';
export type SessionStatus = 'working' | 'blocked' | 'done' | 'idle' | 'unknown' | 'offline' | 'starting';
export interface Session {
  id: string; name: string; agent: Agent; cwd: string; paneId: string | null;
  workspaceId: string | null; status: SessionStatus; messaging: 'connected' | 'limited';
  createdAt: string; updatedAt: string;
}
export interface Message {
  id: string; conversationId: string; sessionId: string | null;
  role: 'user' | 'assistant' | 'worker' | 'system'; text: string; replyTo: string | null; createdAt: string;
}
export interface Operation {
  id: string; kind: 'chat' | 'create_session' | 'send_message';
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'uncertain';
  input: Record<string, unknown>; output: Record<string, unknown> | null;
  error: string | null; createdAt: string; updatedAt: string;
}
export interface JournalEvent { seq: number; type: string; data: Session | Message | Operation; createdAt: string }
export interface NativeSession {
  nativeId: string; paneId: string; workspaceId: string | null; name: string;
  agent: Agent; cwd: string; status: SessionStatus;
}
export interface CreateInput { agent: Agent; cwd: string; name: string; instructions: string }
export interface WorkerLaunch { credentialFile: string; url: string; command: string; piExtension: string }
export interface WorkerAdapter {
  list(): Promise<NativeSession[]>;
  create(id: string, input: CreateInput, launch: WorkerLaunch): Promise<NativeSession>;
  send(session: Session, nativeId: string, text: string): Promise<void>;
  close?(): void;
}
export interface ToolCall { threadId: string; turnId: string; callId: string; tool: string; arguments: unknown }
export type ToolHandler = (call: ToolCall) => Promise<Record<string, unknown>>;
export interface Reasoner {
  readonly status: 'ready' | 'unavailable' | 'mock';
  turn(text: string, operationId: string, tools: ToolHandler): Promise<string>;
  close(): void;
}
export class HttpError extends Error {
  constructor(public status: number, public code: string, message: string) { super(message); }
}
/** A subprocess may have performed work before its response was lost. Never retry automatically. */
export class UncertainError extends Error {}
export class UnsafeDeliveryError extends Error {}
export const now = () => new Date().toISOString();
