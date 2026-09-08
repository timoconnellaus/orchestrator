"""One bounded, non-retrying transcription-only connection per utterance."""

import asyncio
import base64
import json
import time
from typing import Any
from uuid import uuid4

import aiohttp
from livekit import rtc

MODEL = "gpt-live-transcribe"
SAMPLE_RATE = 24000
MAX_TURN_SECONDS = 120
FINAL_TIMEOUT = 10


class TranscriptionTurn:
    def __init__(
        self, *, session: aiohttp.ClientSession, url: str, api_key: str, language: str
    ) -> None:
        self.id = uuid4().hex
        self.partial = ""
        self.final: str | None = None
        self.error: str | None = None
        self.ended = False
        self.done = False
        self.changed = asyncio.Event()
        self._session = session
        self._url = url
        self._api_key = api_key
        self._language = language
        self._audio: asyncio.Queue[rtc.AudioFrame | None] = asyncio.Queue(maxsize=256)
        self._queued_seconds = 0.0
        self._upload_started = False
        self._commit_sent = False
        self._deadline: float | None = None
        self._item: str | None = None
        self._task = asyncio.create_task(self._run())

    def push(self, frame: rtc.AudioFrame) -> None:
        if self.done or self.ended:
            return
        seconds = frame.samples_per_channel / frame.sample_rate
        # The five-second setup deadline needs room for onset pre-roll too.
        # Once upload catches up, enforce the tighter steady-state backlog bound.
        limit = 2 if self._upload_started else 6
        if self._queued_seconds + seconds > limit or self._audio.full():
            self.abort("Speech upload fell behind; please repeat the unfinished turn")
            return
        self._queued_seconds += seconds
        self._audio.put_nowait(frame)

    def finish(self) -> None:
        if self.done or self.ended:
            return
        if self._audio.full():
            self.abort("Speech upload fell behind; please repeat the unfinished turn")
            return
        self.ended = True
        self._audio.put_nowait(None)

    def abort(self, reason: str) -> None:
        if self.done:
            return
        self.error = reason
        self.done = True
        self.changed.set()
        self._task.cancel()

    async def aclose(self) -> None:
        if not self.done:
            self.abort("Voice connection closed before transcription finished")
        await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self) -> None:
        socket: aiohttp.ClientWebSocketResponse | None = None
        try:
            async with asyncio.timeout(MAX_TURN_SECONDS):
                async with asyncio.timeout(5):
                    socket = await self._session.ws_connect(
                        self._url,
                        headers={"Authorization": f"Bearer {self._api_key}"},
                        heartbeat=20,
                        max_msg_size=1024 * 1024,
                    )
                    await self._configure(socket)
                async with asyncio.TaskGroup() as tasks:
                    tasks.create_task(self._send(socket))
                    tasks.create_task(self._receive_result(socket))
        except asyncio.CancelledError:
            self.error = self.error or "Unfinished speech was discarded"
        except Exception as error:
            # Record only a local exception class, never provider payloads/headers.
            cause: BaseException = error
            while isinstance(cause, BaseExceptionGroup):
                cause = cause.exceptions[0]
            self.error = f"Speech transcription failed ({type(cause).__name__}); please repeat"
        finally:
            # Deliver a final immediately; socket close handshakes are cleanup,
            # not part of recognition latency. The owner still tracks this task.
            self.done = True
            self.changed.set()
            if socket is not None:
                try:
                    await socket.close()
                except (aiohttp.ClientError, ConnectionError, TimeoutError):
                    pass

    async def _event(self, socket: aiohttp.ClientWebSocketResponse) -> dict[str, Any]:
        message = await socket.receive(timeout=0.25)
        if message.type != aiohttp.WSMsgType.TEXT:
            raise ConnectionError("Transcription connection closed")
        event = json.loads(message.data)
        if not isinstance(event, dict):
            raise ValueError("Invalid transcription event")
        if event.get("type") in ("error", "conversation.item.input_audio_transcription.failed"):
            raise ValueError("Transcription rejected")
        return event

    async def _configure(self, socket: aiohttp.ClientWebSocketResponse) -> None:
        await socket.send_json(
            {
                "type": "session.update",
                "session": {
                    "type": "transcription",
                    "audio": {
                        "input": {
                            "format": {"type": "audio/pcm", "rate": SAMPLE_RATE},
                            "transcription": {
                                "model": MODEL,
                                "languages": [self._language],
                                "delay": "low",
                            },
                            "turn_detection": None,
                        }
                    },
                },
            }
        )
        while True:
            try:
                event = await self._event(socket)
            except TimeoutError:
                continue
            if event.get("type") == "session.updated":
                return

    async def _append(self, socket: aiohttp.ClientWebSocketResponse, frame: rtc.AudioFrame) -> None:
        async with asyncio.timeout(5):
            await socket.send_json(
                {
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(frame.data.tobytes()).decode("ascii"),
                }
            )

    async def _send(self, socket: aiohttp.ClientWebSocketResponse) -> None:
        resampler: rtc.AudioResampler | None = None
        while True:
            frame = await self._audio.get()
            if frame is None:
                break
            self._queued_seconds -= frame.samples_per_channel / frame.sample_rate
            if self._queued_seconds <= 2:
                self._upload_started = True
            if frame.num_channels != 1:
                raise ValueError("Microphone audio must be mono")
            if frame.sample_rate != SAMPLE_RATE and resampler is None:
                resampler = rtc.AudioResampler(frame.sample_rate, SAMPLE_RATE)
            frames = resampler.push(frame) if resampler is not None else [frame]
            for converted in frames:
                await self._append(socket, converted)
        if resampler is not None:
            for converted in resampler.flush():
                await self._append(socket, converted)
        self._commit_sent = True
        self._deadline = time.monotonic() + FINAL_TIMEOUT
        async with asyncio.timeout(5):
            await socket.send_json({"type": "input_audio_buffer.commit"})

    async def _receive_result(self, socket: aiohttp.ClientWebSocketResponse) -> None:
        acknowledged = False
        completed: str | None = None
        while True:
            # Checked on every iteration, even when unrelated events keep arriving.
            if self._deadline is not None and time.monotonic() >= self._deadline:
                raise TimeoutError("Transcription did not finalize")
            try:
                event = await self._event(socket)
            except TimeoutError:
                continue
            kind = event.get("type")
            if kind not in (
                "input_audio_buffer.committed",
                "conversation.item.input_audio_transcription.delta",
                "conversation.item.input_audio_transcription.completed",
            ):
                continue
            item = event.get("item_id")
            if not isinstance(item, str) or not item or len(item) > 256:
                raise ValueError("Missing transcription identity")
            if self._item is None:
                self._item = item
            if item != self._item:
                raise ValueError("Unexpected second transcription item")
            if kind == "input_audio_buffer.committed":
                if not self._commit_sent:
                    raise ValueError("Unexpected transcription commit")
                acknowledged = True
            elif kind == "conversation.item.input_audio_transcription.delta":
                delta = event.get("delta")
                if isinstance(delta, str):
                    self.partial += delta
                    if len(self.partial) > 65536:
                        raise ValueError("Transcription too long")
                    self.changed.set()
            else:
                text = event.get("transcript")
                if not isinstance(text, str) or len(text) > 65536:
                    raise ValueError("Invalid final transcription")
                completed = text
            if self._commit_sent and acknowledged and completed is not None:
                self.final = completed
                return
