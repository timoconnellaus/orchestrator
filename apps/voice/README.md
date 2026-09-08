# Orchestrator voice

Python LiveKit Agents worker explicitly dispatched as **`orchestrator-voice`**.
Implements [`docs/contract.md`](../../docs/contract.md); only this module is owned here.

```
Phone microphone → LiveKit → Silero VAD → OpenAI STT (REST, finalized text)
  → custom llm_node → control POST /v1/chat → GET /v1/operations/:id
  → terminal output.text → streamed OpenAI TTS → LiveKit → phone
```

**There is no second reasoning model.** Codex reasoning, persistent conversation,
worker tools, queue serialization and durable work belong to control. The local
`LocalNodeOnlyLLM` is a non-provider SDK guard: LiveKit 1.5.12 skips custom nodes
when `llm=None`. Its `chat()` fails closed; the actual custom `llm_node` only uses
HTTP. Nothing invokes OpenAI chat, realtime reasoning, or an LLM turn detector.

## Install and run

Requires uv and Python 3.11 (selected by `.python-version`; package allows 3.11–3.13).
Dependencies are module-local. `uv.lock` pins LiveKit Agents/OpenAI/Silero plugins
1.5.12, OpenAI Python 2.54.0 and all transitive dependencies. **Do not remove the
OpenAI `<3` constraint** without rechecking plugin compatibility: OpenAI 3.8 uses
httpx2, whereas this LiveKit plugin constructs/passes httpx clients.

```sh
cd apps/voice
uv sync --locked
cp .env.example .env
# Edit .env locally with real credentials; never commit it.
uv run --env-file .env orchestrator-voice download-files
uv run --env-file .env orchestrator-voice start
```

`download-files` is an explicit runtime setup step for model assets, not a test
step. Silero is loaded once per worker process. No semantic turn-detector download
is needed. For development use `dev` instead of `start`. Avoid `console` unless you
intend to use the host microphone and pay for speech calls. `.env` is loaded by the
explicit uv flag, not implicitly by application code. Use `--help` without keys.

Parent owns LiveKit server, control token/dispatch wiring, process supervisor,
launcher, deployment credentials and mobile foreground microphone behavior.
The control service must already be reachable. There is no startup greeting.
Runtime speech calls cost money; tests below do not make them.

## Environment

| Name | Default / meaning |
| --- | --- |
| `ORCHESTRATOR_URL` | `http://127.0.0.1:8787`; voice-to-control URL, including any configured path prefix |
| `ORCHESTRATOR_CONVERSATION_ID` | `main`; fallback when dispatch metadata does not supply `conversationId` |
| `LIVEKIT_URL` | Required at runtime; internal worker-reachable URL, e.g. `ws://127.0.0.1:7880` |
| `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | Required worker credentials; never put on phone |
| `OPENAI_API_KEY` | Required for transcription/TTS only |
| `OPENAI_STT_MODEL` | `gpt-4o-mini-transcribe`, official available speech model and pinned LiveKit Python default |
| `OPENAI_STT_LANGUAGE` | `en`; transcription language hint |
| `OPENAI_TTS_MODEL` | `gpt-4o-mini-tts` |
| `OPENAI_TTS_VOICE` | `coral` |
| `VOICE_REQUEST_TIMEOUT_SECONDS` | `10`; per HTTP request timeout |
| `VOICE_SUBMISSION_TIMEOUT_SECONDS` | `25`; hard wall-clock limit for POST plus same-ID retries |
| `VOICE_POLL_TIMEOUT_SECONDS` | `300`; hard wall-clock limit after acknowledgement, including GET requests/delays |
| `VOICE_POLL_INTERVAL_SECONDS` | `1`; interval between polls and POST retries |

Timeouts must be finite and positive. At most three POST attempts are made. The
worker allows 35 seconds for shutdown cleanup; leave submission timeout below
that, or process shutdown can interrupt reconciliation (still never canceling
accepted backend work). Actual worker termination can always be abrupt.

Dispatch uses trusted **job metadata** `{"conversationId":"main"}`. Control should
supply the correct conversation ID for every non-main room; absent metadata uses
the environment fallback, not a guess from the room name. Malformed metadata fails
rather than silently routing to main. IDs are passed unchanged; operation IDs are
URL-escaped. Participant metadata cannot override backend URL or conversation.
`ORCHESTRATOR_URL` must be HTTP(S), without credentials, query or fragment.
Redirects and environment proxy discovery are disabled for control requests.
Use private loopback/tailnet reachability; this worker does not add control auth.

## Speech, silence and eight-hour use

- No wake word. All detected speech can become a command, including ambient voices;
  there is **no ambient-intent classifier**. Arm/disarm and user awareness are phone-owned.
- Silero VAD with fixed endpointing (0.8–3 seconds) defines end of turn. REST STT
  supplies final transcripts, not speculative partials. This is silence-based,
  not semantic turn detection; pauses can split a sentence, and continuous speech
  is subject to SDK buffering limits. Tune only after real microphone testing.
- Preemptive generation **and preemptive TTS are explicitly disabled**. No durable
  task is intentionally started from a partial/speculative transcript.
- `user_away_timeout=None`; no idle callback, prompt, nagging, user-turn limit or
  scheduled hangup. A silent connected app may remain armed for eight hours, but
  **eight hours of uninterrupted microphone/provider/media connectivity is not
  promised or tested**. Android lifecycle, room policy, network, OS sleep and
  provider failures can stop it.
- VAD interruptions stop speech/poll consumption. False-interruption auto-resume
  is disabled; an interrupted response is not automatically read later. There is
  no second background speaker and no "still working" timer.
- TTS uses the official OpenAI plugin and the SDK text StreamAdapter. The plugin
  advertises `streaming=False` for *incremental text input*; **audio responses are
  streamed**. `gpt-4o-mini-tts` uses SSE `speech.audio.delta`, decoded into audio
  frames before the response finishes. Legacy tts-1 uses streamed audio bytes.
  The control answer is final text, not Codex token streaming.
- STT deliberately uses REST (`use_realtime=False`), avoiding an assumed eight-hour
  OpenAI realtime session. Each utterance/request can fail independently. The SDK
  provides bounded speech-provider retries; error/close events are logged honestly.
  No bespoke infinite reconnect or background rearm loop is installed. A terminal
  room/session disconnect requires app/token/dispatch reconnection; SDK transient
  media reconnect is independent of control operations. Leaving the room closes
  the voice session normally; this is not an idle hangup. The worker does not
  delete the room. Typed chat remains independent.
- **Silence does not mean zero traffic or zero cost.** LiveKit microphone media,
  connection keepalives and plugin prewarm traffic can remain; VAD can misclassify
  ambient audio. Default room recording is disabled, but this is not a guarantee
  against provider/platform telemetry or transcript logging. Speech is AI-generated;
  disclose that in the app. Keep logs and credentials private.

## Durable operations and interruption

Each finalized SDK user message gets a client idempotency ID. Re-entering the node
with the same message during this agent job reuses the ID; a distinct spoken turn
gets a new ID even with identical words. Only its text, ID, conversationId and
`source:"voice"` are POSTed, never the entire local conversation history.

A 202 acknowledgement supplies the operation ID (not assumed equal to the client
ID). Poll queued/running until succeeded, failed or uncertain. Only a validated
`succeeded` output `{text,messageId}` is spoken as the result. Failures, uncertainty,
malformed responses and deadlines produce short truthful fallback speech, not a
claim of completion or a raw provider/backend error.

Canceling a voice consumer cancels GET polling/playback, **not POST reconciliation**:
submission is shielded in a retained, deadline-bounded task. Lost acknowledgement,
transport errors, 5xx, malformed 202 or retryable HTTP errors retry the **identical
payload and ID**, up to the same deadline. An initial definitive 4xx rejection
(except 408/429) or redirect is not retried. A rejection after an ambiguous earlier
POST remains "unknown", not proof the initial request was rejected. Exhaustion is
"unknown submission". Accepted polling timeout is "stopped waiting", not failure.
GET retry/reconnect never resubmits an accepted operation; no DELETE/cancel endpoint
is used. Normal shutdown waits for submission reconciliation before closing HTTP.

Control is the durable source of truth. The voice worker has **no local disk outbox**:
if killed during an unknown POST, it cannot reconstruct a retry on restart. It does
not automatically recreate/replay that turn. IDs/acceptance are logged for diagnosis;
check persisted app history/operations before repeating an uncertain action. The
same user words spoken after reconnect are a new operation, not a safe retry.
Results from interrupted/expired voice polling remain in the app but are not
spontaneously spoken. Long Codex turns can therefore complete silently.

## Optional worker announcements: intentionally omitted

Persisted main `message.created` worker notifications remain available to the app.
This worker does not subscribe to event history or speak proactive updates. Doing
that safely needs a bootstrap cursor that cannot replay history, durable-message
deduplication, an explicit bounded queue/drop policy, and arbitration against user
speech and regular answers. Those semantics are not invented here. This also avoids
startup replay, double-speaking assistant replies and speaking over the user.

## Offline validation

```sh
cd apps/voice
uv sync --locked
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv build
uv run orchestrator-voice --help
```

Tests block socket connections and Silero model loading, clear speech/LiveKit keys,
and use httpx MockTransport. They cover contract payloads/URL prefixes, idempotent
retries, unknown/rejected/terminal outcomes, bounded hung requests, cancellations,
malformed results, dispatch/config validation, no idle/speculative generation,
real SDK custom-node scheduling without any provider, real speech constructors,
`gpt-transcribe` REST request compatibility, and synthetic streamed TTS frames
arriving **before** the HTTP response finishes. No network, model downloads,
microphone or paid calls are used by the suite. Dependency installation/build may
need package-network access. Mypy is strict on application code; tests permit
unannotated fixture functions but still check their bodies. Actual audio quality,
account model access, provider outage recovery and an eight-hour soak remain untested.

## Official documentation checked against the lock

Checked current official pages and installed Python 1.5.12 source, not old
VoicePipelineAgent examples:

- [Pipeline nodes](https://docs.livekit.io/agents/logic/nodes.md): custom
  `llm_node(chat_ctx, tools, model_settings)`; installed SDK allows yielded strings.
- [Turn detection/interruption](https://docs.livekit.io/agents/logic/turns.md):
  VAD-only mode and current `turn_handling` nested options. Installed defaults enable
  preemptive generation, so it is explicitly disabled here.
- [Sessions](https://docs.livekit.io/agents/logic/sessions.md): away timeout and
  participant-disconnect behavior, separate from silence.
- [OpenAI STT](https://docs.livekit.io/agents/models/stt/plugins/openai.md): Python
  default `gpt-4o-mini-transcribe`, distinct from current Node realtime defaults.
- [OpenAI TTS](https://docs.livekit.io/agents/models/tts/plugins/openai.md):
  `gpt-4o-mini-tts`; installed `SSEChunkedStream` and default TTS StreamAdapter checked.
- OpenAI [gpt-4o-mini-transcribe](https://developers.openai.com/api/docs/models/gpt-4o-mini-transcribe)
  and [gpt-transcribe](https://developers.openai.com/api/docs/models/gpt-transcribe):
  both officially documented. `OPENAI_STT_MODEL=gpt-transcribe` is an override, not
  the default. The pinned Python plugin accepts arbitrary model strings and sends
  REST multipart WAV with `response_format=json`; the offline test confirms the
  override request shape. This does **not** prove account access or a live model
  response, and must not be confused with Node's realtime transcription option.
