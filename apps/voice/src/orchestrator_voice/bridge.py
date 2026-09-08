"""HTTP-only durable operation bridge; consumer cancellation never cancels submission."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)
FailureKind = Literal[
    "rejected", "unknown_submission", "pending", "failed", "uncertain", "protocol", "partial"
]


class BridgeError(Exception):
    def __init__(self, kind: FailureKind, request_id: str, operation_id: str | None = None):
        self.kind = kind
        self.request_id = request_id
        self.operation_id = operation_id
        super().__init__(f"{kind}: request={request_id}, operation={operation_id}")

    @property
    def spoken_text(self) -> str:
        return {
            "partial": (
                "That partial reply could not be confirmed. "
                "Please check the app for the final result."
            ),
            "rejected": "Control rejected this request. Please check the app before trying again.",
            "unknown_submission": (
                "I couldn't confirm whether control accepted this request. "
                "Please check the app before repeating it; it may still run."
            ),
            "pending": (
                "I stopped waiting for the result. The request may still be running. "
                "Please check the app."
            ),
            "failed": "Control reported this request failed. Please check the app for details.",
            "uncertain": (
                "Control couldn't confirm the outcome. "
                "Please check the app before repeating the request."
            ),
            "protocol": (
                "I couldn't read control's result. The request may have run. Please check the app."
            ),
        }[self.kind]


@dataclass(frozen=True)
class Turn:
    id: str
    text: str
    conversation_id: str

    def payload(self) -> dict[str, str]:
        return {
            "id": self.id,
            "text": self.text,
            "conversationId": self.conversation_id,
            "source": "voice",
        }


@dataclass(frozen=True)
class BridgeLimits:
    request_timeout: float = 10.0
    submission_timeout: float = 25.0
    poll_timeout: float = 300.0
    poll_interval: float = 1.0
    post_attempts: int = 3
    ack_delay: float = 0.75

    def __post_init__(self) -> None:
        import math

        durations = (
            self.request_timeout,
            self.submission_timeout,
            self.poll_timeout,
            self.poll_interval,
            self.ack_delay,
        )
        if any(not math.isfinite(value) or value <= 0 for value in durations):
            raise ValueError("Bridge timeouts and polling interval must be finite and positive")
        if not 1 <= self.post_attempts <= 5:
            raise ValueError("post_attempts must be between 1 and 5")


class ControlBridge:
    def __init__(self, client: httpx.AsyncClient, limits: BridgeLimits | None = None):
        self.client = client
        self.limits = limits or BridgeLimits()
        self._submissions: set[asyncio.Task[str]] = set()
        self._closed = False
        self._spoken_requests: deque[str] = deque(maxlen=128)

    async def answer(self, turn: Turn) -> str:
        """Final text compatibility helper; production consumes stream_answer directly."""
        return "".join([text async for text in self.stream_answer(turn, acknowledge=False)])

    async def stream_answer(
        self, turn: Turn, *, acknowledge: bool = True
    ) -> AsyncGenerator[str, None]:
        if self._closed:
            raise RuntimeError("Bridge is closed")
        if turn.id in self._spoken_requests:
            return  # Never retry playback for a repeated SDK invocation.
        self._spoken_requests.append(turn.id)
        task = asyncio.create_task(self._submit(turn), name=f"submit-{turn.id}")
        self._submissions.add(task)
        task.add_done_callback(self._submission_done)
        operation_id = await asyncio.shield(task)
        prefix = ""
        sequence = 0
        events = self._reply_events(turn.id, operation_id)
        pending: asyncio.Future[dict[str, Any]] | None = None
        ack_due = asyncio.get_running_loop().time() + self.limits.ack_delay
        try:
            async with asyncio.timeout(self.limits.poll_timeout):
                while True:
                    pending = asyncio.ensure_future(anext(events))
                    if acknowledge:
                        done, _ = await asyncio.wait(
                            [pending], timeout=max(0, ack_due - asyncio.get_running_loop().time())
                        )
                        if not done:
                            acknowledge = False
                            yield "Got it.\n\n"
                    event = await pending
                    if event.get("operationId") != operation_id:
                        raise BridgeError("protocol", turn.id, operation_id)
                    seq = event.get("seq")
                    if type(seq) is not int or seq < 1:
                        raise BridgeError("protocol", turn.id, operation_id)
                    if seq <= sequence:
                        continue
                    if seq != sequence + 1:
                        raise BridgeError("protocol", turn.id, operation_id)
                    sequence = seq
                    kind = event.get("type")
                    if kind == "text":
                        text = event.get("text")
                        if not isinstance(text, str) or len(prefix) + len(text) > 65536:
                            raise BridgeError("protocol", turn.id, operation_id)
                        prefix += text
                        if text:
                            acknowledge = False
                            yield text
                    elif kind == "terminal":
                        text = self._terminal_text(event.get("operation"), turn.id, operation_id)
                        if not text.startswith(prefix):
                            raise BridgeError("partial", turn.id, operation_id)
                        if tail := text[len(prefix) :]:
                            yield tail
                        return
                    elif kind == "unavailable":
                        raise BridgeError("pending", turn.id, operation_id)
                    else:
                        raise BridgeError("protocol", turn.id, operation_id)
        except (httpx.TransportError, TimeoutError, StopAsyncIteration):
            raise BridgeError("partial" if prefix else "pending", turn.id, operation_id) from None
        except BridgeError as error:
            if prefix and error.kind != "partial":
                raise BridgeError("partial", turn.id, operation_id) from None
            raise
        finally:
            if pending is not None:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await events.aclose()

    def _submission_done(self, task: asyncio.Task[str]) -> None:
        self._submissions.discard(task)
        if task.cancelled():
            logger.warning("Submission interrupted by process shutdown: %s", task.get_name())
        elif error := task.exception():
            # No transcript, provider secret, response body, or backend credentials in logs.
            logger.warning("Control submission did not resolve: %s", error)

    async def aclose(self) -> None:
        self._closed = True
        # Each submission has its own hard deadline. Finish reconciliation before
        # closing the HTTP client; no backend cancellation endpoint is ever called.
        if self._submissions:
            await asyncio.gather(*self._submissions, return_exceptions=True)
        await self.client.aclose()

    async def _submit(self, turn: Turn) -> str:
        ambiguous = False
        kind: FailureKind
        try:
            async with asyncio.timeout(self.limits.submission_timeout):
                for attempt in range(self.limits.post_attempts):
                    try:
                        response = await self.client.post(
                            "v1/chat", json=turn.payload(), timeout=self.limits.request_timeout
                        )
                    except httpx.TransportError:
                        response = None  # Server may have committed before the connection failed.
                    if response is not None:
                        if response.status_code == 202:
                            body = self._object(response)
                            operation_id = body.get("operationId") if body is not None else None
                            if isinstance(operation_id, str) and operation_id:
                                logger.info(
                                    "Control accepted request=%s operation=%s",
                                    turn.id,
                                    operation_id,
                                )
                                return operation_id
                            # Malformed acknowledgement is also ambiguous; reconcile using SAME id.
                        elif 400 <= response.status_code < 500 and response.status_code not in (
                            408,
                            429,
                        ):
                            kind = "unknown_submission" if ambiguous else "rejected"
                            raise BridgeError(kind, turn.id)
                        elif 300 <= response.status_code < 400:
                            kind = "unknown_submission" if ambiguous else "rejected"
                            raise BridgeError(kind, turn.id)
                    ambiguous = True
                    if attempt + 1 < self.limits.post_attempts:
                        await asyncio.sleep(self.limits.poll_interval)
        except TimeoutError:
            pass
        raise BridgeError("unknown_submission", turn.id)

    @staticmethod
    def _object(response: httpx.Response) -> dict[str, Any] | None:
        try:
            body = response.json()
        except ValueError:
            return None
        return body if isinstance(body, dict) else None

    async def _reply_events(
        self, request_id: str, operation_id: str
    ) -> AsyncGenerator[dict[str, Any], None]:
        path = f"v1/operations/{quote(operation_id, safe='')}/reply"
        async with self.client.stream(
            "GET", path, timeout=httpx.Timeout(self.limits.request_timeout, read=30.0)
        ) as response:
            if (
                response.status_code != 200
                or response.headers.get("content-type") != "text/event-stream"
            ):
                raise BridgeError("protocol", request_id, operation_id)
            buffer = b""
            frames = 0
            async for chunk in response.aiter_bytes():
                if len(chunk) + len(buffer) > 262144:
                    raise BridgeError("protocol", request_id, operation_id)
                buffer += chunk
                while b"\n\n" in buffer:
                    frame, buffer = buffer.split(b"\n\n", 1)
                    if frame.startswith(b":"):
                        continue
                    frames += 1
                    if frames > 4096 or not frame.startswith(b"data: "):
                        raise BridgeError("protocol", request_id, operation_id)
                    try:
                        event = json.loads(frame[6:])
                    except (ValueError, UnicodeDecodeError):
                        raise BridgeError("protocol", request_id, operation_id) from None
                    if not isinstance(event, dict):
                        raise BridgeError("protocol", request_id, operation_id)
                    yield event

    @staticmethod
    def _terminal_text(operation: Any, request_id: str, operation_id: str) -> str:
        if not isinstance(operation, dict) or operation.get("id") != operation_id:
            raise BridgeError("protocol", request_id, operation_id)
        status = operation.get("status")
        if status in ("failed", "uncertain"):
            raise BridgeError(status, request_id, operation_id)
        output = operation.get("output")
        if (
            status != "succeeded"
            or not isinstance(output, dict)
            or not isinstance(output.get("text"), str)
            or len(output["text"]) > 65536
            or not isinstance(output.get("messageId"), str)
        ):
            raise BridgeError("protocol", request_id, operation_id)
        return str(output["text"])
