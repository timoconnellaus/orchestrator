#!/usr/bin/env python3
"""Explicit billable check using a <=15s synthetic WAV, never a microphone.

Run from apps/voice:
  uv run --frozen --env-file ../../.env python ../../scripts/check-realtime-stt.py \
    --wav ../../.data/realtime-question.wav --expect sessions

Uses the production adapter and real Silero VAD. Never contacts control/Codex.
"""

import argparse
import asyncio
import base64
import json
import time
import wave
from typing import Any

import aiohttp
from livekit import rtc
from livekit.agents import stt
from livekit.plugins import silero
from orchestrator_voice.live_transcribe import LiveTranscribeSTT


async def check(path: str, expected: str) -> None:
    with wave.open(path, "rb") as wav:
        rate = wav.getframerate()
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise ValueError("Use a mono PCM16 WAV")
        if not 0 < wav.getnframes() / rate <= 15:
            raise ValueError("Use a synthetic clip no longer than 15 seconds")
        audio = wav.readframes(wav.getnframes())
    uploads: list[tuple[float, int]] = []
    commits: list[float] = []
    clip_start = 0.0
    clip_end = 0.0
    first_delta: float | None = None
    before_commit = False
    finals: list[str] = []
    failures: list[str] = []
    session_updates = 0

    class MeteredSocket(aiohttp.ClientWebSocketResponse):
        async def send_json(self, data: Any, **kwargs: Any) -> None:
            nonlocal session_updates
            if data.get("type") == "session.update":
                session_updates += 1
            if data.get("type") == "input_audio_buffer.append":
                uploads.append((time.monotonic(), len(base64.b64decode(data["audio"]))))
            elif data.get("type") == "input_audio_buffer.commit":
                commits.append(time.monotonic())
            await super().send_json(data, **kwargs)

    detector = silero.VAD.load()
    async with aiohttp.ClientSession(ws_response_class=MeteredSocket) as http:
        engine = LiveTranscribeSTT(vad=detector, http_session=http)
        engine.on("error", lambda event: failures.append(str(event.error)))
        stream = engine.stream()

        async def play(data: bytes) -> None:
            start = time.monotonic()
            chunk_size = int(rate * 0.02) * 2
            for offset in range(0, len(data), chunk_size):
                await asyncio.sleep(
                    max(0, start + offset / (rate * 2) - time.monotonic())
                )
                chunk = data[offset : offset + chunk_size]
                stream.push_frame(
                    rtc.AudioFrame(
                        data=chunk,
                        sample_rate=rate,
                        num_channels=1,
                        samples_per_channel=len(chunk) // 2,
                    )
                )
            await asyncio.sleep(
                max(0, start + len(data) / (rate * 2) - time.monotonic())
            )

        async def produce() -> None:
            nonlocal clip_start, clip_end
            await play(bytes(rate * 2 * 3))
            assert not uploads, "Silence was uploaded before speech"
            clip_start = time.monotonic()
            await play(audio)
            clip_end = time.monotonic()
            await play(bytes(rate * 2 * 3))
            stream.end_input()

        async def consume() -> None:
            nonlocal first_delta, before_commit
            async for event in stream:
                if (
                    event.type == stt.SpeechEventType.INTERIM_TRANSCRIPT
                    and event.alternatives[0].text
                ):
                    if first_delta is None:
                        first_delta = time.monotonic()
                    before_commit = before_commit or not commits
                if event.type == stt.SpeechEventType.FINAL_TRANSCRIPT:
                    finals.append(event.alternatives[0].text)

        try:
            async with asyncio.timeout(45), asyncio.TaskGroup() as tasks:
                tasks.create_task(produce())
                tasks.create_task(consume())
            quiet_uploads = sum(1 for at, _ in uploads if at > clip_end + 1)
            transcript = " ".join(finals)
            report = {
                "model": engine.model,
                "session_updates": session_updates,
                "capture_resets": getattr(stream, "_generation", None),
                "speech_failures": failures,
                "first_delta_ms_from_clip_start": round(
                    (first_delta - clip_start) * 1000
                )
                if first_delta is not None
                else None,
                "delta_before_commit": before_commit,
                "delta_before_clip_end": first_delta is not None
                and first_delta < clip_end,
                "uploaded_audio_seconds": round(
                    sum(size for _, size in uploads) / 48000, 3
                ),
                "late_silence_appends": quiet_uploads,
                "commits": len(commits),
                "final_characters": len(transcript),
                "expected_text_found": expected.lower() in transcript.lower(),
            }
            print(json.dumps(report), flush=True)
            assert not failures, "Speech failed during the synthetic check"
            assert before_commit, "No pre-commit live caption observed"
            assert first_delta is not None and first_delta < clip_end, (
                "Caption arrived after input audio ended"
            )
            assert quiet_uploads == 0, (
                "Silence continued uploading after local endpointing"
            )
            assert expected.lower() in transcript.lower(), (
                "Expected synthetic text missing"
            )
        finally:
            await stream.aclose()
            await engine.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--wav", required=True, help="Synthetic mono PCM16 WAV, at most 15 seconds"
    )
    parser.add_argument("--expect", required=True, help="Expected word or phrase")
    args = parser.parse_args()
    asyncio.run(check(args.wav, args.expect))
