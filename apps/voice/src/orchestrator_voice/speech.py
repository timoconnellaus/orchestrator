"""Provider selection with sentence-owned, non-resumable ElevenLabs audio."""

from typing import Any, Never

from livekit.agents import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions, tts
from livekit.plugins import elevenlabs, openai

from .config import Settings


class ElevenSentenceTTS(elevenlabs.TTS):
    """Use streamed HTTP per sentence, not the SDK's shared WebSocket contexts.

    In SDK 1.5.12, cancelling a WebSocket SynthesizeStream does not itself close
    its provider context. HTTP synthesis instead owns a response context manager;
    our existing StreamAdapter closes that request on cancellation and disables
    retries. It also flushes short receipt acknowledgments before the final reply.
    """

    def __init__(self, *, model: str, voice_id: str) -> None:
        super().__init__(model=model, voice_id=voice_id, encoding="pcm_24000", sync_alignment=False)
        # streaming=False describes incremental TEXT input, not AUDIO output.
        self.capabilities.streaming = False

    def stream(self, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS) -> Never:
        raise NotImplementedError("ElevenLabs replies use sentence-scoped HTTP synthesis")


def make_tts(settings: Settings) -> tts.TTS[Any]:
    if settings.tts_provider == "elevenlabs":
        return ElevenSentenceTTS(
            model=settings.elevenlabs_model, voice_id=settings.elevenlabs_voice_id
        )
    return openai.TTS(model=settings.tts_model, voice=settings.tts_voice)
