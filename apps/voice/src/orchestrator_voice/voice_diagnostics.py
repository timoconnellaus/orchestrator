"""Ephemeral latest-state diagnostics: one sender, no audio-loop network awaits."""

import asyncio
import json
import math
import re
import time
from collections.abc import Awaitable, Callable
from uuid import uuid4

from livekit.agents import vad

from .voice_tuning import VoiceTuning

TOPIC = "orchestrator.voice.diagnostics.v1"
PROBE_TOPIC = "orchestrator.voice.probe.v1"


class VoiceDiagnostics:
    def __init__(
        self,
        tuning: VoiceTuning,
        *,
        available: bool,
        room: str,
        speaker: str,
        sender: str,
        send: Callable[[bytes], Awaitable[None]],
    ) -> None:
        self.tuning = tuning
        self.available = available
        self.room, self.speaker, self.sender = room, speaker, sender
        self.session = str(uuid4())
        self._send = send
        self._nonce = ""
        self._sequence = 0
        self._generation = 0
        self._level = 0.0
        self._speech = False
        self._updated = 0.0
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    def probe(self, data: bytes, identity: str) -> None:
        if identity != self.speaker or len(data) > 256:
            return
        try:
            value = json.loads(data)
            if not isinstance(value, dict) or value.keys() != {"nonce"}:
                return
            nonce = value["nonce"]
            if not isinstance(nonce, str) or not re.fullmatch(r"[a-zA-Z0-9-]{1,64}", nonce):
                return
        except (ValueError, UnicodeError):
            return
        if nonce != self._nonce:
            self._nonce = nonce
            self.reset()

    def reset(self) -> None:
        self._generation += 1
        self._level, self._speech, self._updated = 0.0, False, 0.0

    def observe(self, event: vad.VADEvent, *, speech: bool) -> None:
        self._speech = speech
        if event.type == vad.VADEventType.INFERENCE_DONE:
            # Only incremental inference frames, never START pre-roll/END history.
            count = sum(len(frame.data) for frame in event.frames)
            energy = sum(float(sample) ** 2 for frame in event.frames for sample in frame.data)
            self._level = min(1.0, math.sqrt(energy / count) / 32768) if count else 0.0
            self._updated = time.monotonic()

    def packet(self) -> bytes:
        fresh = self.available and time.monotonic() - self._updated < 1.5
        self._sequence += 1
        return json.dumps(
            {
                "version": 1,
                "room": self.room,
                "speaker": self.speaker,
                "sender": self.sender,
                "session": self.session,
                "nonce": self._nonce,
                "sequence": self._sequence,
                "generation": self._generation,
                "sentAtMs": int(time.time() * 1000),
                "available": self.available,
                "fresh": fresh,
                "level": self._level if fresh else 0.0,
                "speech": self._speech if fresh else False,
                "voiceTuning": self.tuning.wire(),
            },
            allow_nan=False,
            separators=(",", ":"),
        ).encode()

    def start(self) -> None:
        if self._task is None and not self._closed:
            self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            if self._nonce:
                try:
                    await asyncio.wait_for(self._send(self.packet()), timeout=0.15)
                except Exception:
                    # Cancelling the pinned SDK waiter does not cancel its native
                    # publication. Disable diagnostics for this join after any
                    # ambiguous failure; a new send could otherwise accumulate
                    # native requests. Speech continues; the phone expires meters.
                    return
            await asyncio.sleep(0.2)

    async def aclose(self) -> None:
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        self.reset()
