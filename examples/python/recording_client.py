"""Minimal Recording gRPC client using the generated aurigin-protos package.

Opens one Recording.Record session per *.wav in `examples/audio/` and
streams its PCM through, then half-closes and prints the terminal
RecordingComplete. Falls back to 3 s of silence as a connectivity
smoke-test when no WAVs are present (still exercises the full open +
stream + half-close round-trip).

CLI:
    python recording_client.py [--target HOST:PORT] [--audio-dir DIR]
"""

from __future__ import annotations

import argparse
from pathlib import Path

from aurigin.media.v1 import audio_frame_pb2 as af_pb
from aurigin.recording.v1 import recording_pb2 as pb
from aurigin.recording.v1 import recording_pb2_grpc as pb_grpc

from common import WavData, make_sync_channel, read_wav, transport_label

DEFAULT_RATE = 16000
CHANNELS = 1
CHUNK_MS = 500
# 3 s of silence at 16 kHz — enough to exercise the open + stream +
# half-close path in CI without any WAV fixtures present.
SILENCE_CHUNKS = 6


def _silent_session_iter():
    """Fallback iterator: CreateSession + 6 × 500 ms of silence at 16 kHz."""
    yield pb.RecordRequest(create_session_request=pb.CreateSessionRequest())
    pts_ns = 0
    for _ in range(SILENCE_CHUNKS):
        samples = int(DEFAULT_RATE * CHUNK_MS / 1000)
        chunk = b"\x00\x00" * samples * CHANNELS
        yield pb.RecordRequest(
            audio_frame=af_pb.AudioFrame(
                codec=af_pb.AUDIO_CODEC_S16LE,
                sample_rate_hz=DEFAULT_RATE,
                channels=CHANNELS,
                payload=chunk,
                pts_ns=pts_ns,
            ),
        )
        pts_ns += CHUNK_MS * 1_000_000


def _wav_session_iter(wav: WavData):
    """Stream a WAV file as CreateSession + AudioFrame chunks."""
    bytes_per_chunk = int(wav.rate * CHUNK_MS / 1000) * wav.bytes_per_sample

    yield pb.RecordRequest(create_session_request=pb.CreateSessionRequest())

    pts_ns = 0
    for start in range(0, len(wav.samples), bytes_per_chunk):
        chunk = wav.samples[start : start + bytes_per_chunk]
        if not chunk:
            break
        actual_frames = len(chunk) // wav.bytes_per_sample
        yield pb.RecordRequest(
            audio_frame=af_pb.AudioFrame(
                codec=wav.audio_codec,
                sample_rate_hz=wav.rate,
                channels=wav.channels,
                payload=chunk,
                pts_ns=pts_ns,
            ),
        )
        pts_ns += int(actual_frames / wav.rate * 1e9)


def _run_session(stub, request_iter, label: str) -> None:
    print(f"\n=== {label} ===")
    for response in stub.Record(request_iter):
        kind = response.WhichOneof("response")
        if kind == "create_session_response":
            r = response.create_session_response
            print(f"Session: {r.recording_id} | planned_uri={r.planned_blob_uri}")
        elif kind == "progress":
            p = response.progress
            print(f"Progress | bytes={p.bytes_written} | duration={p.duration_ms}ms")
        elif kind == "complete":
            c = response.complete
            print(
                f"COMPLETE | id={c.recording_id} | uri={c.blob_uri} | "
                f"duration={c.duration_ms}ms | bytes={c.bytes} | "
                f"sha256={c.sha256[:16]}… | codec={c.codec} | rate={c.sample_rate_hz}Hz",
            )


def main(target: str = "localhost:50051", audio_dir: str | Path | None = None) -> None:
    audio_dir = (
        Path(audio_dir).resolve()
        if audio_dir
        else Path(__file__).resolve().parent.parent / "audio"
    )
    wavs = sorted(audio_dir.glob("*.wav")) if audio_dir.is_dir() else []

    print(f"# transport={transport_label()}")

    with make_sync_channel(target) as channel:
        stub = pb_grpc.RecordingStub(channel)
        if not wavs:
            _run_session(stub, _silent_session_iter(), "silence (3 s @ 16 kHz)")
            return
        for path in wavs:
            # Pre-validate before opening the stream — a read_wav
            # exception inside the request generator after the call
            # has started surfaces as an opaque StatusCode.UNKNOWN
            # from gRPC. Catching here gives a clean skip line and
            # keeps the dir scan going.
            try:
                wav = read_wav(path)
            except ValueError as exc:
                print(f"\n=== {path.name} ===\nSKIPPED: {exc}")
                continue
            _run_session(stub, _wav_session_iter(wav), path.name)


def cli() -> None:
    """Entry-point wrapper that parses CLI args, then calls main().

    `uv run recording-client` (per pyproject.toml [project.scripts])
    lands here, NOT in `main()` — without this wrapper the entry-point
    shim would call `main()` with no args and silently ignore every
    flag on the command line. `python recording_client.py` also lands
    here via __main__ below.
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n", maxsplit=1)[0])
    parser.add_argument(
        "--target",
        default="localhost:50051",
        help="gRPC server host:port (default: localhost:50051)",
    )
    parser.add_argument(
        "--audio-dir",
        default=None,
        help="Directory to scan for *.wav (default: examples/audio/)",
    )
    args = parser.parse_args()
    main(args.target, args.audio_dir)


if __name__ == "__main__":
    cli()
