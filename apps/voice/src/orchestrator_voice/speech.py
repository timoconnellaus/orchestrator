"""Provider selection with sentence-owned, non-resumable ElevenLabs audio."""

import math
from dataclasses import dataclass
from typing import Any, Never, cast

from livekit.agents import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions, tts
from livekit.plugins import elevenlabs, openai

from .config import Settings


@dataclass(frozen=True)
class _SpeedOverride:
    # The first-party API allows partial voice settings. LiveKit 1.5.12's
    # annotation requires two unrelated fields, but its serializer accepts a
    # dataclass and sends only present fields. Keep this compatibility seam
    # narrow and covered by actual SDK HTTP-payload tests.
    speed: float


class ElevenSentenceTTS(elevenlabs.TTS):
    """Use streamed HTTP per sentence, not the SDK's shared WebSocket contexts.

    In SDK 1.5.12, cancelling a WebSocket SynthesizeStream does not itself close
    its provider context. HTTP synthesis instead owns a response context manager;
    our existing StreamAdapter closes that request on cancellation and disables
    retries. It also flushes short receipt acknowledgments before the final reply.
    """

    def __init__(self, *, model: str, voice_id: str, speed: float = 1.0) -> None:
        if type(speed) not in (int, float) or not math.isfinite(speed) or not 0.8 <= speed <= 1.2:
            raise ValueError("ElevenLabs speed must be between 0.8 and 1.2")
        options: dict[str, Any] = {}
        if speed != 1.0:
            options["voice_settings"] = cast(elevenlabs.VoiceSettings, _SpeedOverride(speed))
        # At 1.0 keep the pre-existing request exactly: no voice-settings override.
        super().__init__(
            model=model, voice_id=voice_id, encoding="pcm_24000", sync_alignment=False, **options
        )
        # streaming=False describes incremental TEXT input, not AUDIO output.
        self.capabilities.streaming = False

    def stream(self, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS) -> Never:
        raise NotImplementedError("ElevenLabs replies use sentence-scoped HTTP synthesis")


def make_tts(settings: Settings, *, elevenlabs_speed: float = 1.0) -> tts.TTS[Any]:
    if settings.tts_provider == "elevenlabs":
        return ElevenSentenceTTS(
            model=settings.elevenlabs_model,
            voice_id=settings.elevenlabs_voice_id,
            speed=elevenlabs_speed,
        )
    return openai.TTS(model=settings.tts_model, voice=settings.tts_voice)
