"""Byte-counting gRPC simulator for Recording.Record.

Pure sink — reads AudioFrames, discards the payload, tracks a running
byte + duration count, and emits a RecordingComplete at end-of-stream.
No actual file write; the simulator returns a deterministic synthetic
`blob_uri` + `sha256` so the orchestrator's bus-event path can be
exercised end-to-end without provisioning blob storage.

Zero scenarios, zero YAML, zero fault injection — deliberately minimal.
Enough to validate the wire shape, open + half-close round-trip, and
downstream `recording_complete` event forwarding.

Env vars:
    PORT               gRPC listen port                     (default 50051)
    OUTPUT_FORMAT      declared in the synthetic blob_uri   (default wav)
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import signal
import sys
import uuid
from pathlib import Path

import grpc
from aurigin.media.v1 import audio_frame_pb2 as af_pb
from aurigin.recording.v1 import recording_pb2 as pb
from aurigin.recording.v1 import recording_pb2_grpc as pb_grpc


# Default paths resolve to the shared examples/ tree so the same certs
# used by the deepfake / fingerprint / replay simulators are reused
# here without duplication. Walk from the package file:
#   recording_simulator_service/server.py
#     → parents[0] = recording_simulator_service/
#     → parents[1] = src/
#     → parents[2] = recording/
#     → parents[3] = simulator/
#     → parents[4] = examples/
_EXAMPLES_DIR = Path(__file__).resolve().parents[4]
DEFAULT_TLS_DIR = _EXAMPLES_DIR / "certs"


# Bytes-per-sample table — kept in sync with aurigin.media.v1.AudioCodec.
# Used to turn an incoming AudioFrame's byte count back into audio ms
# so RecordingComplete.duration_ms reflects actual received audio.
_BYTES_PER_SAMPLE: dict[int, int] = {
    af_pb.AUDIO_CODEC_S16LE: 2,
    af_pb.AUDIO_CODEC_S16BE: 2,
    af_pb.AUDIO_CODEC_S24LE: 3,
    af_pb.AUDIO_CODEC_S32LE: 4,
    af_pb.AUDIO_CODEC_F32LE: 4,
    af_pb.AUDIO_CODEC_PCMU: 1,
    af_pb.AUDIO_CODEC_PCMA: 1,
}


def _load_tls() -> tuple[bytes, bytes] | None:
    cert_path = Path(os.environ.get("TLS_CERT", DEFAULT_TLS_DIR / "server.crt"))
    key_path = Path(os.environ.get("TLS_KEY", DEFAULT_TLS_DIR / "server.key"))
    if cert_path.is_file() and key_path.is_file():
        return key_path.read_bytes(), cert_path.read_bytes()
    return None


def _mtls_client_ca() -> bytes | None:
    if os.environ.get("MTLS", "").lower() not in ("1", "true", "yes"):
        return None
    ca_path = Path(os.environ.get("TLS_CLIENT_CA", DEFAULT_TLS_DIR / "client.crt"))
    if ca_path.is_file():
        return ca_path.read_bytes()
    return None


class RecordingImpl(pb_grpc.RecordingServicer):
    def __init__(self, output_format: str) -> None:
        self._output_format = output_format.lower()

    async def Record(self, request_iterator, context):  # noqa: N802 - gRPC RPC name
        peer = context.peer() if context else "?"
        recording_id: str = ""
        planned_blob_uri: str = ""
        total_audio_ms: int = 0
        total_bytes: int = 0
        last_codec: int = af_pb.AUDIO_CODEC_UNSPECIFIED
        last_rate: int = 0
        # Running SHA-256 of every received payload byte — synthetic
        # (the simulator never persists bytes anywhere) but keeping a
        # real hash lets downstream consumers test their integrity-
        # verification path with a value that actually tracks what the
        # stream carried.
        hasher = hashlib.sha256()

        async for req in request_iterator:
            kind = req.WhichOneof("request")

            if kind == "create_session_request":
                recording_id = f"rec-{uuid.uuid4().hex}"
                planned_blob_uri = f"sim://{recording_id}.{self._output_format}"
                print(f"[incoming] peer={peer} | recording sim", file=sys.stderr, flush=True)
                print(f"[{recording_id}] start | uri={planned_blob_uri}", file=sys.stderr, flush=True)
                yield pb.RecordResponse(
                    create_session_response=pb.CreateSessionResponse(
                        recording_id=recording_id,
                        planned_blob_uri=planned_blob_uri,
                    ),
                )
                continue

            if kind != "audio_frame":
                continue

            af = req.audio_frame
            bps = _BYTES_PER_SAMPLE.get(af.codec)
            if bps is None:
                await context.abort(
                    grpc.StatusCode.UNIMPLEMENTED,
                    f"codec {af.codec} not supported by recording simulator",
                )
                return
            if af.sample_rate_hz == 0 or af.channels == 0:
                await context.abort(
                    grpc.StatusCode.INVALID_ARGUMENT,
                    "audio_frame.sample_rate_hz and channels are required",
                )
                return

            payload = bytes(af.payload)
            hasher.update(payload)
            total_bytes += len(payload)
            frame_ms = int(len(payload) / (af.sample_rate_hz * af.channels * bps) * 1000.0)
            total_audio_ms += frame_ms
            last_codec = af.codec
            last_rate = af.sample_rate_hz

        yield pb.RecordResponse(
            complete=pb.RecordingComplete(
                recording_id=recording_id,
                blob_uri=planned_blob_uri,
                duration_ms=total_audio_ms,
                bytes=total_bytes,
                sha256=hasher.hexdigest(),
                codec=last_codec,
                sample_rate_hz=last_rate,
            ),
        )
        print(
            f"[{recording_id or 'sim-anon'}] end | duration={total_audio_ms}ms | "
            f"bytes={total_bytes} | sha256={hasher.hexdigest()[:16]}…",
            file=sys.stderr, flush=True,
        )


async def _serve_async(port: int, output_format: str) -> None:
    server = grpc.aio.server()
    pb_grpc.add_RecordingServicer_to_server(RecordingImpl(output_format=output_format), server)

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
        f"Recording simulator listening on :{port} | "
        f"output_format={output_format} | transport={tls_status}",
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
    print("Recording simulator stopped.", file=sys.stderr, flush=True)


def serve(port: int = 50051) -> None:
    output_format = os.environ.get("OUTPUT_FORMAT", "wav")
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_serve_async(port, output_format))
    finally:
        loop.close()


if __name__ == "__main__":
    serve(int(os.environ.get("PORT", "50051")))
