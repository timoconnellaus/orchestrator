# Orchestrator

Personal Android voice and chat for coordinating Pi, Codex, and Claude Code sessions in Herdr. No login screen; use your own Tailscale devices.

## Architecture

- **Flutter Android:** main chat, worker threads, session status, new sessions, hands-free microphone and playback.
- **LiveKit + Python voice worker:** speech-to-text, turn detection, interruption and TTS. The voice worker forwards finalized utterances to control; it does not run a second reasoning LLM.
- **TypeScript control:** durable SQLite messages/operations, reconnect event replay, Codex app-server reasoning and Herdr delivery.
- **Worker tools:** per-session MCP for Codex/Claude Code and a Pi extension. Replies are explicit; terminal text is not silently scraped into the conversation.

The phone connection, media room, Codex thread, and coding tasks have independent lifetimes. Losing audio must not cancel a task, lose a result, or blindly retry a session launch.

## Requirements

- macOS with Node >=22.13, Codex CLI (tested protocol target 0.153.4), Herdr (target 0.8.2/protocol 20), `uv`, Flutter and Android SDK.
- LiveKit server for voice (`brew install livekit`; initial target 1.13.6).
- Existing `codex login` authentication for orchestration. Check `codex login status`.
- **An OpenAI API key for speech**, billed separately from a ChatGPT/Codex subscription. Text does not require this key.

App-server remains experimental: upgrades need adapter regression tests against the new generated protocol.

## First setup

```sh
sh scripts/bootstrap.sh
```

This installs module-local dependencies and creates `.env` with random local LiveKit credentials and mode 0600. Existing `.env` is preserved. No keys are printed and no global agent configuration is changed.

Edit `.env` locally:

- Set `ORCHESTRATOR_PROJECT_ROOTS` to colon-separated directories containing the projects you want workers to access. Empty means live creation is not permitted.
- Set `OPENAI_API_KEY` for voice. Keep it out of chat, source control and the phone.
- Keep loopback addresses while testing locally. See Tailscale deployment below before connecting a physical phone.

## Run text first

```sh
# Live Codex reasoning and real Herdr observation
node scripts/run.mjs control

# OR deterministic mock backend, separate database, no live coding workers
node scripts/run.mjs mock
```

Choose one mode on port 8787, not both. Mock responses and workers are explicitly simulated; they are not provider or Herdr validation.

In another terminal:

```sh
cd apps/mobile
flutter run -d emulator-5554
```

The emulator default backend is `http://10.0.2.2:8787`. Edit it in Settings if needed. This checkout's private live setup uses **8792** on the Mac's Tailscale interface; the earlier mock ports are retired. Installation on the connected **Pixel 9a** was explicitly authorized, and basic real-phone voice worked. Target devices explicitly; do not install on other connected devices.

## Enable voice

After setting the speech key, optionally check the configured models with a short, billable synthetic speech round trip (no microphone recording):

```sh
node --env-file=.env scripts/check-speech.mjs
```

Start the media services:

```sh
# Once, fetch the voice worker's local model assets
node scripts/run.mjs voice-download

# In separate terminals, alongside the live control module:
node scripts/run.mjs livekit
node scripts/run.mjs voice
```

Enable listening while the app is visible and grant microphone permission. The Android notification shows the armed microphone. Voice unavailable does not disable text chat.

## Run automatically with launchd

On the Mac that hosts Orchestrator, install the control, LiveKit, and voice processes as per-user LaunchAgents:

```sh
sh scripts/service.sh install
```

The installer preserves `.env`, installs only the server-side dependencies, downloads the voice worker's local model assets, and starts all three processes. It requires `OPENAI_API_KEY` and the generated LiveKit credentials in `.env`. It captures the current `PATH` in the plist files so `node`, `uv`, `livekit-server`, Codex, and Herdr remain discoverable outside an interactive shell.

LaunchAgents start automatically after this user logs in following a Mac restart. They intentionally do not run before login: the service uses the user's files, Codex authentication, and Herdr session. Each process is independently kept alive by launchd.

After finishing and validating a source update, refresh dependencies/assets and reload the services:

```sh
sh scripts/service.sh update
```

`update` does not run `git pull` or the test suite. It only prepares the current checkout and reloads the installed services. Other operations:

```sh
sh scripts/service.sh restart   # restart current code without reinstalling dependencies
sh scripts/service.sh status
sh scripts/service.sh logs
sh scripts/service.sh stop
sh scripts/service.sh start
sh scripts/service.sh uninstall # preserves .env, .data, and logs
```

Service output is stored under `.data/logs/`. Run the script again from an interactive shell if the repository path or command `PATH` changes, because both are recorded in `~/Library/LaunchAgents/dev.tim.orchestrator.*.plist`.

Fresh local configuration defaults to loopback. For a phone, set a reachable `LIVEKIT_NODE_IP` and verify actual WebRTC audio, not just signaling. The current private Pixel setup uses Tailscale for both control and media.

### Live transcription

The source default is **`gpt-live-transcribe`**, using a dedicated transcription-only session. Local Silero VAD uploads speech plus short pre-roll/end padding; idle audio is not continuously sent to OpenAI. A fresh realtime connection opens for each detected utterance and closes after its final transcript; no provider connection or audio upload is needed during idle periods. The phone-to-Mac microphone stream remains active until muted. Codex remains the only reasoner.

The main chat shows a transient **“Hearing… · not yet sent”** caption. It clears on finalization, mute or disconnect and never changes the typed draft, submits chat, or creates saved messages. Only finalized end-of-turn text enters durable control. `OPENAI_STT_MODEL=gpt-4o-mini-transcribe` retains the cheaper buffered fallback, without while-speaking captions.

OpenAI currently lists live transcription at **$0.017/audio minute** (about $1.02/hour of submitted audio), with no charge for an idle connection itself; TTS is separate. See [pricing](https://developers.openai.com/api/docs/pricing) and the [protocol research and bounded evidence](docs/research/realtime-transcription.md). These are not eight-hour endurance or microphone-accuracy guarantees. Source changes are not proof that an existing running service has been updated.

### Reply voice providers

`TTS_PROVIDER=openai` remains the source default. To use ElevenLabs, set
`TTS_PROVIDER=elevenlabs`, `ELEVEN_API_KEY`, and `ELEVEN_TTS_VOICE_ID` in the private
`.env`; `ELEVEN_TTS_MODEL` defaults to `eleven_flash_v2_5`. This changes only spoken
replies: OpenAI transcription and Codex-only reasoning remain unchanged. The key
needs speech-generation permission; voice-metadata lookup is not required when
a voice ID is supplied. ElevenLabs usage has separate billing.

ElevenLabs audio streams over HTTP as each sentence becomes available, using
24 kHz PCM and the existing explicit short-acknowledgment flush. It does not wait
for the whole answer. We intentionally do not use SDK 1.5.12's shared WebSocket
contexts: stream cancellation does not itself close those provider contexts.
Sentence-owned HTTP responses close on interruption, cannot cancel another reply's
response, and are not retried after partial audio. No automatic provider fallback
or replay occurs on errors. Sentence boundaries may affect prosody; voice quality
and phone latency need an actual listening check.

An opt-in **billable** check exercises the selected TTS provider without microphone,
STT, Codex, playback, or coding-worker operations:

```sh
cd apps/voice
uv run --frozen --env-file ../../.env python ../../scripts/check-tts.py
```

Switching or rolling back (`TTS_PROVIDER=openai`) requires a coordinated voice-worker
restart and phone rejoin. Adding a key alone does not change the provider.

### Voice-worker readiness

Every explicit Join voice request gets a fresh `orchestrator-main-<uuid>` room and
agent dispatch. The durable conversation remains `main`; media rooms are not
conversation storage. Closing the speech session also shuts down its voice job,
so a returning phone cannot inherit a dead session or leave closed jobs occupying
admission capacity. This does not cancel accepted control/coding operations.

The personal voice worker admits jobs by active voice-job count, not whole-Mac CPU
usage: at most two active room jobs (including a reconnect/closing room), one idle
prewarmed process, and the SDK's pending-assignment reservation checks. A busy Mac
can still delay audio processing, but unrelated CPU work no longer rejects an idle
voice worker. HTTP health alone does not prove that an agent joined the room.

An opt-in local check verifies real dispatch and the agent's `listening` state
without publishing microphone audio, sending chat, or invoking coding tools:

```sh
cd apps/voice
uv run --frozen --env-file ../../.env python ../../scripts/check-voice-dispatch.py
```

It creates and cleans up only a uniquely named probe room. Also run
`../../scripts/check-voice-rejoin.py` with the same `uv` invocation to test three
join/leave cycles through the production token endpoint. That check verifies
fresh listening agents and departure of closed agents, without publishing audio;
it refuses to join/delete the legacy shared room or a pre-existing room.
Actual phone audio and interruption still require a separate user-driven check.

## Tailscale deployment

- Set `ORCHESTRATOR_HOST` and `LIVEKIT_NODE_IP` to the Mac's Tailscale IP.
- Set public `LIVEKIT_URL` to `ws://<mac-tailscale-ip>:7880`. `LIVEKIT_INTERNAL_URL` stays `ws://127.0.0.1:7880` for the local voice worker.
- Set the app backend to `http://<mac-tailscale-ip>:8787`.
- Restrict tailnet access to the phone and Mac. Needed ports: TCP 8787 (control), TCP 7880 (signaling), TCP 7881 (ICE fallback), UDP 7882 (media). Test actual ICE connectivity; signaling success alone does not prove audio works.
- Do not publish these listeners with Funnel, a public reverse proxy or router port forwarding. Tailscale supplies device access control and transport encryption; the app has no account/login layer.
- LiveKit join tokens are issued automatically. Worker reply credentials are scoped to a managed session and kept in owner-only files; local same-user coding agents are not a sandbox from each other.
- Keep the Mac awake while it hosts active tasks. The optional launchd setup above starts services after login but does not prevent sleep.

## Worker setup

New managed workers receive per-process MCP/extension configuration; global Codex, Claude, and Pi settings are untouched. Existing sessions are observable but may be marked **limited** until a worker messaging adapter is explicitly provisioned. Blocked/unknown/busy sessions are not treated as safe terminal-input targets. See [worker adapter setup](packages/worker-tools/README.md).

## Tests

```sh
sh scripts/check.sh
cd apps/mobile && flutter build apk --debug
```

Default tests use fake provider/Herdr processes and temporary databases; they do not launch real worker sessions or make paid speech/model requests. Record explicit live smoke tests separately.

Optional real Codex check (uses your existing ChatGPT/Codex allowance):

```sh
(cd apps/control && npm run build)
node scripts/smoke-codex.mjs
```

This creates an isolated loopback control server and temporary SQLite database. It verifies an exact model answer, duplicate request acceptance, a real `list_sessions` callback, and thread resume after restarting app-server. Worker mutations are rejected by the harness, not merely discouraged in a prompt. It does not modify existing Herdr workers or global agent configuration. Results and remaining gaps are recorded in [verification](docs/verification.md).

## Known limits to verify

- Eight-hour screen-off battery, microphone and Bluetooth reliability require a physical-device soak test. Emulator tests cannot establish this.
- Continuous capture transmits ambient audio to the Mac. The live STT gate submits detected speech with short onset/end padding, including any false positives. VAD does not establish speaker identity or intent.
- Starting or restarting microphone capture from the Android background is restricted. The app must not silently re-arm after stop or process death.
- Native notification **Stop & close app** is a conservative MVP fallback that closes this app to guarantee microphone release. Ordinary in-app disarm is graceful.
- Barge-in stops speech consumption/playback, not already accepted coding work. Long-running results remain available in chat after interruption.
- Voice announcements for unsolicited worker replies, richer approvals, and automatic repair of uncertain operations are not assumed complete; inspect module READMEs and verification notes.

Implementation interfaces: [contract](docs/contract.md). Initial ownership: [build lanes](docs/build-lanes.md).

### Pushed voice replies (source update)

Voice no longer waits for a one-second completion poll. Eligible, explicitly
identified Codex final-answer text can flow to sentence-based TTS while Codex
finishes. Mutating turns and unphased legacy responses remain final-only; once
an early answer starts, further routing tools are refused. A slow request gets
one delayed "Got it." **after durable acceptance**, not a claim that coding is
complete. Fast answers suppress this receipt. Native commentary remains muted;
no additional speech tool is required for the acknowledgment.

Interrupted/lost streams never automatically replay speech or cancel accepted
work; the final answer remains in chat. See the [reply contract](docs/contract.md#transient-pushed-voice-replies).
`ELEVEN_API_KEY` is reserved for later TTS selection and excluded from Codex's
environment; adding it does not switch the running provider. This source update
requires a coordinated control/voice restart and separate Pixel latency/listening
verification; offline tests are not a phone performance or voice-quality result.

### Voice tuning (source update; not phone-verified)

Settings now includes a compact **Voice tuning** draft with detection sliders,
Default / Noisy room / Responsive presets, and supported phone AEC/NS/AGC requests.
**Save for next join** persists independently of chat history; reset/presets edit
only the draft until saved. Saving does not mute, disconnect, or retune a running
utterance. Disconnect and explicitly join again to use saved changes. Mute/unmute
and SDK reconnect keep the original join snapshot, including room capture defaults.

The panel separates saved values, current join requests, and Mac-confirmed tuning.
Phone switches are requests, not verified hardware state: Android's installed
WebRTC `getSettings()` reports hardcoded booleans, and device behavior may vary.
Disabling echo cancellation on speaker risks the assistant hearing itself.
Local end silence changes Silero only; transcription and fixed 0.8–3 second SDK
endpointing also affect latency. Mac mic/speech indicators are transient received
RMS/local detection, never command acceptance, and expire when stale. Buffered STT
shows telemetry unavailable rather than simulated levels. A compatible control
and voice worker are needed for confirmation; custom tuning is refused when an
old control server does not acknowledge it. See [wire contract](docs/contract.md#voice-tuning-and-ephemeral-diagnostics).
