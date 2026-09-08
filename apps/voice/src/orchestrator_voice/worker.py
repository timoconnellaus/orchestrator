from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, AsyncIterable, Callable
from typing import Any
from uuid import uuid4, uuid5

import httpx
from livekit import rtc
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
from .live_transcribe import MODEL as LIVE_STT_MODEL
from .live_transcribe import LiveTranscribeSTT
from .reply_audio import stream_reply_audio
from .speech import make_tts

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
    ) -> AsyncGenerator[str, None]:
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
        reply = self.bridge.stream_answer(turn)
        try:
            async for text in reply:
                yield text
        except BridgeError as error:
            logger.warning("Control result unavailable: %s", error)
            yield "\n\n" + error.spoken_text
        finally:
            await reply.aclose()
        # CancelledError deliberately propagates: stop speech, not durable work.

    async def tts_node(
        self, text: AsyncIterable[str], model_settings: ModelSettings
    ) -> AsyncGenerator[rtc.AudioFrame, None]:
        provider = self.session.tts
        if provider is None:
            raise RuntimeError("Reply audio requires TTS")
        audio = stream_reply_audio(provider, text, self.session.conn_options.tts_conn_options)
        try:
            async for frame in audio:
                yield frame
        finally:
            await audio.aclose()


def make_session(settings: Settings, vad: silero.VAD) -> AgentSession[None]:
    return AgentSession[None](
        stt=(
            LiveTranscribeSTT(vad=vad, language=settings.stt_language)
            if settings.stt_model == LIVE_STT_MODEL
            else openai.STT(
                model=settings.stt_model, language=settings.stt_language, use_realtime=False
            )
        ),
        tts=make_tts(settings),
        vad=vad,
        turn_handling=turn_handling(),
        user_away_timeout=None,
    )


def room_options() -> room_io.RoomOptions:
    # Typed chat uses control directly; don't accidentally create a second producer
    # from LiveKit's default room text input callback.
    return room_io.RoomOptions(
        text_input=False,
        text_output=True,
        video_input=False,
        close_on_disconnect=True,
        delete_room_on_close=False,
    )


def bind_capture_boundaries(room: rtc.Room, session: AgentSession[None]) -> Callable[[], None]:
    """Reset the actual STT/VAD capture generation on microphone lifecycle events."""
    engine = session.stt
    if not isinstance(engine, LiveTranscribeSTT):
        return lambda: None

    def update(
        publication: rtc.RemoteTrackPublication, participant: rtc.RemoteParticipant, enabled: bool
    ) -> None:
        linked = session.room_io.linked_participant
        if (
            linked is not None
            and participant.identity == linked.identity
            and publication.source == rtc.TrackSource.SOURCE_MICROPHONE
        ):
            engine.set_capture_enabled(enabled)

    def muted(publication: rtc.RemoteTrackPublication, participant: rtc.RemoteParticipant) -> None:
        update(publication, participant, False)

    def unmuted(
        publication: rtc.RemoteTrackPublication, participant: rtc.RemoteParticipant
    ) -> None:
        update(publication, participant, True)

    def subscribed(
        track: rtc.Track,
        publication: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
    ) -> None:
        update(publication, participant, not publication.muted)

    def unsubscribed(
        track: rtc.Track,
        publication: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
    ) -> None:
        update(publication, participant, False)

    room.on("track_muted", muted)
    room.on("track_unmuted", unmuted)
    room.on("track_subscribed", subscribed)
    room.on("track_unsubscribed", unsubscribed)
    linked = session.room_io.linked_participant
    if linked is not None:
        for publication in linked.track_publications.values():
            update(publication, linked, not publication.muted)

    def cleanup() -> None:
        room.off("track_muted", muted)
        room.off("track_unmuted", unmuted)
        room.off("track_subscribed", subscribed)
        room.off("track_unsubscribed", unsubscribed)

    return cleanup


def prewarm(process: JobProcess) -> None:
    process.userdata["vad"] = silero.VAD.load()


def voice_job_load(worker: AgentServer) -> float:
    # This is a personal Mac, not a dedicated autoscaled voice host. Unrelated
    # CPU activity must not reject an idle voice worker. Allow an active room
    # plus one reconnect/closing room; the SDK also accounts for reserved jobs.
    return len(worker.active_jobs) / 2


server = AgentServer(
    setup_fnc=prewarm,
    shutdown_process_timeout=35.0,
    load_fnc=voice_job_load,
    load_threshold=1.0,
    num_idle_processes=1,
)


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
    unbind_capture: Callable[[], None] | None = None

    @session.on("error")
    def on_error(event: ErrorEvent) -> None:
        logger.error(
            "Voice provider error type=%s recoverable=%s; media may be unavailable",
            type(event.error).__name__,
            getattr(event.error, "recoverable", "unknown"),
        )

    @session.on("close")
    def on_close(event: CloseEvent) -> None:
        if unbind_capture is not None:
            unbind_capture()
        logger.warning("Voice session closed reason=%s; app must reconnect", event.reason)
        # A closed AgentSession cannot hear a returning participant. Retire its
        # job now, rather than advertising stale readiness and holding admission
        # capacity until the room expires. Bridge shutdown reconciles submissions;
        # it never cancels accepted control operations or coding workers.
        ctx.shutdown(reason="voice session closed")

    await session.start(
        agent=ControlAgent(bridge, conversation_id),
        room=ctx.room,
        room_options=room_options(),
        record=False,
    )
    unbind_capture = bind_capture_boundaries(ctx.room, session)
    # No greeting, generate_reply, idle timer, nagging, or scheduled hangup.


def main() -> None:
    cli.run_app(server)


if __name__ == "__main__":
    main()
