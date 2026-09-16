"""Deterministic gRPC simulator for Fingerprint.ExtractFingerprint.

Buffers the client's AudioFrame stream into `WINDOW_MS`-long windows
(default 5000 ms — matches the WavLM fine-tune training config in
aurigin-fingerprint) and emits one `EmbeddingResult` per completed
window plus a terminal `FinalResult` at end-of-stream.

Embeddings are derived deterministically from the first 64 bytes of the
window payload: `sha256(payload[:64])` seeds a PRNG that fills a 768-d
float32 vector, L2-normalised. Same input → same embedding on any
machine, useful for:

- Smoke tests that assert on `fingerprint_code`.
- Golden-fixture diffs across releases.
- Client-side integration tests that exercise the full stream shape
  without needing a real GPU-backed backend.

Zero scenarios, zero YAML, zero fault injection — deliberately minimal.
Add those systems if / when embedding-behaviour variation becomes a
real testing need.

Env vars:
    PORT               gRPC listen port                                (default 50051)
    WINDOW_MS          window length in ms (matches fingerprint-service) (default 5000)
    EMBEDDING_DIM      dimensionality of the returned unit vectors    (default 768)
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import signal
import struct
import sys
from pathlib import Path

import grpc

from aurigin.fingerprint.v1 import fingerprint_pb2 as pb
from aurigin.fingerprint.v1 import fingerprint_pb2_grpc as pb_grpc
from aurigin.media.v1 import audio_frame_pb2 as af_pb


# Default paths resolve to the shared examples/ tree so the same certs
# used by the deepfake simulator are reused here without duplication.
# Walk from the package file:
#   fingerprint_simulator_service/server.py
#     → parents[0] = fingerprint_simulator_service/
#     → parents[1] = src/
#     → parents[2] = fingerprint/
#     → parents[3] = simulator/
#     → parents[4] = examples/
_EXAMPLES_DIR = Path(__file__).resolve().parents[4]
DEFAULT_TLS_DIR = _EXAMPLES_DIR / "certs"


# Bytes-per-sample table for the codecs the simulator accepts on the
# wire. Kept in sync with aurigin.media.v1.AudioCodec. Uncoded codecs
# (OPUS) are rejected before this table is consulted.
_BYTES_PER_SAMPLE: dict[int, int] = {
    af_pb.AUDIO_CODEC_S16LE: 2,
    af_pb.AUDIO_CODEC_S16BE: 2,
    af_pb.AUDIO_CODEC_S24LE: 3,
    af_pb.AUDIO_CODEC_S32LE: 4,
    af_pb.AUDIO_CODEC_F32LE: 4,
    af_pb.AUDIO_CODEC_PCMU: 1,
    af_pb.AUDIO_CODEC_PCMA: 1,
}


def _synthetic_embedding(payload: bytes, dim: int) -> bytes:
    """Return `dim` × float32 little-endian bytes, L2-normalised.

    Deterministic: `sha256(payload[:64])` seeds a xorshift-style PRNG
    that fills a float32 array in [-1, +1], then we L2-normalise so the
    output is a unit vector (dot product == cosine similarity, matches
    the real WavLM fine-tune's output convention).

    Purely synthetic — the "embedding" carries no semantic content. The
    goal is deterministic bytes + correct shape + correct normalisation,
    so client-side code paths (base64 encoding, sha256 hashing, Qdrant
    insertion) exercise the exact same message shape they'd see against
    a real backend.
    """
    seed = int.from_bytes(hashlib.sha256(payload[:64]).digest()[:8], "little", signed=False)
    # Linear congruential-ish sequence — cheap, deterministic, sufficient
    # for producing distinct-looking vectors from distinct payloads. Not
    # cryptographic; the sha256 above provides the pseudo-randomness.
    values = [0.0] * dim
    x = seed or 0x9E3779B97F4A7C15  # golden-ratio constant, avoids zero-lock
    for i in range(dim):
        x = (x * 6364136223846793005 + 1442695040888963407) & 0xFFFFFFFFFFFFFFFF
        # Map to [-1, +1] with a bit-mask trick that keeps the value stable
        # across Python versions (no float parsing of hex).
        values[i] = ((x >> 11) & ((1 << 53) - 1)) / float(1 << 52) - 1.0
    norm = (sum(v * v for v in values)) ** 0.5 or 1.0
    unit = [v / norm for v in values]
    # struct.pack '<f' × dim → 4 bytes each, little-endian; matches the
    # canonical wire form documented in the proto.
    return struct.pack(f"<{dim}f", *unit)


def _load_tls() -> tuple[bytes, bytes] | None:
    """Return (key_bytes, cert_bytes) if a usable keypair is present, else None.

    Auto-detect: defaults to examples/certs/server.{crt,key} (committed
    to the repo so the example is TLS-by-default). Override via
    TLS_CERT / TLS_KEY env vars; point them at non-existent paths to
    force insecure mode.
    """
    cert_path = Path(os.environ.get("TLS_CERT", DEFAULT_TLS_DIR / "server.crt"))
    key_path = Path(os.environ.get("TLS_KEY", DEFAULT_TLS_DIR / "server.key"))
    if cert_path.is_file() and key_path.is_file():
        return key_path.read_bytes(), cert_path.read_bytes()
    return None


def _mtls_client_ca() -> bytes | None:
    """Return the PEM bytes to verify client certs against when MTLS=1.

    Defaults to examples/certs/client.crt — the committed self-signed
    client cert acts as its own CA, matching the deepfake simulator's
    setup so both sims can share the same `examples/certs/` tree.
    """
    if os.environ.get("MTLS", "").lower() not in ("1", "true", "yes"):
        return None
    ca_path = Path(os.environ.get("TLS_CLIENT_CA", DEFAULT_TLS_DIR / "client.crt"))
    if ca_path.is_file():
        return ca_path.read_bytes()
    return None


class FingerprintImpl(pb_grpc.FingerprintServicer):
    def __init__(self, window_ms: int, embedding_dim: int) -> None:
        self._window_ms = window_ms
        self._embedding_dim = embedding_dim

    async def ExtractFingerprint(self, request_iterator, context):  # noqa: N802 - gRPC RPC name
        peer = context.peer() if context else "?"
        # Deterministic per-session id: sha256(peer + first-frame payload).
        # Kept short (sim-<8 hex>) matching the deepfake simulator's convention
        # so grep patterns work across both sims.
        session_id: str = ""
        total_audio_ms: int = 0
        embedding_count: int = 0
        window_buf = bytearray()
        window_bytes_target: int | None = None
        window_offset_ms: int = 0

        async for req in request_iterator:
            kind = req.WhichOneof("request")

            if kind == "create_session_request":
                session_id = "sim-" + hashlib.sha256(peer.encode("utf-8", errors="replace")).hexdigest()[:8]
                # Log the inbound call before the runner starts so the operator
                # sees which session was accepted. Matches the deepfake sim's
                # [incoming] line for cross-sim grep parity.
                print(f"[incoming] peer={peer} | fingerprint sim", file=sys.stderr, flush=True)
                print(f"[{session_id}] start", file=sys.stderr, flush=True)
                yield pb.ExtractFingerprintResponse(
                    create_session_response=pb.CreateSessionResponse(session_id=session_id),
                )
                continue

            if kind != "audio_frame":
                # Ignore unknown oneof branches — forward-compat with future
                # request shapes without failing the stream.
                continue

            af = req.audio_frame
            bps = _BYTES_PER_SAMPLE.get(af.codec)
            if bps is None:
                await context.abort(
                    grpc.StatusCode.UNIMPLEMENTED,
                    f"codec {af.codec} not supported by fingerprint simulator",
                )
                return
            if af.sample_rate_hz == 0 or af.channels == 0:
                await context.abort(
                    grpc.StatusCode.INVALID_ARGUMENT,
                    "audio_frame.sample_rate_hz and channels are required",
                )
                return

            # First real audio frame pins the window byte-target — every codec
            # + rate + channel combo produces a different bytes-per-window
            # figure, and we don't force clients to hold codec stable within
            # a session (matches how the real fingerprint-service accepts
            # AudioFrame per-message).
            if window_bytes_target is None:
                window_bytes_target = int(
                    af.sample_rate_hz * af.channels * bps * (self._window_ms / 1000.0)
                )

            window_buf.extend(af.payload)
            frame_ms = int(
                len(af.payload) / (af.sample_rate_hz * af.channels * bps) * 1000.0
            )
            total_audio_ms += frame_ms

            # Flush every full window. A single AudioFrame that carries
            # more than one window's worth of audio (e.g. offline scan)
            # emits multiple EmbeddingResults in one loop iteration.
            while window_bytes_target and len(window_buf) >= window_bytes_target:
                chunk = bytes(window_buf[:window_bytes_target])
                del window_buf[:window_bytes_target]
                embedding = _synthetic_embedding(chunk, self._embedding_dim)
                fp_code = hashlib.sha256(embedding).hexdigest()[:16]
                yield pb.ExtractFingerprintResponse(
                    embedding_result=pb.EmbeddingResult(
                        audio_offset_ms=window_offset_ms,
                        duration_ms=self._window_ms,
                        embedding=embedding,
                        fingerprint_code=fp_code,
                        dim=self._embedding_dim,
                    ),
                )
                embedding_count += 1
                window_offset_ms += self._window_ms

        # Stream closed. Emit terminal FinalResult with observability
        # counters — no aggregate embedding / similarity / verdict, matches
        # the proto's design intent.
        yield pb.ExtractFingerprintResponse(
            final_result=pb.FinalResult(
                total_audio_ms=total_audio_ms,
                embedding_count=embedding_count,
            ),
        )
        print(
            f"[{session_id or 'sim-anon'}] end | total={total_audio_ms}ms | embeddings={embedding_count}",
            file=sys.stderr, flush=True,
        )


async def _serve_async(port: int, window_ms: int, embedding_dim: int) -> None:
    server = grpc.aio.server()
    pb_grpc.add_FingerprintServicer_to_server(
        FingerprintImpl(window_ms=window_ms, embedding_dim=embedding_dim), server,
    )

    tls = _load_tls()
    if tls is not None:
        key_bytes, cert_bytes = tls
        client_ca = _mtls_client_ca()
        if client_ca is not None:
            creds = grpc.ssl_server_credentials(
                [(key_bytes, cert_bytes)],
                root_certificates=client_ca,
                require_client_auth=True,
            )
            tls_status = "mTLS (self-signed, examples/certs/)"
        else:
            creds = grpc.ssl_server_credentials([(key_bytes, cert_bytes)])
            tls_status = "TLS (self-signed, examples/certs/)"
            if os.environ.get("MTLS", "").lower() in ("1", "true", "yes"):
                tls_status += " — MTLS=1 but examples/certs/client.crt missing, falling back to plain TLS"
        server.add_secure_port(f"[::]:{port}", creds)
    else:
        server.add_insecure_port(f"[::]:{port}")
        tls_status = "insecure (no examples/certs/server.crt found)"
    await server.start()
    print(
        f"Fingerprint simulator listening on :{port} | "
        f"window_ms={window_ms} | embedding_dim={embedding_dim} | transport={tls_status}",
        file=sys.stderr, flush=True,
    )

    # Same signal-handling shape as the deepfake simulator — see its
    # server.py for the full rationale (asyncio.run's own SIGINT handler
    # races loop.add_signal_handler and produces noisy shutdown tracebacks).
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def _request_shutdown(signame: str) -> None:
        if not shutdown_event.is_set():
            print(f"\nReceived {signame}, shutting down...", file=sys.stderr, flush=True)
            shutdown_event.set()

    for sig, name in ((signal.SIGINT, "SIGINT"), (signal.SIGTERM, "SIGTERM")):
        loop.add_signal_handler(sig, _request_shutdown, name)

    await shutdown_event.wait()
    await server.stop(grace=2.0)
    print("Fingerprint simulator stopped.", file=sys.stderr, flush=True)


def serve(port: int = 50051) -> None:
    window_ms = int(os.environ.get("WINDOW_MS", "5000"))
    embedding_dim = int(os.environ.get("EMBEDDING_DIM", "768"))
    # Same event-loop pattern as the deepfake simulator — see its server.py
    # for why we don't use asyncio.run().
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_serve_async(port, window_ms, embedding_dim))
    finally:
        loop.close()


if __name__ == "__main__":
    serve(int(os.environ.get("PORT", "50051")))
