import asyncio
import base64
import inspect
import json
from unittest.mock import Mock

import httpx
import openai as openai_sdk
import pytest
from livekit import rtc
from livekit.agents import AgentSession, APIConnectOptions, llm
from livekit.agents.voice import ModelSettings
from livekit.agents.voice.run_result import RunResult
from livekit.plugins import openai, silero
from test_bridge import bridge_for, operation

from orchestrator_voice.config import AGENT_NAME, Settings, turn_handling
from orchestrator_voice.worker import (
    ControlAgent,
    LocalNodeOnlyLLM,
    make_session,
    room_options,
    server,
)


async def collect(agent, context):
    return [text async for text in agent.llm_node(context, [], ModelSettings())]


async def test_llm_node_sends_only_latest_final_text_and_reuses_message_id():
    payloads = []

    def handler(request):
        if request.method == "POST":
            payloads.append(json.loads(request.content))
            return httpx.Response(202, json={"operationId": "op-1"})
        return operation()

    bridge = bridge_for(handler)
    agent = ControlAgent(bridge, "session:voice")
    context = llm.ChatContext()
    context.add_message(role="system", content="do not send this")
    context.add_message(role="user", content="earlier question")
    context.add_message(role="assistant", content="earlier answer")
    context.add_message(role="user", content="latest question")
    try:
        assert await collect(agent, context) == ["Done."]
        assert await collect(agent, context) == ["Done."]
        assert payloads[0] == payloads[1]
        assert payloads[0]["text"] == "latest question"
        assert payloads[0]["conversationId"] == "session:voice"
        assert payloads[0]["source"] == "voice"
        context.add_message(role="user", content="latest question")
        await collect(agent, context)
        assert payloads[2]["id"] != payloads[0]["id"]
    finally:
        await bridge.aclose()


async def test_empty_context_and_blank_transcript_do_not_create_operations():
    def forbidden(request):
        pytest.fail("No POST should be made")

    bridge = bridge_for(forbidden)
    agent = ControlAgent(bridge, "main")
    context = llm.ChatContext()
    try:
        assert await collect(agent, context) == []
        context.add_message(role="user", content="  ")
        assert await collect(agent, context) == []
    finally:
        await bridge.aclose()


async def test_bridge_failure_yields_honest_safe_speech_not_raw_backend_error():
    bridge = bridge_for(lambda r: httpx.Response(409, text="SECRET should never be spoken"))
    agent = ControlAgent(bridge, "main")
    context = llm.ChatContext()
    context.add_message(role="user", content="test")
    try:
        text = "".join(await collect(agent, context))
        assert "rejected" in text
        assert "SECRET" not in text
    finally:
        await bridge.aclose()


async def test_custom_node_cancellation_is_not_spoken_as_failure():
    polling = asyncio.Event()

    async def handler(request):
        if request.method == "POST":
            return httpx.Response(202, json={"operationId": "op-1"})
        polling.set()
        await asyncio.Event().wait()

    bridge = bridge_for(handler)
    agent = ControlAgent(bridge, "main")
    context = llm.ChatContext()
    context.add_message(role="user", content="test")
    consumer = asyncio.create_task(collect(agent, context))
    await polling.wait()
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    await bridge.aclose()


async def test_real_pinned_sdk_routes_to_custom_node_without_reasoning_provider():
    methods = []

    def handler(request):
        methods.append(request.method)
        if request.method == "POST":
            return httpx.Response(202, json={"operationId": "op-1"})
        return operation()

    bridge = bridge_for(handler)
    # No audio sinks, STT, TTS, models, room, network or keys. Exercise real SDK
    # scheduling so llm=None or a broken node signature cannot silently pass.
    async with AgentSession(turn_handling=turn_handling(), user_away_timeout=None) as session:
        await session.start(ControlAgent(bridge, "main"), record=False)
        result: RunResult[None] = await session.run(user_input="Check workers")
        message = result.expect.next_event().is_message(role="assistant").event().item
        assert message.text_content == "Done."
        result.expect.no_more_events()
        assert methods == ["POST", "GET"]
    await bridge.aclose()


async def test_pipeline_constructor_has_no_llm_provider_idle_or_speculation(monkeypatch):
    stt_factory = Mock()
    tts_factory = Mock()
    monkeypatch.setattr(openai, "STT", stt_factory)
    monkeypatch.setattr(openai, "TTS", tts_factory)
    vad = Mock(spec=silero.VAD)
    session = make_session(Settings(stt_model="gpt-transcribe"), vad)
    stt_factory.assert_called_once_with(model="gpt-transcribe", language="en", use_realtime=False)
    tts_factory.assert_called_once_with(model="gpt-4o-mini-tts", voice="coral")
    assert session.llm is None  # Only the agent supplies the local guard.
    assert session.options.user_away_timeout is None
    assert session.options.preemptive_generation.get("enabled") is False
    assert session.options.preemptive_generation.get("preemptive_tts") is False
    assert session.options.interruption.get("enabled") is True
    options = room_options()
    assert options.text_input is False
    assert options.video_input is False
    assert options.close_on_disconnect is True
    assert options.delete_room_on_close is False
    assert server._agent_name == AGENT_NAME == "orchestrator-voice"


def test_local_llm_guard_fails_closed():
    with pytest.raises(RuntimeError, match="no reasoning model"):
        LocalNodeOnlyLLM().chat(chat_ctx=llm.ChatContext())


async def test_pinned_openai_stt_rest_supports_model_string_override_without_network():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"text": "hello"})

    client = openai_sdk.AsyncClient(
        api_key="offline-test-only",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    stt = openai.STT(model="gpt-transcribe", use_realtime=False, client=client)
    try:
        frame = rtc.AudioFrame(
            data=bytes(320), sample_rate=16000, num_channels=1, samples_per_channel=160
        )
        event = await stt.recognize(frame, conn_options=APIConnectOptions(max_retry=0))
        assert event.alternatives[0].text == "hello"
        assert b"gpt-transcribe" in requests[0].content
        assert b"\r\n\r\njson\r\n" in requests[0].content
        assert not stt.capabilities.streaming
    finally:
        await stt.aclose()
        await client.close()


def test_pinned_openai_tts_streams_audio_response_with_default_node_adapter():
    # SDK labels streaming=False because input text is chunked, NOT because it
    # buffers the entire audio response. Confirm actual pinned implementation.
    source = inspect.getsource(openai.tts.SSEChunkedStream)
    assert "with_streaming_response.create" in source
    assert "iter_lines" in source
    assert '"speech.audio.delta"' in source
    assert "streaming=False" in inspect.getsource(openai.TTS.__init__)
    assert "StreamAdapter" in inspect.getsource(ControlAgent.default.tts_node)


async def test_real_pinned_speech_constructors_are_compatible(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-only")
    # No injected OpenAI client: catches httpx/httpx2 major-version mismatches.
    stt = openai.STT(model=Settings().stt_model, use_realtime=False)
    tts = openai.TTS(model=Settings().tts_model, voice=Settings().tts_voice)
    try:
        assert stt.model == "gpt-4o-mini-transcribe"
        assert tts.model == "gpt-4o-mini-tts"
    finally:
        await stt.aclose()
        await tts.aclose()


async def test_default_tts_model_emits_audio_before_http_response_finishes():
    release = asyncio.Event()
    response_finished = asyncio.Event()
    requests = []

    class SpeechResponse(httpx.AsyncByteStream):
        async def __aiter__(self):
            event = {
                "type": "speech.audio.delta",
                "delta": base64.b64encode(bytes(48000)).decode(),
            }
            yield f"data: {json.dumps(event)}\n\n".encode()
            await release.wait()
            response_finished.set()
            yield b'data: {"type":"speech.audio.done"}\n\ndata: [DONE]\n\n'

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=SpeechResponse()
        )

    client = openai_sdk.AsyncClient(
        api_key="offline-test-only",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    tts = openai.TTS(model="gpt-4o-mini-tts", voice="coral", response_format="pcm", client=client)
    try:
        async with tts.synthesize("Done.", conn_options=APIConnectOptions(max_retry=0)) as stream:
            async with asyncio.timeout(2):
                first = await anext(stream)
            assert first.frame.samples_per_channel > 0
            assert not response_finished.is_set()
            release.set()
            async for _ in stream:
                pass
        assert requests[0]["model"] == "gpt-4o-mini-tts"
        assert requests[0]["stream_format"] == "sse"
        assert response_finished.is_set()
    finally:
        release.set()
        await tts.aclose()
        await client.close()
