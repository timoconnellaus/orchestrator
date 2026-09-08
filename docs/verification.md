# Verification record

## Executed checks

| Check | Observed result |
| --- | --- |
| Combined scripts/check.sh gate | PASS: 141 tests total (35 control, 19 worker tools, 57 voice, 30 Flutter), plus configured type/lint/analyzer checks |
| Worker tools TypeScript + tests | 19 tests pass, including deferred Pi flag loading and real MCP stdio EOF cancellation |
| Control TypeScript + deterministic tests + build | 35 tests pass; build passes, including queued-context cutoff and native-session moves |
| Voice locked installation, Ruff, mypy, pytest | All pass; 57 tests pass in integrated checkout |
| Local LiveKit 1.13.6 transport | Two real local RTC participants exchanged reliable data and non-silent synthetic audio; no speech provider calls |
| Flutter analyze/tests/APK in integrated checkout | Clean analysis, 30 tests pass, debug APK builds; includes no-stretch edge behavior and dragging selectable text in long main/worker histories. Fixed APK installed on emulator-5554; an actual drag moved a message 262 px without changing its width/height. Dependencies warn about future Kotlin Gradle plugin compatibility |
| Live Codex answer + read-only routing tool | PASS: real authenticated exact answer through HTTP; duplicate submission kept one operation; persisted thread resumed after app-server restart; exactly one real list_sessions callback. Worker mutations denied in code; no existing workers changed |
| Emulator install/UI/reconnect | Installed only on emulator-5554; settings saved http://10.0.2.2:8790, main chat round trip, mock worker creation and worker follow-up/reply verified through the UI; app force-stop/relaunch preserved URL/history, replayed a reply created while offline exactly once, and kept voice disarmed |
| Real worker provisioning/delivery | Not run; existing Herdr workers were not mutated. Approved project root /Users/tim/repos is now saved in private local configuration |
| Paid OpenAI transcription/TTS | PASS: both gpt-4o-mini-transcribe and gpt-4o-mini-tts accessible. Repeat generated 122,444-byte synthetic WAV and transcribed exactly "Orchestrator speech check." Initial short-phrase assertion failed without a saved transcript, so its cause remains unknown; this verifies access, not an accuracy benchmark. No microphone recording used |
| Eight-hour Android screen-off/Bluetooth soak | Not run: physical-device testing not authorized yet |

## Review and recovery

The first adapter review found Pi flag initialization ordering and MCP stdin EOF cleanup defects. Both were fixed and regression-tested. The separate integration review found future queued requests leaking into earlier reasoning context, pane moves stranding worker identity, and lost POST acknowledgements regressing authoritative mobile operation state. These were fixed with regression coverage. Unexpected Android microphone-service destruction now also requests graceful Dart/LiveKit disarm; notification-only process termination remains restricted to the explicit Stop & close app action.

Live testing additionally found malformed quoted MCP override paths, unset config options being converted into invalid TOML strings, and the tool host mistakenly disabled along with code execution. The adapter now preserves literal MCP names and valid transport fields, omits null optional fields, and enables the routing tool host while keeping code execution, shell, web, and inherited MCP services disabled. These cases have regressions and the real read-only Codex smoke passes.

The implementation workflow's report aggregation failed on an undefined output field, and the control/mobile writers subsequently reached their 30-minute runtime limits during final checks. Their source files were recovered from isolated worktrees without overwriting existing work; verification has passed in the main checkout. The source worktrees remain preserved as recovery references.

## Important distinctions

Mock worker results are simulated. Herdr status observation and successful model replies do not prove live worker provisioning or delivery. Local RTC audio does not establish emulator/phone ICE routing, microphone quality, cloud speech correctness, or eight-hour endurance. Those require separate evidence.
