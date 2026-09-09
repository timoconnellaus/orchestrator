# Orchestrator for Android

Private, no-login Flutter client for [`docs/contract.md`](../../docs/contract.md). Dark indigo/slate Material 3 UI with cyan state accents, Chat / Sessions / Settings navigation, readable selectable messages, worker threads and Pi/Codex/Claude creation.

## Build and check

Flutter used: `/Users/tim/repos/flutter/bin/flutter` (3.47 pre-release toolchain). From this directory:

```sh
/Users/tim/repos/flutter/bin/flutter pub get
/Users/tim/repos/flutter/bin/flutter analyze
/Users/tim/repos/flutter/bin/flutter test
/Users/tim/repos/flutter/bin/flutter build apk --debug
```

APK: `build/app/outputs/flutter-apk/app-debug.apk`. Application ID: `dev.tim.orchestrator.orchestrator`. Android only; generated release configuration currently uses the debug signing key and is **not a production signing setup**.

No device installation is part of these checks. Parent owns installation and runtime acceptance, exclusively on `emulator-5554`; never select a physical device or change global device settings. No persistent development server is needed. Tests use mock HTTP, event streams, local persistence and voice/service adapters, never a microphone or network.

## Control connection and durable text

- Settings persists a validated HTTP(S) origin. Emulator default: `http://10.0.2.2:8787`. Use the control server's reachable Tailscale address for private deployment. No provider keys are stored on the phone. Cleartext HTTP is intentionally permitted for the private control API; do not expose this unauthenticated endpoint publicly.
- `data/control_api.dart` implements direct contract responses, structured errors, 15-second HTTP deadlines, SSE parsing (comments, CRLF, multiline data), 15-second stream-connect and 45-second idle deadlines. Requests own a client that is closed on completion/timeout. SSE reconnects after four seconds, with manual reconnect also available.
- `data/app_store.dart` saves each generated UUID and exact mutation payload **before sending**. Transport failure leaves an explicit **Retry same request** card. Reconnect never resubmits mutations. A server `failed` or `uncertain` operation is visible and is not blindly repeated. Accepted is not presented as completed.
- A single JSON preference value per backend stores messages, sessions, operations, outbox and event cursor together; writes are serialized. The pinned Android legacy `shared_preferences` implementation uses native `commit()` and returns its success. Persistence errors prevent initial transmission. Backup is disabled. This is an MVP materialized cache, not a database: it grows with journal history and has no compaction yet.
- Initialization first merges recent HTTP history, then subscribes to SSE from the **previously saved cursor**, never a made-up current cursor. Replay fills the snapshot/subscription gap. Message IDs and event sequence numbers deduplicate overlap. Operations received before their POST acknowledgment are retained and matched later. The cursor is never saved independently of its materialized data.
- Worker threads merge their own history with journal events. Changing server separates its saved state and requests from other backends, and disconnects voice. Previously accepted operation status is reconciled by GET on reconnect as well as SSE.
- An in-flight request interrupted by reconnect or app restart becomes explicitly retryable with the original ID. Unsent text in a composer has not yet been committed: tap Send to durably save it. Uninstallation, storage corruption or clearing app data cannot preserve local-only requests.

## Voice and Android lifecycle — important limitations

`voice/voice_controller.dart` uses the **official `livekit_client` Flutter SDK**. A user taps Join voice, the visible Activity requests microphone/notification permission and starts a native microphone foreground service, then the app requests `POST /v1/voice/token` with `{conversationId:'main'}` and joins the returned room. Remote subscribed audio is played by LiveKit and speaker output is preferred. Token failure (including missing server voice configuration) disarms and clearly explains that typed chat still works.

- Mic capture is explicitly armed. Mute uses `stopAudioCaptureOnMute: true`, then stops the microphone service while keeping the room available for speaker playback. Arm mic requires another visible Activity action. Disconnect releases tracks and leaves the room gracefully; it does not close the app.
- While connected, `MainActivity.kt` owns an **Orchestrator Voice** `MediaSession` (`mediaSession` method channel call). It stays active across mute, reporting `STATE_PAUSED` when muted and `STATE_PLAYING` when armed, so launchers and system media controls show the conversation. Its Play/Pause callbacks map to unmute/mute and Stop disconnects voice. The session is owned by the Activity and released with the Flutter engine, and no MediaStyle notification carries its token, so a backgrounded muted app may lose the session if the process is reclaimed; Play still needs a visible Activity to re-arm the microphone.
- `MainActivity.kt` rejects background arming. `MicrophoneService.kt` uses `FOREGROUND_SERVICE`, `FOREGROUND_SERVICE_MICROPHONE`, a microphone service type, `RECORD_AUDIO`, `POST_NOTIFICATIONS`, and an ongoing notification. Notification permission is required here so the user has the stop control in the drawer. There is no boot receiver, sticky restart, alarm or reconnect path that arms recording.
- **Approved MVP fallback:** the notification action is explicitly **Stop & close app**. Native code synchronously commits disarmed intent, asks Dart for graceful disconnect, and after 750 ms terminates **only this app's own process** to guarantee native microphone release if Flutter is suspended/unresponsive. It closes the app even if graceful cleanup completed. This fallback is isolated in `stopAndCloseApp()` and is used only for the deliberate notification action, never for a network error, permission rejection, lifecycle callback or general timeout. The stopped state is never restored as armed. A future native track-ownership implementation could avoid closing the UI.
- Android owns the service, but actual media tracks still belong to Flutter/LiveKit/WebRTC, **not** an independent native LiveKit room. No widget callback is needed for notification STOP. Screen-off ordinarily leaves the Activity's engine and service alive; Activity/engine destruction tears down the WebRTC plugin and service. Swiping the task away uses `stopWithTask`. OS process eviction, Doze, permission revocation, network conditions and device/OEM policies can interrupt audio. No eight-hour guarantee or automatic background re-arm is claimed. Muted speaker playback has no separate media-playback foreground service and is also best effort in background.
- Media SDK reconnection preserves an existing explicit arm/mute state; no reconnect handler calls microphone enable. Explicit stop invalidates a pending join, so late connection completion cannot re-arm. Process restart always begins voice-off.
- Silence is not a guarantee of zero audio traffic. There is no ambient speech intent classifier in this app.

### Parent-owned emulator lifecycle acceptance

These checks require real Android execution and were **not** replaced by mock tests:

1. With no backend, send text; inspect saved retry, relaunch and retry the same request ID when backend is available.
2. Grant permissions after tapping Join voice; verify the microphone-typed service and ongoing notification. Deny permission separately and verify text still works.
3. With a configured reachable LiveKit server/voice agent, verify speaker playback and mic capture; mute should release capture and remove the service, while explicit re-arm starts it again from the visible Activity.
4. Turn the emulator screen off while armed, then use notification **Stop & close app**. Confirm this app process disappears, microphone access ceases, and reopening remains disarmed.
5. Interrupt media/network and stop during a pending join. Restore connectivity: no implicit re-arm after mute/disconnect/notification STOP.
6. Exercise task removal/process eviction and permission revocation. Listening may end; it must not restart in background.
7. While connected, confirm the **Orchestrator Voice** session appears in the launcher/system media controls, that it remains listed after muting, and that its Pause, Play and Stop controls mute, unmute and disconnect.

## Design evidence and tooling

Doop provisioning/status tools were unavailable in the worker's tool allowlist. No canvas was created or bound (especially not an unrelated pi-setup canvas). UI was implemented code-native; there are **no claimed Doop renders, screenshots, or emulator visual evidence**. `bg_run` was also unavailable; validation used finite bash commands and one logged background APK build. No persistent dev server was started.
