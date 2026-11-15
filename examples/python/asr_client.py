"""Minimal Transcription gRPC client using the generated aurigin-protos package.

If `examples/audio/` contains .wav files, opens one session per file and
streams its PCM through `Transcription.Transcribe`. Otherwise streams
4 s of silence as a connectivity smoke-test (produces 2 synthetic
TranscriptTurns against the simulator's default 2000 ms turn length).

CLI:
    python asr_client.py [--target HOST:PORT] [--audio-dir DIR]
"""

from __future__ import annotations

import argparse
from pathlib import Path

from aurigin.asr.v1 import asr_pb2 as pb
from aurigin.asr.v1 import asr_pb2_grpc as pb_grpc
from aurigin.common.v1 import endpoint_pb2 as ep_pb
from aurigin.media.v1 import audio_frame_pb2 as af_pb

from common import WavData, make_sync_channel, read_wav, transport_label

DEFAULT_RATE = 16000
CHANNELS = 1
CHUNK_MS = 500
# 4000 ms of silence at 16 kHz — enough to produce 2 TranscriptTurns at
# the simulator's default 2000 ms turn length, so CI sees the per-turn
# path exercised at least twice before FinalResult.
SILENCE_CHUNKS = 8

_ENDPOINT_BY_NAME = {
    "near_end": ep_pb.ENDPOINT_NEAR_END,
    "far_end":  ep_pb.ENDPOINT_FAR_END,
    "mixed":    ep_pb.ENDPOINT_MIXED,
}

_ENDPOINT_LABEL = {
    ep_pb.ENDPOINT_UNSPECIFIED: "unspecified",
    ep_pb.ENDPOINT_NEAR_END:    "near_end",
    ep_pb.ENDPOINT_FAR_END:     "far_end",
    ep_pb.ENDPOINT_MIXED:       "mixed",
}


def _create_session_request(
    endpoint: int | None, track_id: str | None,
) -> pb.CreateSessionRequest:
    """Build a CreateSessionRequest with TranscriptionConfig populating
    endpoint/track_id if the caller set either. An empty config means
    defaults, so the pre-0.3.8 call shape stays available (no flags ⇒
    no config object on the wire)."""
    if endpoint is None and not track_id:
        return pb.CreateSessionRequest()
    cfg = pb.TranscriptionConfig()
    if endpoint is not None:
        cfg.endpoint = endpoint
    if track_id:
        cfg.track_id = track_id
    return pb.CreateSessionRequest(config=cfg)


def _silent_session_iter(endpoint: int | None, track_id: str | None):
    """Fallback iterator: CreateSession + 8 × 500 ms of silence at 16 kHz."""
    yield pb.TranscribeRequest(create_session_request=_create_session_request(endpoint, track_id))
    pts_ns = 0
    for _ in range(SILENCE_CHUNKS):
        samples = int(DEFAULT_RATE * CHUNK_MS / 1000)
        chunk = b"\x00\x00" * samples * CHANNELS
        yield pb.TranscribeRequest(
            audio_frame=af_pb.AudioFrame(
                codec=af_pb.AUDIO_CODEC_S16LE,
                sample_rate_hz=DEFAULT_RATE,
                channels=CHANNELS,
                payload=chunk,
                pts_ns=pts_ns,
            ),
        )
        pts_ns += CHUNK_MS * 1_000_000


def _wav_session_iter(wav: WavData, endpoint: int | None, track_id: str | None):
    """Stream a WAV file (S16LE / F32LE / PCMU / PCMA) as CreateSession + AudioFrame chunks."""
    bytes_per_chunk = int(wav.rate * CHUNK_MS / 1000) * wav.bytes_per_sample

    yield pb.TranscribeRequest(create_session_request=_create_session_request(endpoint, track_id))

    pts_ns = 0
    for start in range(0, len(wav.samples), bytes_per_chunk):
        chunk = wav.samples[start : start + bytes_per_chunk]
        if not chunk:
            break
        actual_frames = len(chunk) // wav.bytes_per_sample
        yield pb.TranscribeRequest(
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
    for response in stub.Transcribe(request_iter):
        kind = response.WhichOneof("response")
        if kind == "create_session_response":
            ack = response.create_session_response
            print(f"Session: {ack.session_id}")
            if ack.model_name or ack.model_version:
                print(f"Model:   {ack.model_name or '?'}@{ack.model_version or '?'}")
        elif kind == "transcript_turn":
            t = response.transcript_turn
            attr = ""
            if t.endpoint and t.endpoint != ep_pb.ENDPOINT_UNSPECIFIED:
                attr += f" ep={_ENDPOINT_LABEL.get(t.endpoint, t.endpoint)}"
            if t.track_id:
                attr += f" track={t.track_id}"
            print(
                f"Turn {t.turn:>3} | {t.t0_ms:>6}-{t.t1_ms:<6}ms | "
                f"conf={t.confidence:.2f} | silence={t.silence_pct:.1f}% |{attr} "
                f"{t.text!r}",
            )
        elif kind == "final_result":
            f = response.final_result
            print(
                f"FINAL     | total={f.total_audio_ms}ms | turns={f.turn_count} | "
                f"wall={f.wall_ms}ms | rtf={f.rtf:.2f} | "
                f"silence_mean={f.silence_pct_mean:.1f}%",
            )


def main(
    target: str = "localhost:50051",
    audio_dir: str | Path | None = None,
    endpoint: int | None = None,
    track_id: str | None = None,
) -> None:
    audio_dir = (
        Path(audio_dir).resolve()
        if audio_dir
        else Path(__file__).resolve().parent.parent / "audio"
    )
    wavs = sorted(audio_dir.glob("*.wav")) if audio_dir.is_dir() else []

    print(f"# transport={transport_label()}")

    with make_sync_channel(target) as channel:
        stub = pb_grpc.TranscriptionStub(channel)
        if not wavs:
            _run_session(
                stub,
                _silent_session_iter(endpoint, track_id),
                "silence (4 s @ 16 kHz)",
            )
            return
        for path in wavs:
            # Pre-validate before opening the stream — same reasoning as
            # client.py / fingerprint_client.py / replay_client.py: a
            # read_wav exception inside the request generator after the
            # call has started surfaces as an opaque StatusCode.UNKNOWN
            # from gRPC. Catching here gives a clean skip line and keeps
            # the dir scan going.
            try:
                wav = read_wav(path)
            except ValueError as exc:
                print(f"\n=== {path.name} ===\nSKIPPED: {exc}")
                continue
            _run_session(stub, _wav_session_iter(wav, endpoint, track_id), path.name)


def cli() -> None:
    """Entry-point wrapper that parses CLI args, then calls main().

    `uv run asr-client` (per pyproject.toml [project.scripts]) lands
    here, NOT in `main()` — without this wrapper the entry-point shim would
    call `main()` with no args and silently ignore every flag on the command
    line. `python asr_client.py` also lands here via __main__ below.
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
    parser.add_argument(
        "--endpoint",
        choices=list(_ENDPOINT_BY_NAME),
        default=None,
        help=(
            "Caller-supplied endpoint attribution attached to "
            "TranscriptionConfig. The service echoes it on every "
            "TranscriptTurn + FinalResult without inspecting."
        ),
    )
    parser.add_argument(
        "--track-id",
        default=None,
        help=(
            "Caller-supplied track_id attribution attached to "
            "TranscriptionConfig. Same pass-through semantics as --endpoint."
        ),
    )
    args = parser.parse_args()
    endpoint = _ENDPOINT_BY_NAME[args.endpoint] if args.endpoint else None
    main(args.target, args.audio_dir, endpoint, args.track_id)


if __name__ == "__main__":
    cli()
