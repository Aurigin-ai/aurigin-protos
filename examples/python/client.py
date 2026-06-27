"""Minimal gRPC client using the generated aurigin-protos package.

If `examples/audio/` contains .wav files, opens one session per file and
streams its PCM through DetectDeepfake. Otherwise streams 5 s of silence
as a connectivity smoke-test.

CLI:
    python client.py [--target HOST:PORT] [--audio-dir DIR] [--csv PATH]
"""

from __future__ import annotations

import argparse
from pathlib import Path

from aurigin.deepfake_detection.v1 import deepfake_detection_pb2 as pb
from aurigin.deepfake_detection.v1 import deepfake_detection_pb2_grpc as pb_grpc
from twilio.tme.extensions.common.v1 import audio_buffer_pb2 as ab_pb

from common import (
    ChunkRow, ResultCSV, WavData, make_sync_channel, read_wav, transport_label,
)

DEFAULT_RATE = 16000
CHANNELS = 1
CHUNK_MS = 500
SILENCE_CHUNKS = 10  # 5 seconds — matches the dfs default analysis_interval_s=5.0
                     # so the fallback fires at least one analysis window in CI.


def _silent_session_iter():
    """Fallback iterator: CreateSession + 10 × 500 ms of silence at 16 kHz."""
    yield pb.DetectDeepfakeRequest(create_session_request=pb.CreateSessionRequest())
    pts_ns = 0
    for _ in range(SILENCE_CHUNKS):
        samples = int(DEFAULT_RATE * CHUNK_MS / 1000)
        chunk = b"\x00\x00" * samples * CHANNELS
        duration_ns = CHUNK_MS * 1_000_000
        yield pb.DetectDeepfakeRequest(
            audio=ab_pb.AudioBuffer(
                type="audio/x-raw", format="S16LE",
                channels=CHANNELS, rate=DEFAULT_RATE,
                duration_ns=duration_ns, pts_ns=pts_ns,
                size=len(chunk), buffer=chunk,
            ),
        )
        pts_ns += duration_ns


def _wav_session_iter(wav: WavData):
    """Stream a WAV file (S16LE or F32LE) as CreateSession + AudioBuffer chunks."""
    bytes_per_chunk = int(wav.rate * CHUNK_MS / 1000) * wav.bytes_per_sample

    yield pb.DetectDeepfakeRequest(create_session_request=pb.CreateSessionRequest())

    pts_ns = 0
    for start in range(0, len(wav.samples), bytes_per_chunk):
        chunk = wav.samples[start : start + bytes_per_chunk]
        if not chunk:
            break
        actual_frames = len(chunk) // wav.bytes_per_sample
        duration_ns = int(actual_frames / wav.rate * 1e9)
        yield pb.DetectDeepfakeRequest(
            audio=ab_pb.AudioBuffer(
                type="audio/x-raw", format=wav.wire_format,
                channels=wav.channels, rate=wav.rate,
                duration_ns=duration_ns, pts_ns=pts_ns,
                size=len(chunk), buffer=chunk,
            ),
        )
        pts_ns += duration_ns


def _run_session(stub, request_iter, label: str, csv_out: ResultCSV | None = None) -> None:
    print(f"\n=== {label} ===")
    session_id: str = ""
    chunks: list[ChunkRow] = []
    audio_duration_ms: int = 0
    global_result: str = "unknown"
    # Wallclock from right before the bidi opens to FinalResult-received.
    # Captures network + server-side work + client-side iteration cost — the
    # "user-perceived" latency for processing this file.
    import time
    t_start = time.perf_counter()

    for response in stub.DetectDeepfake(request_iter):
        kind = response.WhichOneof("response")
        if kind == "create_session_response":
            session_id = response.create_session_response.session_id
            print(f"Session: {session_id}")
        elif kind == "analysis_result":
            r = response.analysis_result
            chunks.append(ChunkRow(
                offset_ms=r.audio_offset_ms,
                duration_ms=r.duration_ms,
                confidence=r.confidence,
                label=r.label,
            ))
            print(f"Analysis | offset={r.audio_offset_ms}ms | score={r.score:.3f} | label={r.label} | confidence={r.confidence:.2f}")
        elif kind == "final_result":
            f = response.final_result
            audio_duration_ms = f.total_audio_ms
            global_result = f.overall_label
            print(f"FINAL    | total={f.total_audio_ms}ms | score={f.overall_score:.3f} | label={f.overall_label} | analyses={f.analysis_count}")

    processing_time_ms = (time.perf_counter() - t_start) * 1000.0
    if csv_out is not None:
        csv_out.write_session(
            label, session_id, chunks, audio_duration_ms, global_result,
            processing_time_ms,
        )


def main(
    target: str = "localhost:50051",
    audio_dir: str | Path | None = None,
    csv_path: str | Path | None = None,
) -> None:
    audio_dir = Path(audio_dir).resolve() if audio_dir else Path(__file__).resolve().parent.parent / "audio"
    wavs = sorted(audio_dir.glob("*.wav")) if audio_dir.is_dir() else []

    print(f"# transport={transport_label()}")

    csv_out: ResultCSV | None = None
    if csv_path:
        csv_out = ResultCSV(csv_path)
        print(f"# csv={csv_path}")

    try:
        with make_sync_channel(target) as channel:
            stub = pb_grpc.DeepfakeDetectionStub(channel)
            if not wavs:
                _run_session(stub, _silent_session_iter(), "silence (5 s @ 16 kHz)", csv_out)
                return
            for path in wavs:
                # Pre-validate before opening the stream. If read_wav raises
                # (mislabeled .wav file, unsupported format, broken header),
                # we'd otherwise surface a cryptic
                #   StatusCode.UNKNOWN: "Exception iterating requests!"
                # from gRPC because the exception fires inside the request
                # generator AFTER the call has started. Catching here lets
                # us print a clear skip line and keep going through the dir.
                try:
                    wav = read_wav(path)
                except ValueError as exc:
                    print(f"\n=== {path.name} ===\nSKIPPED: {exc}")
                    continue
                _run_session(stub, _wav_session_iter(wav), path.name, csv_out)
    finally:
        if csv_out is not None:
            csv_out.close()


def cli() -> None:
    """Entry-point wrapper that parses CLI args, then calls main().

    `uv run client` (per pyproject.toml [project.scripts]) lands here, NOT
    in `main()` — without this wrapper the entry-point shim would call
    `main()` with no args and silently ignore every flag on the command
    line (including `--csv`, `--target`, `--audio-dir`). `python client.py`
    also lands here via __main__ below.
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n", maxsplit=1)[0])
    parser.add_argument("--target", default="localhost:50051", help="gRPC server host:port (default: localhost:50051)")
    parser.add_argument("--audio-dir", default=None, help="Directory to scan for *.wav (default: examples/audio/)")
    parser.add_argument("--csv", default=None, help="Write per-chunk results to this path (overwrites)")
    args = parser.parse_args()
    main(args.target, args.audio_dir, args.csv)


if __name__ == "__main__":
    cli()
