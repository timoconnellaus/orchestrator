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

## Transient pushed voice replies

`GET /v1/operations/:id/reply` follows one accepted chat operation, with
`Content-Type: text/event-stream`, `Cache-Control: no-store`, and frames
`data: <JSON>\n\n`. Each event contains `operationId` and a positive, contiguous
per-attachment `seq`, plus one of:

- `{type:"text", text:string}`: append-only provisional answer text.
- `{type:"terminal", operation:Operation}`: authoritative post-commit snapshot;
  closes the stream. Successful output remains `{text,messageId}`.
- `{type:"unavailable"}`: transient speech expired/overflowed; closes the stream.

This is **not** the durable `/v1/events` journal and is **not resumable audio**.
Last-Event-ID and query cursors are rejected. First attachment catches up the
bounded in-memory prefix if still running; an already terminal operation sends
only its SQLite snapshot. Server callbacks are synchronous across snapshot and
subscription, avoiding a lost-completion race. Limits: 16 retained in-flight
operations, 8 listeners per operation, 2,048 text events/128 KiB per operation,
100 HTTP streams overall, 256 KiB socket backlog, 15-second heartbeat, five-minute
stream/retention deadlines measured from durable acceptance. Expiry immediately
reclaims capacity; old pending operations remain ineligible by their acceptance
time, without retaining tombstones. Generation reserves capacity before producing
text; failed admission stays final-only even if capacity becomes available later.
Publishing cannot recreate a missing stream or a lost prefix. Consumer failure
never cancels reasoning or a queued worker operation.

Early voice output requires an explicitly identified Codex `agentMessage` with
`phase:"final_answer"`, matched thread/turn/item identity, no mutating routing
calls, and no outstanding reads. Beginning early output seals all further routing
tool execution for that turn. Mutating turns remain final-only; receipt/queued
acceptance is not coding completion. Missing phases fall back to completed legacy
agent text, not guessed streaming. Explicit commentary is excluded from the
canonical answer and remains muted. Reasoning and tool payloads are never routed
to speech. There is no added speech/progress tool.

After durable POST acknowledgment, voice may emit one delayed (750 ms) "Got it."
receipt if substantive output has not arrived. It is suppressed for fast answers
and is absent from durable chat. An explicit paragraph flush in the pinned SDK
sentence adapter makes this short acknowledgment synthesize without waiting for
Codex's final sentence. Voice input receives concise speakable-answer guidance.

The bridge consumes one stream with no normal completion polling/reconnection.
It suppresses duplicate sequence numbers, fails closed on gaps/identity changes,
and appends only a verified canonical suffix at success. A corrected canonical
answer or interruption/loss after a prefix never triggers whole-answer replay;
users are directed to durable chat. The bridge suppresses repeated SDK playback
invocations over its last 128 request IDs. Same-ID ambiguous POST reconciliation
is retained and shielded from playback cancellation. Partial speech cannot be
retracted and is never completion evidence. TTS retries are disabled for both
sentence synthesis and streaming providers: an error after partial audio must not
repeat a sentence. The outer TTS node explicitly closes its inner iterator on
cancellation or closure, rather than relying on garbage collection.

`ELEVEN_API_KEY` is an optional private credential slot only; its presence does
not enable ElevenLabs. Both it and `OPENAI_API_KEY` are stripped from the Codex
reasoner subprocess. No provider/model/voice/endpointing settings change in this
reply-streaming slice. Deploy compatible control before the updated voice worker;
older control lacks this endpoint and the voice worker fails closed rather than
silently polling or replaying.

## Voice tuning and ephemeral diagnostics

`POST /v1/voice/token` accepts `{conversationId:"main", voiceTuning?: TuningV1}`.
The entire tuning object is required when present; omission alone selects legacy
defaults. Version 1 fields (unknown fields/versions, non-finite values, numeric
booleans, noninteger durations and out-of-range values are rejected):

| Field | Range | Default |
| --- | --- | --- |
| `version` | 1 | 1 |
| `activationThreshold` | 0.3–0.8 | 0.5 |
| `minSpeechMs` | 50–300 integer ms | 50 |
| `endSilenceMs` | 300–1200 integer ms | 550 |
| `interruptionMs` | 300–1200 integer ms | 500 |
| `elevenLabsSpeed` | 0.8–1.2×, ElevenLabs only | 1.0 |
| `echoCancellation`, `noiseSuppression`, `autoGainControl` | boolean | true |

The response includes canonical `voiceTuning`, `room`, and linked phone `speaker`
identity alongside `url` and `token`. Signed participant metadata and the explicit
`orchestrator-voice` dispatch metadata carry exactly `{conversationId, voiceTuning,
room, speaker}`. Python accepts legacy metadata without tuning, rejects unknown
metadata fields, bounds the encoded metadata to 4096 bytes and identifiers to 256
characters, and checks dispatch room identity. Rooms remain fresh per explicit
join, conversation `main` stays durable, and closed speech sessions retire jobs.
The shared fixture is `docs/fixtures/voice-tuning-v1.json`.

Flutter freezes an immutable validated snapshot before the first join await.
Saving uses the independent `voiceTuning` preference, never the history snapshot,
and surfaces write failures without replacing the last saved value. Presets and
reset edit the draft only. Supported `AudioCaptureOptions` are used both for
microphone enable and **RoomOptions defaults** (the pinned SDK uses these to
recreate tracks on full reconnect); `stopAudioCaptureOnMute:true` remains fixed.
No native live updates or `getSettings()` hardware-verification claims are made.
A missing/mismatched token tuning echo rejects custom joins; legacy default joins
may continue explicitly unconfirmed. A token echo is not Mac confirmation.

Before either STT or AgentSession creates streams, each job reapplies all exposed
VAD options, including `deactivation_threshold=max(activationThreshold-.15,.01)`;
Silero's update method does not derive hysteresis. No running VAD is retuned.
Interruption minimum follows the snapshot; SDK endpointing remains fixed at
0.8/3 seconds. Pre-roll, the **0.55 second overflow readmission silence**, all
upload/buffer/backlog/generation/watchdog limits, no retry/replay, no speculative
generation/TTS, and no false-interruption resume are unchanged.
ElevenLabs speed follows the same per-join snapshot. At 1.0 the existing request
is unchanged; otherwise only speed is sent as a request-local voice setting.
OpenAI ignores this field. See [the adapter note](research/elevenlabs-speed.md).

Diagnostics use lossy named LiveKit topic `orchestrator.voice.diagnostics.v1` to
only the session's linked local speaker. The agent uses one owned coalescing
sender task (at most about 5 Hz, 150 ms send timeout, latest state only, closed at
job shutdown). An ambiguous send failure disables diagnostics for the remainder
of that join: cancelling the SDK waiter does not cancel a native publication,
so another native send must not be launched after a timeout. Meters expire while
speech continues. No network awaits or per-frame tasks are added to VAD handling.
After existing generation/enabled/staleness guards, incremental INFERENCE_DONE
frames supply normalized PCM RMS in [0,1]; START/END and the local gate determine
speech state (inference can precede the state transition). Capture resets/mute
clear state and increment generation. No audio, transcript, command, or secrets
are transmitted by diagnostics; neither diagnostics nor acknowledgements enter
SQLite, chat, outbox, or preferences.

Packets contain exactly `{version:1, room, speaker, sender, session, nonce,
sequence, generation, sentAtMs, available, fresh, level, speech, voiceTuning}`.
Sequence and generation are nonnegative monotonic integers. `session` is a fresh
worker diagnostics UUID. A phone capture/reconnect boundary rotates its nonce
and sends a small reliable `{nonce}` probe on `orchestrator.voice.probe.v1` (at
most once/second; 500 ms local wait cap). The Mac accepts probes only from the
metadata-linked phone, with an exact schema and a 256-byte limit. Probes reset
only diagnostics: they never enable capture or change VAD options. Same-nonce
probes do not reset state. Publishing additionally requires the actual SDK-linked
participant to match the token speaker.

The phone admits at most 2048 bytes, validates exact schema/finite ranges,
expected AGENT kind and `orchestrator-voice` name, actual sender identity, local
speaker/room, tuning equality, pinned sender/session, nonce, sequence and
generation. It drops packets older than 1.5 seconds (or over 1.5 seconds in the
future). Mac audio freshness and phone receipt expiry are also 1.5 seconds.
Clock skew may therefore yield **unavailable**, never fabricated readings.
One pending join acknowledgement can be buffered during microphone enable, with
its original receipt expiry; canceled/stale joins cannot revive it. Mute,
reconnect, disconnect and disposal clear current diagnostics/confirmation without
implicitly rearming. Buffered STT still applies the shared SDK VAD snapshot but
reports `available:false`, `fresh:false`, zero level and no detected speech.
