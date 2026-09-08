"""Opt-in production token/LiveKit restart check; no audio or coding operations.

Run from apps/voice with uv run --frozen --env-file ../../.env python
../../scripts/check-voice-rejoin.py. Performs three explicit join/leave cycles.
Only fresh, previously absent rooms returned by this test's token requests are
owned/cleaned up. Never joins or deletes the legacy shared orchestrator-main room.
"""

import asyncio
import json
import os
import re

import aiohttp
from livekit import api, rtc


async def main() -> None:
    control = os.environ["ORCHESTRATOR_URL"].rstrip("/")
    media = os.environ["LIVEKIT_INTERNAL_URL"]
    key, secret = os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"]
    owned: set[str] = set()
    agents: set[str] = set()
    room = rtc.Room()
    async with (
        aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10), trust_env=False) as http,
        api.LiveKitAPI(media, key, secret) as admin,
    ):
        try:
            for attempt in range(1, 4):
                existing = {
                    item.name
                    for item in (await admin.room.list_rooms(api.ListRoomsRequest())).rooms
                }
                async with http.post(
                    control + "/v1/voice/token", json={"conversationId": "main"}
                ) as response:
                    response.raise_for_status()
                    credentials = await response.json()
                name = credentials["room"]
                if (
                    not re.fullmatch(r"orchestrator-main-[0-9a-f-]{36}", name)
                    or name in existing
                    or name in owned
                ):
                    raise AssertionError("Explicit join did not receive a fresh, isolated room")
                owned.add(name)
                room = rtc.Room()
                ready = asyncio.Event()

                def check(
                    *_args: object, joined_room: rtc.Room = room, signal: asyncio.Event = ready
                ) -> None:
                    for participant in joined_room.remote_participants.values():
                        if (
                            participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT
                            and participant.identity not in agents
                            and participant.attributes.get("lk.agent.state") == "listening"
                        ):
                            signal.set()

                room.on("participant_connected", check)
                room.on("participant_attributes_changed", check)
                # Same token/room as the phone; local signaling avoids unrelated tailnet routing.
                await asyncio.wait_for(room.connect(media, credentials["token"]), 10)
                check()
                await asyncio.wait_for(ready.wait(), 12)
                agents.update(
                    p.identity
                    for p in room.remote_participants.values()
                    if p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT
                )
                assert not room.local_participant.track_publications
                await room.disconnect()
                # Test stimulus: beyond SDK session-close grace, inside old room TTL.
                await asyncio.sleep(3)
                try:
                    remaining = await admin.room.list_participants(
                        api.ListParticipantsRequest(room=name)
                    )
                except api.TwirpError as error:
                    if error.code != "not_found":
                        raise
                else:
                    if any(
                        p.kind == api.ParticipantInfo.Kind.AGENT for p in remaining.participants
                    ):
                        raise AssertionError("Closed speech session still occupies its agent job")
                print(
                    json.dumps(
                        {
                            "attempt": attempt,
                            "fresh_listening_agent": True,
                            "closed_agent_departed": True,
                            "audio_published": False,
                        }
                    )
                )
        finally:
            await room.disconnect()
            for name in owned:
                try:
                    await admin.room.delete_room(api.DeleteRoomRequest(room=name))
                except api.TwirpError as error:
                    if error.code != "not_found":
                        raise RuntimeError(
                            "Could not clean up an owned restart-test room"
                        ) from None


if __name__ == "__main__":
    asyncio.run(main())
