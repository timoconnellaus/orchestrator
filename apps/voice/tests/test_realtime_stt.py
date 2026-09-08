from livekit import rtc
from livekit.agents import vad

from orchestrator_voice.realtime_stt import SpeechGate


def frame(milliseconds: int = 32) -> rtc.AudioFrame:
    samples = milliseconds * 24
    return rtc.AudioFrame(
        data=b"\x01\x00" * samples,
        sample_rate=24000,
        num_channels=1,
        samples_per_channel=samples,
    )


def event(kind: vad.VADEventType, *frames: rtc.AudioFrame) -> vad.VADEvent:
    return vad.VADEvent(
        type=kind,
        samples_index=0,
        timestamp=0,
        speech_duration=0,
        silence_duration=0,
        frames=list(frames),
    )


def test_idle_inference_never_uploads_audio() -> None:
    gate = SpeechGate()
    window = event(vad.VADEventType.INFERENCE_DONE, frame())
    for _ in range(1000):
        output = gate.feed(window)
        assert not output.frames
        assert output.finish is None
    assert not gate.active


def test_start_uploads_preroll_once_and_end_does_not_repeat_utterance() -> None:
    gate = SpeechGate()
    preroll, incremental = frame(550), frame()
    # The start window's INFERENCE event precedes START and must be discarded.
    assert not gate.feed(event(vad.VADEventType.INFERENCE_DONE, incremental)).frames
    start = gate.feed(event(vad.VADEventType.START_OF_SPEECH, preroll))
    assert start.frames == (preroll,)
    assert start.started
    assert gate.feed(event(vad.VADEventType.INFERENCE_DONE, incremental)).frames == (incremental,)
    end = gate.feed(event(vad.VADEventType.END_OF_SPEECH, preroll, incremental))
    assert not end.frames
    assert end.finish == "commit"
    assert gate.feed(event(vad.VADEventType.END_OF_SPEECH)).finish is None
    assert not gate.feed(event(vad.VADEventType.INFERENCE_DONE, incremental)).frames


def test_lost_connection_discards_inflight_speech_until_fresh_start() -> None:
    gate = SpeechGate()
    start = event(vad.VADEventType.START_OF_SPEECH, frame(500))
    inference = event(vad.VADEventType.INFERENCE_DONE, frame())
    assert gate.feed(start).frames
    assert not gate.feed(inference, ready=False).frames
    assert not gate.feed(inference, ready=True).frames
    assert gate.feed(event(vad.VADEventType.END_OF_SPEECH)).finish is None
    assert gate.feed(start).frames
    gate.reset()
    assert not gate.feed(inference).frames


def test_speech_started_offline_is_not_resumed_midword() -> None:
    gate = SpeechGate()
    assert not gate.feed(event(vad.VADEventType.START_OF_SPEECH, frame(500)), ready=False).frames
    assert not gate.feed(event(vad.VADEventType.INFERENCE_DONE, frame())).frames
    assert gate.feed(event(vad.VADEventType.END_OF_SPEECH)).finish is None


def test_tiny_buffer_is_cleared_instead_of_committed_or_carried_forward() -> None:
    gate = SpeechGate()
    gate.feed(event(vad.VADEventType.START_OF_SPEECH, frame(20)))
    assert gate.feed(event(vad.VADEventType.END_OF_SPEECH)).finish == "clear"
    gate.feed(event(vad.VADEventType.START_OF_SPEECH, frame(500)))
    assert gate.feed(event(vad.VADEventType.END_OF_SPEECH)).finish == "commit"
