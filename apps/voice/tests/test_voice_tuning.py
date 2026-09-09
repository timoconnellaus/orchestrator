import json
import math
from pathlib import Path
from unittest.mock import Mock

import pytest
from livekit.plugins import silero
from livekit.plugins.silero.vad import _VADOptions

from orchestrator_voice.config import Settings, turn_handling
from orchestrator_voice.live_transcribe import LiveTranscribeSTT
from orchestrator_voice.voice_tuning import VoiceTuning, job_metadata
from orchestrator_voice.worker import make_session

CONTRACT = json.loads(
    (Path(__file__).parents[3] / "docs/fixtures/voice-tuning-v1.json").read_text()
)


def test_voice_tuning_cross_language_contract() -> None:
    assert VoiceTuning().wire() == CONTRACT["defaults"]
    for patch in CONTRACT["validOverrides"]:
        value = CONTRACT["defaults"] | patch
        assert VoiceTuning.parse(value).wire() == value
    for patch in CONTRACT["invalidOverrides"] + [
        {"activationThreshold": math.nan},
        {"activationThreshold": math.inf},
    ]:
        with pytest.raises(ValueError):
            VoiceTuning.parse(CONTRACT["defaults"] | patch)
    with pytest.raises(ValueError):
        VoiceTuning.parse({})


@pytest.mark.parametrize(
    "metadata",
    [
        "x" * 4097,
        '{"unknown":1}',
        '{"voiceTuning":null}',
        '{"room":true}',
        '{"speaker":""}',
        '{"conversationId":"' + "x" * 257 + '"}',
    ],
)
def test_dispatch_rejects_oversize_and_unknown(metadata: str) -> None:
    with pytest.raises(ValueError):
        job_metadata(metadata)


def test_legacy_and_full_dispatch() -> None:
    assert job_metadata("") == {}
    assert Settings().conversation_for_job('{"conversationId":"main"}') == "main"
    value = {
        "conversationId": "main",
        "room": "room",
        "speaker": "phone",
        "voiceTuning": VoiceTuning().wire(),
    }
    assert job_metadata(json.dumps(value)) == value


async def test_per_job_all_options_and_hysteresis_are_reset_before_shared_consumers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test")
    detector = Mock(spec=silero.VAD)
    custom = VoiceTuning(
        activationThreshold=0.8, minSpeechMs=300, endSilenceMs=1200, interruptionMs=900
    )
    first = make_session(Settings(), detector, custom)
    detector.update_options.assert_called_once_with(
        activation_threshold=0.8,
        deactivation_threshold=0.65,
        min_speech_duration=0.3,
        min_silence_duration=1.2,
    )
    assert first.vad is detector
    assert isinstance(first.stt, LiveTranscribeSTT)
    assert first.stt._vad is detector
    second = make_session(Settings(), detector)
    detector.update_options.assert_called_with(
        activation_threshold=0.5,
        deactivation_threshold=0.35,
        min_speech_duration=0.05,
        min_silence_duration=0.55,
    )
    assert second.vad is detector
    assert turn_handling(custom).get("interruption", {}).get("min_duration") == 0.9
    assert turn_handling(custom).get("endpointing") == turn_handling().get("endpointing")
    assert turn_handling(custom).get("preemptive_generation") == {
        "enabled": False,
        "preemptive_tts": False,
    }


async def test_pinned_silero_update_options_hysteresis_and_safety_defaults() -> None:
    # Real pinned VAD options/update seam, but no ONNX/model loading or inference.
    opts = _VADOptions(
        min_speech_duration=0.05,
        min_silence_duration=0.55,
        prefix_padding_duration=0.5,
        max_buffered_speech=60,
        activation_threshold=0.5,
        deactivation_threshold=0.35,
        sample_rate=16000,
    )
    detector = silero.VAD(session=Mock(), opts=opts)
    detector.update_options(activation_threshold=0.8)
    assert opts.deactivation_threshold == 0.35  # SDK does not recompute it.
    VoiceTuning(activationThreshold=0.8).apply(detector)
    assert opts.deactivation_threshold == 0.65
    VoiceTuning().apply(detector)
    assert opts.activation_threshold == 0.5 and opts.deactivation_threshold == 0.35
    assert opts.min_speech_duration == 0.05 and opts.min_silence_duration == 0.55
    assert opts.prefix_padding_duration == 0.5 and opts.max_buffered_speech == 60
