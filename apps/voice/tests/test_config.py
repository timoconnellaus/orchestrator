import math

import pytest

from orchestrator_voice.bridge import BridgeLimits
from orchestrator_voice.config import Settings, turn_handling


def test_defaults_and_no_speculation_or_idle_limits():
    settings = Settings.from_env({})
    assert settings.backend_url == "http://127.0.0.1:8787/"
    assert settings.conversation_id == "main"
    assert settings.stt_model == "gpt-live-transcribe"
    assert settings.tts_model == "gpt-4o-mini-tts"
    assert settings.tts_voice == "coral"
    assert settings.tts_provider == "openai"
    options = turn_handling()
    assert options.get("turn_detection") == "vad"
    assert options.get("preemptive_generation") == {"enabled": False, "preemptive_tts": False}
    interruption = options.get("interruption", {})
    assert interruption.get("enabled") is True
    assert interruption.get("resume_false_interruption") is False
    assert options.get("user_turn_limit") == {"max_words": None, "max_duration": None}


def test_backend_prefix_conversation_and_speech_overrides():
    settings = Settings.from_env(
        {
            "ORCHESTRATOR_URL": "https://tailnet.test/control/",
            "ORCHESTRATOR_CONVERSATION_ID": "session:env-id",
            "OPENAI_STT_MODEL": "gpt-transcribe",
            "OPENAI_STT_LANGUAGE": "fr",
            "OPENAI_TTS_MODEL": "tts-1",
            "OPENAI_TTS_VOICE": "ash",
            "VOICE_POLL_TIMEOUT_SECONDS": "600",
        }
    )
    assert settings.backend_url == "https://tailnet.test/control/"
    assert settings.conversation_for_job("") == "session:env-id"
    metadata = '{"conversationId":"opaque/dispatch"}'
    assert settings.conversation_for_job(metadata) == "opaque/dispatch"
    assert settings.stt_model == "gpt-transcribe"
    assert settings.stt_language == "fr"
    assert settings.tts_model == "tts-1"
    assert settings.tts_voice == "ash"
    assert settings.limits.poll_timeout == 600


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/db",
        "http://",
        "https://user:secret@test/",
        "http://test/?x=y",
        "http://test/#x",
    ],
)
def test_invalid_backend_url_is_rejected(url):
    with pytest.raises(ValueError):
        Settings.from_env({"ORCHESTRATOR_URL": url})


@pytest.mark.parametrize(
    "metadata", ["not json", "[]", '{"conversationId":12}', '{"conversationId":""}']
)
def test_invalid_dispatch_metadata_fails_instead_of_misrouting(metadata):
    with pytest.raises(ValueError):
        Settings().conversation_for_job(metadata)


@pytest.mark.parametrize("duration", [0, -1, math.inf, math.nan])
def test_invalid_deadlines_fail_fast(duration):
    with pytest.raises(ValueError):
        BridgeLimits(poll_timeout=duration)


def test_elevenlabs_selection_keeps_openai_transcription_and_rollback_settings():
    settings = Settings.from_env(
        {"TTS_PROVIDER": "elevenlabs", "ELEVEN_TTS_VOICE_ID": "testVoiceId123"}
    )
    assert settings.tts_provider == "elevenlabs"
    assert settings.elevenlabs_voice_id == "testVoiceId123"
    assert settings.elevenlabs_model == "eleven_flash_v2_5"
    assert settings.stt_model == "gpt-live-transcribe"
    assert settings.tts_model == "gpt-4o-mini-tts"
    assert settings.tts_voice == "coral"


@pytest.mark.parametrize(
    "env",
    [
        {"TTS_PROVIDER": "typo"},
        {"TTS_PROVIDER": "elevenlabs"},
        {"TTS_PROVIDER": "elevenlabs", "ELEVEN_TTS_VOICE_ID": "bad/voice"},
        {"TTS_PROVIDER": "elevenlabs", "ELEVEN_TTS_VOICE_ID": "voice?query"},
        {"TTS_PROVIDER": "elevenlabs", "ELEVEN_TTS_VOICE_ID": "voice", "ELEVEN_TTS_MODEL": ""},
    ],
)
def test_invalid_tts_selection_fails_closed(env):
    with pytest.raises(ValueError):
        Settings.from_env(env)


@pytest.mark.parametrize("attempts", [0, 6])
def test_retry_count_is_bounded(attempts):
    with pytest.raises(ValueError):
        BridgeLimits(post_attempts=attempts)
