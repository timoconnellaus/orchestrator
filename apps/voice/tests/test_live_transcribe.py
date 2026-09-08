import asyncio
import base64
import json
from dataclasses import dataclass, field
from typing import Any, cast

import aiohttp
import pytest
from livekit import rtc
from livekit.agents import stt, vad

from orchestrator_voice.live_transcribe import LiveTranscribeSTT
from orchestrator_voice.transcription_turn import TranscriptionTurn

PREFIX = "conversation.item.input_audio_transcription."


def transcripts(events: list[stt.SpeechEvent], *, final: bool) -> list[str]:
    kind = stt.SpeechEventType.FINAL_TRANSCRIPT if final else stt.SpeechEventType.INTERIM_TRANSCRIPT
    return [event.alternatives[0].text for event in events if event.type == kind]


def marker(kind: int) -> rtc.AudioFrame:
    return rtc.AudioFrame(
        data=kind.to_bytes(2, "little") * 2400,
        sample_rate=24000,
        num_channels=1,
        samples_per_channel=2400,
    )


class MarkerVAD(vad.VAD):
    """Stateful deterministic VAD; no acoustic/model/network test."""

    def __init__(self) -> None:
        super().__init__(capabilities=vad.VADCapabilities(update_interval=0.1))
        self.streams: list[MarkerStream] = []

    def stream(self) -> vad.VADStream:
        stream = MarkerStream(self)
        self.streams.append(stream)
        return stream


class MarkerStream(vad.VADStream):
    async def _main_task(self) -> None:
        speaking = False
        clock = 0.0
        captured: list[rtc.AudioFrame] = []
        async for frame in self._input_ch:
            if isinstance(frame, self._FlushSentinel):
                continue
            clock += frame.samples_per_channel / frame.sample_rate
            kind = frame.data[0]
            self._event_ch.send_nowait(
                vad.VADEvent(
                    type=vad.VADEventType.INFERENCE_DONE,
                    samples_index=0,
                    timestamp=clock,
                    speech_duration=0,
                    silence_duration=0.6 if kind == 0 else 0,
                    speaking=speaking,
                    frames=[frame],
                )
            )
            if kind == 1 and not speaking:
                speaking = True
                captured = [frame]
                self._event_ch.send_nowait(
                    vad.VADEvent(
                        type=vad.VADEventType.START_OF_SPEECH,
                        samples_index=0,
                        timestamp=clock,
                        speech_duration=0.1,
                        silence_duration=0,
                        speaking=True,
                        frames=[frame],
                    )
                )
            elif kind == 3 and speaking:
                captured.append(frame)
                speaking = False
                self._event_ch.send_nowait(
                    vad.VADEvent(
                        type=vad.VADEventType.END_OF_SPEECH,
                        samples_index=0,
                        timestamp=clock,
                        speech_duration=0.1,
                        silence_duration=0.6,
                        speaking=False,
                        frames=captured,
                    )
                )
            elif speaking:
                captured.append(frame)


class FakeSocket:
    def __init__(self, harness: "SocketHarness", item: str) -> None:
        self.harness = harness
        self.item = item
        self.packets: list[dict[str, Any]] = []
        self.incoming: asyncio.Queue[aiohttp.WSMessage] = asyncio.Queue()
        self.closed = False
        self.committed = asyncio.Event()

    def reply(self, event: dict[str, Any]) -> None:
        self.incoming.put_nowait(aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, json.dumps(event), ""))

    async def send_json(self, packet: dict[str, Any]) -> None:
        if self.closed:
            raise ConnectionError("Socket closed")
        self.packets.append(packet)
        if packet["type"] == "session.update":
            if self.harness.configure:
                self.reply({"type": "session.updated"})
        elif packet["type"] == "input_audio_buffer.append":
            self.harness.appended.set()
            self.reply({"type": PREFIX + "delta", "item_id": self.item, "delta": "Hello"})
        elif packet["type"] == "input_audio_buffer.commit":
            self.committed.set()
            if sum(socket.committed.is_set() for socket in self.harness.sockets) >= 2:
                self.harness.two_commits.set()
            if self.harness.acknowledge:
                self.reply({"type": "input_audio_buffer.committed", "item_id": self.item})
            if self.harness.automatic:
                self.reply(
                    {"type": PREFIX + "completed", "item_id": self.item, "transcript": "Hello"}
                )

    async def receive(self, *, timeout: float) -> aiohttp.WSMessage:  # noqa: ASYNC109 -- aiohttp interface
        message = await asyncio.wait_for(self.incoming.get(), timeout)
        self.incoming.task_done()
        return message

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.incoming.put_nowait(aiohttp.WSMessage(aiohttp.WSMsgType.CLOSED, None, ""))


@dataclass
class SocketHarness:
    url: str = "wss://offline.invalid/"
    automatic: bool = True
    acknowledge: bool = True
    configure: bool = True
    sockets: list[FakeSocket] = field(default_factory=list)
    appended: asyncio.Event = field(default_factory=asyncio.Event)
    two_commits: asyncio.Event = field(default_factory=asyncio.Event)

    async def ws_connect(self, url: str, **kwargs: Any) -> FakeSocket:
        assert url == self.url
        assert kwargs["headers"] == {"Authorization": "Bearer test"}
        socket = FakeSocket(self, f"item-{len(self.sockets) + 1}")
        self.sockets.append(socket)
        return socket

    def engine(self, detector: MarkerVAD | None = None) -> LiveTranscribeSTT:
        # Deliberate transport-boundary double. The global no-network guard stays on.
        return LiveTranscribeSTT(
            vad=detector or MarkerVAD(),
            api_key="test",
            http_session=cast(aiohttp.ClientSession, self),
            url=self.url,
        )


async def collect(stream: stt.SpeechStream) -> list[stt.SpeechEvent]:
    async with asyncio.timeout(3):
        return [event async for event in stream]


async def test_idle_has_no_provider_connection() -> None:
    provider = SocketHarness()
    engine = provider.engine()
    stream = engine.stream()
    try:
        for _ in range(5):
            stream.push_frame(marker(0))
        stream.end_input()
        assert await collect(stream) == []
        assert provider.sockets == []
    finally:
        await engine.aclose()


async def test_stream_uses_only_transcription_and_gated_audio_once() -> None:
    provider = SocketHarness()
    engine = provider.engine()
    stream = engine.stream()
    try:
        for kind in [0, 0, 1, 2, 3, 0, 0]:
            stream.push_frame(marker(kind))
        stream.end_input()
        assert transcripts(await collect(stream), final=True) == ["Hello"]
        assert len(provider.sockets) == 1
        packets = provider.sockets[0].packets
        assert [packet["type"] for packet in packets] == [
            "session.update",
            "input_audio_buffer.append",
            "input_audio_buffer.append",
            "input_audio_buffer.append",
            "input_audio_buffer.commit",
        ]
        settings = packets[0]["session"]
        assert settings["type"] == "transcription"
        assert settings["audio"]["input"]["turn_detection"] is None
        assert settings["audio"]["input"]["transcription"] == {
            "model": "gpt-live-transcribe",
            "languages": ["en"],
            "delay": "low",
        }
        assert [base64.b64decode(packet["audio"]) for packet in packets[1:4]] == [
            marker(kind).data.tobytes() for kind in [1, 2, 3]
        ]
    finally:
        await engine.aclose()


async def test_partials_arrive_without_committing_or_ending_speech() -> None:
    provider = SocketHarness()
    engine = provider.engine()
    stream = engine.stream()
    heard = asyncio.Event()
    events: list[stt.SpeechEvent] = []

    async def consume() -> None:
        async for event in stream:
            events.append(event)
            if event.type == stt.SpeechEventType.INTERIM_TRANSCRIPT and event.alternatives[0].text:
                heard.set()

    consuming = asyncio.create_task(consume())
    try:
        stream.push_frame(marker(1))
        await asyncio.wait_for(heard.wait(), 2)
        assert not provider.sockets[0].committed.is_set()
        stream.push_frame(marker(3))
        stream.end_input()
        await asyncio.wait_for(consuming, 2)
        assert transcripts(events, final=True) == ["Hello"]
    finally:
        consuming.cancel()
        await asyncio.gather(consuming, return_exceptions=True)
        await engine.aclose()


async def test_mute_resets_stateful_vad_and_rejects_queued_old_capture() -> None:
    provider, detector = SocketHarness(), MarkerVAD()
    engine = provider.engine(detector)
    stream = engine.stream()
    try:
        stream.push_frame(marker(1))
        await asyncio.wait_for(provider.appended.wait(), 2)
        stream.push_frame(marker(2))  # Queued before the boundary; must never cross it.
        engine.set_capture_enabled(False)
        engine.set_capture_enabled(True)  # Even a very short mute is a real boundary.
        for kind in [1, 3]:
            stream.push_frame(marker(kind))
        stream.end_input()
        events = await collect(stream)
        assert transcripts(events, final=True) == ["Hello"]
        assert "" in transcripts(events, final=False)
        assert len(detector.streams) == 2
        assert len(provider.sockets) == 2
        assert not provider.sockets[0].committed.is_set()
        for socket in provider.sockets:
            assert all(
                base64.b64decode(packet["audio"]) != marker(2).data.tobytes()
                for packet in socket.packets
                if packet["type"] == "input_audio_buffer.append"
            )
    finally:
        await engine.aclose()


async def test_failed_turn_preserves_later_completed_turn_without_replay() -> None:
    provider = SocketHarness(automatic=False)
    engine = provider.engine()
    stream = engine.stream()
    consuming = asyncio.create_task(collect(stream))
    try:
        for kind in [1, 3, 1, 3]:
            stream.push_frame(marker(kind))
        stream.end_input()
        await asyncio.wait_for(provider.two_commits.wait(), 2)
        second = provider.sockets[1]
        second.reply(
            {"type": PREFIX + "completed", "item_id": second.item, "transcript": "Second turn"}
        )
        first = provider.sockets[0]
        first.reply(
            {"type": PREFIX + "failed", "item_id": first.item, "error": {"message": "private"}}
        )
        events = await consuming
        assert transcripts(events, final=True) == ["Second turn"]
        assert len(provider.sockets) == 2  # No reconnect/replay of the failed utterance.
        assert "" in transcripts(events, final=False)
    finally:
        consuming.cancel()
        await asyncio.gather(consuming, return_exceptions=True)
        await engine.aclose()


@pytest.mark.parametrize("acknowledge", [True, False])
async def test_missing_final_or_ack_has_a_deadline(
    acknowledge: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("orchestrator_voice.transcription_turn.FINAL_TIMEOUT", 0.03)
    provider = SocketHarness(automatic=False, acknowledge=acknowledge)
    engine = provider.engine()
    stream = engine.stream()
    try:
        for kind in [1, 3]:
            stream.push_frame(marker(kind))
        stream.end_input()
        events = await collect(stream)
        assert transcripts(events, final=True) == []
        assert transcripts(events, final=False)[-1] == ""
        assert len(provider.sockets) == 1
    finally:
        await engine.aclose()


async def test_overflow_discards_whole_generation_instead_of_executing_a_tail() -> None:
    provider = SocketHarness()
    engine = provider.engine()
    stream = engine.stream()
    try:
        for _ in range(30):
            stream.push_frame(marker(1))
        stream.end_input()
        assert transcripts(await collect(stream), final=True) == []
        assert not provider.sockets
    finally:
        await engine.aclose()


async def test_close_during_setup_leaves_no_provider_task_or_socket() -> None:
    provider = SocketHarness(configure=False)
    turn = TranscriptionTurn(
        session=cast(aiohttp.ClientSession, provider),
        url=provider.url,
        api_key="test",
        language="en",
    )
    turn.push(marker(1))
    # Yield to the scheduled connection setup, then close while its ack is pending.
    await asyncio.sleep(0)
    await turn.aclose()
    assert turn._task.done()
    assert all(socket.closed for socket in provider.sockets)


async def test_mute_preserves_a_fully_captured_turn_awaiting_its_result() -> None:
    provider = SocketHarness(automatic=False)
    engine = provider.engine()
    stream = engine.stream()
    consuming = asyncio.create_task(collect(stream))
    try:
        stream.push_frame(marker(1))
        stream.push_frame(marker(3))
        await asyncio.wait_for(provider.appended.wait(), 2)
        socket = provider.sockets[0]
        await asyncio.wait_for(socket.committed.wait(), 2)
        engine.set_capture_enabled(False)
        socket.reply(
            {
                "type": PREFIX + "completed",
                "item_id": socket.item,
                "transcript": "Finished before mute",
            }
        )
        stream.end_input()
        assert transcripts(await consuming, final=True) == ["Finished before mute"]
    finally:
        consuming.cancel()
        await asyncio.gather(consuming, return_exceptions=True)
        await engine.aclose()


async def test_completion_is_not_accepted_until_its_commit_is_acknowledged() -> None:
    provider = SocketHarness(automatic=False, acknowledge=False)
    turn = TranscriptionTurn(
        session=cast(aiohttp.ClientSession, provider),
        url=provider.url,
        api_key="test",
        language="en",
    )
    try:
        turn.push(marker(1))
        turn.finish()
        await asyncio.wait_for(provider.appended.wait(), 2)
        socket = provider.sockets[0]
        await asyncio.wait_for(socket.committed.wait(), 2)
        socket.reply(
            {"type": PREFIX + "completed", "item_id": socket.item, "transcript": "Complete"}
        )
        await asyncio.wait_for(socket.incoming.join(), 2)
        assert turn.final is None
        socket.reply({"type": "input_audio_buffer.committed", "item_id": socket.item})
        await asyncio.wait_for(turn._task, 2)
        assert turn.final == "Complete"
        assert turn.error is None
    finally:
        await turn.aclose()


async def test_final_deadline_is_enforced_even_with_continuous_provider_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("orchestrator_voice.transcription_turn.FINAL_TIMEOUT", 0.03)
    provider = SocketHarness(automatic=False)
    turn = TranscriptionTurn(
        session=cast(aiohttp.ClientSession, provider),
        url=provider.url,
        api_key="test",
        language="en",
    )
    traffic: asyncio.Task[None] | None = None
    try:
        turn.push(marker(1))
        turn.finish()
        await asyncio.wait_for(provider.appended.wait(), 2)
        socket = provider.sockets[0]
        await asyncio.wait_for(socket.committed.wait(), 2)

        async def chatter() -> None:
            for _ in range(100):
                socket.reply({"type": "rate_limits.updated"})
                await asyncio.sleep(0.005)

        traffic = asyncio.create_task(chatter())
        await asyncio.wait_for(turn._task, 2)
        assert turn.final is None
        assert turn.error is not None and "TimeoutError" in turn.error
    finally:
        if traffic is not None:
            traffic.cancel()
            await asyncio.gather(traffic, return_exceptions=True)
        await turn.aclose()


async def test_setup_buffer_preserves_audio_during_an_allowed_slow_handshake() -> None:
    provider = SocketHarness(configure=False)
    turn = TranscriptionTurn(
        session=cast(aiohttp.ClientSession, provider),
        url=provider.url,
        api_key="test",
        language="en",
    )
    try:
        # Setup has a five-second deadline; its bounded buffer must tolerate
        # more than the steady-state two-second network backlog allowance.
        for _ in range(25):
            turn.push(marker(1))
        assert not turn.done
        await asyncio.sleep(0)
        assert len(provider.sockets) == 1
        provider.sockets[0].reply({"type": "session.updated"})
        turn.finish()
        await asyncio.wait_for(turn._task, 2)
        assert turn.final == "Hello"
        assert turn.error is None
    finally:
        await turn.aclose()
