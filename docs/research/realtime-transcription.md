# Research: OpenAI transcription-only realtime STT for LiveKit Agents 1.5.12

## Summary
Use `openai.STT(model="gpt-4o-mini-transcribe", use_realtime=True)` as an incremental, transcription-only upgrade; retain Codex app-server reasoning and `gpt-4o-mini-tts`. The installed plugin exposes interim/final transcripts, but **does not gate audio on speech**, and mini-transcribe's incremental output must not be confused with guaranteed captions while someone is still speaking. For lowest connection latency and speech-only upstream audio, prefer an open transcription WebSocket plus local VAD/pre-roll and explicit, serialized commit handling in a dedicated STT adapter; this is more than a configuration change.

Research used only public documentation and installed package source. Both `livekit/agents/version.py` and `livekit/plugins/openai/version.py` report **1.5.12**. No API inference calls, secrets, application source changes, or repository edits were made.

## Findings

1. **Current API: transcription session, not realtime reasoning.** The installed `_connect_ws` connects to `wss://api.openai.com/v1/realtime?intent=transcription`, authenticates server-side with a Bearer key, and sends the GA-shaped `session.update` below. It does not instantiate a realtime LLM or call `response.create`. Thus Codex and TTS need not change. Prefer this nested shape over older examples using `transcription_session.update` and top-level `input_audio_format`. [OpenAI transcription guide](https://developers.openai.com/api/docs/guides/realtime-transcription); local `livekit/plugins/openai/stt.py`, `STT._connect_ws`.

   ```json
   {
     "type": "session.update",
     "session": {
       "type": "transcription",
       "audio": {
         "input": {
           "format": {"type": "audio/pcm", "rate": 24000},
           "transcription": {"model": "gpt-4o-mini-transcribe", "language": "en"},
           "turn_detection": {"type": "server_vad", "threshold": 0.5, "prefix_padding_ms": 600, "silence_duration_ms": 350}
         }
       }
     }
   }
   ```

   Audio is mono PCM16 at 24 kHz in this plugin, sent as base64 `input_audio_buffer.append` messages in 50 ms chunks. For a custom manually endpointed adapter, set `audio.input.turn_detection: null` and send `input_audio_buffer.commit` at the end of each turn. Wait for session configuration acknowledgement before treating a custom connection as ready. No model response is needed for transcription.

2. **Interim events exist; during-speech latency is not established for mini-transcribe.** OpenAI emits `conversation.item.input_audio_transcription.delta` with `delta`, `item_id`, and `content_index`, and `.completed` with final `transcript`. Match items by ID; completion order between turns is not guaranteed. The installed plugin sets `streaming=True` and `interim_results=True` in realtime mode, accumulates deltas into `INTERIM_TRANSCRIPT`, and emits `FINAL_TRANSCRIPT` at completion. It throttles interim emission to a **0.5-second interval**, checked only when another delta arrives (not a scheduled guaranteed update). [OpenAI transcription guide](https://developers.openai.com/api/docs/guides/realtime-transcription); local `stt.py`, `_delta_transcript_interval` and `SpeechStream._run.recv_task`.

   **High priority limitation:** the current OpenAI cost guide says input transcription happens when buffered audio is committed, manually or by VAD. The current transcription guide explicitly distinguishes true as-audio-arrives `gpt-live-transcribe` from commit-triggered transcription, but no longer explains mini's pre-commit behavior. Therefore advertise mini's deltas as incremental recognition output, **not a promise of word-by-word captions before end-of-speech**. Measure first-delta relative to `input_audio_buffer.committed`. Switching to WebSocket can still eliminate the batch adapter's wait-then-upload workflow and reuse the connection. [OpenAI costs](https://developers.openai.com/api/docs/guides/realtime-costs).

3. **High: built-in VAD is not audio gating.** In local `stt.py`, `send_task` appends **every incoming frame** irrespective of speech; optional `vad_task` only sends commits on local `END_OF_SPEECH`. `Agent.default.stt_node` likewise forwards every frame for streaming STT; it uses `stt.StreamAdapter` plus session VAD only when the STT is non-streaming. Consequently adding `AgentSession(vad=...)` or plugin `vad=...` does not mean audio stops flowing to OpenAI during silence. Local paths: `livekit/plugins/openai/stt.py`, `SpeechStream._run.send_task/vad_task`; `livekit/agents/voice/agent.py`, `Agent.default.stt_node` (approximately lines 416–470).

   **High: do not hard-gate audio while relying on server VAD.** If you stop forwarding immediately on local end-of-speech, server VAD may never receive enough trailing silence to commit. Either (a) forward genuine trailing silence beyond the server's configured threshold, with margin, then close the gate, or (b) disable server VAD and explicitly commit. Server prefix padding cannot recover microphone samples that the client never sent.

   **High: no supported mini-specific gate/commit flag.** The plugin's `flush()` handling only drains its 50 ms byte buffer; it does **not** send a commit. `turn_detection=None` is outside the public parameter type, even though the current implementation may serialize it as JSON null. Passing a plugin VAD without disabling server VAD creates two independent commit authorities. Its send and VAD tasks also send independently, so source does not establish that the last pending audio bytes precede the commit. Avoid private WebSocket access, dual VAD commits, or treating `flush()` as endpointing. Use a tested custom STT adapter for deterministic manual commits.

4. **Recommended speech gate design: local VAD continuously, remote STT only on admitted audio.** Keep microphone/LiveKit reception active and run local Silero; maintain a bounded ring buffer of roughly **300–600 ms pre-roll** (tuning suggestion, not an API requirement). On speech-start, append pre-roll exactly once and then live frames. At speech-end, retain a short tail, drain pending bytes, and send one commit on the same serialized outbound queue. Continue receiving events while idle. Track per-item text and usage; reconcile final text rather than appending it to the interim. Handle short/noise-only detections, failed transcriptions, reconnects, and speech resumption during endpointing. [OpenAI transcription guide](https://developers.openai.com/api/docs/guides/realtime-transcription).

   Installed Silero defaults are `min_speech_duration=0.05`, `min_silence_duration=0.55`, `prefix_padding_duration=0.5`; its inference runs at 8/16 kHz, distinct from the OpenAI 24 kHz transport. These values add their own onset/endpoint delay. Gate only the STT branch, **not the audio needed for LiveKit interruption/turn detection**. Local `livekit/plugins/silero/vad.py`, `VAD.load` (lines 60–105); `livekit/agents/voice/agent.py`, `stt_node` extension point. Server-side gating prevents idle audio reaching OpenAI, not reaching LiveKit; device-side gating is a separate Flutter/media design and needs pre-roll too.

5. **Billing: idle socket is not the same as streamed silence.** OpenAI explicitly states there is currently **no charge for connections or network bandwidth**. Thus an open socket with no appended audio and no work is not a separately billed STT wall-clock minute under the documented token model. Infrastructure/LiveKit charges remain independent. Input transcription uses the transcription model's own rate card, not realtime reasoning/audio-generation rates. [OpenAI costs](https://developers.openai.com/api/docs/guides/realtime-costs).

   However, do **not** claim every sent silence frame is charged, or that all silence is free. The cost guide says VAD filters empty conversational input, but its transcription section says transcription occurs on committed buffers; it does not precisely specify mini-transcribe's silence/padding accounting in every case. Manually committing long silence or noise can invoke ASR and produce usage. Local gating is the clearest way to avoid transmitting idle audio and reduce accidental work. Capture `.completed.usage` and inspect billed usage in a later authorized experiment; the installed plugin emits aggregate `RECOGNITION_USAGE` but does not preserve all token-detail fields. Its audio-duration estimate derives from server speech timestamps and can be zero in manual-commit mode, so it is not an invoice meter.

   | Model | Official price | Implication |
   |---|---|---|
   | `gpt-4o-mini-transcribe` | $1.25 / 1M audio input tokens; $5 / 1M output tokens; estimated **$0.003/audio minute** | About $0.18 for 60 audio minutes at the estimate; not a socket-minute fee or exact fixed tariff. |
   | `gpt-4o-transcribe` | $2.50 / 1M input tokens; $10 / 1M output tokens; estimated **$0.006/minute** | More expensive transcription alternative. |
   | `gpt-4o-mini-tts` | **$0.60 / 1M text input tokens**, **$12 / 1M audio output tokens** | Separate unchanged synthesis bill; do not use STT minutes to estimate it. |
   | Current `gpt-live-transcribe` / `gpt-realtime-whisper` | **$0.017/audio minute** | Documented live-streaming alternatives, roughly 5.7× mini's estimated minute price; availability/integration must be verified. |

   [OpenAI pricing](https://developers.openai.com/api/docs/pricing), [mini-transcribe model](https://developers.openai.com/api/docs/models/gpt-4o-mini-transcribe), [mini-TTS model](https://developers.openai.com/api/docs/models/gpt-4o-mini-tts). Prompt/text-input token accounting needs separate verification; the fetched mini model/pricing extracts do not clearly enumerate its text-input price. The commonly quoted TTS $0.015/minute estimate was not confirmed in the retrieved primary pricing content, so rely on the token prices above.

6. **Warm idle connection wins latency; connect-on-speech mainly saves resources/privacy exposure, not socket charges.**

   | Strategy | Latency and risk | Recommendation |
   |---|---|---|
   | Open socket, continuously send mic | No repeated handshake; simplest stock-plugin route; remote receives silence/noise | First controlled latency baseline, not speech-only mode. |
   | Open socket, local speech gate | No speech-time handshake; requires pre-roll, endpoint/commit control and reconnect recovery | Preferred target when latency is primary. |
   | Connect on local speech | Adds DNS/TCP/TLS/WebSocket/session setup; pre-roll alone is insufficient unless all audio during connection is buffered too | Consider after long inactivity or for explicit privacy/resource policy. |

   There is no measured handshake latency from this research, and no documented connection-fee saving from reconnecting every utterance. Bound queued audio during reconnect, avoid duplicate replays, and retain the stream until pending finals arrive. A hybrid can close after extended inactivity and reconnect on user interaction before speech.

7. **Medium: connection duration and ordering need defensive handling.** Local `stt.py` has a comment saying 15-minute timeout, sets `_max_session_duration=10*60`, and triggers active-session rotation on a **completed transcript** after ten minutes. A long idle active stream does not reach that completion-triggered check. Current OpenAI general conversation docs instead say **60 minutes maximum**; that page describes `type:"realtime"`, not an explicit mini-transcription-only idle guarantee. Treat the installed 15-minute comment as potentially stale, not current authoritative policy. No transcription-specific inactivity timeout was confirmed. Reconnect on closure and rotate at safe utterance boundaries; never rely on an indefinitely live idle socket. [Realtime conversation lifecycle](https://developers.openai.com/api/docs/guides/realtime-conversations).

   **Medium:** plugin receive code uses one `current_text` accumulator despite OpenAI's cross-turn ordering warning. Rapid overlapping completions/deltas may mix interim text; a custom adapter should hold text per `(item_id, content_index)`. Final transcript events themselves use their own item IDs. Handle `.failed` transcription events explicitly in a custom implementation; the inspected stock receive loop has no dedicated branch for them.

   Mini's model page lists a **16,000-token context**, **2,000 maximum output tokens**, no free-tier support, and tier-dependent limits (Tier 1: 500 RPM / 50,000 TPM). This is not a promise of unlimited continuous utterance length. Do not confuse batch upload-size limits with realtime session duration. Empty/tiny manual commits need testing; a precise minimum commit duration was not verified from the successfully fetched primary references. [Mini model limits](https://developers.openai.com/api/docs/models/gpt-4o-mini-transcribe).

8. **Flutter interim captions can use LiveKit's existing transcription channel.** Subscribe to `lk.transcription`, correlate with `lk.segment_id` plus participant identity, read `lk.transcription_final`, and **replace** cumulative user interim text rather than append it. Final replaces the same segment. Do not implement captions only from finalized conversation messages. The current Flutter `TranscriptionStreamReceiver` already handles user full-text updates versus agent text chunks and uses segment IDs for stable UI keys; verify it exists in the app's pinned Flutter SDK. [LiveKit text/transcriptions](https://docs.livekit.io/agents/multimodality/text/), [Flutter receiver](https://docs.livekit.io/reference/client-sdk-flutter/livekit_client/TranscriptionStreamReceiver-class.html).

   Installed `livekit/agents/voice/room_io/_output.py`, `_ParticipantStreamTranscriptionOutput` (approximately lines 350–525), publishes interim and final attributes and stable segment IDs; `_ParticipantTranscriptionOutput` also forwards legacy transcriptions. Prefer the modern stream path and avoid consuming both paths into duplicate UI rows. For long-lived streams, consume chunks as they arrive rather than waiting for stream closure. Actual end-to-end app forwarding and Flutter SDK version were not inspected.

## Concrete rollout recommendation

1. **Smallest change:** enable mini STT realtime mode only; retain existing Codex reasoning, TTS, and LiveKit turn/interruption logic. Preserve the plugin's default server VAD initially (350 ms silence, 600 ms prefix). This is a latency experiment, not yet a claim of during-speech captions or gated audio.
2. Enable Flutter interim rendering via `lk.transcription`. Measure speech start → first delta → first visible caption, speech end → server commit → final transcript → Codex start → first TTS audio. Compare against the existing batch mode; no latency numbers are claimed here.
3. If speech-only upstream audio is required, implement an STT-branch adapter with warm socket, local pre-roll, one endpoint authority, serialized appends/commit, and per-item transcript state. Test offline with recorded frames and fake WebSocket events before any authorized paid trial. A simpler server-VAD gate may forward enough real trailing silence instead of committing manually, but must be tested for endpoint completion.
4. If true captions during long speech are required and mini only emits after commit, evaluate a genuine live-transcription model separately. Current LiveKit source recognizes `gpt-realtime-whisper` and provides local-VAD commits for it, but still does not gate frames. Do not blindly switch to current OpenAI `gpt-live-transcribe`: its guide uses different transcription options (e.g. `languages` rather than `language`) and installed plugin support was not established. Neither option requires replacing Codex.

## Sources

- Kept: [OpenAI realtime transcription guide](https://developers.openai.com/api/docs/guides/realtime-transcription) — current session shape, append/commit, events, ordering and live-versus-committed distinction.
- Kept: [OpenAI managing costs](https://developers.openai.com/api/docs/guides/realtime-costs) — explicit no-connection-charge statement and separate transcription billing/usage.
- Kept: [OpenAI pricing](https://developers.openai.com/api/docs/pricing), [mini-transcribe](https://developers.openai.com/api/docs/models/gpt-4o-mini-transcribe), [mini-TTS](https://developers.openai.com/api/docs/models/gpt-4o-mini-tts) — prices and model limits.
- Kept: [OpenAI realtime conversations](https://developers.openai.com/api/docs/guides/realtime-conversations) — general 60-minute session statement, with transcription-specific applicability caveat.
- Kept: [LiveKit STT guide](https://docs.livekit.io/agents/models/stt/openai/), [LiveKit text guide](https://docs.livekit.io/agents/multimodality/text/), [Flutter receiver API](https://docs.livekit.io/reference/client-sdk-flutter/livekit_client/TranscriptionStreamReceiver-class.html) — official integration and caption semantics.
- Kept: installed official SDK files under `/Users/tim/repos/orchestrator/apps/voice/.venv/lib/python3.11/site-packages/`: `livekit/plugins/openai/stt.py` (entire file), `livekit/agents/voice/agent.py` (default STT node), `livekit/agents/voice/room_io/_output.py` (transcription output), `livekit/plugins/silero/vad.py` (VAD defaults), and both `version.py` files — version-specific evidence takes precedence over rolling docs.
- Dropped: community pricing threads — non-primary and unnecessary where model pages provide rates.
- Dropped: generic Node plugin defaults as evidence for Python — different SDK behavior; Python 1.5.12 was inspected directly.
- Unavailable: `openai.com/api/pricing/` (403), exact commit-reference fetch (403/response too large). Used the successfully fetched official developer pricing and guide instead.

## Gaps / residual risks

No paid benchmark or billing experiment was run. Mini's pre-commit delta timing, exact silence/padding accounting, tiny-commit minimum, transcription-specific idle/session timeout, and current newer-model account availability remain unverified. Documentation is rolling and now focuses on newer transcription models; preserve the version-specific findings and validate mini rather than transferring newer-model guarantees. Repository app wiring and Flutter version were outside the inspected source scope. No existing staging state was queried or modified.


## Integration decision

The parent verified the account lists `gpt-live-transcribe` and fetched the current official transcription guide directly. The guide explicitly supports pre-commit deltas, `languages: ["en"]`, `delay: "low"`, and `turn_detection: null` for this model. Use that dedicated live model, rather than the brief's conservative mini-model rollout, to meet the user's while-speaking caption requirement. A custom STT adapter will isolate serialized appends/commits, local VAD/pre-roll, per-item text, and reconnect behavior. No realtime reasoning model is introduced.

## Bounded live protocol evidence

A synthetic mono PCM16/24 kHz question was paced over a transcription-only
WebSocket using `gpt-live-transcribe`, `languages: ["en"]`, `delay: "low"`, and
`turn_detection: null`. The first delta arrived **918 ms after the first append,
before explicit commit**. The final transcript correctly contained the complete
synthetic question. No control endpoint, reasoning model, or worker mutation was
involved. This proves pre-commit streaming for this account and model, not phone
microphone latency, accuracy in noise, or eight-hour endurance.

The standalone local gate passed five offline tests plus Ruff and mypy: idle
inference emits no upload action; start pre-roll is uploaded once; END's repeated
utterance audio is not re-uploaded; disconnect discards the unfinished utterance
until a new speech start; tiny buffers are cleared instead of carried forward.
These are event-level tests, not an acoustic or network/endurance benchmark.

`gpt-live-transcribe` is now the source default. An explicitly configured
`gpt-4o-mini-transcribe` remains a cheaper **buffered** fallback, with no guarantee
of while-speaking captions. Existing private environment settings and the live
phone service are not changed merely by updating source defaults.

## Lifecycle simplification after advisory review

The implementation now opens one realtime transcription connection per detected
utterance, rather than retaining a warm multi-turn provider session. Pre-roll
preserves the opening audio while the connection is established. Each connection
has one explicit commit, bounded setup/finalization deadlines, and no retry of its
audio. Later completed utterances remain ordered independently when an earlier
utterance fails. Long idle conversations therefore do not depend on keeping a
single provider session alive past its lifetime limit.

Explicit microphone mute/unmute/subscription events reset capture generations
and the actual local VAD stream. Frame/event queues have age/size guards; overflow
discards a whole unfinished turn and requires a silence boundary, rather than
accepting a truncated tail. Cancellation uses an empty **interim** event: the
pinned SDK ignores empty finals before caption forwarding. These lifecycle
changes require fresh offline and live adapter checks; the 918 ms result above
remains evidence for the raw provider protocol only.
