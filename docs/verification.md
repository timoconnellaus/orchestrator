# Verification record

## Executed checks

| Check | Observed result |
| --- | --- |
| Combined scripts/check.sh gate | PASS: 220 tests total (56 control, 19 worker tools, 111 voice, 34 Flutter), plus configured type/lint/analyzer checks |
| Worker tools TypeScript + tests | 19 tests pass, including deferred Pi flag loading and real MCP stdio EOF cancellation |
| Control TypeScript + deterministic tests + build | 56 tests pass; build passes, including queued-context cutoff, native-session moves, pushed reply safety, and stripping both speech-provider keys from the Codex child environment |
| Voice locked installation, Ruff, mypy, pytest | All pass; 111 tests in the final combined run. Coverage includes speech gating, capture generations, isolated utterance failures, timeouts, pushed replies, no TTS replay after partial audio, iterator cleanup, and pinned-SDK caption/acknowledgment forwarding |
| Local LiveKit 1.13.6 transport | Two real local RTC participants exchanged reliable data and non-silent synthetic audio; no speech provider calls |
| Flutter analyze/tests/APK in integrated checkout | Clean analysis, 34 tests pass, debug APK builds; live captions are tested without modifying drafts, outbox, or durable chat. The caption APK was installed on the authorized Pixel with adb reporting Success; includes no-stretch edge behavior and dragging selectable text in long main/worker histories. Earlier no-stretch APK installed on emulator-5554; an actual drag moved a message 262 px without changing its width/height. Dependencies warn about future Kotlin Gradle plugin compatibility |
| Live Codex answer + read-only routing tool | PASS: real authenticated exact answer through HTTP; duplicate submission kept one operation; persisted thread resumed after app-server restart; exactly one real list_sessions callback. Worker mutations denied in code; no existing workers changed |
| Emulator install/UI/reconnect | Historical emulator-only run: settings saved <http://10.0.2.2:8790>, main chat round trip, mock worker creation and worker follow-up/reply verified through the UI; app force-stop/relaunch preserved URL/history, replayed a reply created while offline exactly once, and kept voice disarmed |
| Real worker provisioning/delivery | Not run; existing Herdr workers were not mutated. Approved project root /Users/tim/repos is now saved in private local configuration |
| Paid OpenAI transcription/TTS | PASS: both gpt-4o-mini-transcribe and gpt-4o-mini-tts accessible. Repeat generated 122,444-byte synthetic WAV and transcribed exactly "Orchestrator speech check." Initial short-phrase assertion failed without a saved transcript, so its cause remains unknown; this verifies access, not an accuracy benchmark. No microphone recording used |
| Authorized Pixel 9a basic voice | Previous APK installed with permission; user confirmed voice worked, corroborated by successful source-voice operations and assistant replies in the journal. Private control/media use Tailscale, control port 8792. Caption update installed after explicit approval; control/voice services restarted with zero active control requests and healthy endpoints. Private STT configuration now selects gpt-live-transcribe. The updated app was observed Live and mic armed; actual caption accuracy/interruption still need user confirmation |
| Gated gpt-live-transcribe synthetic check | PASS: actual local Silero plus production STT adapter and OpenAI; first interim 2,861 ms into a 4,588 ms clip, before commit; 5.652 audio seconds uploaded, one commit, no initial-idle uploads, no late-silence appends, no capture resets/errors; expected synthetic text present. No control/Codex endpoint or microphone used |
| Deployed Codex credential separation | Earlier deployed Codex subprocess inspected privately: OpenAI speech key absent. Latest code has regressions excluding both OpenAI and ElevenLabs speech keys. No process environment or key was printed |
| Eight-hour Android screen-off/Bluetooth soak | Not run or separately authorized; Pixel installation/basic voice authorization is not endurance evidence |

## Runtime launch timeouts

The original media, control and voice background launches each had an explicit
36000-second timeout. These expired independently of the voice restart bug.
Media was restarted and its silent agent-dispatch check passed. Control and voice
were subsequently restarted without that explicit timeout. On control recovery,
the journal contained 22 succeeded operations and no unfinished operations;
no coding workers were restarted and no mutation was blindly retried.
The subsequent silent voice probe passed: the agent joined and reached listening
without publishing audio. Actual Pixel stop/start verification remains pending.
These remain session-launched services, not reboot/crash-supervised system services.
This recovery is not an eight-hour endurance result.

## Voice stop/start regression — deployed, server rejoin checks pass

After initial voice success, the user reported no detection after stopping and
starting voice. Logs showed the first speech session closed at 11:32:17 UTC;
two subsequent phone joins reused its room without creating a fresh voice job.
The old agent continued advertising `listening` despite its closed AgentSession.
Thus HTTP health, active-job count, and a stale agent attribute were insufficient.

A silent same-room reproduction passed its first join and failed its second.
Changing only to fresh rooms allowed two joins, but the third failed: closed jobs
still held both admission slots. The production token regression was red on room
reuse, and both session-close regressions were red on missing job shutdown.

Each explicit token request now allocates `orchestrator-main-<uuid>` while keeping
conversation metadata `main`. Session close unbinds capture and calls
`ctx.shutdown`; bridge shutdown still reconciles submissions without cancelling
accepted operations. The full gate passed 220 tests. Control and voice were
restarted only after confirming no rooms and zero queued/running operations;
coding workers and LiveKit media were left running.

`scripts/check-voice-rejoin.py` was red against the old deployed token endpoint,
before creating any room. The initial post-deployment check failed on a too-short
3-second cleanup assumption: the Python client's disconnect completed before the
server processed its leave, followed by the SDK's session-close grace. The check
now observes departure with a 10-second limit, still below the old orphan-room
lifetime, instead of assuming a fixed delay.

All three production-token join/leave cycles then passed with fresh listening
agents and closed-agent departure (cleanup 4866, 4865 and 4862 ms). The probe never
publishes audio or touches existing/legacy rooms. It waits for departure between
cycles; this is not proof of arbitrarily rapid toggles, actual microphone capture,
or endurance. The original Pixel stop/start symptom still awaits user confirmation.

## ElevenLabs selected voice — deployed, phone audition confirmed

The user selected a voice ID for ElevenLabs. Voice-metadata lookup returned HTTP
401 `missing_permissions`, but the actual speech endpoint succeeded using that ID;
no broader key permissions were requested. The pinned plugin is
`livekit-plugins-elevenlabs==1.5.12`, with `eleven_flash_v2_5` and PCM 24 kHz output.

The first bounded synthetic check returned 2.259 seconds of non-silent audio
(peak 32768, RMS 8566.4). Its first audio frame took **5564 ms**. This is a backend
probe, not phone latency, voice-quality evidence, or a speed-improvement claim.
A second bounded check returned its first audio frame in **497 ms** and completed
in **904 ms**: sentence TTFBs were **495 ms** (7 characters) and **274 ms**
(31 characters), producing 2.445 seconds of audio. The initial delay was not
reproduced; its exact cause was not captured. These samples do not establish phone
latency, sustained performance, or comparative voice quality.

Production selection uses sentence-scoped HTTP audio through the existing reply
adapter, not native shared WebSocket contexts. Offline checks cover short receipt
flushes, no retry after partial audio, and overlapping replies whose response
cleanup must remain independent. The full offline gate passed 218 tests, plus
configured type/lint/analyzer checks. After confirming no active voice rooms, the
private TTS selection was switched and only the voice worker restarted. The new
owned Python processes were verified to have the selected ElevenLabs provider,
voice ID and Flash model. The private `.env` remains mode 0600 with both keys
preserved; a runtime check also confirmed both speech keys are absent from the
owned Codex process. STT, control and coding workers were not changed/restarted.
The post-restart silent probe passed: `agent_joined: true`,
`agent_listening: true`, `audio_published: false`; its temporary room was cleaned up.
After being asked to rejoin on the Pixel, audition the voice and interrupt its
answer, the user confirmed: “Yep it works.” The phone audition is accepted.
No quantitative latency, separate mute/reconnect test, or endurance result is
established by that acknowledgment.

## Phone admission failure — assignment fix verified

The user's first post-update attempt joined LiveKit at 08:06:30 UTC and published
a microphone track, but the room job was rejected with `no servers available`.
The idle voice worker had marked itself unavailable at 08:06:26 with system load
0.928 against the default 0.7 threshold. No agent joined and no voice operation
reached control; HTTP health had not tested this admission path.

A pinned-SDK regression reproduced rejection of an idle worker at simulated 95%
Mac CPU. Admission now uses active voice-job count (two maximum), one prewarmed
idle process, and the SDK's reserved-slot/draining checks. Four regressions pass;
the combined gate passes 206 tests. Only the idle voice worker was restarted,
after confirming no active rooms; control/coding workers were not restarted.
`scripts/check-voice-dispatch.py` passed against the restarted worker:
`agent_joined: true`, `agent_listening: true`, `audio_published: false`. It created
and cleaned up only its temporary probe room. This verifies actual agent dispatch
and session readiness, not merely HTTP health. The user then retried on the Pixel
and confirmed “ok that's working,” establishing the basic voice request/reply path.
Quantified phone latency, captions, interruption, mute/reconnect, and endurance
were not separately confirmed by that acknowledgment.

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

## Voice tuning source update — offline verification only

In the isolated implementation worktree, `sh scripts/check.sh` passes **249 tests**
(59 control, 19 worker tools, 126 voice, 45 Flutter), with TypeScript builds,
Ruff, mypy, Flutter analysis and script syntax checks clean. `flutter build apk
--debug` also passes. Existing dependency warnings concern future Kotlin Gradle
plugin compatibility; dependencies were not upgraded. These results apply only
to this source update, not the historical deployment checks above.

The voice tuning slice adds cross-language defaults/invalid-case fixtures, token
and dispatch forwarding tests, persisted settings/save-failure tests, compact
320-pixel Material3 panel tests, all eight supported capture-option combinations
(including full-reconnect room defaults), frozen-join and old-server mismatch
coverage. Python exercises the actual pinned Silero option-update seam without
loading ONNX, and the existing in-memory STT transport verifies guarded received
RMS and real capture resets. Diagnostics tests cover coalescing while transport
blocks, owned sender shutdown, stale/reset state, strict sender/room/schema/
sequence/generation/nonce validation, fallback availability, and bounded pending
join acknowledgement expiry. Existing offline network guards are unchanged.

This update has **not been deployed, installed on a phone, microphone-tested, or
verified against production**. No speech/model API, adb, capture, or existing
worker operations were run for it. Hardware processing effectiveness, acoustic
threshold/preset quality, phone/Mac clock agreement, real LiveKit diagnostic
routing, Android reconnect behavior and audible interruption still need separate
user-approved physical-device checks. A debug APK build or offline test success
is not evidence of any of these properties. Integrate compatible control, voice,
and mobile sources together; deploy/restart only as a separate parent-owned step.
