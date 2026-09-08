# Orchestrator v1 contract

User approved standalone `/Users/tim/repos/orchestrator`, Flutter Android, Tailscale, LiveKit voice, Codex orchestration (existing ChatGPT login), OpenAI transcription + TTS, emulator-only installation. No separate Pi orchestrator. Pi/Codex/Claude workers remain in Herdr. No login screen. Never change existing user sessions or global CLI configuration during development.

## Modules and ownership

- `apps/control`: TypeScript/Node 22.13+ HTTP interface, SQLite journal, operation queue, Codex app-server stdio adapter, Herdr adapter, worker reply interface, LiveKit token issuance. Own package.json/lockfile/tests; no root edits.
- `apps/voice`: Python 3.11+ LiveKit Agents worker; a custom LLM node forwards finalized text to control and returns its answer. It has no separate reasoning model. Own pyproject/lock/tests/README.
- `apps/mobile`: Flutter Android app. Own pubspec/lock/native Android/tests/README. Only install to `emulator-5554` when requested by parent.
- `packages/worker-tools`: stdio MCP worker tools and Pi extension bridging to control. Parent owns this.
- Parent owns root docs, scripts and deployment config. Keep dependencies module-local; no npm workspace root lockfile.

## Transport

Control defaults to `127.0.0.1:8787`. Native phone can connect using emulator `10.0.2.2:8787` or a configured Tailscale address. Explicit host binding required for tailnet deployment; no public listener by default. Reject browser Origin requests, bound JSON body size, no wildcard CORS. No user auth UI; LiveKit requires auto-issued room tokens. Worker reply tokens are scoped to a session and never appear in general session lists or event journal.

All JSON success bodies are direct objects, not nested `result` envelopes. Errors: `{error:{code:string,message:string}}`. Timestamps ISO-8601 strings. IDs opaque strings. App conversation ID defaults to `main`. Worker threads use `session:<sessionId>`.

## Data

```
Session {id, name, agent: 'pi'|'codex'|'claude', cwd, paneId: string|null,
 workspaceId: string|null, status: 'working'|'blocked'|'done'|'idle'|'unknown'|'offline'|'starting',
 messaging: 'connected'|'limited', createdAt, updatedAt}
Message {id, conversationId, sessionId: string|null,
 role: 'user'|'assistant'|'worker'|'system', text, replyTo: string|null, createdAt}
Operation {id, kind: 'chat'|'create_session'|'send_message',
 status:'queued'|'running'|'succeeded'|'failed'|'uncertain',
 input: object, output: object|null, error: string|null, createdAt, updatedAt}
Event {seq: integer, type: string, data: object, createdAt}
```

## HTTP interface

- `GET /health` -> `{status:'ok',mode:'mock'|'live',codex:'ready'|'unavailable'|'mock',voiceConfigured:boolean}`. No secrets.
- `GET /v1/sessions` -> `{sessions:Session[]}`. Refresh/reconcile Herdr when safe. Explicit no-mutation observation.
- `POST /v1/sessions` body `{id,agent,cwd,name,instructions}` -> HTTP 202 `{operationId}`. id is the client idempotency key. Validate configured project roots. Provision workspace/pane with no focus stealing; create dedicated Git worktree where supported rather than share a working directory silently. Never auto retry a side effect with ambiguous outcome.
- `GET /v1/sessions/:id` -> `{session:Session}`.
- `POST /v1/sessions/:id/messages` body `{id,text}` -> HTTP 202 `{operationId}`. Save outbound message to its worker thread. Queue while working/blocked/unknown; send only when verified idle/done and native session still matches. Sending is not proof of task completion.
- `GET /v1/messages?conversationId=main` -> `{messages:Message[]}` ordered oldest-first, bounded recent history (e.g. latest 200).
- `POST /v1/chat` body `{id,text,conversationId:'main',source?:'text'|'voice'}` -> HTTP 202 `{operationId}`. Durable before acknowledging. Queue serialized turns to the single persistent Codex thread; keep queue ordering across producers. Succeeded operation output is `{text:string,messageId:string}`. App callers must reuse id for a retry; differing payload under same id yields 409.
- `GET /v1/operations/:id` -> `{operation:Operation}`.
- `GET /v1/events?after=0` -> SSE `id: <seq>\nevent: <type>\ndata: <JSON Event>\n\n`. Resume using cursor or Last-Event-ID. Heartbeats comments. Events `message.created` data Message, `session.updated` data Session, `operation.updated` data Operation. Journal replay must not race live subscription. Optional `assistant.delta` only for ephemeral display; final Message is authoritative. No client depends on delta for correctness.
- `POST /v1/voice/token` body `{conversationId:'main'}` -> `{url:string,token:string,room:string}`. Stable room name `orchestrator-main`, unique phone participant ID, room scoped. Explicit agent dispatch name `orchestrator-voice` if supported by SDK. Return 503 `voice_not_configured` if credentials/URL not configured. URL must be reachable by the phone, separate internal URL allowed.
- `POST /v1/worker/register` Authorization `Bearer <worker-token>`, body `{}` -> `{sessionId:string}`. Marks messaging connected after credential verification, no caller supplied identity.
- `GET /v1/worker/inbox` same worker token -> `{messages:Message[]}` for that session; bounded and non-destructive. Delivery/wakeup remains control adapter responsibility, not MCP polling by itself.
- `POST /v1/worker/replies` same worker token, body `{id,text,replyTo?:string,kind:'progress'|'question'|'result'|'error'}` -> `{message:Message}`. Durable idempotent reply, derive sessionId from credential not body. Validate replyTo belongs to that worker's thread. Emit/save a main-conversation notification as well as worker-thread reply so phone and orchestrator know of it. Do not auto-reply to worker responses in an unbounded bot loop.

## Environment names

Control: `ORCHESTRATOR_MODE=mock|live` (default live), `ORCHESTRATOR_HOST=127.0.0.1`, `ORCHESTRATOR_PORT=8787`, `ORCHESTRATOR_DB=.data/orchestrator.sqlite`, `ORCHESTRATOR_PROJECT_ROOTS` (path-delimited allowed project roots; no arbitrary cwd by default), `HERDR_BIN_PATH`, `HERDR_SOCKET_PATH`, `CODEX_BIN=codex`, optional `CODEX_MODEL`, `LIVEKIT_URL` (phone URL), `LIVEKIT_INTERNAL_URL` (Mac URL), `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, `ORCHESTRATOR_WORKER_COMMAND` (path to worker MCP launcher), `ORCHESTRATOR_PI_EXTENSION` (path to Pi extension).

Worker adapters: `ORCHESTRATOR_URL=http://127.0.0.1:8787`, `ORCHESTRATOR_WORKER_TOKEN` (secret; scoped, inherited only by the launched worker).

Voice: `ORCHESTRATOR_URL=http://127.0.0.1:8787`, `LIVEKIT_URL=ws://127.0.0.1:7880` (internal URL), `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, `OPENAI_API_KEY`, `OPENAI_STT_MODEL` (default documented available speech model), `OPENAI_TTS_MODEL=gpt-4o-mini-tts`, `OPENAI_TTS_VOICE=coral`.

Flutter persists editable backend URL, default `http://10.0.2.2:8787` in emulator. No provider secrets on phone. Mobile supports typed chat without LiveKit/voice credentials.

## Codex

Installed CLI 0.153.4; `codex login status` reports ChatGPT login. Use supported app-server JSONL stdio, initialize/initialized, thread/start or resume, turn/start, item/agentMessage/delta, item/completed, turn/completed. App-server is experimental, pin/test installed protocol. Generate schemas locally with `codex app-server generate-ts --experimental --out /tmp/...` (no auth secrets). Dynamic tools are experimental: initialize capabilities experimentalApi true and inspect generated thread fields. `item/tool/call` -> `{contentItems:[{type:'inputText',text:JSON.stringify(result)}],success:boolean}`. Expose only list_sessions, create_session, send_message, get_session to reasoning. Deny command/file/permission approval requests, no dangerous permission bypass. Bound pending requests and crash recovery; persisted in-flight operations become uncertain rather than auto-replayed. Cancelled audio consumer must NOT cancel an accepted durable Codex turn. No global Codex config edits, no new account authentication flow.

## Voice

Use LiveKit Python official SDK, Silero VAD + suitable turn detection, OpenAI STT and TTS, custom llm_node delegates to POST chat and GET operation until terminal. Voice interruption cancels playback/polling, not durable work. Disable speculative/preemptive generation before creating durable tasks. No idle check-in or automatic idle hangup. No assumption of eight-hour uninterrupted provider session. Retry/reconnect the media independently. Foreground listening remains Android-owned. Do not claim ambient speech intent detection or zero audio traffic during silence.

## Testing and safety

All default automated suites use deterministic mocks, temp DBs, fake Herdr/Codex subprocesses. Never mutate a real existing Herdr session in tests. Test dedup/conflicting id payload, ordering/restart ambiguity, replay, busy queue, blocked status, invalid replies, adapter process exit/errors, and path/JSON validation. Live read-only Codex/Herdr smoke test is parent-owned and must be explicit. Flutter mock client tests must work without mic/network. Native Android microphone service must be started by visible Activity/user action, have ongoing notification and stop action, stop on disarm, and handle permissions/restart honestly. No promised automatic mic restart from background. No install on physical devices.
