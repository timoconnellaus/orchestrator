"""Opt-in, billable, short TTS-only check through the production reply pipeline.

Run from apps/voice: uv run --frozen --env-file ../../.env python ../../scripts/check-tts.py
Uses the selected provider/voice. No microphone, STT, Codex, or coding-worker calls.
Audio is measured in memory, not played or saved. Never retries partial output.
"""

import asyncio
import json
import logging
import math
import time
from collections.abc import AsyncGenerator

from livekit.agents import APIConnectOptions
from livekit.agents.metrics import TTSMetrics
from livekit.agents.utils import http_context
from orchestrator_voice.config import Settings
from orchestrator_voice.reply_audio import stream_reply_audio
from orchestrator_voice.speech import make_tts


async def main() -> None:
    # Do not let third-party exception logging expose raw provider payloads.
    logging.disable(logging.CRITICAL)
    settings = Settings.from_env()
    started = time.perf_counter()
    first_audio_ms = None
    samples = peak = squares = 0

    async def text() -> AsyncGenerator[str, None]:
        yield "Got it.\n\n"
        yield "Your coding assistant is ready."

    async with http_context.open():
        provider = make_tts(settings)
        sentence_metrics: list[dict[str, int | bool]] = []

        @provider.on("metrics_collected")
        def on_metrics(metric: TTSMetrics) -> None:
            sentence_metrics.append(
                {
                    "characters": metric.characters_count,
                    "ttfb_ms": round(metric.ttfb * 1000),
                    "duration_ms": round(metric.duration * 1000),
                    "cancelled": metric.cancelled,
                }
            )

        audio = stream_reply_audio(provider, text(), APIConnectOptions(max_retry=0))
        try:
            async with asyncio.timeout(35):
                async for frame in audio:
                    if first_audio_ms is None:
                        first_audio_ms = round((time.perf_counter() - started) * 1000)
                    values = frame.data
                    samples += len(values)
                    peak = max(peak, max((abs(v) for v in values), default=0))
                    squares += sum(v * v for v in values)
                    if samples > provider.sample_rate * provider.num_channels * 30:
                        raise RuntimeError("Unexpectedly long synthetic audio")
            rms = math.sqrt(squares / samples) if samples else 0
            if peak < 100 or rms < 10:
                raise RuntimeError("No meaningful synthetic audio received")
            print(
                json.dumps(
                    {
                        "provider": settings.tts_provider,
                        "first_audio_frame_ms": first_audio_ms,
                        "total_ms": round((time.perf_counter() - started) * 1000),
                        "sentence_metrics": sentence_metrics,
                        "audio_seconds": round(
                            samples / provider.sample_rate / provider.num_channels, 3
                        ),
                        "peak": peak,
                        "rms": round(rms, 1),
                        "phone_latency_measured": False,
                    }
                )
            )
        except Exception as error:  # noqa: BLE001 - Sanitize provider failures at the CLI boundary.
            status = getattr(error, "status_code", None)
            print(
                json.dumps(
                    {
                        "provider": settings.tts_provider,
                        "error_type": type(error).__name__,
                        "http_status": status if isinstance(status, int) else None,
                        "audio_samples_received": samples,
                    }
                )
            )
            raise SystemExit(1) from None
        finally:
            await audio.aclose()
            await provider.aclose()


if __name__ == "__main__":
    asyncio.run(main())
