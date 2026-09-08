"""Opt-in real LiveKit agent-readiness check, with no microphone or speech input.

Run from apps/voice with uv run --frozen --env-file ../../.env python
../../scripts/check-voice-dispatch.py. Creates/deletes only a uniquely named probe
room. Never publishes audio, sends chat, or invokes coding-worker tools.
"""

import asyncio
import json
import os
from datetime import timedelta
from uuid import uuid4

from livekit import api, rtc


async def main() -> None:
    room_name = "orchestrator-dispatch-probe-" + uuid4().hex[:12]
    url = os.environ["LIVEKIT_INTERNAL_URL"]
    key, secret = os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"]
    room = rtc.Room()
    ready = asyncio.Event()
    agent_seen = False

    def check(*_args: object) -> None:
        nonlocal agent_seen
        for participant in room.remote_participants.values():
            if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT:
                agent_seen = True
                if participant.attributes.get("lk.agent.state") == "listening":
                    ready.set()

    room.on("participant_connected", check)
    room.on("participant_attributes_changed", check)
    token = (
        api.AccessToken(key, secret)
        .with_identity("phone-dispatch-probe-" + uuid4().hex[:8])
        .with_ttl(timedelta(minutes=2))
        .with_grants(
            api.VideoGrants(
                room_join=True, room=room_name, can_publish=False, can_subscribe=True
            )
        )
        .with_room_config(
            api.RoomConfiguration(
                agents=[
                    api.RoomAgentDispatch(
                        agent_name="orchestrator-voice",
                        metadata=json.dumps({"conversationId": "main"}),
                    )
                ]
            )
        )
        .to_jwt()
    )
    try:
        await asyncio.wait_for(room.connect(url, token), 10)
        check()
        await asyncio.wait_for(ready.wait(), 12)
        assert not room.local_participant.track_publications
        print(
            json.dumps(
                {
                    "agent_joined": agent_seen,
                    "agent_listening": True,
                    "audio_published": False,
                }
            )
        )
    except TimeoutError:
        print(
            json.dumps(
                {
                    "agent_joined": agent_seen,
                    "agent_listening": ready.is_set(),
                    "audio_published": False,
                }
            )
        )
        raise AssertionError(
            "Voice agent did not reach listening state in the probe room"
        ) from None
    finally:
        await room.disconnect()
        async with api.LiveKitAPI(url, key, secret) as client:
            try:
                await client.room.delete_room(api.DeleteRoomRequest(room=room_name))
            except api.TwirpError as error:
                if error.code != "not_found":
                    raise RuntimeError(
                        "Could not clean up the owned probe room"
                    ) from None


if __name__ == "__main__":
    asyncio.run(main())
