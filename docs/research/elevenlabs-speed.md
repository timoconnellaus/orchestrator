# ElevenLabs speaking-speed override

The pinned LiveKit ElevenLabs 1.5.12 `VoiceSettings.speed` supports 0.8–1.2,
with 1.0 used as this app's unchanged-behavior default. This is speaking rate,
not time-to-first-audio. OpenAI TTS ignores this ElevenLabs-specific setting.

The [first-party Python API model](https://github.com/elevenlabs/elevenlabs-python/blob/main/src/elevenlabs/types/voice_settings.py)
defines speed, stability, similarity boost, style and speaker boost as optional
fields, each defaulting to `None`. The [stream endpoint](https://elevenlabs.io/docs/api-reference/text-to-speech/stream)
describes voice settings as request-local overrides of stored voice settings.
It does not explicitly specify server-side handling of every omitted subfield.

Our default 1.0 sends no voice-settings override, preserving the existing request.
For another speed, we send only `{"speed": value}`: we do not guess or explicitly
change stability/similarity/style, fetch protected voice metadata, or modify the
stored voice. LiveKit 1.5.12's Python type requires unrelated fields even though
its HTTP serializer uses `dataclasses.asdict`; the narrow `_SpeedOverride` adapter
bridges that typing mismatch. Tests execute the actual pinned HTTP serializer and
assert the exact payload, including absent unrelated overrides. Recheck this seam
when upgrading the plugin. No new paid/provider call was used to validate this change.
