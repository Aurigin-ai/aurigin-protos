"""Random-score gRPC simulator for ReplayDetection.DetectReplay.

Buffers the client's AudioFrame stream into `WINDOW_MS`-long windows
(default 3000 ms — matches the halo-3 replay-detector training config
in aurigin-replay) and emits one `AnalysisResult` per completed window
plus a terminal `FinalResult` at end-of-stream.

Each window's `score` is a uniform draw from `[0, 1)` — no correlation
to the audio payload. Purpose is to exercise the wire shape + client-
side handling paths (verdict routing, label mapping, threshold logic)
with realistic variance, not to reproduce fixture-level goldens. Point
the deterministic-fingerprint simulator at a golden diff test instead
if repeatability matters.

Zero scenarios, zero YAML, zero fault injection — deliberately minimal.
Add a scenario system if / when score-behaviour tests need it.

Env vars:
    PORT               gRPC listen port                             (default 50051)
    WINDOW_MS          window length in ms (matches replay-service) (default 3000)
    DECISION_THRESHOLD score >= this → REPLAY, else GENUINE         (default 0.5)
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import random
import signal
import sys
from pathlib import Path

import grpc

from aurigin.media.v1 import audio_frame_pb2 as af_pb
from aurigin.replay_detection.v1 import replay_detection_pb2 as pb
from aurigin.replay_detection.v1 import replay_detection_pb2_grpc as pb_grpc


# Default paths resolve to the shared examples/ tree so the same certs
# used by the deepfake + fingerprint simulators are reused here without
# duplication. Walk from the package file:
#   replay_simulator_service/server.py
#     → parents[0] = replay_simulator_service/
#     → parents[1] = src/
#     → parents[2] = replay/
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


def _synthetic_score(_payload: bytes) -> float:
    """Return a random score in [0.0, 1.0).

    No dependency on the audio payload — the simulator emits uniform
    random values so clients see realistic per-window variance. Use the
    fingerprint simulator's deterministic-embedding pattern instead when
    repeatability is what you need (e.g. golden-fixture diffs)."""
    return random.random()


def _label_for(score: float, threshold: float) -> tuple[int, str]:
    """Map (score, threshold) → (ReplayLabel enum, raw string).

    Matches the halo-3 model output convention: below threshold =
    genuine, at-or-above = replay. Mirrors the proto's `label` +
    `label_raw` fields — clients that consume label_raw stay
    forward-compatible with future model revisions emitting new
    string labels ("silence" / "error") we don't produce here.
    """
    if score >= threshold:
        return pb.REPLAY_LABEL_REPLAY, "replay"
    return pb.REPLAY_LABEL_GENUINE, "genuine"


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
    client cert acts as its own CA, matching the fingerprint + deepfake
    simulators so all three sims share the same `examples/certs/` tree.
    """
    if os.environ.get("MTLS", "").lower() not in ("1", "true", "yes"):
        return None
    ca_path = Path(os.environ.get("TLS_CLIENT_CA", DEFAULT_TLS_DIR / "client.crt"))
    if ca_path.is_file():
        return ca_path.read_bytes()
    return None


class ReplayDetectionImpl(pb_grpc.ReplayDetectionServicer):
    def __init__(self, window_ms: int, decision_threshold: float) -> None:
        self._window_ms = window_ms
        self._decision_threshold = decision_threshold

    async def DetectReplay(self, request_iterator, context):  # noqa: N802 - gRPC RPC name
        peer = context.peer() if context else "?"
        session_id: str = ""
        total_audio_ms: int = 0
        analysis_count: int = 0
        score_sum: float = 0.0
        # Worst-case aggregate — any REPLAY window wins over GENUINE.
        aggregate_label: int = pb.REPLAY_LABEL_UNSPECIFIED
        aggregate_label_raw: str = ""
        window_buf = bytearray()
        window_bytes_target: int | None = None
        window_offset_ms: int = 0

        async for req in request_iterator:
            kind = req.WhichOneof("request")

            if kind == "create_session_request":
                session_id = "sim-" + hashlib.sha256(peer.encode("utf-8", errors="replace")).hexdigest()[:8]
                print(f"[incoming] peer={peer} | replay sim", file=sys.stderr, flush=True)
                print(f"[{session_id}] start", file=sys.stderr, flush=True)
                yield pb.DetectReplayResponse(
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
                    f"codec {af.codec} not supported by replay simulator",
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
            # a session (matches how the real replay-service accepts
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
            # emits multiple AnalysisResults in one loop iteration.
            while window_bytes_target and len(window_buf) >= window_bytes_target:
                chunk = bytes(window_buf[:window_bytes_target])
                del window_buf[:window_bytes_target]
                score = _synthetic_score(chunk)
                label_enum, label_raw = _label_for(score, self._decision_threshold)
                yield pb.DetectReplayResponse(
                    analysis_result=pb.AnalysisResult(
                        audio_offset_ms=window_offset_ms,
                        duration_ms=self._window_ms,
                        score=score,
                        label=label_enum,
                        label_raw=label_raw,
                        # Confidence: distance from threshold, mapped to [0, 1].
                        # Purely synthetic — real backend derives this from
                        # softmax margins.
                        confidence=min(1.0, abs(score - self._decision_threshold) * 2.0),
                    ),
                )
                analysis_count += 1
                score_sum += score
                # Worst-case aggregate — REPLAY wins over GENUINE.
                if label_enum == pb.REPLAY_LABEL_REPLAY:
                    aggregate_label = pb.REPLAY_LABEL_REPLAY
                    aggregate_label_raw = "replay"
                elif aggregate_label != pb.REPLAY_LABEL_REPLAY:
                    aggregate_label = pb.REPLAY_LABEL_GENUINE
                    aggregate_label_raw = "genuine"
                window_offset_ms += self._window_ms

        # Stream closed. Emit terminal FinalResult with observability counters.
        overall_score = (score_sum / analysis_count) if analysis_count else 0.0
        yield pb.DetectReplayResponse(
            final_result=pb.FinalResult(
                total_audio_ms=total_audio_ms,
                overall_score=overall_score,
                overall_label=aggregate_label,
                overall_label_raw=aggregate_label_raw,
                analysis_count=analysis_count,
            ),
        )
        print(
            f"[{session_id or 'sim-anon'}] end | total={total_audio_ms}ms | "
            f"analyses={analysis_count} | overall_score={overall_score:.3f} | "
            f"label={aggregate_label_raw or 'unspecified'}",
            file=sys.stderr, flush=True,
        )


async def _serve_async(port: int, window_ms: int, decision_threshold: float) -> None:
    server = grpc.aio.server()
    pb_grpc.add_ReplayDetectionServicer_to_server(
        ReplayDetectionImpl(window_ms=window_ms, decision_threshold=decision_threshold), server,
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
        f"Replay simulator listening on :{port} | "
        f"window_ms={window_ms} | decision_threshold={decision_threshold} | transport={tls_status}",
        file=sys.stderr, flush=True,
    )

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
    print("Replay simulator stopped.", file=sys.stderr, flush=True)


def serve(port: int = 50051) -> None:
    window_ms = int(os.environ.get("WINDOW_MS", "3000"))
    decision_threshold = float(os.environ.get("DECISION_THRESHOLD", "0.5"))
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_serve_async(port, window_ms, decision_threshold))
    finally:
        loop.close()


if __name__ == "__main__":
    serve(int(os.environ.get("PORT", "50051")))
