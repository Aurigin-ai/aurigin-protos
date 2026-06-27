"""N concurrent live phone calls → DetectDeepfake.

The recommended multi-call integration pattern: when a single PBX (e.g.
a FreeSWITCH instance running `mod_audio_fork`) is handling many calls
simultaneously, you want **one** long-lived gRPC channel multiplexing
N concurrent bidi streams — not N separate channels. This file shows
exactly that shape:

  - One `make_aio_channel(target)` for the whole run.
  - N concurrent `stub.DetectDeepfake()` bidi streams over that channel,
    each running the same `send_call` / `recv_call` loop that
    `phone_call.py` documents in detail (imported, not duplicated).
  - Per-stream `call-NN` label prefixes every log line so interleaved
    output is grep-friendly.
  - Optional `--stagger-ms` to spread call starts out (mimics a real
    PBX where calls arrive over time, not all at exactly t=0).
  - Graceful SIGINT/SIGTERM via `common.install_signal_shutdown` cancels
    every in-flight stream cleanly — no orphan bidi sockets, summary
    still prints.
  - Optional `--csv PATH` captures per-chunk results across all streams
    in one file — useful for finding the connection-count knee on a
    real backend (where do errors start? when does median latency
    degrade? when does the GPU saturate?).

CLI:
    python phone_call_burst.py [--audio FILE] [--target localhost:50051]
                               [--chunk-ms 100] [--duration 30]
                               [--concurrency 5] [--stagger-ms 500]
                               [--scenario-id ID] [--csv PATH]

Defaults:
  - --concurrency 1 (effectively `phone_call.py`)
  - --stagger-ms 0   (thundering-herd: every call starts at the same instant)
  - --audio omitted  → picks the first .wav in `examples/audio/`
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

import grpc

from aurigin.deepfake_detection.v1 import deepfake_detection_pb2_grpc as pb_grpc

from common import (
    ResultCSV, WavData,
    install_signal_shutdown, make_aio_channel, read_wav, transport_label,
)
# The per-call building blocks. `phone_call_burst` is "send_call + recv_call
# instantiated N times over one channel" — importing keeps that relationship
# explicit and prevents drift between the two files' send/recv loops.
from phone_call import recv_call, send_call

DEFAULT_CHUNK_MS = 100
DEFAULT_DURATION_S = 30.0
DEFAULT_CONCURRENCY = 1


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


async def _run_one_call(
    stub, label: str, wav: WavData, chunk_ms: int, duration_s: float,
    metadata: tuple[tuple[str, str], ...], delay_s: float,
    csv_out: ResultCSV | None, file_name: str,
) -> tuple[str, BaseException | None]:
    """Open one bidi stream, run send_call + recv_call concurrently. Returns
    (label, exception_or_none) so a failing call doesn't take the others
    down — multi-call survival counts need to be meaningful.

    The per-call work is exactly what phone_call.py does (same imports,
    same loops); the multi-call coordination — fan-out, stagger, signal
    handler, summary — lives in main()."""
    if delay_s > 0:
        await asyncio.sleep(delay_s)
    call = stub.DetectDeepfake(metadata=metadata) if metadata else stub.DetectDeepfake()
    sink: dict = {
        "session_id": "", "chunks": [],
        "audio_duration_ms": 0, "global_result": "unknown",
    }
    t_start = time.perf_counter()
    try:
        await asyncio.gather(
            send_call(call, wav, chunk_ms, duration_s),
            recv_call(call, label=label, sink=sink),
        )
        result: BaseException | None = None
    except BaseException as exc:  # CancelledError, AioRpcError, etc.
        result = exc
    finally:
        if csv_out is not None:
            csv_out.write_session(
                file_name, sink["session_id"], sink["chunks"],
                sink["audio_duration_ms"], sink["global_result"],
                (time.perf_counter() - t_start) * 1000.0,
            )
    return label, result


def _make_labels(n: int) -> list[str]:
    width = max(2, len(str(n)))
    return [f"call-{i:0{width}d}" for i in range(1, n + 1)]


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", maxsplit=1)[0])
    parser.add_argument("--audio", type=Path, default=None, help="WAV file to stream (defaults to first in audio/)")
    parser.add_argument("--duration", type=float, default=DEFAULT_DURATION_S, help="Per-call length in seconds")
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
    parser.add_argument("--csv", default=None, help="Write per-chunk results to this path (overwrites)")
    args = parser.parse_args()

    if args.concurrency < 1:
        parser.error("--concurrency must be >= 1")
    if args.stagger_ms < 0:
        parser.error("--stagger-ms must be >= 0")

    audio_path = _resolve_audio(args.audio)
    wav = read_wav(audio_path)
    labels = _make_labels(args.concurrency)
    metadata: tuple[tuple[str, str], ...] = (
        (("x-scenario-id", args.scenario_id),) if args.scenario_id else ()
    )
    scenario_suffix = f" | scenario={args.scenario_id}" if args.scenario_id else ""
    stagger_suffix = f" | stagger={args.stagger_ms}ms" if args.stagger_ms else ""

    csv_out: ResultCSV | None = ResultCSV(args.csv) if args.csv else None
    if csv_out is not None:
        print(f"# csv={args.csv}")

    print(
        f"📞 Calling {args.target} | source={audio_path.name} "
        f"({wav.duration_s:.2f}s @ {wav.rate}Hz/{wav.channels}ch {wav.wire_format}) "
        f"| duration={args.duration:.1f}s | frame={args.chunk_ms}ms | "
        f"concurrency={args.concurrency}{stagger_suffix}{scenario_suffix} "
        f"| transport={transport_label()}",
    )
    print("─" * 70)

    try:
        async with make_aio_channel(args.target) as channel:
            stub = pb_grpc.DeepfakeDetectionStub(channel)

            stagger_s = args.stagger_ms / 1000
            tasks = [
                asyncio.create_task(_run_one_call(
                    stub, label, wav, args.chunk_ms, args.duration,
                    metadata, delay_s=i * stagger_s,
                    csv_out=csv_out, file_name=audio_path.name,
                ))
                for i, label in enumerate(labels)
            ]
            # Graceful Ctrl-C: cancel each per-call task. _run_one_call's
            # `except BaseException` converts CancelledError into a clean
            # (label, exc) tuple so asyncio.gather still completes with all
            # results — we get the usual summary instead of a traceback.
            shutdown = install_signal_shutdown(tasks)
            results = await asyncio.gather(*tasks)
    finally:
        if csv_out is not None:
            csv_out.close()

    failures = [(label, exc) for label, exc in results if exc is not None]
    if shutdown.seen:
        print("─" * 70, file=sys.stderr)
        print(
            f"Shutdown complete ({args.concurrency - len(failures)}/{args.concurrency} streams finished cleanly).",
            file=sys.stderr,
        )
        return
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


def cli() -> None:
    """Sync entrypoint for `uv run phone-call-burst`."""
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(main())
    finally:
        loop.close()


if __name__ == "__main__":
    cli()
