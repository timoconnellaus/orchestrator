import asyncio
import base64
import json
from collections.abc import AsyncGenerator, AsyncIterable
from types import SimpleNamespace

import httpx
import openai as openai_sdk
import pytest
from livekit import rtc
from livekit.agents import APIConnectOptions, APIError
from livekit.agents.voice import ModelSettings
from livekit.plugins import openai

from orchestrator_voice.bridge import ControlBridge
from orchestrator_voice.worker import ControlAgent


def unused_bridge() -> ControlBridge:
    def reject_request(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("TTS must not submit control requests")

    return ControlBridge(httpx.AsyncClient(transport=httpx.MockTransport(reject_request)))


async def test_production_tts_node_does_not_retry_after_partial_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = asyncio.Event()
    requests = []

    class BrokenSpeech(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncGenerator[bytes, None]:
            event = {
                "type": "speech.audio.delta",
                "delta": base64.b64encode(bytes(48000)).decode(),
            }
            yield f"data: {json.dumps(event)}\n\n".encode()
            await release.wait()
            raise httpx.ReadError("Offline simulated interruption")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=BrokenSpeech()
        )

    client = openai_sdk.AsyncClient(
        api_key="offline-only",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    provider = openai.TTS(model="gpt-4o-mini-tts", response_format="pcm", client=client)
    options = APIConnectOptions(retry_interval=0.001)
    assert options.max_retry == 3  # Exercise the actual session default, not a test override.
    session = SimpleNamespace(tts=provider, conn_options=SimpleNamespace(tts_conn_options=options))
    monkeypatch.setattr(ControlAgent, "session", property(lambda _: session))
    bridge = unused_bridge()
    agent = ControlAgent(bridge, "main")

    async def text() -> AsyncGenerator[str, None]:
        yield "One sentence.\n\n"

    audio = agent.tts_node(text(), ModelSettings())
    try:
        async with asyncio.timeout(3):
            first = await anext(audio)
            assert first.samples_per_channel > 0
            release.set()
            with pytest.raises(APIError):
                async for _ in audio:
                    pass
        assert len(requests) == 1
        assert options.max_retry == 3  # Do not mutate shared session configuration.
    finally:
        release.set()
        await audio.aclose()
        await bridge.aclose()
        await provider.aclose()
        await client.close()


async def test_closing_tts_node_at_yield_awaits_inner_audio_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed = asyncio.Event()
    retained: list[AsyncGenerator[rtc.AudioFrame, None]] = []

    async def inner() -> AsyncGenerator[rtc.AudioFrame, None]:
        try:
            yield rtc.AudioFrame(
                data=bytes(480), sample_rate=24000, num_channels=1, samples_per_channel=240
            )
            await asyncio.Event().wait()
        finally:
            closed.set()

    def fake_audio(
        _provider: object, _text: AsyncIterable[str], _options: APIConnectOptions
    ) -> AsyncGenerator[rtc.AudioFrame, None]:
        stream = inner()
        retained.append(stream)  # Prevent GC from accidentally making the test pass.
        return stream

    session = SimpleNamespace(
        tts=object(), conn_options=SimpleNamespace(tts_conn_options=APIConnectOptions())
    )
    monkeypatch.setattr(ControlAgent, "session", property(lambda _: session))
    monkeypatch.setattr("orchestrator_voice.worker.stream_reply_audio", fake_audio)
    bridge = unused_bridge()
    agent = ControlAgent(bridge, "main")

    async def text() -> AsyncGenerator[str, None]:
        yield "Unused."

    audio = agent.tts_node(text(), ModelSettings())
    try:
        await anext(audio)
        await audio.aclose()
        assert closed.is_set()
    finally:
        await audio.aclose()
        for stream in retained:
            await stream.aclose()
        await bridge.aclose()
