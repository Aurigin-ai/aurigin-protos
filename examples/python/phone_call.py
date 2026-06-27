"""Single live phone call → DetectDeepfake.

A minimal worked example of the **integration pattern** for a real-time
audio source (FreeSWITCH `mod_audio_fork`, Twilio Media Streams, a SIPREC
tap, etc.) piped over a gRPC bidi stream. Reads a WAV file from disk and
streams its PCM in real time so the example is self-contained; in a
production integration you'd replace the WAV reader with your media-fork
socket reader — the rest of the file (bidi setup, paced send loop,
concurrent response reader) stays the same.

For load-testing N concurrent calls against a real backend (finding the
connection-count knee, comparing fp16/fp32 throughput, etc.), see the
sibling `phone_call_burst.py`.

CLI:
    python phone_call.py [--audio FILE] [--target localhost:50051]
                         [--chunk-ms 100] [--duration 30]
                         [--scenario-id ID]

Defaults:
  - if --audio is omitted, picks the first .wav in `examples/audio/`
    (gitignored — drop a fixture in).
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from aurigin.deepfake_detection.v1 import deepfake_detection_pb2 as pb
from aurigin.deepfake_detection.v1 import deepfake_detection_pb2_grpc as pb_grpc
from twilio.tme.extensions.common.v1 import audio_buffer_pb2 as ab_pb

from common import (
    ChunkRow, WavData, install_signal_shutdown, make_aio_channel, read_wav,
    transport_label,
)

DEFAULT_CHUNK_MS = 100
DEFAULT_DURATION_S = 30.0


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
            "Drop a 16-bit PCM or 32-bit IEEE-float WAV in examples/audio/ or pass one with --audio.",
        )
    return wavs[0]


async def send_call(call, wav: WavData, chunk_ms: int, duration_s: float) -> None:
    """Stream `wav` in real-time-paced AudioBuffer chunks until `duration_s` is up.

    Also imported by phone_call_burst.py — same loop, just instantiated N times.

    THIS LOOP IS THE INTEGRATION PATTERN. For a FreeSWITCH `mod_audio_fork`
    or Twilio Media Stream, replace the `pcm[cursor:…]` slicing with:

        async for frame in fork_socket:
            await call.write(pb.DetectDeepfakeRequest(
                audio=ab_pb.AudioBuffer(
                    type="audio/x-raw", format="S16LE",
                    channels=1, rate=8000,
                    duration_ns=int(len(frame) / 8000 * 1e9),
                    pts_ns=pts_ns,
                    size=len(frame), buffer=frame,
                ),
            ))
            pts_ns += duration_ns

    No manual `asyncio.sleep` pacing needed in that version — the socket IS
    the clock. We loop a finite WAV here just so the example self-contains.
    """
    bytes_per_chunk = max(1, int(wav.rate * chunk_ms / 1000) * wav.bytes_per_sample)
    chunk_seconds = chunk_ms / 1000

    await call.write(pb.DetectDeepfakeRequest(create_session_request=pb.CreateSessionRequest()))

    loop = asyncio.get_running_loop()
    next_send = loop.time()
    pts_ns = 0
    cursor = 0
    elapsed_s = 0.0

    while elapsed_s < duration_s:
        chunk = wav.samples[cursor : cursor + bytes_per_chunk]
        cursor += bytes_per_chunk
        if cursor >= len(wav.samples):
            cursor = 0  # loop the file so a short fixture can simulate a long call
        actual_frames = len(chunk) // wav.bytes_per_sample
        duration_ns = int(actual_frames / wav.rate * 1e9)

        await call.write(pb.DetectDeepfakeRequest(
            audio=ab_pb.AudioBuffer(
                type="audio/x-raw", format=wav.wire_format,
                channels=wav.channels, rate=wav.rate,
                duration_ns=duration_ns, pts_ns=pts_ns,
                size=len(chunk), buffer=chunk,
            ),
        ))
        pts_ns += duration_ns
        elapsed_s += chunk_seconds

        # Deadline-based wallclock pacing — ~1 s of audio per second of
        # wallclock. Drift-free: if a single send blocks longer than
        # chunk_seconds, the next sleep_for shrinks rather than compounding.
        next_send += chunk_seconds
        sleep_for = next_send - loop.time()
        if sleep_for > 0:
            await asyncio.sleep(sleep_for)

    await call.done_writing()


async def recv_call(
    call,
    *,
    label: str | None = None,
    sink: dict | None = None,
) -> None:
    """Print every server response as it arrives. Runs concurrently with the
    sender via asyncio.gather — that's the other half of the bidi pattern.

    Optional `label` / `sink` exist so phone_call_burst.py can reuse this
    same function: `label` prefixes every log line with the per-stream id,
    and `sink` (a dict) captures session_id + chunks + final aggregates so
    the caller can flush a CSV row block after the call ends.
    """
    def log(message: str) -> None:
        print(f"[{label}] {message}" if label else message)

    async for response in call:
        kind = response.WhichOneof("response")
        if kind == "create_session_response":
            session_id = response.create_session_response.session_id
            if sink is not None:
                sink["session_id"] = session_id
            log(f"📞 Session: {session_id}")
        elif kind == "analysis_result":
            r = response.analysis_result
            if sink is not None:
                sink["chunks"].append(ChunkRow(
                    offset_ms=r.audio_offset_ms, duration_ms=r.duration_ms,
                    confidence=r.confidence, label=r.label,
                ))
            log(
                f"   Analysis @ {r.audio_offset_ms / 1000:6.2f}s "
                f"| score={r.score:.3f} | label={r.label:18s} | confidence={r.confidence:.2f}",
            )
        elif kind == "final_result":
            f = response.final_result
            if sink is not None:
                sink["audio_duration_ms"] = f.total_audio_ms
                sink["global_result"] = f.overall_label
            log(
                f"☎️  Call ended | total={f.total_audio_ms / 1000:.2f}s "
                f"| score={f.overall_score:.3f} | label={f.overall_label} | analyses={f.analysis_count}",
            )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", maxsplit=1)[0])
    parser.add_argument("--audio", type=Path, default=None, help="WAV file to stream (defaults to first in audio/)")
    parser.add_argument("--duration", type=float, default=DEFAULT_DURATION_S, help="Call length in seconds")
    parser.add_argument("--chunk-ms", type=int, default=DEFAULT_CHUNK_MS, help="Audio frame size in milliseconds")
    parser.add_argument("--target", default="localhost:50051", help="gRPC server host:port")
    parser.add_argument(
        "--scenario-id", default=None,
        help="Server-side simulator scenario to request (sent as x-scenario-id metadata).",
    )
    args = parser.parse_args()

    audio_path = _resolve_audio(args.audio)
    wav = read_wav(audio_path)
    scenario_suffix = f" | scenario={args.scenario_id}" if args.scenario_id else ""
    print(
        f"📞 Calling {args.target} | source={audio_path.name} "
        f"({wav.duration_s:.2f}s @ {wav.rate}Hz/{wav.channels}ch {wav.wire_format}) "
        f"| duration={args.duration:.1f}s | frame={args.chunk_ms}ms"
        f"{scenario_suffix} | transport={transport_label()}",
    )
    print("─" * 70)

    metadata: tuple[tuple[str, str], ...] = (
        (("x-scenario-id", args.scenario_id),) if args.scenario_id else ()
    )

    async with make_aio_channel(args.target) as channel:
        stub = pb_grpc.DeepfakeDetectionStub(channel)
        call = stub.DetectDeepfake(metadata=metadata) if metadata else stub.DetectDeepfake()

        # Send + receive concurrently: this is the bidi pattern. Sender writes
        # AudioBuffer messages at real-time pace; receiver reads
        # AnalysisResult / FinalResult messages as the server emits them.
        # Wrapped in an inner coroutine so we can asyncio.create_task() it —
        # gather() returns a _GatheringFuture (not a coroutine), which
        # create_task() rejects. The task wrapper is what
        # install_signal_shutdown cancels on Ctrl-C.
        async def _bidi() -> None:
            await asyncio.gather(send_call(call, wav, args.chunk_ms, args.duration), recv_call(call))

        task = asyncio.create_task(_bidi())
        install_signal_shutdown([task])
        try:
            await task
        except asyncio.CancelledError:
            pass  # graceful SIGINT/SIGTERM exit — banner already printed by the handler


def cli() -> None:
    """Sync entrypoint for `uv run phone-call`."""
    # See the long note in server.py: asyncio.run() installs a Python-level
    # SIGINT handler that conflicts with grpc.aio's own handlers, racing on
    # Ctrl-C and leaving a noisy traceback. Driving the loop manually keeps
    # the shutdown path clean: KeyboardInterrupt → finally → loop.close().
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(main())
    finally:
        loop.close()


if __name__ == "__main__":
    cli()
