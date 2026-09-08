from __future__ import annotations

import logging
from collections.abc import AsyncIterable
from typing import Any
from uuid import uuid4, uuid5

import httpx
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    JobProcess,
    cli,
    llm,
    room_io,
)
from livekit.agents.voice import CloseEvent, ErrorEvent, ModelSettings
from livekit.plugins import openai, silero

from .bridge import BridgeError, ControlBridge, Turn
from .config import AGENT_NAME, Settings, turn_handling

logger = logging.getLogger(__name__)


class LocalNodeOnlyLLM(llm.LLM[None]):
    """SDK 1.5.12 skips even custom nodes with llm=None; this is NOT a provider.

    The custom Agent.llm_node is the only generation path. Fail closed if a
    framework change accidentally routes through the default LLM implementation.
    """

    @property
    def model(self) -> str:
        return "control-http-bridge"

    @property
    def provider(self) -> str:
        return "local"

    def chat(self, **kwargs: Any) -> llm.LLMStream:
        raise RuntimeError("Use ControlAgent.llm_node; no reasoning model is configured")


class ControlAgent(Agent):
    def __init__(self, bridge: ControlBridge, conversation_id: str):
        super().__init__(instructions="", llm=LocalNodeOnlyLLM())
        self.bridge = bridge
        self.conversation_id = conversation_id
        self._turn_namespace = uuid4()

    async def llm_node(
        self,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool],
        model_settings: ModelSettings,
    ) -> AsyncIterable[str]:
        # The pipeline invokes this only after end-of-turn; preemptive generation
        # is disabled. Do not send history (Codex owns it), partials, or tool calls.
        latest = next(
            (
                item
                for item in reversed(chat_ctx.items)
                if isinstance(item, llm.ChatMessage) and item.role == "user"
            ),
            None,
        )
        if latest is None or not latest.text_content or not latest.text_content.strip():
            return
        # The same SDK message re-entering this node gets the same id. Identical
        # words in a later, distinct user turn deliberately get a different id.
        turn = Turn(
            id=str(uuid5(self._turn_namespace, latest.id)),
            text=latest.text_content,
            conversation_id=self.conversation_id,
        )
        try:
            yield await self.bridge.answer(turn)
        except BridgeError as error:
            logger.warning("Control result unavailable: %s", error)
            yield error.spoken_text
        # CancelledError deliberately propagates: stop polling/TTS, not durable work.


def make_session(settings: Settings, vad: silero.VAD) -> AgentSession[None]:
    return AgentSession[None](
        stt=openai.STT(
            model=settings.stt_model, language=settings.stt_language, use_realtime=False
        ),
        tts=openai.TTS(model=settings.tts_model, voice=settings.tts_voice),
        vad=vad,
        turn_handling=turn_handling(),
        user_away_timeout=None,
    )


def room_options() -> room_io.RoomOptions:
    # Typed chat uses control directly; don't accidentally create a second producer
    # from LiveKit's default room text input callback.
    return room_io.RoomOptions(
        text_input=False,
        video_input=False,
        close_on_disconnect=True,
        delete_room_on_close=False,
    )


def prewarm(process: JobProcess) -> None:
    process.userdata["vad"] = silero.VAD.load()


server = AgentServer(setup_fnc=prewarm, shutdown_process_timeout=35.0)


@server.rtc_session(agent_name=AGENT_NAME)
async def entrypoint(ctx: JobContext) -> None:
    settings = Settings.from_env()
    conversation_id = settings.conversation_for_job(ctx.job.metadata)
    bridge = ControlBridge(
        httpx.AsyncClient(
            base_url=settings.backend_url,
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        ),
        settings.limits,
    )
    ctx.add_shutdown_callback(bridge.aclose)
    session = make_session(settings, ctx.proc.userdata["vad"])

    @session.on("error")
    def on_error(event: ErrorEvent) -> None:
        logger.error(
            "Voice provider error type=%s recoverable=%s; media may be unavailable",
            type(event.error).__name__,
            getattr(event.error, "recoverable", "unknown"),
        )

    @session.on("close")
    def on_close(event: CloseEvent) -> None:
        logger.warning("Voice session closed reason=%s; app must reconnect", event.reason)

    await session.start(
        agent=ControlAgent(bridge, conversation_id),
        room=ctx.room,
        room_options=room_options(),
        record=False,
    )
    # No greeting, generate_reply, idle timer, nagging, or scheduled hangup.


def main() -> None:
    cli.run_app(server)


if __name__ == "__main__":
    main()
