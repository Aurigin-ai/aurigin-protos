"""Minimal AudioVerification.Stream client — SDK smoke-test target.

Opens one bidi session against `aurigin.client.v1.AudioVerification.Stream`
per source and prints every `Verdict` / `EmbeddingVerdict` / `TranscriptTurn`
/ `FraudVerdict` / `FinalResult` the server responds with.

Source of audio (in priority order):
  * Any 2-or-3 of `--near-end-file` / `--far-end-file` / `--mixed-file` —
    stream the WAVs interleaved on one session, each frame tagged with
    its endpoint via ClientAudioFrame. Covers every per-leg scenario
    (NEAR+FAR 1-on-1, NEAR+MIXED Teams-style, NEAR+FAR+MIXED conference).
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

Mid-call state transitions:
  * `--metadata-at SECONDS KEY=VAL[,KEY=VAL...]` (repeatable) — schedule
    a ClientMetadata emission at SECONDS from stream start. Models
    aurigin-guard-win detecting a new foreground app, a meeting
    starting, a mic being suspended, etc.
  * `--demo-teams-meeting` — convenience shorthand that schedules two
    canned emissions modelling a Chrome tab running a Teams meeting
    (meeting_state=in_meeting at 2s, meeting_state=idle at 10s).

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
    python orchestrator_client.py --token <jwt> --target host.example:443 \\
        --near-end-file mic.wav --mixed-file conf_mix.wav --track-id teams-call
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


def _make_metadata_request(attrs: dict[str, str]) -> pb.StreamRequest:
    """Build a StreamRequest carrying a ClientMetadata message with the
    given key/value attributes. `emitted_at_ms` is stamped from wall-
    clock at call time — the client's view of when the state transition
    occurred (distinct from the server's receive time)."""
    import time
    return pb.StreamRequest(
        client_metadata=pb.ClientMetadata(
            emitted_at_ms=int(time.time() * 1000),
            attributes=attrs,
        ),
    )


def _drain_due_metadata(
    schedule: list[tuple[float, dict[str, str]]],
    elapsed_s: float,
):
    """Yield any pending ClientMetadata requests whose scheduled time
    has been reached. Mutates `schedule` in place by popping fired
    events (expected to be sorted ascending by time)."""
    while schedule and schedule[0][0] <= elapsed_s:
        _, attrs = schedule.pop(0)
        yield _make_metadata_request(attrs)


async def _silence_iter(
    duration_s: float,
    endpoint: int | None,
    track_id: str | None,
    metadata_schedule: list[tuple[float, dict[str, str]]] | None = None,
):
    """Yield session-open + duration_s worth of S16LE silence in _CHUNK_MS chunks.

    Optionally interleaves ClientMetadata emissions at the stream-time
    offsets in `metadata_schedule` — sorted (time_s, attrs) tuples.
    """
    yield _new_session_request(f"silence-{duration_s}s")
    samples_per_chunk = int(_RATE * _CHUNK_MS / 1000)
    silence = b"\x00\x00" * samples_per_chunk * _CHANNELS
    total_chunks = int(duration_s * 1000 / _CHUNK_MS)
    pts_ns = 0
    pending = list(metadata_schedule or ())
    elapsed_s = 0.0
    for _ in range(total_chunks):
        for md in _drain_due_metadata(pending, elapsed_s):
            yield md
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
        elapsed_s += _CHUNK_MS / 1000
        # Pace roughly at wall-clock so the server emits interleaved Verdicts on its own timer.
        await asyncio.sleep(_CHUNK_MS / 1000)
    # Flush any trailing scheduled entries past the audio end.
    for md in _drain_due_metadata(pending, float("inf")):
        yield md


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
    metadata_schedule: list[tuple[float, dict[str, str]]] | None = None,
):
    """Yield session-open + WAV samples in _CHUNK_MS chunks at native rate/codec.

    Optionally interleaves ClientMetadata emissions per
    `metadata_schedule` — same semantics as _silence_iter.
    """
    yield _new_session_request(label)
    pending = list(metadata_schedule or ())
    elapsed_s = 0.0
    for payload, pts_ns in _chunk_wav(wav):
        for md in _drain_due_metadata(pending, elapsed_s):
            yield md
        yield _make_frame_request(
            codec=wav.audio_codec,
            rate=wav.rate,
            channels=wav.channels,
            payload=payload,
            pts_ns=pts_ns,
            endpoint=endpoint,
            track_id=track_id,
        )
        elapsed_s += _CHUNK_MS / 1000
        await asyncio.sleep(_CHUNK_MS / 1000)
    for md in _drain_due_metadata(pending, float("inf")):
        yield md


async def _multi_wav_iter(
    sources: list[tuple[int, WavData]],
    label: str,
    track_id: str | None,
    metadata_schedule: list[tuple[float, dict[str, str]]] | None = None,
):
    """Yield session-open + interleaved frames from up to three endpoint-
    tagged WAVs in one session. Each tick walks every endpoint slot once
    and emits one frame per side that still has audio. Files of
    different lengths continue independently until all are drained.

    `sources` is a list of (endpoint_enum, WavData) pairs — any
    combination of NEAR_END / FAR_END / MIXED, min 2 entries (dual
    mode is the smoke-test target for per-leg routing). Metadata
    scheduling behaves the same way as the single-source iterators.
    """
    yield _new_session_request(label)
    pending = list(metadata_schedule or ())
    elapsed_s = 0.0
    chunked = [(endpoint, wav, _chunk_wav(wav)) for endpoint, wav in sources]
    for frame_slice in zip_longest(*[chunks for _, _, chunks in chunked]):
        for md in _drain_due_metadata(pending, elapsed_s):
            yield md
        for (endpoint, wav, _), frame in zip(chunked, frame_slice, strict=True):
            if frame is None:
                continue
            payload, pts_ns = frame
            yield _make_frame_request(
                codec=wav.audio_codec,
                rate=wav.rate,
                channels=wav.channels,
                payload=payload,
                pts_ns=pts_ns,
                endpoint=endpoint,
                track_id=track_id,
            )
        elapsed_s += _CHUNK_MS / 1000
        await asyncio.sleep(_CHUNK_MS / 1000)
    for md in _drain_due_metadata(pending, float("inf")):
        yield md


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
    mixed_file: str | None,
    endpoint: int | None,
    track_id: str | None,
    metadata_schedule: list[tuple[float, dict[str, str]]] | None = None,
) -> None:
    use_tls = _use_tls(target, tls_mode)

    schedule = sorted(metadata_schedule or (), key=lambda item: item[0])
    md_summary = (
        ", ".join(f"{t:.1f}s:{{{','.join(sorted(a))}}}" for t, a in schedule)
        if schedule else "(none)"
    )
    print(
        f"# target={target} tls={use_tls} token_prefix={token[:10]}…"
        f" endpoint={_ENDPOINT_LABEL.get(endpoint, 'untagged') if endpoint else 'untagged'}"
        f" track_id={track_id or '(none)'} metadata={md_summary}",
    )
    metadata = (("authorization", f"Bearer {token}"),)

    channel_cm = (
        grpc.aio.secure_channel(target, grpc.ssl_channel_credentials())
        if use_tls
        else grpc.aio.insecure_channel(target)
    )
    async with channel_cm as channel:
        stub = pb_grpc.AudioVerificationStub(channel)

        # Multi-file mode: one session, interleaved endpoints. Any
        # combination of 2 or 3 of (near, far, mixed) is valid; frames
        # of each kind are tagged with its endpoint and interleaved
        # per 100-ms tick.
        multi_sources: list[tuple[int, WavData]] = []
        if near_end_file:
            multi_sources.append((ep_pb.ENDPOINT_NEAR_END, read_wav(Path(near_end_file))))
        if far_end_file:
            multi_sources.append((ep_pb.ENDPOINT_FAR_END, read_wav(Path(far_end_file))))
        if mixed_file:
            multi_sources.append((ep_pb.ENDPOINT_MIXED, read_wav(Path(mixed_file))))
        if len(multi_sources) >= 2:
            label = "multi " + "+".join(
                Path(f).name for f in (near_end_file, far_end_file, mixed_file) if f
            )
            request_iter = _multi_wav_iter(multi_sources, label, track_id, schedule)
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

        # Only emit the metadata schedule on the FIRST source — avoids
        # replaying the Teams-meeting-started transition on every WAV
        # in a dir sweep.
        for i, (label, wav) in enumerate(sources):
            iter_schedule = schedule if i == 0 else None
            request_iter = (
                _wav_iter(wav, label, endpoint, track_id, iter_schedule)
                if wav is not None
                else _silence_iter(duration_s, endpoint, track_id, iter_schedule)
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
            "WAV file for the NEAR_END leg. Combine with --far-end-file "
            "and/or --mixed-file (any 2 or 3) to stream multiple "
            "endpoints interleaved on one session, each frame tagged "
            "with its endpoint via the ClientAudioFrame variant."
        ),
    )
    parser.add_argument(
        "--far-end-file",
        default=None,
        help="WAV file for the FAR_END leg. See --near-end-file.",
    )
    parser.add_argument(
        "--mixed-file",
        default=None,
        help=(
            "WAV file for the MIXED stream (subject + peers inseparable — "
            "SIPREC mix-mode, conference-mix recordings). Combine with "
            "--near-end-file for NEAR+MIXED (e.g. MS Teams integration "
            "that delivers a conference mix alongside a separate mic "
            "capture), with --far-end-file, or with both for a "
            "three-endpoint smoke test."
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
    parser.add_argument(
        "--metadata-at",
        action="append",
        default=[],
        metavar="SECONDS KEY=VAL[,KEY=VAL...]",
        nargs=2,
        help=(
            "Schedule a ClientMetadata emission at SECONDS from stream "
            "start, carrying the given comma-separated KEY=VAL attributes. "
            "Repeatable; values may contain spaces (quote the whole "
            "second argument) but NOT commas. Example: "
            "--metadata-at 2 active_app=chrome.exe,meeting_state=in_meeting"
        ),
    )
    parser.add_argument(
        "--demo-teams-meeting",
        action="store_true",
        help=(
            "Convenience shorthand — schedules two ClientMetadata "
            "emissions modelling an aurigin-guard-win session where a "
            "Chrome Teams meeting starts partway through and ends before "
            "stream close. Equivalent to two --metadata-at invocations; "
            "ignored if any --metadata-at is also given."
        ),
    )
    args = parser.parse_args()

    # Multi-file mode (any 2 or 3 of near/far/mixed) is mutually
    # exclusive with single-source flags.
    multi_count = sum(
        1 for f in (args.near_end_file, args.far_end_file, args.mixed_file) if f
    )
    if multi_count == 1:
        parser.error(
            "--near-end-file / --far-end-file / --mixed-file need at least two "
            "of them together — a single endpoint input belongs on --audio-file "
            "(with --endpoint)",
        )
    if multi_count >= 2 and (args.audio_file or args.audio_dir):
        parser.error("multi-file mode conflicts with --audio-file/--audio-dir")
    if multi_count >= 2 and args.endpoint:
        parser.error("--endpoint is implied by multi-file mode; don't set both")

    endpoint = _ENDPOINT_BY_NAME[args.endpoint] if args.endpoint else None

    # Parse --metadata-at pairs into (time_s, {key: value}) tuples.
    # Fall back to --demo-teams-meeting if the explicit flag was not set.
    schedule: list[tuple[float, dict[str, str]]] = []
    for time_str, kv_str in args.metadata_at:
        try:
            time_s = float(time_str)
        except ValueError:
            parser.error(f"--metadata-at: expected a float SECONDS, got {time_str!r}")
        attrs: dict[str, str] = {}
        for pair in kv_str.split(","):
            pair = pair.strip()
            if not pair:
                continue
            if "=" not in pair:
                parser.error(
                    f"--metadata-at: expected KEY=VAL pairs separated by commas, "
                    f"got {pair!r} (in {kv_str!r})",
                )
            k, v = pair.split("=", 1)
            attrs[k.strip()] = v.strip()
        if not attrs:
            parser.error(f"--metadata-at: no KEY=VAL attributes parsed from {kv_str!r}")
        schedule.append((time_s, attrs))

    if not schedule and args.demo_teams_meeting:
        # Models aurigin-guard-win detecting a Chrome tab running Teams.
        # Timings are chosen so a default 15 s `--duration` sees both
        # transitions and still has audio flowing past "idle" before
        # stream close.
        schedule = [
            (
                2.0,
                {
                    "active_app": "chrome.exe",
                    "active_window_title": "Weekly Sync | Microsoft Teams",
                    "meeting_state": "in_meeting",
                },
            ),
            (10.0, {"meeting_state": "idle"}),
        ]

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
            args.mixed_file,
            endpoint,
            args.track_id,
            schedule,
        ),
    )


if __name__ == "__main__":
    cli()
