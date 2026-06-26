"""Minimal gRPC client using the generated aurigin-protos package.

If `examples/audio/` contains .wav files, opens one session per file and
streams its PCM through DetectDeepfake. Otherwise streams 3 s of silence
as a connectivity smoke-test.

CLI:
    python client.py [--target HOST:PORT]
"""

from __future__ import annotations

import argparse
from pathlib import Path

from aurigin.deepfake_detection.v1 import deepfake_detection_pb2 as pb
from aurigin.deepfake_detection.v1 import deepfake_detection_pb2_grpc as pb_grpc
from twilio.tme.extensions.common.v1 import audio_buffer_pb2 as ab_pb

DEFAULT_RATE = 16000
CHANNELS = 1
CHUNK_MS = 500
SILENCE_CHUNKS = 6  # 3 seconds

# WAVE format tags we recognise. The stdlib `wave` module only handles PCM
# (tag 1) and raises on IEEE Float (tag 3), so we parse the RIFF header
# ourselves and dispatch to S16LE / F32LE.
_WAVE_FORMAT_PCM = 0x0001
_WAVE_FORMAT_IEEE_FLOAT = 0x0003


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


def _read_wav(path: Path) -> tuple[bytes, int, int, str]:
    """Parse a RIFF/WAVE file. Returns (samples, rate, channels, wire_format).

    `wire_format` is the AudioBuffer.format string: "S16LE" for 16-bit PCM,
    "F32LE" for 32-bit IEEE float — matching the formats the deepfake-service
    decoder accepts.
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
        offset += 8 + size + (size & 1)  # chunks are word-aligned
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


def _wav_session_iter(path: Path):
    """Stream a WAV file (S16LE or F32LE) as CreateSession + AudioBuffer chunks."""
    samples, rate, channels, wire_format = _read_wav(path)
    bytes_per_sample = (4 if wire_format == "F32LE" else 2) * channels
    bytes_per_chunk = int(rate * CHUNK_MS / 1000) * bytes_per_sample

    yield pb.DetectDeepfakeRequest(create_session_request=pb.CreateSessionRequest())

    pts_ns = 0
    for start in range(0, len(samples), bytes_per_chunk):
        chunk = samples[start : start + bytes_per_chunk]
        if not chunk:
            break
        actual_frames = len(chunk) // bytes_per_sample
        duration_ns = int(actual_frames / rate * 1e9)
        yield pb.DetectDeepfakeRequest(
            audio=ab_pb.AudioBuffer(
                type="audio/x-raw", format=wire_format,
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
