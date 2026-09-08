import { createServer, type IncomingMessage, type ServerResponse } from 'node:http';
import { z } from 'zod';
import { idSchema, voiceSchema } from './config.js';
import type { ControlService } from './service.js';
import { HttpError, type JournalEvent } from './types.js';
import { voiceConfigured, voiceToken } from './voice.js';
const BODY_LIMIT = 64 * 1024;
async function jsonBody(request: IncomingMessage): Promise<unknown> {
  if (!/^application\/json(?:\s*;|$)/i.test(request.headers['content-type'] ?? '')) throw new HttpError(415, 'unsupported_media_type', 'Use application/json');
  if (request.headers['content-encoding'] && request.headers['content-encoding'] !== 'identity') throw new HttpError(415, 'unsupported_encoding', 'Compressed bodies are not supported');
  if (Number(request.headers['content-length'] ?? 0) > BODY_LIMIT) throw new HttpError(413, 'body_too_large', 'JSON body exceeds 64 KiB');
  const chunks: Buffer[] = []; let size = 0;
  for await (const chunk of request.iterator({ destroyOnReturn: false })) {
    size += chunk.length;
    if (size > BODY_LIMIT) throw new HttpError(413, 'body_too_large', 'JSON body exceeds 64 KiB');
    chunks.push(Buffer.from(chunk));
  }
  try { return JSON.parse(Buffer.concat(chunks).toString('utf8')); }
  catch { throw new HttpError(400, 'invalid_json', 'Malformed JSON body'); }
}
function respond(response: ServerResponse, status: number, value: unknown): void {
  response.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', ...(status >= 400 ? { Connection: 'close' } : {}) });
  response.end(JSON.stringify(value));
}
function workerToken(request: IncomingMessage): string {
  const match = /^Bearer ([A-Za-z0-9_-]{32,128})$/.exec(request.headers.authorization ?? '');
  if (!match) throw new HttpError(401, 'invalid_worker_token', 'Worker bearer credential required');
  return match[1]!;
}
function pathId(value: string): string {
  try { return idSchema.parse(decodeURIComponent(value)); }
  catch { throw new HttpError(400, 'invalid_id', 'Invalid path id'); }
}
function cursor(value: string): number {
  if (!/^\d+$/.test(value) || !Number.isSafeInteger(Number(value))) throw new HttpError(400, 'invalid_cursor', 'Event cursor must be a nonnegative safe integer');
  return Number(value);
}
export function createHttpServer(service: ControlService) {
  const streams = new Set<ServerResponse>();
  const server = createServer((request, response) => {
    void route(request, response).catch(error => {
      if (response.headersSent) { response.destroy(); return; }
      if (error instanceof HttpError) respond(response, error.status, { error: { code: error.code, message: error.message } });
      else if (error instanceof z.ZodError) respond(response, 400, { error: { code: 'invalid_body', message: 'Request does not match the endpoint schema' } });
      else respond(response, 500, { error: { code: 'internal_error', message: 'Control could not complete the request' } });
    });
  });
  server.requestTimeout = 15000; server.headersTimeout = 10000; server.keepAliveTimeout = 5000;
  server.on('close', () => { for (const stream of streams) stream.destroy(); });
  async function route(request: IncomingMessage, response: ServerResponse): Promise<void> {
    if (request.headers.origin !== undefined || request.headers['sec-fetch-site'] !== undefined) throw new HttpError(403, 'browser_not_allowed', 'Browser-origin requests are not supported');
    const url = new URL(request.url ?? '/', 'http://control.local'); const path = url.pathname;
    const method = request.method;
    // Worker bearer credentials cannot be used to invoke the unscoped control API.
    if (request.headers.authorization && !path.startsWith('/v1/worker/')) throw new HttpError(403, 'worker_scope_required', 'Worker credentials are only accepted on worker endpoints');
    if (method === 'GET' && path === '/health') return respond(response, 200, { status: 'ok', mode: service.config.mode, codex: service.reasoner.status, voiceConfigured: voiceConfigured(service.config) });
    if (method === 'GET' && path === '/v1/sessions') { await service.refresh(); return respond(response, 200, { sessions: service.store.sessions() }); }
    if (method === 'POST' && path === '/v1/sessions') return respond(response, 202, { operationId: service.create(await jsonBody(request)).id });
    if (method === 'POST' && path === '/v1/chat') return respond(response, 202, { operationId: service.chat(await jsonBody(request)).id });
    const messages = /^\/v1\/sessions\/([^/]+)\/messages$/.exec(path);
    if (method === 'POST' && messages) return respond(response, 202, { operationId: service.send(pathId(messages[1]!), await jsonBody(request)).id });
    const session = /^\/v1\/sessions\/([^/]+)$/.exec(path);
    if (method === 'GET' && session) return respond(response, 200, { session: service.session(pathId(session[1]!)) });
    const reply = /^\/v1\/operations\/([^/]+)\/reply$/.exec(path);
    if (method === 'GET' && reply) {
      if (request.headers['last-event-id'] || url.search) throw new HttpError(400, 'reply_not_resumable', 'Speech streams cannot resume');
      const op = service.operation(pathId(reply[1]!));
      if (streams.size >= 100) throw new HttpError(503, 'too_many_streams', 'Too many event streams');
      let unsubscribe = () => {}; let stopped = false;
      response.on('close', () => { stopped = true; clearInterval(heartbeat); clearTimeout(deadline); unsubscribe(); streams.delete(response); });
      const heartbeat = setInterval(() => {
        if (response.writableLength > 262144) response.destroy();
        else if (!stopped) response.write(': heartbeat\n\n');
      }, 15000); heartbeat.unref();
      const deadline = setTimeout(() => response.destroy(), 300000); deadline.unref();
      const start = () => {
        if (response.headersSent) return;
        response.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store', 'Connection': 'keep-alive', 'X-Accel-Buffering': 'no' });
        response.flushHeaders(); streams.add(response);
      };
      unsubscribe = service.replies.follow(op, event => {
        if (stopped) return;
        start();
        if (response.writableLength > 262144) { response.destroy(); return; }
        response.write(`data: ${JSON.stringify(event)}\n\n`);
        if (event.type !== 'text') response.end();
      });
      start();
      if (stopped) unsubscribe();
      return;
    }
    const operation = /^\/v1\/operations\/([^/]+)$/.exec(path);
    if (method === 'GET' && operation) return respond(response, 200, { operation: service.operation(pathId(operation[1]!)) });
    if (method === 'GET' && path === '/v1/messages') {
      const conversationId = url.searchParams.get('conversationId') ?? 'main';
      if (conversationId !== 'main') {
        if (!conversationId.startsWith('session:')) throw new HttpError(400, 'invalid_conversation', 'Use main or session:<sessionId>');
        service.session(pathId(conversationId.slice(8)));
      }
      return respond(response, 200, { messages: service.store.messages(conversationId) });
    }
    if (method === 'GET' && path === '/v1/events') {
      const after = cursor(String(request.headers['last-event-id'] ?? url.searchParams.get('after') ?? '0'));
      if (streams.size >= 100) throw new HttpError(503, 'too_many_streams', 'Too many event streams');
      response.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache, no-transform', 'Connection': 'keep-alive', 'X-Accel-Buffering': 'no' });
      response.flushHeaders(); streams.add(response);
      let stopped = false;
      const write = (event: JournalEvent) => {
        if (stopped) return;
        // Disconnect slow consumers; the last delivered cursor can replay from SQLite on reconnect.
        if (response.writableLength > 1024 * 1024) { stopped = true; response.destroy(); return; }
        response.write(`id: ${event.seq}\nevent: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`);
      };
      const unsubscribe = service.store.follow(after, write);
      const heartbeat = setInterval(() => { if (!stopped) response.write(': heartbeat\n\n'); }, 15000); heartbeat.unref();
      response.on('close', () => { stopped = true; clearInterval(heartbeat); unsubscribe(); streams.delete(response); });
      return;
    }
    if (method === 'POST' && path === '/v1/voice/token') { voiceSchema.parse(await jsonBody(request)); return respond(response, 200, await voiceToken(service.config)); }
    if (path.startsWith('/v1/worker/')) {
      const token = workerToken(request); service.store.authenticate(token);
      if (method === 'POST' && path === '/v1/worker/register') { z.object({}).strict().parse(await jsonBody(request)); return respond(response, 200, service.register(token)); }
      if (method === 'GET' && path === '/v1/worker/inbox') return respond(response, 200, { messages: service.inbox(token) });
      if (method === 'POST' && path === '/v1/worker/replies') return respond(response, 200, { message: service.reply(token, await jsonBody(request)) });
    }
    throw new HttpError(404, 'not_found', 'Endpoint not found');
  }
  return { server, closeStreams: () => { for (const response of streams) response.destroy(); } };
}
