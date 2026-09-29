"""Minimal AudioVerification.Stream client — SDK smoke-test target.

Opens one bidi session against `aurigin.client.v1.AudioVerification.Stream`,
streams synthesised silence for a configurable duration, and prints every
`Verdict` + `EmbeddingVerdict` + `FinalResult` the server responds with.

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
"""

from __future__ import annotations

import argparse
import asyncio

import grpc
from aurigin.client.v1 import audio_verification_pb2 as pb
from aurigin.client.v1 import audio_verification_pb2_grpc as pb_grpc
from aurigin.common.v1 import session_pb2 as session_pb
from aurigin.media.v1 import audio_frame_pb2 as af_pb

# Same demo tokens the orchestrator simulator ships with — kept here so
# `python orchestrator_client.py` (no flags) still works against a
# fresh `docker compose up` of the sim.
_DEMO_API_KEY = "sk_test_aurigin_sim_demo_0000000000000000"

_RATE = 16000
_CHANNELS = 1
_CHUNK_MS = 100
_BYTES_PER_SAMPLE = 2  # S16LE


async def _request_iter(duration_s: float):
    """CreateSessionRequest → duration_s worth of S16LE silence in _CHUNK_MS chunks."""
    yield pb.StreamRequest(
        create_session_request=pb.CreateSessionRequest(
            config=pb.ClientSessionConfig(
                session_type=session_pb.SESSION_TYPE_USER_STREAM,
                attributes={"example": "orchestrator_client.py"},
            ),
        ),
    )
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
        # Sleep between chunks so the request pace roughly matches wall-clock —
        # gives the server time to emit interleaved Verdicts on its own timer.
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


async def run(target: str, token: str, duration_s: float, tls_mode: str) -> None:
    use_tls = _use_tls(target, tls_mode)
    print(f"# target={target} tls={use_tls} duration={duration_s}s token_prefix={token[:10]}…")
    metadata = (("authorization", f"Bearer {token}"),)

    channel_cm = (
        grpc.aio.secure_channel(target, grpc.ssl_channel_credentials())
        if use_tls
        else grpc.aio.insecure_channel(target)
    )
    async with channel_cm as channel:
        stub = pb_grpc.AudioVerificationStub(channel)
        call = stub.Stream(_request_iter(duration_s), metadata=metadata)
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
    args = parser.parse_args()
    asyncio.run(run(args.target, args.token, args.duration, args.tls))


if __name__ == "__main__":
    cli()
