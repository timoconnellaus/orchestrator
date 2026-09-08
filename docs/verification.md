# Verification record

## Executed checks

| Check | Observed result |
| --- | --- |
| Combined scripts/check.sh gate | PASS: 202 tests total (56 control, 19 worker tools, 93 voice, 34 Flutter), plus configured type/lint/analyzer checks |
| Worker tools TypeScript + tests | 19 tests pass, including deferred Pi flag loading and real MCP stdio EOF cancellation |
| Control TypeScript + deterministic tests + build | 56 tests pass; build passes, including queued-context cutoff, native-session moves, pushed reply safety, and stripping both speech-provider keys from the Codex child environment |
| Voice locked installation, Ruff, mypy, pytest | All pass; 93 tests in the final combined run. Coverage includes speech gating, capture generations, isolated utterance failures, timeouts, pushed replies, no TTS replay after partial audio, iterator cleanup, and pinned-SDK caption/acknowledgment forwarding |
| Local LiveKit 1.13.6 transport | Two real local RTC participants exchanged reliable data and non-silent synthetic audio; no speech provider calls |
| Flutter analyze/tests/APK in integrated checkout | Clean analysis, 34 tests pass, debug APK builds; live captions are tested without modifying drafts, outbox, or durable chat. The caption APK was installed on the authorized Pixel with adb reporting Success; includes no-stretch edge behavior and dragging selectable text in long main/worker histories. Earlier no-stretch APK installed on emulator-5554; an actual drag moved a message 262 px without changing its width/height. Dependencies warn about future Kotlin Gradle plugin compatibility |
| Live Codex answer + read-only routing tool | PASS: real authenticated exact answer through HTTP; duplicate submission kept one operation; persisted thread resumed after app-server restart; exactly one real list_sessions callback. Worker mutations denied in code; no existing workers changed |
| Emulator install/UI/reconnect | Historical emulator-only run: settings saved http://10.0.2.2:8790, main chat round trip, mock worker creation and worker follow-up/reply verified through the UI; app force-stop/relaunch preserved URL/history, replayed a reply created while offline exactly once, and kept voice disarmed |
| Real worker provisioning/delivery | Not run; existing Herdr workers were not mutated. Approved project root /Users/tim/repos is now saved in private local configuration |
| Paid OpenAI transcription/TTS | PASS: both gpt-4o-mini-transcribe and gpt-4o-mini-tts accessible. Repeat generated 122,444-byte synthetic WAV and transcribed exactly "Orchestrator speech check." Initial short-phrase assertion failed without a saved transcript, so its cause remains unknown; this verifies access, not an accuracy benchmark. No microphone recording used |
| Authorized Pixel 9a basic voice | Previous APK installed with permission; user confirmed voice worked, corroborated by successful source-voice operations and assistant replies in the journal. Private control/media use Tailscale, control port 8792. Caption update installed after explicit approval; control/voice services restarted with zero active control requests and healthy endpoints. Private STT configuration now selects gpt-live-transcribe. The updated app was observed Live and mic armed; actual caption accuracy/interruption still need user confirmation |
| Gated gpt-live-transcribe synthetic check | PASS: actual local Silero plus production STT adapter and OpenAI; first interim 2,861 ms into a 4,588 ms clip, before commit; 5.652 audio seconds uploaded, one commit, no initial-idle uploads, no late-silence appends, no capture resets/errors; expected synthetic text present. No control/Codex endpoint or microphone used |
| Deployed Codex credential separation | Earlier deployed Codex subprocess inspected privately: OpenAI speech key absent. Latest code has regressions excluding both OpenAI and ElevenLabs speech keys. No process environment or key was printed |
| Eight-hour Android screen-off/Bluetooth soak | Not run or separately authorized; Pixel installation/basic voice authorization is not endurance evidence |

## Pushed reply update — deployed, phone verification pending

Commit `7a6f053` was deployed after explicit restart approval. Phone capture was
stopped first; the journal had zero queued/running control operations before
control was restarted. Control and voice HTTP health checks pass. The live
`/v1/operations/:id/reply` endpoint returned a canonical terminal snapshot for an
existing completed operation without creating a new request. The existing Pixel
APK was reopened and observed `Live`, `Voice off`, `Join voice`; no reinstall or
worker shutdown was needed. Codex initializes lazily on its first request.

The implementation worker timed out after a successful full gate; its final
cleanup passed focused checks, and the parent reran the full gate successfully
(197 tests). Independent control and voice reviews then identified three defects,
all reproduced and fixed with regressions. The final combined gate passes 202
tests with configured type/lint/build checks:

- Expired queued speech streams retained capacity. Expiry now frees slots, while
  acceptance-age checks prevent revival; rejected generation admission remains
  final-only if capacity later becomes available.
- SDK sentence retries could repeat partial audio: an in-memory provider failure
  reproduced four synthesis requests under production defaults. This path now
  makes one request and propagates the error, without mutating shared options.
- Closing the outer TTS node at a yielded frame did not await inner cleanup.
  Explicit iterator ownership fixes this; the regression retains the inner
  generator so garbage collection cannot hide the problem.

The default short receipt acknowledgment is emitted only after durable acceptance
and a 750 ms delay, suppressed when answer text arrives first. A pinned-SDK,
in-memory audio test confirms synthesis before the final control result. Native
commentary remains muted; no new speech tool or second reasoning model was added.
A separate real-Codex probe passed using a fresh isolated thread and the existing
ChatGPT login: 49 text chunks for a 254-character answer; first chunk (2 characters)
at 4,796 ms including startup, versus turn completion at 7,225 ms. The streamed
prefix matched the canonical answer; all tools were denied and zero were called.
No speech provider or existing worker session was involved. This verifies real
Codex early-phase support, not phone playback timing. Phone response latency and
audible interruption of this update remain unverified. ElevenLabs has a private
key slot but is not selected as the voice provider.

## Realtime scope and limitations

The transcription-only model emits words while audio arrives; Codex remains the
only reasoner. The adapter opens a fresh provider session per detected utterance,
so first-caption latency includes connection setup. A separate no-audio handshake
measured 655 ms to open and 1,103 ms to configure; the full 2,861 ms measurement
above is the relevant end-to-end synthetic caption timing.

The first real gated attempt failed with zero uploaded audio and insufficient
failure diagnostics. Real local VAD and the full pipeline with a fake provider
were then verified independently. A startup-buffer/deadline mismatch was reproduced
and fixed; the subsequent real-provider run passed. The exact first failure cause
was not captured. These checks do not establish noisy-microphone accuracy or
Pixel caption latency. Actual Pixel mute/interruption/reconnect verification and
the approved disposable scratch worker test remain pending.

Utterances are bounded to 120 seconds; setup/finalization/backlog limits fail
closed rather than executing a truncated tail. Only **control-accepted** work is
durable; a provider commit or displayed caption alone is not control acceptance.

## Review and recovery

The first adapter review found Pi flag initialization ordering and MCP stdin EOF cleanup defects. Both were fixed and regression-tested. The separate integration review found future queued requests leaking into earlier reasoning context, pane moves stranding worker identity, and lost POST acknowledgements regressing authoritative mobile operation state. These were fixed with regression coverage. Unexpected Android microphone-service destruction now also requests graceful Dart/LiveKit disarm; notification-only process termination remains restricted to the explicit Stop & close app action.

Live testing additionally found malformed quoted MCP override paths, unset config options being converted into invalid TOML strings, and the tool host mistakenly disabled along with code execution. The adapter now preserves literal MCP names and valid transport fields, omits null optional fields, and enables the routing tool host while keeping code execution, shell, web, and inherited MCP services disabled. These cases have regressions and the real read-only Codex smoke passes.

The implementation workflow's report aggregation failed on an undefined output field, and the control/mobile writers subsequently reached their 30-minute runtime limits during final checks. Their source files were recovered from isolated worktrees without overwriting existing work; verification has passed in the main checkout. The source worktrees remain preserved as recovery references.

## Important distinctions

Mock worker results are simulated. Herdr status observation and successful model replies do not prove live worker provisioning or delivery. Local RTC audio does not establish emulator/phone ICE routing, microphone quality, cloud speech correctness, or eight-hour endurance. Those require separate evidence.
