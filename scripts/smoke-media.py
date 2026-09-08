"""Local LiveKit data/audio transport smoke test. No speech/model provider calls.

Run after integration: cd apps/voice && uv run --frozen python ../../scripts/smoke-media.py
Loads the private root .env without displaying credentials; creates a temporary room.
"""

import asyncio
import math
import os
import struct
import uuid
from pathlib import Path

from livekit import api, rtc


def load_local_env() -> None:
    for line in (Path(__file__).resolve().parents[1] / ".env").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key, value.strip().strip('"').strip("'"))


async def main() -> None:
    load_local_env()
    url = os.environ.get("LIVEKIT_INTERNAL_URL", "ws://127.0.0.1:7880")
    key = os.environ["LIVEKIT_API_KEY"]
    secret = os.environ["LIVEKIT_API_SECRET"]
    room_name = "orchestrator-smoke-" + uuid.uuid4().hex[:12]
    publisher, subscriber = rtc.Room(), rtc.Room()
    audio_seen = asyncio.Event()
    data_seen = asyncio.Event()
    consumers: set[asyncio.Task[None]] = set()

    async def consume(track: rtc.Track) -> None:
        stream = rtc.AudioStream(track, sample_rate=48000, num_channels=1)
        try:
            async for event in stream:
                if any(event.frame.data):
                    audio_seen.set()
                    return
        finally:
            await stream.aclose()

    @subscriber.on("track_subscribed")
    def on_track(track, _publication, _participant):
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            task = asyncio.create_task(consume(track))
            consumers.add(task)
            task.add_done_callback(consumers.discard)

    @subscriber.on("data_received")
    def on_data(packet):
        if packet.data == b"orchestrator-transport-smoke":
            data_seen.set()

    def token(identity: str) -> str:
        return (
            api.AccessToken(key, secret)
            .with_identity(identity)
            .with_grants(api.VideoGrants(room_join=True, room=room_name))
            .to_jwt()
        )

    source = rtc.AudioSource(48000, 1)
    track = rtc.LocalAudioTrack.create_audio_track("smoke-tone", source)
    try:
        await asyncio.wait_for(subscriber.connect(url, token("smoke-listener")), 20)
        await asyncio.wait_for(publisher.connect(url, token("smoke-publisher")), 20)
        options = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        await publisher.local_participant.publish_track(track, options)
        await publisher.local_participant.publish_data(b"orchestrator-transport-smoke", reliable=True)
        # Generate non-silent PCM for the transport test, not user microphone audio.
        for frame_index in range(100):
            frame = rtc.AudioFrame.create(48000, 1, 480)
            pcm = b"".join(struct.pack("<h", int(2000 * math.sin(2 * math.pi * 440 * (frame_index * 480 + i) / 48000))) for i in range(480))
            frame.data.cast("B")[:] = pcm
            await source.capture_frame(frame)
        await asyncio.wait_for(asyncio.gather(audio_seen.wait(), data_seen.wait()), 20)
        print("PASS: two local participants exchanged reliable data and non-silent audio.")
    finally:
        for task in list(consumers):
            task.cancel()
        await asyncio.gather(*consumers, return_exceptions=True)
        await publisher.disconnect()
        await subscriber.disconnect()
        await source.aclose()
        async with api.LiveKitAPI(url, key, secret) as client:
            await client.room.delete_room(api.DeleteRoomRequest(room=room_name))


asyncio.run(main())
