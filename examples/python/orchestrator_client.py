"""Minimal AudioVerification.Stream client — SDK smoke-test target.

Opens one bidi session against `aurigin.client.v1.AudioVerification.Stream`
per source and prints every `Verdict` / `EmbeddingVerdict` / `TranscriptTurn`
/ `FraudVerdict` / `FinalResult` the server responds with.

Source of audio (in priority order):
  * `--near-end-file PATH --far-end-file PATH` — stream both WAVs in one
    session, interleaved per chunk, each frame tagged with its endpoint.
    Use this to exercise the dual-leg routing path.
  * `--audio-file PATH` — stream exactly that WAV.
  * `--audio-dir DIR`   — open one session per *.wav found in DIR.
  * neither             — synthesise `--duration` seconds of silence
                          (connectivity smoke-test).

Endpoint attribution:
  * `--endpoint {near_end,far_end,mixed}` — tag every frame with this
    endpoint (uses the ClientAudioFrame wrapper variant).
  * `--track-id STR` — also carried on ClientAudioFrame; opaque to the
    server, kept stable within the session.
  * With no `--endpoint` / `--track-id` / dual-file flags, falls back to
    the raw AudioFrame wire shape (backward-compatible path).

Points at the orchestrator simulator by default (`localhost:50053`).
Override `--target` to hit any AudioVerification server.

Auth is MANDATORY — the SDK-facing gRPC surface expects
`authorization: Bearer <token>` in metadata.

TLS: `--tls auto` (default) picks insecure for `localhost:*` and any
`:80` target, secure for everything else.

CLI:
    python orchestrator_client.py --token sk_test_aurigin_sim_demo_0000000000000000
    python orchestrator_client.py --token <jwt> --target host.example:443 \\
        --audio-file examples/audio/922.wav
    python orchestrator_client.py --token <jwt> --target host.example:443 \\
        --audio-file examples/audio/922.wav --endpoint far_end
    python orchestrator_client.py --token <jwt> --target host.example:443 \\
        --near-end-file mic.wav --far-end-file speaker.wav --track-id demo-call
"""

from __future__ import annotations

import argparse
import asyncio
from itertools import zip_longest
from pathlib import Path

import grpc
from aurigin.client.v1 import audio_verification_pb2 as pb
from aurigin.client.v1 import audio_verification_pb2_grpc as pb_grpc
from aurigin.common.v1 import endpoint_pb2 as ep_pb
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

# `--endpoint` CLI values → Endpoint enum. The "none" sentinel means
# "don't tag frames at all" (falls back to raw AudioFrame wire shape).
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


def _new_session_request(label: str) -> pb.StreamRequest:
    return pb.StreamRequest(
        create_session_request=pb.CreateSessionRequest(
            config=pb.ClientSessionConfig(
                session_type=session_pb.SESSION_TYPE_USER_STREAM,
                attributes={"example": "orchestrator_client.py", "source": label},
            ),
        ),
    )


def _make_frame_request(
    codec: int,
    rate: int,
    channels: int,
    payload: bytes,
    pts_ns: int,
    endpoint: int | None,
    track_id: str | None,
) -> pb.StreamRequest:
    """Build a StreamRequest for one audio frame. Switches between raw
    AudioFrame and ClientAudioFrame depending on whether any attribution
    was supplied — so the no-flag path stays wire-identical to pre-0.3.8.
    """
    frame = af_pb.AudioFrame(
        codec=codec,
        sample_rate_hz=rate,
        channels=channels,
        payload=payload,
        pts_ns=pts_ns,
    )
    if endpoint is None and not track_id:
        return pb.StreamRequest(audio_frame=frame)
    client_frame = pb.ClientAudioFrame(frame=frame)
    if endpoint is not None:
        client_frame.endpoint = endpoint
    if track_id:
        client_frame.track_id = track_id
    return pb.StreamRequest(client_audio_frame=client_frame)


async def _silence_iter(
    duration_s: float,
    endpoint: int | None,
    track_id: str | None,
):
    """Yield session-open + duration_s worth of S16LE silence in _CHUNK_MS chunks."""
    yield _new_session_request(f"silence-{duration_s}s")
    samples_per_chunk = int(_RATE * _CHUNK_MS / 1000)
    silence = b"\x00\x00" * samples_per_chunk * _CHANNELS
    total_chunks = int(duration_s * 1000 / _CHUNK_MS)
    pts_ns = 0
    for _ in range(total_chunks):
        yield _make_frame_request(
            codec=af_pb.AUDIO_CODEC_S16LE,
            rate=_RATE,
            channels=_CHANNELS,
            payload=silence,
            pts_ns=pts_ns,
            endpoint=endpoint,
            track_id=track_id,
        )
        pts_ns += _CHUNK_MS * 1_000_000
        # Pace roughly at wall-clock so the server emits interleaved Verdicts on its own timer.
        await asyncio.sleep(_CHUNK_MS / 1000)


def _chunk_wav(wav: WavData) -> list[tuple[bytes, int]]:
    """Pre-slice a WAV into (chunk_bytes, pts_ns) tuples sized at _CHUNK_MS."""
    bytes_per_chunk = int(wav.rate * _CHUNK_MS / 1000) * wav.bytes_per_sample
    chunks: list[tuple[bytes, int]] = []
    pts_ns = 0
    for start in range(0, len(wav.samples), bytes_per_chunk):
        chunk = wav.samples[start : start + bytes_per_chunk]
        if not chunk:
            break
        chunks.append((chunk, pts_ns))
        actual_frames = len(chunk) // wav.bytes_per_sample
        pts_ns += int(actual_frames / wav.rate * 1e9)
    return chunks


async def _wav_iter(
    wav: WavData,
    label: str,
    endpoint: int | None,
    track_id: str | None,
):
    """Yield session-open + WAV samples in _CHUNK_MS chunks at native rate/codec."""
    yield _new_session_request(label)
    for payload, pts_ns in _chunk_wav(wav):
        yield _make_frame_request(
            codec=wav.audio_codec,
            rate=wav.rate,
            channels=wav.channels,
            payload=payload,
            pts_ns=pts_ns,
            endpoint=endpoint,
            track_id=track_id,
        )
        await asyncio.sleep(_CHUNK_MS / 1000)


async def _dual_wav_iter(
    near_wav: WavData,
    far_wav: WavData,
    label: str,
    track_id: str | None,
):
    """Yield session-open + interleaved NEAR_END and FAR_END frames in
    one session. Both WAVs are chunked at _CHUNK_MS wall-clock; each tick
    emits one NEAR frame then one FAR frame (if either side still has
    audio left). Files of different lengths continue independently until
    both are drained.
    """
    yield _new_session_request(label)
    near_chunks = _chunk_wav(near_wav)
    far_chunks = _chunk_wav(far_wav)
    for near, far in zip_longest(near_chunks, far_chunks):
        if near is not None:
            payload, pts_ns = near
            yield _make_frame_request(
                codec=near_wav.audio_codec,
                rate=near_wav.rate,
                channels=near_wav.channels,
                payload=payload,
                pts_ns=pts_ns,
                endpoint=ep_pb.ENDPOINT_NEAR_END,
                track_id=track_id,
            )
        if far is not None:
            payload, pts_ns = far
            yield _make_frame_request(
                codec=far_wav.audio_codec,
                rate=far_wav.rate,
                channels=far_wav.channels,
                payload=payload,
                pts_ns=pts_ns,
                endpoint=ep_pb.ENDPOINT_FAR_END,
                track_id=track_id,
            )
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


def _attr_suffix(endpoint: int, track_id: str) -> str:
    """Format attribution for printed output; empty when neither is set."""
    parts: list[str] = []
    if endpoint and endpoint != ep_pb.ENDPOINT_UNSPECIFIED:
        parts.append(f"endpoint={_ENDPOINT_LABEL.get(endpoint, endpoint)}")
    if track_id:
        parts.append(f"track={track_id}")
    return (" " + " ".join(parts)) if parts else ""


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
                    f"score={v.score:.3f} confidence={v.confidence:.3f}"
                    f"{_attr_suffix(v.endpoint, v.track_id)}",
                )
            elif kind == "embedding_verdict":
                e = resp.embedding_verdict
                print(
                    f"EMBED   | consumer={e.consumer_name} "
                    f"offset={e.audio_offset_ms}ms dim={e.dim} "
                    f"fingerprint={e.fingerprint_code}"
                    f"{_attr_suffix(e.endpoint, e.track_id)}",
                )
            elif kind == "transcript_turn":
                t = resp.transcript_turn
                print(
                    f"TURN    | turn={t.turn} t0={t.t0_ms}ms t1={t.t1_ms}ms "
                    f"text={t.text!r} confidence={t.confidence:.3f}"
                    f"{_attr_suffix(t.endpoint, t.track_id)}",
                )
            elif kind == "fraud_verdict":
                fv = resp.fraud_verdict
                print(
                    f"FRAUD   | turn={fv.turn} score={fv.score:.3f} "
                    f"decision={fv.decision} family={fv.family or '-'} "
                    f"type={fv.type or '-'}"
                    f"{_attr_suffix(fv.endpoint, fv.track_id)}",
                )
            elif kind == "notification":
                n = resp.notification
                print(f"NOTICE  | type={n.type} message={n.message!r}")
            elif kind == "final_result":
                f = resp.final_result
                print(f"FINAL   | total_audio_ms={f.total_audio_ms}")
                for pc in f.per_consumer:
                    line = (
                        f"        | consumer={pc.consumer_name} "
                        f"label={pc.overall_label_raw or pc.overall_label} "
                        f"score={pc.overall_score:.3f} count={pc.analysis_count}"
                    )
                    if pc.model_version:
                        line += f" model_version={pc.model_version}"
                    print(line)
    except grpc.aio.AioRpcError as exc:
        print(f"ERROR   | code={exc.code().name} detail={exc.details()!r}")


async def run(
    target: str,
    token: str,
    duration_s: float,
    tls_mode: str,
    audio_file: str | None,
    audio_dir: str | None,
    near_end_file: str | None,
    far_end_file: str | None,
    endpoint: int | None,
    track_id: str | None,
) -> None:
    use_tls = _use_tls(target, tls_mode)

    print(
        f"# target={target} tls={use_tls} token_prefix={token[:10]}…"
        f" endpoint={_ENDPOINT_LABEL.get(endpoint, 'untagged') if endpoint else 'untagged'}"
        f" track_id={track_id or '(none)'}",
    )
    metadata = (("authorization", f"Bearer {token}"),)

    channel_cm = (
        grpc.aio.secure_channel(target, grpc.ssl_channel_credentials())
        if use_tls
        else grpc.aio.insecure_channel(target)
    )
    async with channel_cm as channel:
        stub = pb_grpc.AudioVerificationStub(channel)

        # Dual-file mode: one session, two interleaved endpoints.
        if near_end_file and far_end_file:
            near_wav = read_wav(Path(near_end_file))
            far_wav = read_wav(Path(far_end_file))
            label = f"dual {Path(near_end_file).name}+{Path(far_end_file).name}"
            request_iter = _dual_wav_iter(near_wav, far_wav, label, track_id)
            await _run_session(stub, request_iter, label, metadata)
            return

        # Single-source modes.
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

        for label, wav in sources:
            request_iter = (
                _wav_iter(wav, label, endpoint, track_id)
                if wav is not None
                else _silence_iter(duration_s, endpoint, track_id)
            )
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
    parser.add_argument(
        "--near-end-file",
        default=None,
        help=(
            "WAV file for the NEAR_END leg. When combined with --far-end-file, "
            "both are streamed interleaved on one session, each frame tagged "
            "with its endpoint."
        ),
    )
    parser.add_argument(
        "--far-end-file",
        default=None,
        help=(
            "WAV file for the FAR_END leg. See --near-end-file."
        ),
    )
    parser.add_argument(
        "--endpoint",
        choices=list(_ENDPOINT_BY_NAME),
        default=None,
        help=(
            "Tag every frame with this Endpoint (uses the ClientAudioFrame "
            "variant on StreamRequest). Omit for the raw AudioFrame wire "
            "shape — the pre-0.3.8 default."
        ),
    )
    parser.add_argument(
        "--track-id",
        default=None,
        help=(
            "Opaque track_id carried on ClientAudioFrame. Stable per session; "
            "surfaces on Verdict/EmbeddingVerdict/TranscriptTurn/FraudVerdict."
        ),
    )
    args = parser.parse_args()

    # Dual-file mode is mutually exclusive with single-source flags.
    if (args.near_end_file is None) != (args.far_end_file is None):
        parser.error("--near-end-file and --far-end-file must be given together")
    if args.near_end_file and (args.audio_file or args.audio_dir):
        parser.error("--near-end-file/--far-end-file conflicts with --audio-file/--audio-dir")
    if args.near_end_file and args.endpoint:
        parser.error("--endpoint is implied by dual-file mode; don't set both")

    endpoint = _ENDPOINT_BY_NAME[args.endpoint] if args.endpoint else None

    asyncio.run(
        run(
            args.target,
            args.token,
            args.duration,
            args.tls,
            args.audio_file,
            args.audio_dir,
            args.near_end_file,
            args.far_end_file,
            endpoint,
            args.track_id,
        ),
    )


if __name__ == "__main__":
    cli()
