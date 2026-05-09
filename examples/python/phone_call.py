"""
Simulate a mobile phone call: stream an audio file (looped to fill the
configured duration) to DetectDeepfake at real-time pace, and print
analysis events as they arrive.

Differences from `client.py`:
  - One continuous gRPC session for the whole call, not one per file.
  - Real-time pacing: ~1 second of audio sent per second of wallclock,
    matching how a live phone call would feed the service.
  - Loops the input file if it's shorter than `--duration`.
  - 100 ms frames by default — closer to telephony cadence (typical RTP
    payloads are 20 ms; 100 ms gives readable analysis output without
    flooding the terminal).
  - Uses grpc.aio so analysis results print concurrently with sends,
    rather than queuing up until the stream closes.

CLI:
    python phone_call.py [--audio path/to.wav] [--duration 30] \
        [--chunk-ms 100] [--target localhost:50051]

If --audio is not provided, picks the first .wav in
`examples/audio/`. The dir is gitignored — drop a fixture in.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import wave
from pathlib import Path

import grpc

from aurigin.deepfake_detection.v1 import deepfake_detection_pb2 as pb
from aurigin.deepfake_detection.v1 import deepfake_detection_pb2_grpc as pb_grpc
from twilio.tme.extensions.common.v1 import audio_buffer_pb2 as ab_pb

DEFAULT_CHUNK_MS = 100
DEFAULT_DURATION_S = 30.0


def _load_pcm(path: Path) -> tuple[bytes, int, int]:
    """Read a WAV file as (pcm_bytes, sample_rate, channels). 16-bit only."""
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError(f"{path.name}: expected 16-bit PCM, got {w.getsampwidth() * 8}-bit")
        return w.readframes(w.getnframes()), w.getframerate(), w.getnchannels()


def _resolve_audio(arg: Path | None) -> Path:
    if arg is not None:
        if not arg.is_file():
            raise FileNotFoundError(arg)
        return arg
    audio_dir = Path(__file__).resolve().parent.parent / "audio"
    wavs = sorted(audio_dir.glob("*.wav")) if audio_dir.is_dir() else []
    if not wavs:
        raise SystemExit(
            "No --audio supplied and no .wav files found in examples/audio/. "
            "Drop a 16-bit PCM WAV in examples/audio/ or pass one with --audio."
        )
    return wavs[0]


async def _send_call(
    call,
    pcm: bytes,
    sample_rate: int,
    channels: int,
    chunk_ms: int,
    duration_s: float,
) -> None:
    """Send CreateSession + paced AudioBuffers for `duration_s` seconds, then close write side."""
    bytes_per_sample = 2 * channels
    bytes_per_chunk = max(1, int(sample_rate * chunk_ms / 1000) * bytes_per_sample)
    chunk_seconds = chunk_ms / 1000

    await call.write(pb.DetectDeepfakeRequest(create_session_request=pb.CreateSessionRequest()))

    loop = asyncio.get_running_loop()
    next_send = loop.time()
    pts_ns = 0
    cursor = 0
    elapsed_s = 0.0

    while elapsed_s < duration_s:
        chunk = pcm[cursor : cursor + bytes_per_chunk]
        cursor += bytes_per_chunk
        if cursor >= len(pcm):
            cursor = 0  # loop the file to extend the call
        actual_frames = len(chunk) // bytes_per_sample
        duration_ns = int(actual_frames / sample_rate * 1e9)

        await call.write(
            pb.DetectDeepfakeRequest(
                audio=ab_pb.AudioBuffer(
                    type="audio/x-raw",
                    format="S16LE",
                    channels=channels,
                    rate=sample_rate,
                    duration_ns=duration_ns,
                    pts_ns=pts_ns,
                    size=len(chunk),
                    buffer=chunk,
                ),
            )
        )
        pts_ns += duration_ns
        elapsed_s += chunk_seconds

        # Pace at real time. Use a deadline-based scheduler so we don't drift
        # if a single send blocks longer than expected.
        next_send += chunk_seconds
        sleep_for = next_send - loop.time()
        if sleep_for > 0:
            await asyncio.sleep(sleep_for)

    await call.done_writing()


async def _recv_call(call) -> None:
    """Print every server response as it arrives."""
    async for response in call:
        kind = response.WhichOneof("response")
        if kind == "create_session_response":
            print(f"📞 Session: {response.create_session_response.session_id}")
        elif kind == "analysis_result":
            r = response.analysis_result
            print(
                f"   Analysis @ {r.audio_offset_ms / 1000:6.2f}s "
                f"| score={r.score:.3f} | label={r.label:18s} | confidence={r.confidence:.2f}"
            )
        elif kind == "final_result":
            f = response.final_result
            print("─" * 70)
            print(
                f"☎️  Call ended | total={f.total_audio_ms / 1000:.2f}s "
                f"| score={f.overall_score:.3f} | label={f.overall_label} | analyses={f.analysis_count}"
            )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", maxsplit=1)[0])
    parser.add_argument("--audio", type=Path, default=None, help="WAV file to stream (defaults to first in audio/)")
    parser.add_argument("--duration", type=float, default=DEFAULT_DURATION_S, help="Call length in seconds")
    parser.add_argument("--chunk-ms", type=int, default=DEFAULT_CHUNK_MS, help="Audio frame size in milliseconds")
    parser.add_argument("--target", default="localhost:50051", help="gRPC server host:port")
    args = parser.parse_args()

    audio_path = _resolve_audio(args.audio)
    pcm, rate, channels = _load_pcm(audio_path)

    print(
        f"📞 Calling {args.target} | source={audio_path.name} "
        f"({len(pcm) / (rate * 2 * channels):.2f}s @ {rate}Hz/{channels}ch) "
        f"| duration={args.duration:.1f}s | frame={args.chunk_ms}ms"
    )
    print("─" * 70)

    async with grpc.aio.insecure_channel(args.target) as channel:
        stub = pb_grpc.DeepfakeDetectionStub(channel)
        call = stub.DetectDeepfake()
        try:
            await asyncio.gather(
                _send_call(call, pcm, rate, channels, args.chunk_ms, args.duration),
                _recv_call(call),
            )
        except grpc.aio.AioRpcError as e:
            print(f"gRPC error: {e.code().name}: {e.details()}", file=sys.stderr)
            sys.exit(1)


def cli() -> None:
    """Sync entrypoint for `uv run phone-call`."""
    asyncio.run(main())


if __name__ == "__main__":
    cli()
