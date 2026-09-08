"""Speech-only upload primitives for transcription-only Realtime sessions."""

from dataclasses import dataclass
from typing import Literal

from livekit import rtc
from livekit.agents import vad


@dataclass(frozen=True)
class GatedAudio:
    frames: tuple[rtc.AudioFrame, ...] = ()
    finish: Literal["commit", "clear"] | None = None
    started: bool = False


class SpeechGate:
    """Admit Silero's start pre-roll once, then incremental speech windows.

    Silero emits INFERENCE_DONE before START/END for the same window. Its
    END event repeats the entire utterance; uploading that would duplicate
    audio. Disconnect resets admission until a fresh START, never replaying a
    partially uploaded utterance into another provider session.
    """

    def __init__(self) -> None:
        self.active = False
        self._seconds = 0.0

    def reset(self) -> None:
        self.active = False
        self._seconds = 0.0

    def feed(self, event: vad.VADEvent, *, ready: bool = True) -> GatedAudio:
        if not ready:
            self.reset()
            return GatedAudio()
        started = event.type == vad.VADEventType.START_OF_SPEECH
        if started:
            self.active = True
            self._seconds = 0.0
        if self.active and event.type in (
            vad.VADEventType.START_OF_SPEECH,
            vad.VADEventType.INFERENCE_DONE,
        ):
            frames = tuple(event.frames)
            self._seconds += sum(f.samples_per_channel / f.sample_rate for f in frames)
            return GatedAudio(frames=frames, started=started)
        if self.active and event.type == vad.VADEventType.END_OF_SPEECH:
            # Avoid provider errors on tiny buffers, and don't leak them into the next turn.
            finish: Literal["commit", "clear"] = "commit" if self._seconds >= 0.1 else "clear"
            self.reset()
            return GatedAudio(finish=finish)
        return GatedAudio()
