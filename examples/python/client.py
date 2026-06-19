"""Minimal gRPC client using the generated aurigin-protos package.

If `examples/audio/` contains .wav files, opens one session per file and
streams its PCM through DetectDeepfake. Otherwise streams 3 s of silence
as a connectivity smoke-test.

CLI:
    python client.py [--target HOST:PORT]
"""

from __future__ import annotations

import argparse
import wave
from pathlib import Path

import grpc

from aurigin.deepfake_detection.v1 import deepfake_detection_pb2 as pb
from aurigin.deepfake_detection.v1 import deepfake_detection_pb2_grpc as pb_grpc
from twilio.tme.extensions.common.v1 import audio_buffer_pb2 as ab_pb

DEFAULT_RATE = 16000
CHANNELS = 1
CHUNK_MS = 500
SILENCE_CHUNKS = 6  # 3 seconds


def _silent_session_iter():
    """Fallback iterator: CreateSession + 6 × 500 ms of silence at 16 kHz."""
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


def _wav_session_iter(path: Path):
    """Stream a WAV file (S16LE) as CreateSession + AudioBuffer chunks."""
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError(f"{path.name}: expected 16-bit PCM, got {w.getsampwidth() * 8}-bit")
        channels = w.getnchannels()
        rate = w.getframerate()
        frames_per_chunk = int(rate * CHUNK_MS / 1000)
        bytes_per_chunk = frames_per_chunk * channels * 2

        yield pb.DetectDeepfakeRequest(create_session_request=pb.CreateSessionRequest())

        pts_ns = 0
        while True:
            chunk = w.readframes(frames_per_chunk)
            if not chunk:
                break
            actual_frames = len(chunk) // (channels * 2)
            duration_ns = int(actual_frames / rate * 1e9)
            yield pb.DetectDeepfakeRequest(
                audio=ab_pb.AudioBuffer(
                    type="audio/x-raw", format="S16LE",
                    channels=channels, rate=rate,
                    duration_ns=duration_ns, pts_ns=pts_ns,
                    size=len(chunk), buffer=chunk,
                ),
            )
            pts_ns += duration_ns


def _run_session(stub, request_iter, label: str) -> None:
    print(f"\n=== {label} ===")
    for response in stub.DetectDeepfake(request_iter):
        kind = response.WhichOneof("response")
        if kind == "create_session_response":
            print(f"Session: {response.create_session_response.session_id}")
        elif kind == "analysis_result":
            r = response.analysis_result
            print(f"Analysis | offset={r.audio_offset_ms}ms | score={r.score:.3f} | label={r.label} | confidence={r.confidence:.2f}")
        elif kind == "final_result":
            f = response.final_result
            print(f"FINAL    | total={f.total_audio_ms}ms | score={f.overall_score:.3f} | label={f.overall_label} | analyses={f.analysis_count}")


def main(target: str = "localhost:50051", audio_dir: str | Path | None = None) -> None:
    audio_dir = Path(audio_dir).resolve() if audio_dir else Path(__file__).resolve().parent.parent / "audio"
    wavs = sorted(audio_dir.glob("*.wav")) if audio_dir.is_dir() else []

    from _tls import make_sync_channel, transport_label
    print(f"# transport={transport_label()}")
    with make_sync_channel(target) as channel:
        stub = pb_grpc.DeepfakeDetectionStub(channel)
        if not wavs:
            _run_session(stub, _silent_session_iter(), "silence (3 s @ 16 kHz)")
            return
        for wav in wavs:
            _run_session(stub, _wav_session_iter(wav), wav.name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n", maxsplit=1)[0])
    parser.add_argument("--target", default="localhost:50051", help="gRPC server host:port (default: localhost:50051)")
    parser.add_argument("--audio-dir", default=None, help="Directory to scan for *.wav (default: examples/audio/)")
    args = parser.parse_args()
    main(args.target, args.audio_dir)
