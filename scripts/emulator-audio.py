#!/usr/bin/env python3
"""Synthetic mic injection / output capture for emulator-5554 only.

Run with: uv run --no-project --python 3.11 --with grpcio-tools python scripts/emulator-audio.py ...
Requires the installed Android SDK emulator_controller.proto. Never enables host mic.
"""
import argparse
import array
import importlib
import os
from pathlib import Path
import subprocess
import sys
import time
import wave

import grpc
import grpc_tools
from grpc_tools import protoc
from google.protobuf.empty_pb2 import Empty

SERIAL = "emulator-5554"
ROOT = Path(__file__).resolve().parent.parent
SDK = Path(os.environ.get("ANDROID_HOME", Path.home() / "Library/Android/sdk"))


def adb(*args):
    return subprocess.check_output([str(SDK / "platform-tools/adb"), "-s", SERIAL, *args], text=True, timeout=15)


def connect():
    path = next(line.strip() for line in adb("emu", "avd", "discoverypath").splitlines() if line.startswith("/"))
    data = dict(line.split("=", 1) for line in Path(path).read_text().splitlines() if "=" in line)
    assert data["port.serial"] == "5554"
    assert data.get("grpc.token"), "Expected emulator-local gRPC authentication token"
    generated = ROOT / ".data/emulator-grpc"
    generated.mkdir(parents=True, exist_ok=True, mode=0o700)
    include = Path(grpc_tools.__file__).parent / "_proto"
    source = SDK / "emulator/lib"
    result = protoc.main(["grpc_tools.protoc", f"-I{source}", f"-I{include}", f"--python_out={generated}", f"--grpc_python_out={generated}", str(source / "emulator_controller.proto")])
    assert result == 0
    sys.path.insert(0, str(generated))
    messages = importlib.import_module("emulator_controller_pb2")
    services = importlib.import_module("emulator_controller_pb2_grpc")
    channel = grpc.insecure_channel(f"127.0.0.1:{int(data['grpc.port'])}")
    return channel, services.EmulatorControllerStub(channel), messages, [("authorization", "Bearer " + data["grpc.token"])]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["status", "inject", "capture"])
    parser.add_argument("file", nargs="?")
    parser.add_argument("--seconds", type=float, default=60)
    args = parser.parse_args()
    channel, client, pb, auth = connect()
    try:
        if args.action == "status":
            client.getStatus(Empty(), metadata=auth, timeout=10)
            print("PASS: authenticated emulator-5554 gRPC access")
            return
        assert args.file, "Specify a WAV file"
        if args.action == "inject":
            # A silent host mic stays disabled before, during and after injection.
            adb("emu", "avd", "hostmicoff")
            with wave.open(args.file, "rb") as source:
                assert source.getnchannels() == 1 and source.getsampwidth() == 2
                rate = source.getframerate()
                assert 8000 <= rate <= 48000
                audio = source.readframes(source.getnframes())
            fmt = pb.AudioFormat(samplingRate=rate, channels=pb.AudioFormat.Mono, format=pb.AudioFormat.AUD_FMT_S16, mode=pb.AudioFormat.MODE_REAL_TIME)
            # Silence on both sides lets VAD delimit the utterance. Real-time pacing
            # prevents overwriting the emulator's 300 ms input queue.
            audio = bytes(rate * 2) + audio + bytes(rate * 2 * 3)
            frames = rate // 50
            def packets():
                started = time.monotonic()
                for index, offset in enumerate(range(0, len(audio), frames * 2)):
                    pause = started + index * frames / rate - time.monotonic()
                    if pause > 0:
                        time.sleep(pause)
                    yield pb.AudioPacket(format=fmt, timestamp=time.time_ns() // 1000, audio=audio[offset:offset + frames * 2])
            client.injectAudio(packets(), metadata=auth, timeout=len(audio) / (rate * 2) + 20)
            print(f"Injected {len(audio) / (rate * 2):.1f}s synthetic audio into {SERIAL}")
        else:
            assert 0 < args.seconds <= 300
            fmt = pb.AudioFormat(samplingRate=24000, channels=pb.AudioFormat.Mono, format=pb.AudioFormat.AUD_FMT_S16)
            stream = client.streamAudio(fmt, metadata=auth, timeout=args.seconds)
            count = peak = energetic_samples = 0
            with wave.open(args.file, "wb") as target:
                target.setnchannels(1)
                target.setsampwidth(2)
                target.setframerate(24000)
                try:
                    for packet in stream:
                        target.writeframes(packet.audio)
                        count += len(packet.audio)
                        pcm = array.array('h', packet.audio)
                        peak = max(peak, max(map(abs, pcm), default=0))
                        energetic_samples += sum(abs(sample) > 200 for sample in pcm)
                except grpc.RpcError as error:
                    if error.code() != grpc.StatusCode.DEADLINE_EXCEEDED:
                        raise
            print(f"Captured {count} audio bytes from {SERIAL}; PCM peak {peak}, {energetic_samples} samples above noise floor")
            assert energetic_samples > 2400, "No sustained audible output captured (codec noise does not count)"
    finally:
        channel.close()


if __name__ == "__main__":
    main()
