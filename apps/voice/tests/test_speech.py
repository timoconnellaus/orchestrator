"""Actual pinned ElevenLabs HTTP synthesis through the production sentence adapter."""

import asyncio
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import Mock

import aiohttp
import pytest
from livekit.agents import APIConnectOptions, APIError
from livekit.plugins import silero

from orchestrator_voice.config import Settings
from orchestrator_voice.live_transcribe import LiveTranscribeSTT
from orchestrator_voice.reply_audio import stream_reply_audio
from orchestrator_voice.speech import ElevenSentenceTTS, make_tts
from orchestrator_voice.voice_tuning import VoiceTuning
from orchestrator_voice.worker import make_session


class SpeechResponse:
    content_type = "audio/pcm"

    def __init__(self, *, block: bool = False, fail: bool = False) -> None:
        self.content = self
        self.release = asyncio.Event()
        if not block:
            self.release.set()
        self.fail = fail
        self.closed = False

    async def __aenter__(self) -> "SpeechResponse":
        return self

    async def __aexit__(self, *_args: object) -> None:
        self.closed = True

    def raise_for_status(self) -> None:
        pass

    async def iter_chunks(self) -> AsyncGenerator[tuple[bytes, bool], None]:
        yield (b"\xb0\x04" * 24000, False)
        await self.release.wait()
        if self.fail:
            raise aiohttp.ClientPayloadError("Offline simulated partial response failure")


class SpeechSession:
    def __init__(self, *responses: SpeechResponse) -> None:
        self.responses = responses
        self.requests: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> SpeechResponse:
        index = len(self.requests)
        self.requests.append({"url": url, **kwargs})
        assert index < len(self.responses), "Unexpected retry or extra synthesis request"
        return self.responses[index]


def configured_provider(monkeypatch: pytest.MonkeyPatch, speed: float = 1.0) -> ElevenSentenceTTS:
    monkeypatch.setenv("ELEVEN_API_KEY", "offline-only")
    provider = make_tts(
        Settings(tts_provider="elevenlabs", elevenlabs_voice_id="testVoiceId123"),
        elevenlabs_speed=speed,
    )
    assert isinstance(provider, ElevenSentenceTTS)
    assert not provider.capabilities.streaming
    assert provider.sample_rate == 24000
    return provider


@pytest.mark.parametrize("speed", [0.8, 1.0, 1.2])
async def test_actual_sdk_http_payload_overrides_only_nondefault_speed(
    monkeypatch: pytest.MonkeyPatch,
    speed: float,
) -> None:
    provider = configured_provider(monkeypatch, speed)
    http = SpeechSession(SpeechResponse())
    monkeypatch.setattr(provider, "_ensure_session", lambda: http)

    async def text() -> AsyncGenerator[str, None]:
        yield "Speed test."

    audio = stream_reply_audio(provider, text(), APIConnectOptions())
    try:
        async with asyncio.timeout(3):
            async for _ in audio:
                pass
        assert len(http.requests) == 1
        assert http.requests[0]["json"]["voice_settings"] == (
            None if speed == 1.0 else {"speed": speed}
        )
    finally:
        await audio.aclose()
        await provider.aclose()


@pytest.mark.parametrize("speed", [0.79, 1.21, float("nan"), float("inf"), True])
def test_speed_rejected_before_synthesis(speed: float) -> None:
    with pytest.raises(ValueError, match="speed"):
        ElevenSentenceTTS(model="eleven_flash_v2_5", voice_id="testVoiceId123", speed=speed)


async def test_elevenlabs_ack_audio_arrives_before_final_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = configured_provider(monkeypatch)
    http = SpeechSession(SpeechResponse(), SpeechResponse())
    monkeypatch.setattr(provider, "_ensure_session", lambda: http)
    release_answer = asyncio.Event()

    async def text() -> AsyncGenerator[str, None]:
        yield "Got it.\n"
        yield "\n"  # The explicit flush may itself span Codex/bridge chunks.
        await release_answer.wait()
        yield "Here is your answer."

    audio = stream_reply_audio(provider, text(), APIConnectOptions())
    try:
        async with asyncio.timeout(3):
            frame = await anext(audio)
            assert any(frame.data)
            assert not release_answer.is_set()
            assert [request["json"]["text"] for request in http.requests] == ["Got it."]
            release_answer.set()
            async for _ in audio:
                pass
        assert [request["json"]["text"] for request in http.requests] == [
            "Got it.",
            "Here is your answer.",
        ]
        for request in http.requests:
            assert "/testVoiceId123/stream?" in request["url"]
            assert "output_format=pcm_24000" in request["url"]
            assert request["json"]["model_id"] == "eleven_flash_v2_5"
        assert all(response.closed for response in http.responses)
    finally:
        release_answer.set()
        await audio.aclose()
        await provider.aclose()


async def test_elevenlabs_partial_audio_failure_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = configured_provider(monkeypatch)
    response = SpeechResponse(block=True, fail=True)
    http = SpeechSession(response)
    monkeypatch.setattr(provider, "_ensure_session", lambda: http)
    options = APIConnectOptions(retry_interval=0.001)

    async def text() -> AsyncGenerator[str, None]:
        yield "One sentence.\n\n"

    audio = stream_reply_audio(provider, text(), options)
    try:
        async with asyncio.timeout(3):
            assert any((await anext(audio)).data)
            response.release.set()
            with pytest.raises(APIError):
                async for _ in audio:
                    pass
        assert len(http.requests) == 1
        assert options.max_retry == 3
        assert response.closed
    finally:
        response.release.set()
        await audio.aclose()
        await provider.aclose()


async def test_interruption_closes_only_its_own_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = configured_provider(monkeypatch)
    old, new = SpeechResponse(block=True), SpeechResponse(block=True)
    http = SpeechSession(old, new)
    monkeypatch.setattr(provider, "_ensure_session", lambda: http)

    async def text(value: str) -> AsyncGenerator[str, None]:
        yield value + "\n\n"

    first = stream_reply_audio(provider, text("Old answer."), APIConnectOptions())
    second = stream_reply_audio(provider, text("New answer."), APIConnectOptions())
    try:
        async with asyncio.timeout(3):
            await anext(first)
            await anext(second)
            await first.aclose()
            assert old.closed
            assert not new.closed
            new.release.set()
            async for _ in second:
                pass
        assert new.closed
        assert len(http.requests) == 2
    finally:
        old.release.set()
        new.release.set()
        await first.aclose()
        await second.aclose()
        await provider.aclose()


def test_native_shared_websocket_path_is_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = configured_provider(monkeypatch)
    with pytest.raises(NotImplementedError):
        provider.stream()


async def test_session_uses_selected_voice_without_changing_stt_or_turn_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "offline-only")
    monkeypatch.setenv("ELEVEN_API_KEY", "offline-only")
    session = make_session(
        Settings(tts_provider="elevenlabs", elevenlabs_voice_id="testVoiceId123"),
        Mock(spec=silero.VAD),
        tuning=VoiceTuning(elevenLabsSpeed=1.2),
    )
    assert isinstance(session.tts, ElevenSentenceTTS)
    assert getattr(session.tts._opts.voice_settings, "speed", None) == 1.2
    assert isinstance(session.stt, LiveTranscribeSTT)
    assert session.llm is None
    assert session.options.preemptive_generation.get("enabled") is False
    assert session.options.interruption.get("enabled") is True
    await session.tts.aclose()
    await session.stt.aclose()


def test_elevenlabs_missing_key_does_not_fall_back_to_openai() -> None:
    with pytest.raises(ValueError, match="ElevenLabs API key is required"):
        make_tts(Settings(tts_provider="elevenlabs", elevenlabs_voice_id="testVoiceId123"))
