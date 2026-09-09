from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from livekit.agents.voice.turn import TurnHandlingOptions

from .bridge import BridgeLimits
from .voice_tuning import VoiceTuning, job_metadata

AGENT_NAME = "orchestrator-voice"
DEFAULT_VOICE_TUNING = VoiceTuning()


def turn_handling(tuning: VoiceTuning = DEFAULT_VOICE_TUNING) -> TurnHandlingOptions:
    return {
        "turn_detection": "vad",
        "endpointing": {"mode": "fixed", "min_delay": 0.8, "max_delay": 3.0},
        "preemptive_generation": {"enabled": False, "preemptive_tts": False},
        "interruption": {
            "enabled": True,
            "mode": "vad",
            "min_duration": tuning.interruptionMs / 1000,
            "min_words": 0,
            "resume_false_interruption": False,
            "false_interruption_timeout": None,
        },
        "user_turn_limit": {"max_words": None, "max_duration": None},
    }


@dataclass(frozen=True)
class Settings:
    backend_url: str = "http://127.0.0.1:8787/"
    conversation_id: str = "main"
    stt_model: str = "gpt-live-transcribe"
    stt_language: str = "en"
    tts_model: str = "gpt-4o-mini-tts"
    tts_voice: str = "coral"
    limits: BridgeLimits = BridgeLimits()
    tts_provider: str = "openai"
    elevenlabs_model: str = "eleven_flash_v2_5"
    elevenlabs_voice_id: str = ""

    def __post_init__(self) -> None:
        if self.tts_provider not in ("openai", "elevenlabs"):
            raise ValueError("TTS_PROVIDER must be openai or elevenlabs")
        if self.tts_provider == "elevenlabs":
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", self.elevenlabs_voice_id):
                raise ValueError("ELEVEN_TTS_VOICE_ID must be a nonempty, URL-safe voice ID")
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", self.elevenlabs_model):
                raise ValueError("ELEVEN_TTS_MODEL must be a nonempty model ID")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        url = env.get("ORCHESTRATOR_URL", "http://127.0.0.1:8787").rstrip("/") + "/"
        parsed = urlsplit(url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("ORCHESTRATOR_URL must be HTTP(S), without credentials/query/fragment")
        conversation_id = env.get("ORCHESTRATOR_CONVERSATION_ID", "main")
        if not conversation_id.strip():
            raise ValueError("ORCHESTRATOR_CONVERSATION_ID must not be empty")
        return cls(
            backend_url=url,
            conversation_id=conversation_id,
            stt_model=env.get("OPENAI_STT_MODEL", "gpt-live-transcribe"),
            stt_language=env.get("OPENAI_STT_LANGUAGE", "en"),
            tts_model=env.get("OPENAI_TTS_MODEL", "gpt-4o-mini-tts"),
            tts_voice=env.get("OPENAI_TTS_VOICE", "coral"),
            tts_provider=env.get("TTS_PROVIDER", "openai"),
            elevenlabs_model=env.get("ELEVEN_TTS_MODEL", "eleven_flash_v2_5"),
            elevenlabs_voice_id=env.get("ELEVEN_TTS_VOICE_ID", ""),
            limits=BridgeLimits(
                request_timeout=float(env.get("VOICE_REQUEST_TIMEOUT_SECONDS", "10")),
                submission_timeout=float(env.get("VOICE_SUBMISSION_TIMEOUT_SECONDS", "25")),
                poll_timeout=float(env.get("VOICE_POLL_TIMEOUT_SECONDS", "300")),
                poll_interval=float(env.get("VOICE_POLL_INTERVAL_SECONDS", "1")),
            ),
        )

    def conversation_for_job(self, metadata: str) -> str:
        if not metadata:
            return self.conversation_id
        value = job_metadata(metadata)
        if not isinstance(value, dict):
            raise ValueError("Dispatch metadata must be a JSON object")
        conversation = value.get("conversationId", self.conversation_id)
        if not isinstance(conversation, str) or not conversation.strip():
            raise ValueError("Dispatch conversationId must be a nonempty string")
        return conversation
