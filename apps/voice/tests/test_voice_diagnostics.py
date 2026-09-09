import asyncio
import json
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import pytest
from livekit.agents import vad
from livekit.rtc import participant as rtc_participant
from livekit.rtc._ffi_client import FfiClient
from test_live_transcribe import SocketHarness, marker

from orchestrator_voice.voice_diagnostics import VoiceDiagnostics
from orchestrator_voice.voice_tuning import VoiceTuning


async def discard(data: bytes) -> None:
    pass


def diagnostics(send: Callable[[bytes], Awaitable[None]] = discard) -> VoiceDiagnostics:
    return VoiceDiagnostics(
        VoiceTuning(), available=True, room="room", speaker="phone", sender="agent", send=send
    )


def event(kind: vad.VADEventType, sample: int = 16384) -> vad.VADEvent:
    return vad.VADEvent(
        type=kind,
        samples_index=0,
        timestamp=0.1,
        speech_duration=0,
        silence_duration=0,
        speaking=False,
        frames=[marker(sample)],
    )


def test_incremental_rms_transition_state_stale_reset_and_no_audio_or_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [100.0]
    monkeypatch.setattr("orchestrator_voice.voice_diagnostics.time.monotonic", lambda: clock[0])
    meter = diagnostics()
    meter.probe(b'{"nonce":"capture-1"}', "phone")
    meter.observe(event(vad.VADEventType.INFERENCE_DONE), speech=False)
    first = json.loads(meter.packet())
    assert first["level"] == 0.5
    assert not first["speech"]  # inference can precede START
    meter.observe(event(vad.VADEventType.START_OF_SPEECH, 30000), speech=True)
    started = json.loads(meter.packet())
    assert started["speech"] and started["level"] == 0.5  # no pre-roll RMS
    meter.observe(event(vad.VADEventType.END_OF_SPEECH, 30000), speech=False)
    ended = json.loads(meter.packet())
    assert not ended["speech"] and ended["level"] == 0.5  # no repeated END frames
    assert set(ended) == {
        "version",
        "room",
        "speaker",
        "sender",
        "session",
        "nonce",
        "sequence",
        "generation",
        "sentAtMs",
        "available",
        "fresh",
        "level",
        "speech",
        "voiceTuning",
    }
    assert len(meter.packet()) < 2048
    clock[0] += 2
    assert json.loads(meter.packet())["level"] == 0
    meter.reset()
    reset = json.loads(meter.packet())
    assert not reset["fresh"] and not reset["speech"]
    assert reset["generation"] > first["generation"]
    meter.available = False
    meter.observe(event(vad.VADEventType.INFERENCE_DONE), speech=True)
    fallback = json.loads(meter.packet())
    assert not fallback["available"] and not fallback["fresh"] and fallback["level"] == 0


def test_probes_are_bounded_linked_and_do_not_rearm_or_reconfigure() -> None:
    meter = diagnostics()
    for data, sender in [
        (b'{"nonce":"one"}', "other"),
        (b"[]", "phone"),
        (b"x" * 257, "phone"),
        (b'{"nonce":true}', "phone"),
        (b'{"nonce":"one","audio":"no"}', "phone"),
    ]:
        meter.probe(data, sender)
        assert json.loads(meter.packet())["nonce"] == ""
    meter.probe(b'{"nonce":"one"}', "phone")
    one = json.loads(meter.packet())
    meter.probe(b'{"nonce":"one"}', "phone")
    assert json.loads(meter.packet())["generation"] == one["generation"]
    meter.probe(b'{"nonce":"two"}', "phone")
    two = json.loads(meter.packet())
    assert two["generation"] > one["generation"]
    assert two["voiceTuning"] == VoiceTuning().wire()


async def test_one_owned_sender_coalesces_blocked_transport_and_closes() -> None:
    sent: list[dict[str, Any]] = []
    blocked = asyncio.Event()
    concurrent = 0
    maximum = 0

    async def send(data: bytes) -> None:
        nonlocal concurrent, maximum
        concurrent += 1
        maximum = max(concurrent, maximum)
        sent.append(json.loads(data))
        try:
            await blocked.wait()
        finally:
            concurrent -= 1

    meter = diagnostics(send)
    meter.probe(b'{"nonce":"one"}', "phone")
    meter.start()
    task = meter._task
    meter.start()
    assert meter._task is task
    try:
        await asyncio.sleep(0.01)
        for _ in range(1000):
            meter.observe(event(vad.VADEventType.INFERENCE_DONE, 8192), speech=True)
        blocked.set()  # Brief backpressure resolves before the native-send deadline.
        await asyncio.sleep(0.4)
        assert maximum == 1
        assert 1 <= len(sent) <= 3
        assert sent[-1]["level"] == 0.25  # latest state, not a per-frame backlog
    finally:
        await meter.aclose()
    meter.start()  # a late startup cannot revive a closed job sender
    count = len(sent)
    await asyncio.sleep(0.25)
    assert len(sent) == count and concurrent == 0 and meter._task is None


async def test_native_publication_timeout_disables_sender_for_join(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kinds: list[list[str]] = []

    class Queue:
        def subscribe(self) -> "Queue":
            return self

        def unsubscribe(self, _waiter: object) -> None:
            pass

        async def wait_for(self, _predicate: object) -> None:
            await asyncio.Event().wait()  # Native callback never arrives.

    class Ffi:
        queue = Queue()

        def request(self, request: Any) -> SimpleNamespace:
            kinds.append([field.name for field, _ in request.ListFields()])
            return SimpleNamespace(publish_data=SimpleNamespace(async_id=len(kinds)))

    monkeypatch.setattr(FfiClient, "instance", Ffi())
    local: Any = SimpleNamespace(_ffi_handle=SimpleNamespace(handle=1))

    async def send(payload: bytes) -> None:
        await rtc_participant.LocalParticipant.publish_data(local, payload, reliable=False)

    meter = diagnostics(send)
    meter.probe(b'{"nonce":"one"}', "phone")
    meter.start()
    try:
        await asyncio.sleep(0.4)
        meter.probe(b'{"nonce":"two"}', "phone")
        meter.start()  # A new capture nonce must not reopen an ambiguous native send.
        await asyncio.sleep(0.4)
        assert kinds == [["publish_data"]]
    finally:
        await meter.aclose()


async def test_live_transcribe_guarded_seam_mute_generation_and_dispose() -> None:
    provider = SocketHarness()
    engine = provider.engine()
    meter = diagnostics()
    engine.diagnostics = meter
    meter.probe(b'{"nonce":"one"}', "phone")
    stream = engine.stream()
    try:
        stream.push_frame(marker(1))
        await asyncio.wait_for(provider.appended.wait(), 2)
        first = json.loads(meter.packet())
        assert first["speech"] and first["fresh"] and first["level"] > 0
        stream.push_frame(marker(32767))  # queued old generation is never metered
        engine.set_capture_enabled(False)
        await asyncio.sleep(0.03)
        muted = json.loads(meter.packet())
        assert muted["generation"] > first["generation"]
        assert not muted["speech"] and muted["level"] == 0 and not muted["fresh"]
        engine.set_capture_enabled(True)
        stream.push_frame(marker(0))
        await asyncio.sleep(0.03)
        assert json.loads(meter.packet())["fresh"]
        assert json.loads(meter.packet())["level"] == 0
    finally:
        await engine.aclose()
    assert not json.loads(meter.packet())["fresh"]
