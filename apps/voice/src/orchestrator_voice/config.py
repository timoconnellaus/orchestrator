from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from livekit.agents.voice.turn import TurnHandlingOptions

from .bridge import BridgeLimits

AGENT_NAME = "orchestrator-voice"


def turn_handling() -> TurnHandlingOptions:
    return {
        "turn_detection": "vad",
        "endpointing": {"mode": "fixed", "min_delay": 0.8, "max_delay": 3.0},
        "preemptive_generation": {"enabled": False, "preemptive_tts": False},
        "interruption": {
            "enabled": True,
            "mode": "vad",
            "min_duration": 0.5,
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
    stt_model: str = "gpt-4o-mini-transcribe"
    stt_language: str = "en"
    tts_model: str = "gpt-4o-mini-tts"
    tts_voice: str = "coral"
    limits: BridgeLimits = BridgeLimits()

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
            stt_model=env.get("OPENAI_STT_MODEL", "gpt-4o-mini-transcribe"),
            stt_language=env.get("OPENAI_STT_LANGUAGE", "en"),
            tts_model=env.get("OPENAI_TTS_MODEL", "gpt-4o-mini-tts"),
            tts_voice=env.get("OPENAI_TTS_VOICE", "coral"),
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
        value = json.loads(metadata)
        if not isinstance(value, dict):
            raise ValueError("Dispatch metadata must be a JSON object")
        conversation = value.get("conversationId", self.conversation_id)
        if not isinstance(conversation, str) or not conversation.strip():
            raise ValueError("Dispatch conversationId must be a nonempty string")
        return conversation
