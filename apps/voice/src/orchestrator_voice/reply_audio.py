"""Sentence-stream speech with explicit paragraph flush for short receipt acknowledgments."""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterable
from dataclasses import replace
from typing import Any

from livekit import rtc
from livekit.agents import APIConnectOptions, tokenize, tts
from livekit.agents.tokenize.token_stream import BufferedSentenceStream
from livekit.agents.tokenize.tokenizer import SentenceStream


class ReplySentenceTokenizer(tokenize.SentenceTokenizer):
    def __init__(self) -> None:
        self._base = tokenize.blingfire.SentenceTokenizer(retain_format=True)

    def tokenize(self, text: str, *, language: str | None = None) -> list[str]:
        return self._base.tokenize(text, language=language)

    def stream(self, *, language: str | None = None) -> SentenceStream:
        return _ParagraphStream(self)


class _ParagraphStream(BufferedSentenceStream):
    def __init__(self, tokenizer: ReplySentenceTokenizer) -> None:
        super().__init__(tokenizer=tokenizer.tokenize, min_token_len=1, min_ctx_len=1)
        self._newline = ""

    def push_text(self, text: str) -> None:
        parts = (self._newline + text).split("\n\n")
        self._newline = ""
        for part in parts[:-1]:
            super().push_text(part)
            super().flush()
        tail = parts[-1]
        if tail.endswith("\n"):
            tail = tail[:-1]
            self._newline = "\n"
        super().push_text(tail)

    def flush(self) -> None:
        super().push_text(self._newline)
        self._newline = ""
        super().flush()


async def stream_reply_audio(
    provider: tts.TTS[Any],
    text: AsyncIterable[str],
    conn_options: APIConnectOptions,
) -> AsyncGenerator[rtc.AudioFrame, None]:
    adapter = (
        tts.StreamAdapter(tts=provider, sentence_tokenizer=ReplySentenceTokenizer())
        if not provider.capabilities.streaming
        else None
    )
    engine = adapter or provider
    try:
        # SDK sentence synthesis can retry after emitting audio. Never replay an
        # interrupted sentence; use a copy so other session settings are unchanged.
        async with engine.stream(conn_options=replace(conn_options, max_retry=0)) as stream:

            async def forward() -> None:
                try:
                    async for chunk in text:
                        stream.push_text(chunk)
                finally:
                    stream.end_input()

            task = asyncio.create_task(forward())
            try:
                async for event in stream:
                    yield event.frame
                await task
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    finally:
        if adapter is not None:
            await adapter.aclose()
