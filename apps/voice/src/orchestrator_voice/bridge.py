"""HTTP-only durable operation bridge; consumer cancellation never cancels submission."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)
FailureKind = Literal[
    "rejected", "unknown_submission", "pending", "failed", "uncertain", "protocol"
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

    def __post_init__(self) -> None:
        import math

        durations = (
            self.request_timeout,
            self.submission_timeout,
            self.poll_timeout,
            self.poll_interval,
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

    async def answer(self, turn: Turn) -> str:
        if self._closed:
            raise RuntimeError("Bridge is closed")
        # Shield only POST/reconciliation, not polling or playback. Retain the task
        # after interruption so uncertain POST outcomes still get same-ID retries.
        task = asyncio.create_task(self._submit(turn), name=f"submit-{turn.id}")
        self._submissions.add(task)
        task.add_done_callback(self._submission_done)
        operation_id = await asyncio.shield(task)
        return await self._poll(turn.id, operation_id)

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

    async def _poll(self, request_id: str, operation_id: str) -> str:
        try:
            async with asyncio.timeout(self.limits.poll_timeout):
                while True:
                    try:
                        response = await self.client.get(
                            f"v1/operations/{quote(operation_id, safe='')}",
                            timeout=self.limits.request_timeout,
                        )
                    except httpx.TransportError:
                        response = None
                    if response is not None and response.status_code == 200:
                        body = self._object(response)
                        operation = body.get("operation") if body is not None else None
                        if not isinstance(operation, dict) or operation.get("id") != operation_id:
                            raise BridgeError("protocol", request_id, operation_id)
                        status = operation.get("status")
                        if status == "succeeded":
                            output = operation.get("output")
                            if (
                                not isinstance(output, dict)
                                or not isinstance(output.get("text"), str)
                                or not isinstance(output.get("messageId"), str)
                            ):
                                raise BridgeError("protocol", request_id, operation_id)
                            return str(output["text"])
                        if status in ("failed", "uncertain"):
                            kind: FailureKind = "failed" if status == "failed" else "uncertain"
                            raise BridgeError(kind, request_id, operation_id)
                        if status not in ("queued", "running"):
                            raise BridgeError("protocol", request_id, operation_id)
                    elif response is not None and response.status_code not in (404, 408, 429):
                        if response.status_code < 500:
                            raise BridgeError("protocol", request_id, operation_id)
                    await asyncio.sleep(self.limits.poll_interval)
        except TimeoutError:
            raise BridgeError("pending", request_id, operation_id) from None
