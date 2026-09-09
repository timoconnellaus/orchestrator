"""Version-one join snapshot. Wire durations are integer milliseconds."""

import json
import math
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class VoiceTuning:
    version: int = 1
    activationThreshold: float = 0.5
    minSpeechMs: int = 50
    endSilenceMs: int = 550
    interruptionMs: int = 500
    elevenLabsSpeed: float = 1.0
    echoCancellation: bool = True
    noiseSuppression: bool = True
    autoGainControl: bool = True

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != 1:
            raise ValueError("Unsupported voice tuning version")
        for name, low, high in (
            ("activationThreshold", 0.3, 0.8),
            ("elevenLabsSpeed", 0.8, 1.2),
            ("minSpeechMs", 50, 300),
            ("endSilenceMs", 300, 1200),
            ("interruptionMs", 300, 1200),
        ):
            value = getattr(self, name)
            types = (int, float) if name in ("activationThreshold", "elevenLabsSpeed") else (int,)
            if type(value) not in types or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"Invalid {name}")
        for name in ("echoCancellation", "noiseSuppression", "autoGainControl"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"Invalid {name}")

    def wire(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def parse(cls, value: object) -> "VoiceTuning":
        if not isinstance(value, dict) or value.keys() != cls().wire().keys():
            raise ValueError("Voice tuning requires exactly the version-one fields")
        return cls(**value)

    def apply(self, vad: Any) -> None:
        # Silero update_options does NOT derive hysteresis. Set every exposed
        # option before either consumer creates streams, even for default jobs.
        vad.update_options(
            activation_threshold=self.activationThreshold,
            deactivation_threshold=max(self.activationThreshold - 0.15, 0.01),
            min_speech_duration=self.minSpeechMs / 1000,
            min_silence_duration=self.endSilenceMs / 1000,
        )


def job_metadata(metadata: str) -> dict[str, Any]:
    if len(metadata.encode("utf-8")) > 4096:
        raise ValueError("Dispatch metadata too large")
    value = json.loads(metadata) if metadata else {}
    if not isinstance(value, dict) or value.keys() - {
        "conversationId",
        "voiceTuning",
        "room",
        "speaker",
    }:
        raise ValueError("Invalid dispatch metadata fields")
    for key in ("conversationId", "room", "speaker"):
        if key in value and (
            not isinstance(value[key], str) or not value[key].strip() or len(value[key]) > 256
        ):
            raise ValueError(f"Invalid dispatch {key}")
    if "voiceTuning" in value:
        VoiceTuning.parse(value["voiceTuning"])
    return value
