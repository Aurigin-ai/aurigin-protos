"""
Simulate a mobile phone call by streaming audio to DetectDeepfake.

Default mode reads a WAV file (looped to fill --duration, paced in real
time). With --fifo, reads from a named pipe instead — designed for a
FreeSWITCH `record_session <fifo>` source feeding G.711 μ-law at 8 kHz.

In both modes analysis events print as they arrive, thanks to grpc.aio's
concurrent send/recv tasks.

CLI:
    # File mode (default)
    python phone_call.py [--audio path/to.wav] [--duration 30] \\
        [--chunk-ms 100] [--target localhost:50051]

    # FIFO mode (overrides file mode when supplied)
    python phone_call.py --fifo /var/lib/freeswitch/recordings/live.r16 \\
        [--codec mulaw|pcm16] [--chunk-ms 100] [--target localhost:50051]

Defaults:
  - File mode: if --audio is omitted, picks the first .wav in
    `examples/audio/` (gitignored — drop a fixture in).
  - FIFO mode: --codec defaults to `mulaw` (G.711 narrowband, the
    FreeSWITCH default). `pcm16` forwards bytes as 16-bit linear PCM
    without decoding.
"""

from __future__ import annotations

import argparse
import asyncio
import audioop  # stdlib; deprecated in 3.13, still present in 3.11/3.12
import sys
import wave
from pathlib import Path

import grpc

from aurigin.deepfake_detection.v1 import deepfake_detection_pb2 as pb
from aurigin.deepfake_detection.v1 import deepfake_detection_pb2_grpc as pb_grpc
from twilio.tme.extensions.common.v1 import audio_buffer_pb2 as ab_pb

DEFAULT_CHUNK_MS = 100
DEFAULT_DURATION_S = 30.0

# FreeSWITCH narrowband defaults for FIFO mode; both codec paths assume mono.
FIFO_SAMPLE_RATE = 8000
FIFO_CHANNELS = 1


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


async def _send_fifo(call, fifo_path: Path, codec: str, chunk_ms: int) -> None:
    """Stream a FreeSWITCH-style named pipe until the writer closes.

    The writer (FreeSWITCH `record_session`, or anything pumping audio at
    real-time rate) provides natural pacing — we just read, decode if
    needed, and forward as AudioBuffers.
    """
    samples_per_chunk = int(FIFO_SAMPLE_RATE * chunk_ms / 1000)
    if codec == "mulaw":
        bytes_per_input_chunk = samples_per_chunk * FIFO_CHANNELS  # 1 byte/sample
    elif codec == "pcm16":
        bytes_per_input_chunk = samples_per_chunk * FIFO_CHANNELS * 2  # 2 bytes/sample
    else:
        raise ValueError(f"Unknown codec: {codec}")

    await call.write(pb.DetectDeepfakeRequest(create_session_request=pb.CreateSessionRequest()))

    loop = asyncio.get_running_loop()
    print(f"⏳ Waiting for writer on {fifo_path} (open() blocks)...")
    fifo = await loop.run_in_executor(None, lambda: open(str(fifo_path), "rb"))
    print(">>> Writer connected, streaming audio")
    try:
        pts_ns = 0
        while True:
            data = await loop.run_in_executor(None, fifo.read, bytes_per_input_chunk)
            if not data:
                print("<<< FIFO closed by writer")
                break

            # Decode μ-law -> linear S16LE; pcm16 passes through unchanged.
            pcm = audioop.ulaw2lin(data, 2) if codec == "mulaw" else data
            actual_frames = len(pcm) // (2 * FIFO_CHANNELS)
            duration_ns = int(actual_frames / FIFO_SAMPLE_RATE * 1e9)

            await call.write(
                pb.DetectDeepfakeRequest(
                    audio=ab_pb.AudioBuffer(
                        type="audio/x-raw",
                        format="S16LE",
                        channels=FIFO_CHANNELS,
                        rate=FIFO_SAMPLE_RATE,
                        duration_ns=duration_ns,
                        pts_ns=pts_ns,
                        size=len(pcm),
                        buffer=pcm,
                    ),
                )
            )
            pts_ns += duration_ns
    finally:
        fifo.close()

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
    parser.add_argument("--duration", type=float, default=DEFAULT_DURATION_S, help="Call length in seconds (file mode only)")
    parser.add_argument("--chunk-ms", type=int, default=DEFAULT_CHUNK_MS, help="Audio frame size in milliseconds")
    parser.add_argument("--target", default="localhost:50051", help="gRPC server host:port")
    parser.add_argument("--fifo", type=Path, default=None, help="Read live audio from a named pipe instead of a WAV file")
    parser.add_argument("--codec", choices=["mulaw", "pcm16"], default="mulaw", help="FIFO codec (file mode auto-detects)")
    args = parser.parse_args()

    async with grpc.aio.insecure_channel(args.target) as channel:
        stub = pb_grpc.DeepfakeDetectionStub(channel)
        call = stub.DetectDeepfake()

        if args.fifo is not None:
            print(
                f"📞 Calling {args.target} | source={args.fifo} (FIFO, {args.codec}, "
                f"{FIFO_SAMPLE_RATE}Hz/{FIFO_CHANNELS}ch) | frame={args.chunk_ms}ms"
            )
            print("─" * 70)
            sender = _send_fifo(call, args.fifo, args.codec, args.chunk_ms)
        else:
            audio_path = _resolve_audio(args.audio)
            pcm, rate, channels = _load_pcm(audio_path)
            print(
                f"📞 Calling {args.target} | source={audio_path.name} "
                f"({len(pcm) / (rate * 2 * channels):.2f}s @ {rate}Hz/{channels}ch) "
                f"| duration={args.duration:.1f}s | frame={args.chunk_ms}ms"
            )
            print("─" * 70)
            sender = _send_call(call, pcm, rate, channels, args.chunk_ms, args.duration)

        try:
            await asyncio.gather(sender, _recv_call(call))
        except grpc.aio.AioRpcError as e:
            print(f"gRPC error: {e.code().name}: {e.details()}", file=sys.stderr)
            sys.exit(1)


def cli() -> None:
    """Sync entrypoint for `uv run phone-call`."""
    asyncio.run(main())


if __name__ == "__main__":
    cli()
