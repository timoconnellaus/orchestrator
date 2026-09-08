import asyncio
import base64
import inspect
import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import httpx
import openai as openai_sdk
import pytest
from livekit import rtc
from livekit.agents import AgentSession, APIConnectOptions, llm
from livekit.agents.voice import ModelSettings
from livekit.agents.voice.audio_recognition import AudioRecognition
from livekit.agents.voice.room_io._output import _ParticipantLegacyTranscriptionOutput
from livekit.agents.voice.run_result import RunResult
from livekit.plugins import openai, silero
from test_bridge import bridge_for, operation

from orchestrator_voice.config import AGENT_NAME, Settings, turn_handling
from orchestrator_voice.live_transcribe import LiveTranscribeSTT
from orchestrator_voice.worker import (
    ControlAgent,
    LocalNodeOnlyLLM,
    bind_capture_boundaries,
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
        assert await collect(agent, context) == []  # Same invocation cannot replay audio.
        assert len(payloads) == 1
        assert payloads[0]["text"] == "latest question"
        assert payloads[0]["conversationId"] == "session:voice"
        assert payloads[0]["source"] == "voice"
        context.add_message(role="user", content="latest question")
        await collect(agent, context)
        assert payloads[1]["id"] != payloads[0]["id"]
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


async def test_default_pipeline_uses_live_gated_transcription_without_another_reasoner(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-only")
    monkeypatch.setattr(openai, "TTS", Mock())
    session = make_session(Settings(), Mock(spec=silero.VAD))
    assert isinstance(session.stt, LiveTranscribeSTT)
    assert session.stt.model == "gpt-live-transcribe"
    assert session.stt.capabilities.streaming
    assert session.stt.capabilities.interim_results
    assert session.llm is None
    assert room_options().text_output is True
    assert room_options().text_input is False
    await session.stt.aclose()


def test_capture_boundaries_only_follow_linked_phone_microphone():
    room = Mock(spec=rtc.Room)
    session = Mock(spec=AgentSession)
    session.stt = Mock(spec=LiveTranscribeSTT)
    phone = Mock(identity="phone", track_publications={})
    session.room_io.linked_participant = phone
    callbacks: dict[str, Callable[..., None]] = {}
    room.on.side_effect = lambda name, callback: callbacks.setdefault(name, callback)
    cleanup = bind_capture_boundaries(room, session)
    microphone = Mock(source=rtc.TrackSource.SOURCE_MICROPHONE)
    callbacks["track_muted"](microphone, Mock(identity="another-phone"))
    callbacks["track_muted"](Mock(source=rtc.TrackSource.SOURCE_CAMERA), phone)
    session.stt.set_capture_enabled.assert_not_called()
    callbacks["track_muted"](microphone, phone)
    callbacks["track_unmuted"](microphone, phone)
    assert [call.args for call in session.stt.set_capture_enabled.call_args_list] == [
        (False,),
        (True,),
    ]
    cleanup()
    assert room.off.call_count == 4


async def test_pinned_sdk_empty_interim_clears_recognition_and_legacy_caption():
    from livekit.agents import stt
    from livekit.agents.language import LanguageCode

    room = Mock(spec=rtc.Room)
    room.local_participant.identity = "agent"
    room.remote_participants = {}
    room.isconnected.return_value = True
    room.local_participant.publish_transcription = AsyncMock()
    sink = _ParticipantLegacyTranscriptionOutput(room, participant="phone", is_delta_stream=False)
    sink._track_id = "microphone-track"  # Subscription boundary is mocked, not a live room.
    forwarding: list[asyncio.Task[None]] = []

    def interim(event, *, speaking):
        forwarding.append(asyncio.create_task(sink.capture_text(event.alternatives[0].text)))

    state = SimpleNamespace(
        _stt_request_ids=[],
        _turn_detection_mode="vad",
        _interruption_enabled=False,
        _vad=Mock(),
        _speaking=False,
        _audio_interim_transcript="",
        _audio_transcript="Prior final",
        _hooks=SimpleNamespace(
            on_interim_transcript=interim,
            on_final_transcript=Mock(side_effect=AssertionError("Not a final")),
        ),
    )
    recognizer = cast(AudioRecognition, state)
    try:
        for text in ["Unfinished words", ""]:
            await AudioRecognition._on_stt_event(
                recognizer,
                stt.SpeechEvent(
                    type=stt.SpeechEventType.INTERIM_TRANSCRIPT,
                    alternatives=[stt.SpeechData(language=LanguageCode("en"), text=text)],
                ),
            )
            await asyncio.gather(*forwarding)
        packets = [
            call.args[0] for call in room.local_participant.publish_transcription.call_args_list
        ]
        assert [packet.segments[0].text for packet in packets] == ["Unfinished words", ""]
        assert all(not packet.segments[0].final for packet in packets)
        assert packets[0].segments[0].id == packets[1].segments[0].id
        assert state._audio_interim_transcript == ""
        assert state._audio_transcript == "Prior final"
    finally:
        await sink.aclose()


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
    stt = openai.STT(model="gpt-4o-mini-transcribe", use_realtime=False)
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


async def test_receipt_ack_synthesizes_playable_audio_before_control_final():
    from test_bridge import ReplyBytes, final_event, streaming_bridge

    from orchestrator_voice.bridge import BridgeLimits
    from orchestrator_voice.reply_audio import stream_reply_audio

    release = asyncio.Event()
    bridge, methods = streaming_bridge(
        ReplyBytes([], release, [final_event("Confirmed final answer.", seq=1)]),
        BridgeLimits(ack_delay=0.01),
    )
    requests = []

    def tts_handler(request):
        requests.append(json.loads(request.content))
        event = {"type": "speech.audio.delta", "delta": base64.b64encode(bytes(48000)).decode()}
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                f"data: {json.dumps(event)}\n\n"
                'data: {"type":"speech.audio.done"}\n\ndata: [DONE]\n\n'
            ),
        )

    client = openai_sdk.AsyncClient(
        api_key="offline-only",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(tts_handler)),
    )
    provider = openai.TTS(
        model="gpt-4o-mini-tts", voice="coral", response_format="pcm", client=client
    )
    agent = ControlAgent(bridge, "main")
    context = llm.ChatContext()
    context.add_message(role="user", content="Check workers")
    audio = stream_reply_audio(
        provider, agent.llm_node(context, [], ModelSettings()), APIConnectOptions(max_retry=0)
    )
    try:
        first = await asyncio.wait_for(anext(audio), 2)
        assert first.samples_per_channel > 0
        assert requests[0]["input"] == "Got it."
        assert not release.is_set()
        release.set()
        async for _ in audio:
            pass
        assert [r["input"] for r in requests] == ["Got it.", "Confirmed final answer."]
        assert methods == ["POST", "GET"]
    finally:
        release.set()
        await audio.aclose()
        await bridge.aclose()
        await provider.aclose()
        await client.close()


async def test_closing_llm_node_at_yield_closes_its_reply_stream_immediately():
    """Explicit generator closure must not rely on asynchronous garbage collection."""
    from test_bridge import ReplyBytes, streaming_bridge, text_event

    stream = ReplyBytes([text_event("First. ")], asyncio.Event())
    bridge, methods = streaming_bridge(stream)
    agent = ControlAgent(bridge, "main")
    context = llm.ChatContext()
    context.add_message(role="user", content="status")
    reply = agent.llm_node(context, [], ModelSettings())
    assert await anext(reply) == "First. "
    await reply.aclose()
    assert stream.closed
    assert methods == ["POST", "GET"]
    await bridge.aclose()
