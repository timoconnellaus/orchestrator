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

Fresh local configuration defaults to loopback. For a phone, set a reachable `LIVEKIT_NODE_IP` and verify actual WebRTC audio, not just signaling. The current private Pixel setup uses Tailscale for both control and media.

### Live transcription

The source default is **`gpt-live-transcribe`**, using a dedicated transcription-only session. Local Silero VAD uploads speech plus short pre-roll/end padding; idle audio is not continuously sent to OpenAI. A fresh realtime connection opens for each detected utterance and closes after its final transcript; no provider connection or audio upload is needed during idle periods. The phone-to-Mac microphone stream remains active until muted. Codex remains the only reasoner.

The main chat shows a transient **“Hearing… · not yet sent”** caption. It clears on finalization, mute or disconnect and never changes the typed draft, submits chat, or creates saved messages. Only finalized end-of-turn text enters durable control. `OPENAI_STT_MODEL=gpt-4o-mini-transcribe` retains the cheaper buffered fallback, without while-speaking captions.

OpenAI currently lists live transcription at **$0.017/audio minute** (about $1.02/hour of submitted audio), with no charge for an idle connection itself; TTS is separate. See [pricing](https://developers.openai.com/api/docs/pricing) and the [protocol research and bounded evidence](docs/research/realtime-transcription.md). These are not eight-hour endurance or microphone-accuracy guarantees. Source changes are not proof that an existing running service has been updated.

## Tailscale deployment

- Set `ORCHESTRATOR_HOST` and `LIVEKIT_NODE_IP` to the Mac's Tailscale IP.
- Set public `LIVEKIT_URL` to `ws://<mac-tailscale-ip>:7880`. `LIVEKIT_INTERNAL_URL` stays `ws://127.0.0.1:7880` for the local voice worker.
- Set the app backend to `http://<mac-tailscale-ip>:8787`.
- Restrict tailnet access to the phone and Mac. Needed ports: TCP 8787 (control), TCP 7880 (signaling), TCP 7881 (ICE fallback), UDP 7882 (media). Test actual ICE connectivity; signaling success alone does not prove audio works.
- Do not publish these listeners with Funnel, a public reverse proxy or router port forwarding. Tailscale supplies device access control and transport encryption; the app has no account/login layer.
- LiveKit join tokens are issued automatically. Worker reply credentials are scoped to a managed session and kept in owner-only files; local same-user coding agents are not a sandbox from each other.
- Keep the Mac awake while it hosts active tasks. Automatic startup/login services are not installed by these scripts.

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
