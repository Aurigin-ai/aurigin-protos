"""Deterministic gRPC simulator for Transcription.Transcribe.

Buffers the client's AudioFrame stream and emits one TranscriptTurn
every `TURN_MS` of audio (default 2000 ms — simulates a 2-second
average turn length, typical of conversational speech). A terminal
FinalResult is emitted at end-of-stream.

Each turn's `text` is drawn deterministically from a fixed vocabulary
by turn number, so repeated runs over the same audio produce identical
transcripts. Purpose is to exercise the wire shape + client-side
handling paths (turn routing, text extraction, FinalResult aggregation)
with realistic cadence, not to actually transcribe audio. Point a real
`aurigin-asr` instance at your client when you need real STT output.

Zero scenarios, zero YAML, zero fault injection — deliberately minimal,
matches the replay simulator's shape.

Env vars:
    PORT          gRPC listen port                                  (default 50051)
    TURN_MS       audio duration per emitted TranscriptTurn (ms)    (default 2000)
    MODEL_NAME    value echoed on CreateSessionResponse             (default "parakeet-sim")
    MODEL_VERSION value echoed on CreateSessionResponse + FinalResult (default "0.0.0-sim")
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import signal
import sys
from pathlib import Path

import grpc

from aurigin.asr.v1 import asr_pb2 as pb
from aurigin.asr.v1 import asr_pb2_grpc as pb_grpc
from aurigin.media.v1 import audio_frame_pb2 as af_pb


# Default paths resolve to the shared examples/ tree so the same certs
# used by the deepfake + fingerprint + replay simulators are reused here
# without duplication. Walk from the package file:
#   asr_simulator_service/server.py
#     → parents[0] = asr_simulator_service/
#     → parents[1] = src/
#     → parents[2] = asr/
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


# Fixed vocabulary of simulated turn transcripts. Walked cyclically by
# turn index so clients see a dense, deterministic sequence that mirrors
# the shape of a real one-side conversation (variable turn lengths,
# natural phrasing). Keep every line lower_snake_space to match
# Parakeet's default decoder output.
_SAMPLE_TRANSCRIPTS: tuple[str, ...] = (
    "hello who is this",
    "oh from the bank okay what happened",
    "wait let me check my phone",
    "yes i can see the alert",
    "you want me to move the money to a new account",
    "that sounds strange are you sure",
    "give me your badge number first",
    "i will call the bank back directly",
)


def _text_for(turn: int) -> str:
    """Return a deterministic transcript line for the given turn index."""
    return _SAMPLE_TRANSCRIPTS[turn % len(_SAMPLE_TRANSCRIPTS)]


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
    client cert acts as its own CA, matching the sibling simulators so
    all four sims share the same `examples/certs/` tree.
    """
    if os.environ.get("MTLS", "").lower() not in ("1", "true", "yes"):
        return None
    ca_path = Path(os.environ.get("TLS_CLIENT_CA", DEFAULT_TLS_DIR / "client.crt"))
    if ca_path.is_file():
        return ca_path.read_bytes()
    return None


class TranscriptionImpl(pb_grpc.TranscriptionServicer):
    def __init__(
        self,
        turn_ms: int,
        model_name: str,
        model_version: str,
    ) -> None:
        self._turn_ms = turn_ms
        self._model_name = model_name
        self._model_version = model_version

    async def Transcribe(self, request_iterator, context):  # noqa: N802 - gRPC RPC name
        peer = context.peer() if context else "?"
        session_id: str = ""
        total_audio_ms: int = 0
        turn_count: int = 0
        turn_bytes_target: int | None = None
        turn_buf = bytearray()
        turn_offset_ms: int = 0

        async for req in request_iterator:
            kind = req.WhichOneof("request")

            if kind == "create_session_request":
                session_id = "sim-" + hashlib.sha256(peer.encode("utf-8", errors="replace")).hexdigest()[:8]
                print(f"[incoming] peer={peer} | asr sim", file=sys.stderr, flush=True)
                print(f"[{session_id}] start", file=sys.stderr, flush=True)
                yield pb.TranscribeResponse(
                    create_session_response=pb.CreateSessionResponse(
                        session_id=session_id,
                        model_name=self._model_name,
                        model_version=self._model_version,
                    ),
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
                    f"codec {af.codec} not supported by asr simulator",
                )
                return
            if af.sample_rate_hz == 0 or af.channels == 0:
                await context.abort(
                    grpc.StatusCode.INVALID_ARGUMENT,
                    "audio_frame.sample_rate_hz and channels are required",
                )
                return

            # First real audio frame pins the turn byte-target. We don't
            # force clients to hold codec stable within a session (matches
            # how the real aurigin-asr accepts AudioFrame per-message), so
            # the target is computed once from the first frame's shape.
            if turn_bytes_target is None:
                turn_bytes_target = int(
                    af.sample_rate_hz * af.channels * bps * (self._turn_ms / 1000.0)
                )

            turn_buf.extend(af.payload)
            frame_ms = int(
                len(af.payload) / (af.sample_rate_hz * af.channels * bps) * 1000.0
            )
            total_audio_ms += frame_ms

            # Flush every full turn. A single AudioFrame that carries
            # more than one turn's worth of audio (e.g. offline scan)
            # emits multiple TranscriptTurns in one loop iteration.
            while turn_bytes_target and len(turn_buf) >= turn_bytes_target:
                del turn_buf[:turn_bytes_target]
                text = _text_for(turn_count)
                yield pb.TranscribeResponse(
                    transcript_turn=pb.TranscriptTurn(
                        turn=turn_count,
                        t0_ms=turn_offset_ms,
                        t1_ms=turn_offset_ms + self._turn_ms,
                        text=text,
                        # Confidence: fixed high value — simulator doesn't
                        # expose a knob for degraded ASR. Pair with a
                        # scenario system when confidence-gate tests need it.
                        confidence=0.95,
                        # Zero silence — simulator emits synthetic speech-
                        # only turns. Real aurigin-asr fills this with the
                        # VAD-measured silence fraction across the turn.
                        silence_pct=0.0,
                    ),
                )
                turn_count += 1
                turn_offset_ms += self._turn_ms

        # Stream closed. Emit terminal FinalResult with observability counters.
        rtf = 0.0  # simulator doesn't measure wall time; emit zero per proto spec
        yield pb.TranscribeResponse(
            final_result=pb.FinalResult(
                total_audio_ms=total_audio_ms,
                turn_count=turn_count,
                wall_ms=0,
                rtf=rtf,
                model_version=self._model_version,
                silence_pct_mean=0.0,
            ),
        )
        print(
            f"[{session_id or 'sim-anon'}] end | total={total_audio_ms}ms | "
            f"turns={turn_count}",
            file=sys.stderr, flush=True,
        )


async def _serve_async(
    port: int,
    turn_ms: int,
    model_name: str,
    model_version: str,
) -> None:
    server = grpc.aio.server()
    pb_grpc.add_TranscriptionServicer_to_server(
        TranscriptionImpl(
            turn_ms=turn_ms,
            model_name=model_name,
            model_version=model_version,
        ),
        server,
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
        f"ASR simulator listening on :{port} | "
        f"turn_ms={turn_ms} | "
        f"model={model_name}@{model_version} | transport={tls_status}",
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
    print("ASR simulator stopped.", file=sys.stderr, flush=True)


def serve(port: int = 50051) -> None:
    turn_ms = int(os.environ.get("TURN_MS", "2000"))
    model_name = os.environ.get("MODEL_NAME", "parakeet-sim")
    model_version = os.environ.get("MODEL_VERSION", "0.0.0-sim")
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(
            _serve_async(port, turn_ms, model_name, model_version)
        )
    finally:
        loop.close()


if __name__ == "__main__":
    serve(int(os.environ.get("PORT", "50051")))
