"""Minimal AudioVerification.Stream client — SDK smoke-test target.

Opens one bidi session against `aurigin.client.v1.AudioVerification.Stream`
per source and prints every `Verdict` + `EmbeddingVerdict` + `FinalResult`
the server responds with.

Source of audio (in priority order):
  * `--audio-file PATH` — stream exactly that WAV.
  * `--audio-dir DIR`   — open one session per *.wav found in DIR.
  * neither             — synthesise `--duration` seconds of silence
                          (connectivity smoke-test).

Points at the orchestrator simulator by default (`localhost:50053`).
Override `--target` to hit any AudioVerification server.

Auth is MANDATORY — the SDK-facing gRPC surface expects
`authorization: Bearer <token>` in metadata (spec: aurigin.common.v1.Principal
CallerType comment — same header for JWTs and API keys). Pick one of the
two demo tokens the simulator accepts (see its README) via
`--token <literal>` or supply your own for a real server.

TLS: `--tls auto` (default) picks insecure for `localhost:*` and any
`:80` target, secure for everything else. Override with
`--tls always` / `--tls never` when the target hostname doesn't give
it away.

CLI:
    python orchestrator_client.py --token sk_test_aurigin_sim_demo_0000000000000000
    python orchestrator_client.py --token <jwt> --target host.example:443
    python orchestrator_client.py --token <jwt> --target host.example:50053 --duration 30
    python orchestrator_client.py --token <jwt> --target host.example:443 \\
        --audio-file examples/audio/922.wav
    python orchestrator_client.py --token <jwt> --target host.example:443 \\
        --audio-dir examples/audio
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import grpc
from aurigin.client.v1 import audio_verification_pb2 as pb
from aurigin.client.v1 import audio_verification_pb2_grpc as pb_grpc
from aurigin.common.v1 import session_pb2 as session_pb
from aurigin.media.v1 import audio_frame_pb2 as af_pb
from common import WavData, read_wav

# Same demo tokens the orchestrator simulator ships with — kept here so
# `python orchestrator_client.py` (no flags) still works against a
# fresh `docker compose up` of the sim.
_DEMO_API_KEY = "sk_test_aurigin_sim_demo_0000000000000000"

_RATE = 16000
_CHANNELS = 1
_CHUNK_MS = 100
_BYTES_PER_SAMPLE = 2  # S16LE


def _new_session_request(label: str) -> pb.StreamRequest:
    return pb.StreamRequest(
        create_session_request=pb.CreateSessionRequest(
            config=pb.ClientSessionConfig(
                session_type=session_pb.SESSION_TYPE_USER_STREAM,
                attributes={"example": "orchestrator_client.py", "source": label},
            ),
        ),
    )


async def _silence_iter(duration_s: float):
    """CreateSessionRequest → duration_s worth of S16LE silence in _CHUNK_MS chunks."""
    yield _new_session_request(f"silence-{duration_s}s")
    samples_per_chunk = int(_RATE * _CHUNK_MS / 1000)
    silence = b"\x00\x00" * samples_per_chunk * _CHANNELS
    total_chunks = int(duration_s * 1000 / _CHUNK_MS)
    pts_ns = 0
    for _ in range(total_chunks):
        yield pb.StreamRequest(
            audio_frame=af_pb.AudioFrame(
                codec=af_pb.AUDIO_CODEC_S16LE,
                sample_rate_hz=_RATE,
                channels=_CHANNELS,
                payload=silence,
                pts_ns=pts_ns,
            ),
        )
        pts_ns += _CHUNK_MS * 1_000_000
        # Pace roughly at wall-clock so the server emits interleaved Verdicts on its own timer.
        await asyncio.sleep(_CHUNK_MS / 1000)


async def _wav_iter(wav: WavData, label: str):
    """CreateSessionRequest → WAV samples in _CHUNK_MS chunks at native rate/codec."""
    yield _new_session_request(label)
    bytes_per_chunk = int(wav.rate * _CHUNK_MS / 1000) * wav.bytes_per_sample
    pts_ns = 0
    for start in range(0, len(wav.samples), bytes_per_chunk):
        chunk = wav.samples[start : start + bytes_per_chunk]
        if not chunk:
            break
        actual_frames = len(chunk) // wav.bytes_per_sample
        yield pb.StreamRequest(
            audio_frame=af_pb.AudioFrame(
                codec=wav.audio_codec,
                sample_rate_hz=wav.rate,
                channels=wav.channels,
                payload=chunk,
                pts_ns=pts_ns,
            ),
        )
        pts_ns += int(actual_frames / wav.rate * 1e9)
        await asyncio.sleep(_CHUNK_MS / 1000)


def _use_tls(target: str, mode: str) -> bool:
    """`always` / `never` are explicit; `auto` picks insecure for
    localhost or any `:80` target, secure for everything else."""
    if mode == "always":
        return True
    if mode == "never":
        return False
    host, _, port = target.rpartition(":")
    if host in {"localhost", "127.0.0.1", "::1"} or port == "80":
        return False
    return True


async def _run_session(stub, request_iter, label: str, metadata) -> None:
    print(f"\n=== {label} ===")
    call = stub.Stream(request_iter, metadata=metadata)
    try:
        async for resp in call:
            kind = resp.WhichOneof("response")
            if kind == "create_session_response":
                print(f"SESSION | id={resp.create_session_response.session_id}")
            elif kind == "verdict":
                v = resp.verdict
                print(
                    f"VERDICT | consumer={v.consumer_name} "
                    f"offset={v.audio_offset_ms}ms label={v.label_raw or v.label} "
                    f"score={v.score:.3f} confidence={v.confidence:.3f}",
                )
            elif kind == "embedding_verdict":
                e = resp.embedding_verdict
                print(
                    f"EMBED   | consumer={e.consumer_name} "
                    f"offset={e.audio_offset_ms}ms dim={e.dim} "
                    f"fingerprint={e.fingerprint_code}",
                )
            elif kind == "notification":
                n = resp.notification
                print(f"NOTICE  | type={n.type} message={n.message!r}")
            elif kind == "final_result":
                f = resp.final_result
                print(f"FINAL   | total_audio_ms={f.total_audio_ms}")
                for pc in f.per_consumer:
                    print(
                        f"        | consumer={pc.consumer_name} "
                        f"label={pc.overall_label_raw or pc.overall_label} "
                        f"score={pc.overall_score:.3f} count={pc.analysis_count}",
                    )
    except grpc.aio.AioRpcError as exc:
        print(f"ERROR   | code={exc.code().name} detail={exc.details()!r}")


async def run(
    target: str,
    token: str,
    duration_s: float,
    tls_mode: str,
    audio_file: str | None,
    audio_dir: str | None,
) -> None:
    use_tls = _use_tls(target, tls_mode)

    sources: list[tuple[str, WavData | None]] = []
    if audio_file:
        path = Path(audio_file)
        sources.append((path.name, read_wav(path)))
    elif audio_dir:
        wavs = sorted(Path(audio_dir).glob("*.wav"))
        if not wavs:
            print(f"# no *.wav found in {audio_dir}; falling back to silence")
            sources.append((f"silence-{duration_s}s", None))
        for path in wavs:
            try:
                sources.append((path.name, read_wav(path)))
            except ValueError as exc:
                print(f"SKIP    | {path.name}: {exc}")
    else:
        sources.append((f"silence-{duration_s}s", None))

    print(f"# target={target} tls={use_tls} sessions={len(sources)} token_prefix={token[:10]}…")
    metadata = (("authorization", f"Bearer {token}"),)

    channel_cm = (
        grpc.aio.secure_channel(target, grpc.ssl_channel_credentials())
        if use_tls
        else grpc.aio.insecure_channel(target)
    )
    async with channel_cm as channel:
        stub = pb_grpc.AudioVerificationStub(channel)
        for label, wav in sources:
            request_iter = _wav_iter(wav, label) if wav is not None else _silence_iter(duration_s)
            await _run_session(stub, request_iter, label, metadata)


def cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", maxsplit=1)[0])
    parser.add_argument(
        "--target",
        default="localhost:50053",
        help="gRPC server host:port (default: localhost:50053 — orchestrator simulator)",
    )
    parser.add_argument(
        "--token",
        default=_DEMO_API_KEY,
        help="Bearer token — API key (sk_…) or JWT (eyJ…) accepted by the target",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=15.0,
        help="Seconds of silence to stream (default: 15 — long enough for 2-3 sim verdicts)",
    )
    parser.add_argument(
        "--tls",
        choices=["auto", "always", "never"],
        default="auto",
        help="TLS mode. auto (default) = insecure for localhost / :80, secure otherwise",
    )
    parser.add_argument(
        "--audio-file",
        default=None,
        help="Stream this WAV file (one session). Overrides --audio-dir and --duration.",
    )
    parser.add_argument(
        "--audio-dir",
        default=None,
        help="Stream every *.wav in DIR as its own session. Ignored if --audio-file is set.",
    )
    args = parser.parse_args()
    asyncio.run(run(args.target, args.token, args.duration, args.tls, args.audio_file, args.audio_dir))


if __name__ == "__main__":
    cli()
