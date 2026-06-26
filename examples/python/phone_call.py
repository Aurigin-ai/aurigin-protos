"""
Simulate one or more mobile phone calls by streaming audio to DetectDeepfake.

Reads a WAV file (looped to fill --duration, paced in real time) and prints
analysis events as they arrive (grpc.aio runs sender + receiver concurrently).

With --concurrency N, opens N concurrent bidi streams over a single
gRPC channel. Each stream gets a client-side label (`call-01`, `call-02`,
...) that prefixes every log line so interleaved output stays readable.
The server-issued session_id is logged per stream alongside the label.

CLI:
    python phone_call.py [--audio path/to.wav] [--duration 30] \\
        [--chunk-ms 100] [--target localhost:50051] [--concurrency 1] \\
        [--scenario-id ID]

Defaults:
  - if --audio is omitted, picks the first .wav in `examples/audio/`
    (gitignored — drop a fixture in).
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from pathlib import Path

import grpc

from aurigin.deepfake_detection.v1 import deepfake_detection_pb2 as pb
from aurigin.deepfake_detection.v1 import deepfake_detection_pb2_grpc as pb_grpc
from twilio.tme.extensions.common.v1 import audio_buffer_pb2 as ab_pb

DEFAULT_CHUNK_MS = 100
DEFAULT_DURATION_S = 30.0
DEFAULT_CONCURRENCY = 1

_WAVE_FORMAT_PCM = 0x0001
_WAVE_FORMAT_IEEE_FLOAT = 0x0003


def _log(label: str, message: str, *, file=None) -> None:
    """Print one line prefixed by the per-stream label."""
    print(f"[{label}] {message}", file=file)


def _load_pcm(path: Path) -> tuple[bytes, int, int, str]:
    """Read a WAV file as (samples, sample_rate, channels, wire_format).

    `wire_format` is "S16LE" for 16-bit PCM or "F32LE" for 32-bit IEEE
    float — matching the formats the deepfake-service decoder accepts.
    Parses RIFF directly because the stdlib `wave` module rejects float WAVs.
    """
    buf = path.read_bytes()
    if buf[:4] != b"RIFF" or buf[8:12] != b"WAVE":
        raise ValueError(f"{path.name}: not a RIFF/WAVE file")
    offset = 12
    audio_format = channels = bits_per_sample = 0
    rate = 0
    data_start = -1
    data_len = 0
    while offset + 8 <= len(buf):
        chunk_id = buf[offset : offset + 4]
        size = int.from_bytes(buf[offset + 4 : offset + 8], "little")
        if chunk_id == b"fmt ":
            audio_format = int.from_bytes(buf[offset + 8 : offset + 10], "little")
            channels = int.from_bytes(buf[offset + 10 : offset + 12], "little")
            rate = int.from_bytes(buf[offset + 12 : offset + 16], "little")
            bits_per_sample = int.from_bytes(buf[offset + 22 : offset + 24], "little")
        elif chunk_id == b"data":
            data_start = offset + 8
            data_len = size
            break
        offset += 8 + size + (size & 1)
    if data_start < 0:
        raise ValueError(f"{path.name}: no data chunk")

    if audio_format == _WAVE_FORMAT_PCM and bits_per_sample == 16:
        wire_format = "S16LE"
    elif audio_format == _WAVE_FORMAT_IEEE_FLOAT and bits_per_sample == 32:
        wire_format = "F32LE"
    else:
        raise ValueError(
            f"{path.name}: unsupported WAV (format tag {audio_format}, "
            f"{bits_per_sample}-bit) — expected 16-bit PCM or 32-bit IEEE float",
        )
    return buf[data_start : data_start + data_len], rate, channels, wire_format


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
            "Drop a 16-bit PCM or 32-bit IEEE-float WAV in examples/audio/ or pass one with --audio."
        )
    return wavs[0]


async def _send_call(
    call,
    label: str,
    pcm: bytes,
    sample_rate: int,
    channels: int,
    wire_format: str,
    chunk_ms: int,
    duration_s: float,
) -> None:
    """Send CreateSession + paced AudioBuffers for `duration_s` seconds, then close write side."""
    del label  # silent sender; only the receiver prints per-stream output
    bytes_per_sample = (4 if wire_format == "F32LE" else 2) * channels
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
                    format=wire_format,
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


async def _recv_call(call, label: str) -> None:
    """Print every server response as it arrives, prefixed with the stream label."""
    async for response in call:
        kind = response.WhichOneof("response")
        if kind == "create_session_response":
            _log(label, f"📞 Session: {response.create_session_response.session_id}")
        elif kind == "analysis_result":
            r = response.analysis_result
            _log(
                label,
                f"   Analysis @ {r.audio_offset_ms / 1000:6.2f}s "
                f"| score={r.score:.3f} | label={r.label:18s} | confidence={r.confidence:.2f}",
            )
        elif kind == "final_result":
            f = response.final_result
            _log(
                label,
                f"☎️  Call ended | total={f.total_audio_ms / 1000:.2f}s "
                f"| score={f.overall_score:.3f} | label={f.overall_label} | analyses={f.analysis_count}",
            )


async def _run_one_call(
    stub,
    label: str,
    sender_factory,
    metadata: tuple[tuple[str, str], ...] = (),
    delay_s: float = 0.0,
) -> tuple[str, BaseException | None]:
    """Open one bidi stream, run sender + receiver concurrently. Return (label, exception_or_none).

    With delay_s > 0, sleeps that long before opening the stream — used by
    --stagger-ms to fan out the start times across concurrent calls.
    """
    if delay_s > 0:
        await asyncio.sleep(delay_s)
    call = stub.DetectDeepfake(metadata=metadata) if metadata else stub.DetectDeepfake()
    try:
        await asyncio.gather(sender_factory(call, label), _recv_call(call, label))
        return label, None
    except BaseException as exc:  # includes asyncio.CancelledError, grpc.aio.AioRpcError
        return label, exc


def _make_labels(n: int) -> list[str]:
    width = max(2, len(str(n)))
    return [f"call-{i:0{width}d}" for i in range(1, n + 1)]


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", maxsplit=1)[0])
    parser.add_argument("--audio", type=Path, default=None, help="WAV file to stream (defaults to first in audio/)")
    parser.add_argument("--duration", type=float, default=DEFAULT_DURATION_S, help="Call length in seconds")
    parser.add_argument("--chunk-ms", type=int, default=DEFAULT_CHUNK_MS, help="Audio frame size in milliseconds")
    parser.add_argument("--target", default="localhost:50051", help="gRPC server host:port")
    parser.add_argument(
        "-c", "--concurrency", type=int, default=DEFAULT_CONCURRENCY,
        help="Number of concurrent streams to open over a single channel.",
    )
    parser.add_argument(
        "--stagger-ms", type=int, default=0,
        help="Delay between successive concurrent kickoffs in ms. 0 = all calls start at the same instant.",
    )
    parser.add_argument(
        "--scenario-id", default=None,
        help="Server-side simulator scenario to request (sent as x-scenario-id metadata).",
    )
    args = parser.parse_args()

    if args.concurrency < 1:
        parser.error("--concurrency must be >= 1")
    if args.stagger_ms < 0:
        parser.error("--stagger-ms must be >= 0")

    labels = _make_labels(args.concurrency)
    metadata: tuple[tuple[str, str], ...] = (
        (("x-scenario-id", args.scenario_id),) if args.scenario_id else ()
    )
    scenario_suffix = f" | scenario={args.scenario_id}" if args.scenario_id else ""
    stagger_suffix = f" | stagger={args.stagger_ms}ms" if args.stagger_ms else ""

    from _tls import make_aio_channel, transport_label
    transport_suffix = f" | transport={transport_label()}"

    async with make_aio_channel(args.target) as channel:
        stub = pb_grpc.DeepfakeDetectionStub(channel)

        audio_path = _resolve_audio(args.audio)
        pcm, rate, channels, wire_format = _load_pcm(audio_path)
        bytes_per_sample = (4 if wire_format == "F32LE" else 2) * channels
        print(
            f"📞 Calling {args.target} | source={audio_path.name} "
            f"({len(pcm) / (rate * bytes_per_sample):.2f}s @ {rate}Hz/{channels}ch {wire_format}) "
            f"| duration={args.duration:.1f}s | frame={args.chunk_ms}ms | "
            f"concurrency={args.concurrency}{stagger_suffix}{scenario_suffix}{transport_suffix}"
        )
        print("─" * 70)

        def file_sender(call, label):
            return _send_call(call, label, pcm, rate, channels, wire_format, args.chunk_ms, args.duration)

        stagger_s = args.stagger_ms / 1000
        tasks = [
            asyncio.create_task(
                _run_one_call(stub, label, file_sender, metadata, delay_s=i * stagger_s),
            )
            for i, label in enumerate(labels)
        ]

        # Graceful Ctrl-C: cancel each per-call task. _run_one_call's
        # `except BaseException` converts CancelledError into a clean
        # (label, exc) tuple, so asyncio.gather still completes with all
        # results and we can print the usual summary instead of dumping a
        # traceback.
        loop = asyncio.get_running_loop()
        shutdown_seen = False

        def _request_shutdown(signame: str) -> None:
            nonlocal shutdown_seen
            if shutdown_seen:
                return
            shutdown_seen = True
            print(
                f"\nReceived {signame}, cancelling streams...",
                file=sys.stderr, flush=True,
            )
            for t in tasks:
                if not t.done():
                    t.cancel()

        for sig, name in ((signal.SIGINT, "SIGINT"), (signal.SIGTERM, "SIGTERM")):
            loop.add_signal_handler(sig, _request_shutdown, name)

        results = await asyncio.gather(*tasks)

    failures = [(label, exc) for label, exc in results if exc is not None]
    if shutdown_seen:
        # All failures are cancellations from our signal handler. The summary
        # below still prints so the user can see what each stream did before
        # tear-down, but the process exits 0 — Ctrl-C is intentional, not an
        # error.
        print("─" * 70, file=sys.stderr)
        print(f"Shutdown complete ({args.concurrency - len(failures)}/{args.concurrency} streams finished cleanly).", file=sys.stderr)
        return
    if args.concurrency > 1:
        print("─" * 70)
        ok = args.concurrency - len(failures)
        print(f"Summary: {ok}/{args.concurrency} streams OK, {len(failures)} failed")
        for label, exc in failures:
            if isinstance(exc, grpc.aio.AioRpcError):
                print(f"  [{label}] gRPC error: {exc.code().name}: {exc.details()}", file=sys.stderr)
            else:
                print(f"  [{label}] {type(exc).__name__}: {exc}", file=sys.stderr)
        if failures:
            sys.exit(1)
    elif failures:
        _, exc = failures[0]
        if isinstance(exc, grpc.aio.AioRpcError):
            print(f"gRPC error: {exc.code().name}: {exc.details()}", file=sys.stderr)
        else:
            print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)


def cli() -> None:
    """Sync entrypoint for `uv run phone-call`."""
    # See the long note in server.py: asyncio.run() installs a Python-level
    # SIGINT handler that fights our loop.add_signal_handler. Driving the
    # loop manually keeps add_signal_handler as the only SIGINT path so
    # Ctrl-C cancels the per-call tasks cleanly instead of unwinding through
    # the runner with a CancelledError traceback.
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(main())
    finally:
        loop.close()


if __name__ == "__main__":
    cli()
