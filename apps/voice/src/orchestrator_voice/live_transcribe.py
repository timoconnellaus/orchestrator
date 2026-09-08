"""Locally gated live captions; one non-retrying provider connection per utterance."""

import asyncio
import os
import time
import weakref
from collections import deque
from dataclasses import dataclass, field, replace
from functools import partial

import aiohttp
from livekit import rtc
from livekit.agents import (
    DEFAULT_API_CONNECT_OPTIONS,
    NOT_GIVEN,
    APIConnectionError,
    APIConnectOptions,
    NotGivenOr,
    stt,
    utils,
    vad,
)
from livekit.agents.language import LanguageCode

from .realtime_stt import SpeechGate
from .transcription_turn import MODEL as MODEL
from .transcription_turn import TranscriptionTurn


class LiveTranscribeSTT(stt.STT[None]):
    def __init__(
        self,
        *,
        vad: vad.VAD,
        language: str = "en",
        api_key: str | None = None,
        http_session: aiohttp.ClientSession | None = None,
        url: str = "wss://api.openai.com/v1/realtime?intent=transcription",
    ) -> None:
        super().__init__(
            capabilities=stt.STTCapabilities(
                streaming=True,
                interim_results=True,
                offline_recognize=False,
            )
        )
        self._vad = vad
        self._language = language
        self._api_key = api_key if api_key is not None else os.environ.get("OPENAI_API_KEY", "")
        if not self._api_key:
            raise ValueError("OPENAI_API_KEY is required for live transcription")
        self._http_session = http_session
        self._url = url
        self._enabled = True
        self._streams: weakref.WeakSet[_LiveStream] = weakref.WeakSet()

    @property
    def model(self) -> str:
        return MODEL

    @property
    def provider(self) -> str:
        return "openai"

    def set_capture_enabled(self, enabled: bool) -> None:
        if self._enabled == enabled:
            return
        self._enabled = enabled
        for stream in list(self._streams):
            stream.capture_boundary(require_silence=False)

    async def _recognize_impl(
        self,
        buffer: utils.AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions,
    ) -> stt.SpeechEvent:
        raise NotImplementedError("Live transcription requires a stream")

    def stream(
        self,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> stt.SpeechStream:
        stream = _LiveStream(
            self,
            language=language if isinstance(language, str) else self._language,
            conn_options=conn_options,
        )
        self._streams.add(stream)
        return stream

    async def aclose(self) -> None:
        await asyncio.gather(*(stream.aclose() for stream in list(self._streams)))


@dataclass
class _CapturePipeline:
    generation: int
    stream: vad.VADStream
    require_silence: bool
    submitted: float = 0
    processed: float = 0
    gate: SpeechGate = field(default_factory=SpeechGate)
    task: asyncio.Task[None] | None = None


class _LiveStream(stt.SpeechStream):
    class _Boundary(stt.SpeechStream._FlushSentinel):
        pass

    def __init__(
        self, owner: LiveTranscribeSTT, *, language: str, conn_options: APIConnectOptions
    ) -> None:
        # No base resampler: one push maps to exactly one generation stamp.
        # Admitted audio is resampled inside its individual provider turn.
        super().__init__(stt=owner, conn_options=replace(conn_options, max_retry=0))
        self._owner = owner
        self._language = LanguageCode(language)
        self._generation = 0
        self._require_silence = False
        self._stamps: deque[tuple[int, float]] = deque()
        self._queued_seconds = 0.0
        self._overflowed = False
        self._last_audio = time.monotonic()
        self._pipeline: _CapturePipeline | None = None
        self._current: TranscriptionTurn | None = None
        self._turns: asyncio.Queue[TranscriptionTurn | None] = asyncio.Queue(maxsize=5)
        self._all_turns: set[TranscriptionTurn] = set()
        self._pending_turns = 0
        self._vad_failed: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    def capture_boundary(self, *, require_silence: bool) -> None:
        if self._input_ch.closed:
            return
        self._generation += 1
        self._require_silence = require_silence
        if self._current is not None and not self._current.ended:
            self._current.abort("Unfinished speech was discarded at a capture boundary")
        self._current = None
        self._input_ch.send_nowait(self._Boundary())

    def push_frame(self, frame: rtc.AudioFrame) -> None:
        if self._overflowed and self._queued_seconds > 0.5:
            return  # Already invalidated this entire capture generation.
        self._overflowed = False
        if frame.num_channels != 1:
            raise ValueError("Microphone audio must be mono")
        super().push_frame(frame)
        seconds = frame.samples_per_channel / frame.sample_rate
        self._stamps.append((self._generation, seconds))
        self._queued_seconds += seconds
        self._last_audio = time.monotonic()
        if self._queued_seconds > 2:
            self._overflowed = True
            self.capture_boundary(require_silence=True)
            self._warning("Microphone input fell behind; please repeat after a pause")

    def _warning(self, text: str) -> None:
        self._emit_error(APIConnectionError(text), recoverable=True)

    def _speech(self, turn: TranscriptionTurn, text: str, *, final: bool) -> None:
        self._event_ch.send_nowait(
            stt.SpeechEvent(
                type=stt.SpeechEventType.FINAL_TRANSCRIPT
                if final
                else stt.SpeechEventType.INTERIM_TRANSCRIPT,
                request_id=turn.id,
                alternatives=[stt.SpeechData(language=self._language, text=text)],
            )
        )

    async def _run(self) -> None:
        tasks = [
            asyncio.create_task(self._audio()),
            asyncio.create_task(self._output()),
            asyncio.create_task(self._watchdog()),
        ]
        try:
            done, _ = await asyncio.wait(
                [*tasks, self._vad_failed], return_when=asyncio.FIRST_COMPLETED
            )
            if self._vad_failed in done:
                await self._vad_failed
            for task in tasks:
                if task in done:
                    await task
            await tasks[0]
            await tasks[1]
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._stop_vad()
            if self._vad_failed.done() and not self._vad_failed.cancelled():
                self._vad_failed.exception()
            await asyncio.gather(*(turn.aclose() for turn in list(self._all_turns)))

    async def _stop_vad(self) -> None:
        pipeline, self._pipeline = self._pipeline, None
        if pipeline is None:
            return
        if pipeline.task is not None:
            pipeline.task.cancel()
            await asyncio.gather(pipeline.task, return_exceptions=True)
        await pipeline.stream.aclose()

    def _observe_vad(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and (error := task.exception()) is not None:
            if not self._vad_failed.done():
                self._vad_failed.set_exception(error)

    async def _audio(self) -> None:
        async for frame in self._input_ch:
            if isinstance(frame, self._Boundary):
                await self._stop_vad()
                continue
            if isinstance(frame, self._FlushSentinel):
                continue  # Flush is not an end-of-speech or capture-generation boundary.
            generation, seconds = self._stamps.popleft()
            self._queued_seconds -= seconds
            if generation != self._generation or not self._owner._enabled:
                continue
            if self._pipeline is None:
                pipeline = _CapturePipeline(
                    generation, self._owner._vad.stream(), self._require_silence
                )
                self._pipeline = pipeline
                pipeline.task = asyncio.create_task(self._vad_events(pipeline))
                pipeline.task.add_done_callback(self._observe_vad)
            pipeline = self._pipeline
            if pipeline.submitted - pipeline.processed > 2:
                self.capture_boundary(require_silence=True)
                self._warning("Speech detection fell behind; please repeat after a pause")
                continue
            pipeline.submitted += seconds
            pipeline.stream.push_frame(frame)
        if self._pipeline is not None:
            self._pipeline.stream.end_input()
            if self._pipeline.task is not None:
                await self._pipeline.task
        if self._current is not None and not self._current.ended:
            self._current.abort("Audio ended before the utterance was complete")
        self._turns.put_nowait(None)

    async def _vad_events(self, pipeline: _CapturePipeline) -> None:
        async for event in pipeline.stream:
            pipeline.processed = event.timestamp
            if pipeline.generation != self._generation or not self._owner._enabled:
                continue
            if pipeline.submitted - pipeline.processed > 1:
                self.capture_boundary(require_silence=True)
                self._warning("Stale microphone audio was discarded; please repeat after a pause")
                continue
            if pipeline.require_silence:
                if not event.speaking and event.silence_duration >= 0.55:
                    pipeline.require_silence = False
                continue
            action = pipeline.gate.feed(event)
            if action.started:
                if self._pending_turns >= 4:
                    pipeline.gate.reset()
                    self._warning("Transcription is busy; please repeat after a pause")
                    continue
                turn = TranscriptionTurn(
                    session=self._owner._http_session or utils.http_context.http_session(),
                    url=self._owner._url,
                    api_key=self._owner._api_key,
                    language=str(self._language),
                )
                self._current = turn
                self._all_turns.add(turn)
                self._pending_turns += 1
                turn._task.add_done_callback(partial(self._turn_closed, turn))
                self._turns.put_nowait(turn)
            if self._current is None:
                continue
            for frame in action.frames:
                self._current.push(frame)
            if action.finish:
                if action.finish == "commit":
                    self._current.finish()
                else:
                    self._current.abort("Speech was too short to transcribe")
                self._current = None

    def _turn_closed(self, turn: TranscriptionTurn, task: asyncio.Task[None]) -> None:
        self._all_turns.discard(turn)
        if not task.cancelled() and task.exception() is not None and not self._event_ch.closed:
            self._warning("Speech connection cleanup failed")

    async def _output(self) -> None:
        while (turn := await self._turns.get()) is not None:
            self._event_ch.send_nowait(stt.SpeechEvent(type=stt.SpeechEventType.START_OF_SPEECH))
            previous = ""
            while not turn.done:
                turn.changed.clear()
                if turn.partial != previous:
                    self._speech(turn, turn.partial, final=False)
                    previous = turn.partial
                await turn.changed.wait()
            if turn.error is None and turn.final:
                self._speech(turn, turn.final, final=True)
            else:
                # Empty FINAL is ignored by the SDK. Empty INTERIM clears its
                # interim buffer and reaches RoomIO without creating a command.
                self._speech(turn, "", final=False)
                if turn.error is not None:
                    self._warning(turn.error)
            self._event_ch.send_nowait(stt.SpeechEvent(type=stt.SpeechEventType.END_OF_SPEECH))
            self._pending_turns -= 1

    async def _watchdog(self) -> None:
        while True:
            await asyncio.sleep(0.25)
            if self._current is not None and not self._current.ended:
                if time.monotonic() - self._last_audio > 2:
                    self.capture_boundary(require_silence=True)
                    self._warning("Microphone frames stopped; unfinished speech was discarded")
