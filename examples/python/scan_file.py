"""Fast offline scan of one long audio file — parallel slices, no pacing.

Companion to `phone_call.py` / `phone_call_burst.py`, but explicitly NOT
a real-time simulator:

  - `phone_call.py` / `phone_call_burst.py` — simulate live phone calls
    at wallclock RTF ≈ 1.0. Sleeps `chunk_ms` between AudioFrames.
    Used for load-testing the "N concurrent live calls" shape.
  - `client.py` — single-session one-shot: streams as fast as gRPC
    accepts (RTF > 1). Good for a quick single-file check, but the
    server-side batcher can never fill batches from ONE session (windows
    arrive every ANALYSIS_INTERVAL_S = 5 s from a single stream, way
    longer than BATCH_FLUSH_MS = 10 ms). So `client.py` gets batch=1
    inference on every window, leaving GPU throughput on the table.
  - `scan_file.py` (this file) — splits one file into N contiguous
    slices and streams them as N concurrent sessions on ONE gRPC channel,
    each at full speed (no sleep). The server-side batcher now sees
    N windows arriving within the flush interval and can fill batches
    up to `BATCH_SIZE`. Practical throughput: near-linear scaling in N
    until GPU / model saturates.

Sizing recommendations for a long file (say a 60-min recording):

  - **Conservative** (`--concurrency 8 --chunk-ms 100`, defaults):
      ~8× faster than `client.py` on the same file. Safe on any dfs.
  - **Aggressive** (`--concurrency 90 --frame-seconds 40`):
      splits a 60-min file into 90 × 40 s slices, one AudioFrame per
      slice. Rationale: 90 = the top concurrent-call ceiling the dfs
      can serve on the current hardware; each stream carries 8
      analysis windows (8 × 5 s = 40 s) so every stream perfectly fills
      one `BATCH_SIZE=8` batch on the server. Result: 90 concurrent
      windows arrive at the batcher within one flush interval, GPU
      saturates on batched inference, RTF > 100×. Requires:
        - dfs has GPU headroom (`nvidia-smi` idle before the run)
        - HTTP/2 MAX_CONCURRENT_STREAMS on the server ≥ 90 (default: 100)
        - grpc max message size ≥ frame_seconds × rate × 2 bytes
          (scan_file bumps this to 16 MB per channel — see `_CHANNEL_OPTS`)

Boundary handling: slices are contiguous, no overlap. With 5-s alignment
(the default) every slice length is a multiple of `analysis_interval_s`
AND every slice-boundary falls on a 5-s grid line — the server's window
boundaries within slice[i] never cross slice[i]'s end. Zero coverage
loss on inter-slice boundaries. The only residue is a possible
sub-window tail at the very end of the file (< 5 s), which the last
slice absorbs and the server handles per `tail_strategy` (default:
`drop` — recommended, since a 5-s-trained model can misfire on a 1-s
partial window).

Set `align_seconds=0` to disable alignment (legacy shape — trades score
integrity for coverage; not recommended for production).

CSV output matches `phone_call_burst.py` / `client.py`: one row per
per-chunk `AnalysisResult`, N session-groups per file (one per slice).
`chunk_offset` is ABSOLUTE within the source file (the client adds each
slice's offset to the server-reported per-slice offset).

CLI:
    uv run scan-file --file audio.wav [--target HOST:PORT]
                     [--concurrency 8] [--chunk-ms 100 | --frame-seconds N]
                     [--csv PATH] [--scenario-id ID]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

import grpc
from aurigin.deepfake_detection.v1 import deepfake_detection_pb2 as pb
from aurigin.deepfake_detection.v1 import deepfake_detection_pb2_grpc as pb_grpc
from aurigin.media.v1 import audio_frame_pb2 as af_pb

from common import (
    ChunkRow,
    ResultCSV,
    WavData,
    install_signal_shutdown,
    read_wav,
    transport_label,
)
from common.tls import _channel_credentials  # module-private, reused only here

DEFAULT_CONCURRENCY = 8
DEFAULT_CHUNK_MS = 100  # Bigger than phone_call.py's 20ms: fewer roundtrips
# for the same audio, doesn't matter for offline scan.

# `--extreme` preset — maximum-throughput offline file scan. Rationale:
#   90 = top concurrent-call ceiling of the current dfs (HTTP/2 default
#        MAX_CONCURRENT_STREAMS=100; picks 90 with headroom for control)
#   40 s per AudioFrame = 8 × ANALYSIS_INTERVAL_S (5 s), so each stream
#        fills exactly one server-side batch of BATCH_SIZE=8. 90 streams
#        × 8 windows = 720 concurrent windows arriving within the batcher
#        flush interval → GPU saturates on batched inference.
# Use with a long file (≥ 60 min) for the intended benefit. Short files
# get penalised by boundary loss (~5 s per slice boundary).
EXTREME_CONCURRENCY = 90
EXTREME_FRAME_SECONDS = 40.0

# gRPC channel options tuned for large AudioFrame payloads.
# `--frame-seconds 40` at 48 kHz S16LE = 40 * 48000 * 2 = 3.84 MB per frame,
# right up against grpc's 4 MB default. Bump to 16 MB so any realistic
# sample-rate + frame-size combo fits with headroom. Doesn't affect the
# real-time streaming clients (their frames are tiny).
_MAX_MSG_BYTES = 16 * 1024 * 1024
_CHANNEL_OPTS: list[tuple[str, int]] = [
    ("grpc.max_send_message_length", _MAX_MSG_BYTES),
    ("grpc.max_receive_message_length", _MAX_MSG_BYTES),
    # Keepalive — send HTTP/2 PING every 30 s of idle, fail after 10 s
    # no pong. Prevents the load balancer / server from silently
    # dropping the channel mid-scan if one slice stalls.
    ("grpc.keepalive_time_ms", 30_000),
    ("grpc.keepalive_timeout_ms", 10_000),
    ("grpc.keepalive_permit_without_calls", 1),
    ("grpc.http2.max_pings_without_data", 0),
    # Allow more concurrent streams from OUR side than the HTTP/2
    # default of 100. Server still enforces its own MAX_CONCURRENT_STREAMS.
    ("grpc.max_concurrent_streams", 256),
]


def _make_aio_channel_with_opts(target: str) -> grpc.aio.Channel:
    """Local channel factory — same TLS resolution as `common.make_aio_channel`
    but injects the enlarged message-size options. Kept local so the shared
    factory in `common/tls.py` stays minimal (real-time clients don't need it)."""
    creds = _channel_credentials()
    if creds is not None:
        return grpc.aio.secure_channel(target, creds, options=_CHANNEL_OPTS)
    return grpc.aio.insecure_channel(target, options=_CHANNEL_OPTS)


def _resolve_audio(path_arg: Path | None) -> Path:
    """Same resolution rule as phone_call.py — first .wav in examples/audio/."""
    if path_arg is not None:
        return path_arg.resolve()
    audio_dir = Path(__file__).resolve().parent.parent / "audio"
    candidates = sorted(audio_dir.glob("*.wav"))
    if not candidates:
        raise SystemExit(f"No .wav files in {audio_dir}. Pass --file explicitly.")
    return candidates[0]


def _split_wav(
    wav: WavData,
    concurrency: int,
    *,
    align_seconds: float = 5.0,
) -> list[tuple[bytes, int]]:
    """Split the PCM into N contiguous slices, aligned to full analysis-window boundaries.

    Returns `[(slice_pcm_bytes, slice_offset_ms), ...]`. Each slice runs
    as its own gRPC session server-side.

    **Alignment matters for score integrity.** Server analyses at fixed
    `analysis_interval_s` windows (default 5 s). If a slice length isn't a
    multiple of that interval, the trailing partial window becomes a
    "tail" — a 1-2 s clip that gets fed to a 5-s-trained model, producing
    spuriously high spoof scores. To eliminate tails entirely, we round
    every slice length DOWN to a multiple of `align_seconds`.

    **Residue distribution kills the straggler.** After the aligned per-slice
    allocation, there are `leftover = total - per_slice × concurrency` frames
    left over. Those frames form some number of *aligned windows* plus a
    <align_seconds sub-window residue. Naively giving the whole leftover to
    the last slice makes it a straggler that dominates wall time at high
    concurrency (measured: --concurrency 256 on a 1h file put ~31% of the
    audio on one final session, blowing wall from 21s → 44s).

    Fix: hand the aligned leftover windows out ONE AT A TIME to the first N
    slices; only the sub-window residue (<align_seconds) lands on the very
    last slice. Every slice ends up within ±1 aligned window of every other,
    the batcher stays fed throughout, no straggler.

    Example: 3701-s file, concurrency=256, align_seconds=5 (16 kHz):
      - per_slice = 10 s (2 windows)  ← 3701//256 = 14.5s → aligned down
      - leftover  = 1141 s = 228 aligned windows + 1 s sub-window residue
      - slices 0..227:  15 s each (3 windows)   ← extra window
      - slices 228..254: 10 s each (2 windows)  ← plain per_slice
      - slice 255:      11 s (2 windows + 1 s residue tail)
      - Max chunks/session drops from 231 → 3. GPU stays batch-fed.

    `align_seconds=0` disables alignment (legacy behavior — produces tails
    on inter-slice boundaries; kept for callers that care about coverage
    over score integrity).
    """
    bps = wav.bytes_per_sample * wav.channels  # bytes per sample-frame
    total_frames = len(wav.samples) // bps

    if align_seconds > 0:
        align_frames = int(align_seconds * wav.rate)
        # Target per-slice frame count, rounded DOWN to align_frames.
        target = total_frames // concurrency
        per_slice = (target // align_frames) * align_frames
        if per_slice == 0:
            raise SystemExit(
                f"File too short for --concurrency={concurrency} at align={align_seconds}s "
                f"({total_frames} sample-frames, {total_frames / wav.rate:.1f}s total). "
                f"Reduce concurrency or use a longer file."
            )
        leftover_frames = total_frames - per_slice * concurrency
        leftover_windows = leftover_frames // align_frames
    else:
        per_slice = total_frames // concurrency
        if per_slice == 0:
            raise SystemExit(
                f"File too short ({total_frames} sample-frames) for --concurrency={concurrency}."
            )
        align_frames = 0
        leftover_windows = 0

    slices: list[tuple[bytes, int]] = []
    cursor = 0
    for i in range(concurrency):
        extra = align_frames if i < leftover_windows else 0
        end = cursor + per_slice + extra
        if i == concurrency - 1:
            # Last slice always ends at EOF — picks up the sub-window residue
            # (< align_frames) that couldn't be redistributed. In the
            # align_seconds=0 path this absorbs the whole leftover (legacy).
            end = total_frames
        start_byte = cursor * bps
        end_byte = end * bps
        offset_ms = int(cursor / wav.rate * 1000)
        slices.append((wav.samples[start_byte:end_byte], offset_ms))
        cursor = end
    return slices


async def _fast_stream_slice(
    call,
    slice_pcm: bytes,
    slice_offset_ms: int,
    wav: WavData,
    bytes_per_chunk: int,
    label: str,
    sink: dict,
    detection_config: pb.DetectionConfig | None = None,
) -> None:
    """Send CreateSession + AudioFrames for one slice as fast as gRPC accepts.

    NO `asyncio.sleep` — this is offline scan, we want maximum throughput.
    Each frame's `pts_ns` is slice-relative (0-based); server-reported
    offsets get shifted by `slice_offset_ms` in the recv loop.

    `detection_config` (optional) is passed on the CreateSessionRequest so
    per-session server-side behavior (VAD mode, silence threshold,
    analysis interval) can be overridden without touching env vars. When
    None, server env defaults apply.
    """
    sink.setdefault("chunks", [])
    sink["slice_offset_ms"] = slice_offset_ms
    sink["send_start_ns"] = time.perf_counter_ns()

    create_req = pb.CreateSessionRequest()
    if detection_config is not None:
        create_req.config.CopyFrom(detection_config)
    await call.write(pb.DetectDeepfakeRequest(create_session_request=create_req))

    pts_ns = 0
    for start in range(0, len(slice_pcm), bytes_per_chunk):
        chunk = slice_pcm[start : start + bytes_per_chunk]
        if not chunk:
            break
        actual_frames = len(chunk) // wav.bytes_per_sample
        duration_ns = int(actual_frames / wav.rate * 1e9)
        await call.write(
            pb.DetectDeepfakeRequest(
                audio_frame=af_pb.AudioFrame(
                    codec=wav.audio_codec,
                    sample_rate_hz=wav.rate,
                    channels=wav.channels,
                    payload=chunk,
                    pts_ns=pts_ns,
                ),
            ),
        )
        pts_ns += duration_ns

    await call.done_writing()


async def _recv_slice(call, label: str, sink: dict) -> None:
    """Collect AnalysisResults from one slice, shift offsets to absolute.

    Timestamps every message boundary in `sink` (perf_counter_ns) so the
    aggregate report can compute honest per-slice latencies. Timing points:

      send_start_ns      → written in _fast_stream_slice before CreateSession
      session_open_ns    → CreateSessionResponse received (measures server open)
      first_result_ns    → first AnalysisResult received (first analysis window)
      last_result_ns     → last AnalysisResult received
      final_ns           → FinalResult received (session-complete on server)

    All in ns; divide by 1e6 for ms. Deltas the report exposes:

      channel_to_open_ms   ~ TLS handshake amortisation + server accept
      open_to_first_ms     ~ time to first analysis (audio buffered → VAD → model)
      first_to_last_ms     ~ streaming duration (bulk of the work)
      last_to_final_ms     ~ tail flush + finalise

    Mirrors `phone_call.recv_call` but adds `slice_offset_ms` to every
    chunk's `audio_offset_ms` so the CSV rows are timeline-correct
    across slices.
    """
    slice_offset_ms = sink.get("slice_offset_ms", 0)
    print(f"[{label}] started (slice_offset={slice_offset_ms / 1000:.1f}s)")

    async for response in call:
        kind = response.WhichOneof("response")
        now_ns = time.perf_counter_ns()
        if kind == "create_session_response":
            sink["session_id"] = response.create_session_response.session_id
            sink["session_open_ns"] = now_ns
        elif kind == "analysis_result":
            r = response.analysis_result
            if "first_result_ns" not in sink:
                sink["first_result_ns"] = now_ns
            sink["last_result_ns"] = now_ns
            sink["chunks"].append(
                ChunkRow(
                    offset_ms=r.audio_offset_ms + slice_offset_ms,
                    duration_ms=r.duration_ms,
                    score=r.score,
                    confidence=r.confidence,
                    label=r.label,
                ),
            )
        elif kind == "final_result":
            f = response.final_result
            sink["final_ns"] = now_ns
            sink["audio_duration_ms"] = f.total_audio_ms
            sink["global_result"] = f.overall_label
            sink["global_score"] = f.overall_score
            sink["analysis_count"] = f.analysis_count
            print(
                f"[{label}] done: audio={f.total_audio_ms / 1000:.1f}s "
                f"score={f.overall_score:.3f} label={f.overall_label} "
                f"analyses={f.analysis_count}",
            )


async def _run_scan(
    target: str,
    audio_path: Path,
    concurrency: int,
    chunk_ms: int,
    csv_path: str | None,
    scenario_id: str | None,
    vad_mode: str = "auto",
    vad_threshold_pct: float | None = None,
    tail_strategy: str = "auto",
    min_analysis_duration_s: float | None = None,
) -> None:
    wav = read_wav(audio_path)
    slices = _split_wav(wav, concurrency, align_seconds=5.0)
    bytes_per_chunk = max(1, int(wav.rate * chunk_ms / 1000) * wav.bytes_per_sample)

    # Per-session DetectionConfig (aurigin-protos 0.4+). None → server env
    # defaults; populated → override at CreateSessionRequest.config so the
    # server logs "vad_source=per-session" for every slice in this run.
    detection_config: pb.DetectionConfig | None = None
    _VAD_ENUM = {"auto": 0, "on": 1, "off": 2}
    _TAIL_ENUM = {"auto": 0, "drop": 1, "extend": 2, "recompute": 3}
    _any_override = (
        vad_mode != "auto"
        or vad_threshold_pct is not None
        or tail_strategy != "auto"
        or min_analysis_duration_s is not None
    )
    if _any_override:
        detection_config = pb.DetectionConfig()
        if vad_mode != "auto":
            # Guarded — older aurigin-protos wheels won't have vad_mode.
            if "vad_mode" in detection_config.DESCRIPTOR.fields_by_name:
                detection_config.vad_mode = _VAD_ENUM[vad_mode]
            else:
                print(
                    "⚠️  installed aurigin-protos wheel doesn't ship vad_mode field — "
                    "--vad-mode ignored. Upgrade aurigin-protos to ≥ 0.4.",
                    file=sys.stderr,
                )
        if vad_threshold_pct is not None:
            if (
                "vad_silence_threshold_pct"
                in detection_config.DESCRIPTOR.fields_by_name
            ):
                detection_config.vad_silence_threshold_pct = vad_threshold_pct
            else:
                print(
                    "⚠️  installed aurigin-protos wheel doesn't ship vad_silence_threshold_pct — "
                    "--vad-threshold ignored. Upgrade aurigin-protos to ≥ 0.4.",
                    file=sys.stderr,
                )
        if tail_strategy != "auto":
            if "tail_strategy" in detection_config.DESCRIPTOR.fields_by_name:
                detection_config.tail_strategy = _TAIL_ENUM[tail_strategy]
            else:
                print(
                    "⚠️  installed aurigin-protos wheel doesn't ship tail_strategy field — "
                    "--tail-strategy ignored. Upgrade aurigin-protos to ≥ 0.5.",
                    file=sys.stderr,
                )
        if min_analysis_duration_s is not None:
            if (
                "min_analysis_duration_s"
                in detection_config.DESCRIPTOR.fields_by_name
            ):
                detection_config.min_analysis_duration_s = min_analysis_duration_s
            else:
                print(
                    "⚠️  installed aurigin-protos wheel doesn't ship min_analysis_duration_s — "
                    "--min-analysis-duration ignored. Upgrade aurigin-protos to ≥ 0.5.",
                    file=sys.stderr,
                )

    slice_seconds = [
        len(pcm) / (wav.bytes_per_sample * wav.channels * wav.rate) for pcm, _ in slices
    ]
    min_s = min(slice_seconds) if slice_seconds else 0
    max_s = max(slice_seconds) if slice_seconds else 0
    print(
        f"📁 Scanning {audio_path.name} "
        f"({wav.duration_s:.1f}s @ {wav.rate}Hz/{wav.channels}ch {wav.wire_format}) "
        f"| target={target} | {transport_label()}",
    )
    if abs(max_s - min_s) < 0.001:
        size_label = f"{min_s:.1f}s each"
    else:
        size_label = (
            f"{min_s:.1f}-{max_s:.1f}s each (residue balanced across earlier slices)"
        )
    print(
        f"  Splitting into {concurrency} slices ({size_label}, 5s-aligned) · chunk_ms={chunk_ms}",
    )
    if detection_config is not None:
        cfg_parts = []
        if vad_mode != "auto":
            cfg_parts.append(f"vad_mode={vad_mode}")
        if vad_threshold_pct is not None:
            cfg_parts.append(f"vad_threshold_pct={vad_threshold_pct}")
        if tail_strategy != "auto":
            cfg_parts.append(f"tail_strategy={tail_strategy}")
        if min_analysis_duration_s is not None:
            cfg_parts.append(f"min_analysis_duration_s={min_analysis_duration_s}")
        print(f"  Per-session DetectionConfig: {', '.join(cfg_parts)}")

    channel = _make_aio_channel_with_opts(target)
    stub = pb_grpc.DeepfakeDetectionStub(channel)

    # Force TCP + mTLS handshake to complete NOW, before spawning N
    # concurrent slices. Otherwise the first slice pays the ~100-300 ms
    # channel-init cost on its critical path while the other N-1 sit
    # queued behind it. `channel_ready()` blocks until the channel is
    # in READY state; all subsequent RPCs reuse the warm connection.
    handshake_start = time.perf_counter()
    try:
        await asyncio.wait_for(channel.channel_ready(), timeout=15.0)
    except TimeoutError as e:
        raise SystemExit(
            f"channel didn't reach READY within 15s (target={target})"
        ) from e
    handshake_ms = (time.perf_counter() - handshake_start) * 1000
    print(
        f"  channel ready in {handshake_ms:.0f} ms (one TLS handshake shared across all {concurrency} streams)"
    )

    metadata = [("x-scenario-id", scenario_id)] if scenario_id else None
    sinks: list[dict] = [{} for _ in range(concurrency)]

    wall_start = time.perf_counter()

    async def _one_slice(i: int, slice_pcm: bytes, slice_offset_ms: int) -> None:
        call = (
            stub.DetectDeepfake(metadata=metadata)
            if metadata
            else stub.DetectDeepfake()
        )
        label = f"slice-{i:02d}"
        try:
            await asyncio.gather(
                _fast_stream_slice(
                    call,
                    slice_pcm,
                    slice_offset_ms,
                    wav,
                    bytes_per_chunk,
                    label,
                    sinks[i],
                ),
                _recv_slice(call, label, sinks[i]),
            )
        except grpc.aio.AioRpcError as e:
            print(
                f"[{label}] gRPC error: {e.code().name}: {e.details()}", file=sys.stderr
            )
            sinks[i].setdefault("session_id", "")

    tasks = [
        asyncio.create_task(_one_slice(i, pcm, off), name=f"scan-slice-{i:02d}")
        for i, (pcm, off) in enumerate(slices)
    ]
    install_signal_shutdown(tasks)

    try:
        await asyncio.gather(*tasks, return_exceptions=False)
    finally:
        await channel.close()

    wall_ms = (time.perf_counter() - wall_start) * 1000

    # ─── Aggregate + report ────────────────────────────────────────────
    all_chunks = [c for s in sinks for c in s.get("chunks", [])]
    total_audio_ms = wav.duration_s * 1000
    total_analyses = sum(s.get("analysis_count", 0) for s in sinks)
    global_scores = [s.get("global_score") for s in sinks if "global_score" in s]
    mean_score = sum(global_scores) / len(global_scores) if global_scores else 0.0
    worst_verdict = max(
        (s.get("global_result", "no_final_result") for s in sinks),
        key=lambda v: {
            "spoofed": 3,
            "partially_spoofed": 2,
            "bonafide": 1,
            "silence": 0,
            "unknown": -1,
        }.get(v, -1),
    )

    print()
    print("═══ Scan complete ═══")
    print(f"  wall_time:  {wall_ms / 1000:.2f} s")
    print(f"  audio:      {total_audio_ms / 1000:.2f} s")
    print(
        f"  RTF:        {total_audio_ms / wall_ms:.2f}× "
        f"({concurrency}-slice parallel · vs single-session baseline)"
    )
    print(
        f"  slices:     {concurrency} (all completed)"
        if all(s.get("session_id") for s in sinks)
        else f"  slices:     {sum(1 for s in sinks if s.get('session_id'))}/{concurrency} completed"
    )
    print(f"  analyses:   {total_analyses} windows across all slices")
    print(f"  chunks:     {len(all_chunks)} per-window results")
    print(f"  mean_score: {mean_score:.4f}")
    print(f"  worst_verdict: {worst_verdict}")

    # ─── Per-slice timing report (min / p50 / p95 / max, in ms) ────────
    # All timestamps captured via time.perf_counter_ns() so cross-slice
    # deltas are honest to the μs. Only include slices that reached every
    # milestone (partial failures skew percentiles).
    def _pct(vals: list[float], p: float) -> float:
        if not vals:
            return 0.0
        s = sorted(vals)
        k = max(0, min(len(s) - 1, round((p / 100.0) * (len(s) - 1))))
        return s[k]

    def _stats(name: str, vals: list[float]) -> str:
        if not vals:
            return f"  {name:<22} (no data)"
        return (
            f"  {name:<22} "
            f"min={min(vals):7.1f}  p50={_pct(vals, 50):7.1f}  "
            f"p95={_pct(vals, 95):7.1f}  max={max(vals):7.1f}   (ms)"
        )

    open_to_first = [
        (s["first_result_ns"] - s["session_open_ns"]) / 1e6
        for s in sinks
        if "session_open_ns" in s and "first_result_ns" in s
    ]
    send_to_open = [
        (s["session_open_ns"] - s["send_start_ns"]) / 1e6
        for s in sinks
        if "send_start_ns" in s and "session_open_ns" in s
    ]
    first_to_last = [
        (s["last_result_ns"] - s["first_result_ns"]) / 1e6
        for s in sinks
        if "first_result_ns" in s and "last_result_ns" in s
    ]
    last_to_final = [
        (s["final_ns"] - s["last_result_ns"]) / 1e6
        for s in sinks
        if "last_result_ns" in s and "final_ns" in s
    ]
    send_to_final = [
        (s["final_ns"] - s["send_start_ns"]) / 1e6
        for s in sinks
        if "send_start_ns" in s and "final_ns" in s
    ]

    print()
    print(f"  ── per-slice timing (n={len(send_to_final)}/{concurrency}) ──")
    print(
        f"  channel_ready          {handshake_ms:7.1f}  (one-shot, TLS handshake shared)"
    )
    print(_stats("send_start→open", send_to_open))
    print(_stats("open→first_result", open_to_first))
    print(_stats("first→last_result", first_to_last))
    print(_stats("last→final_result", last_to_final))
    print(_stats("send_start→final", send_to_final))

    if csv_path:
        with ResultCSV(csv_path) as csv_out:
            for i, sink in enumerate(sinks):
                sid = sink.get("session_id", "")
                if not sid:
                    continue
                # per-slice processing_time_ms = the whole scan's wall time
                # (all slices ran concurrently within that window). Downstream
                # aggregation typically averages processing_time_ms per file,
                # so using the shared wall time is the honest number here.
                csv_out.write_session(
                    file_name=audio_path.name,
                    session_id=sid,
                    chunks=sink.get("chunks", []),
                    audio_duration_ms=sink.get("audio_duration_ms", 0),
                    global_result=sink.get("global_result", "no_final_result"),
                    processing_time_ms=wall_ms,
                )
        print(f"  csv:        {csv_path}")


def cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", maxsplit=1)[0])
    parser.add_argument(
        "--file",
        type=Path,
        default=None,
        help="WAV file to scan (defaults to first in ../audio/)",
    )
    parser.add_argument(
        "--target", default="localhost:50051", help="gRPC server host:port"
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=f"Parallel slice count (default: {DEFAULT_CONCURRENCY})",
    )
    frame_group = parser.add_mutually_exclusive_group()
    frame_group.add_argument(
        "--chunk-ms",
        type=int,
        default=None,
        help=f"AudioFrame size in ms (default: {DEFAULT_CHUNK_MS} — bigger than phone_call.py's 20ms since we don't pace)",
    )
    frame_group.add_argument(
        "--frame-seconds",
        type=float,
        default=None,
        help="AudioFrame size in seconds (natural-unit alternative to --chunk-ms). "
        "Use with --concurrency to size per-slice payloads for max batch fill "
        "on the server (see docstring: 90 streams × 40 s = the ideal shape).",
    )
    parser.add_argument(
        "--extreme",
        action="store_true",
        help=f"Preset for maximum-throughput scanning: "
        f"--concurrency {EXTREME_CONCURRENCY} + --frame-seconds {EXTREME_FRAME_SECONDS:g}. "
        f"Saturates the dfs GPU on batched inference. Requires a long file (≥60min) "
        f"and headroom on the target dfs. Explicit --concurrency / --frame-seconds "
        f"still override the preset — set --extreme + tune the two knobs to sweep "
        f"the (concurrency, frame_seconds) grid for your specific GPU.",
    )
    parser.add_argument(
        "--csv", default=None, help="Write per-chunk results to this path (overwrites)"
    )
    parser.add_argument(
        "--scenario-id",
        default=None,
        help="Server-side simulator scenario (sent as x-scenario-id metadata).",
    )
    parser.add_argument(
        "--vad-mode",
        choices=["auto", "on", "off"],
        default="auto",
        help="Per-session VAD gate override via DetectionConfig.vad_mode "
        "(aurigin-protos ≥ 0.4). 'auto' = follow server env default "
        "(VAD_ENABLED); 'on' / 'off' force per-session. Set 'off' to "
        "benchmark GPU-only inference floor on speech-heavy audio "
        "where VAD's CPU cost outweighs its silence-skip savings.",
    )
    parser.add_argument(
        "--vad-threshold",
        type=float,
        default=None,
        help="Per-session VAD silence-skip threshold percent [0-100] via "
        "DetectionConfig.vad_silence_threshold_pct (aurigin-protos ≥ 0.4). "
        "Unset = follow server env default (SILENCE_THRESHOLD_PCT, "
        "typically 80). Lowering skips more windows (faster, risks "
        "missing short speech); raising analyses more content (slower, "
        "no accuracy loss). Only meaningful when --vad-mode is auto or on.",
    )
    parser.add_argument(
        "--tail-strategy",
        choices=["auto", "drop", "extend", "recompute"],
        default="auto",
        help="Per-session tail-window strategy via DetectionConfig.tail_strategy "
        "(aurigin-protos ≥ 0.5). 'auto' = follow server env default "
        "(TAIL_STRATEGY, typically drop). 'drop' = silently skip sub-min "
        "residuals (accuracy-first, right for file scans — kills the "
        "spurious tail-window artifact where a 1-2s clip gets scored by "
        "a 5-s-trained model). 'extend' = fold residual into prior window "
        "(coverage-first, right for live calls). 'recompute' = slide "
        "last window back to end-of-stream for HTTP-parity (+1 inference).",
    )
    parser.add_argument(
        "--min-analysis-duration",
        type=float,
        default=None,
        help="Per-session minimum-residual-duration override in seconds via "
        "DetectionConfig.min_analysis_duration_s (aurigin-protos ≥ 0.5). "
        "Unset = follow server env default (MIN_ANALYSIS_DURATION_S, typically "
        "1.0). Residuals shorter than this are handled per --tail-strategy. "
        "Cannot go below ~1.0 without breaking wav2vec2's feature extractor.",
    )
    args = parser.parse_args()

    # Resolve concurrency: explicit arg wins over --extreme preset, which
    # wins over the conservative default. Same shape for the frame size.
    # This lets users --extreme + --concurrency 128 to try tuning past
    # the recommended ceiling on a beefier GPU.
    if args.concurrency != DEFAULT_CONCURRENCY:
        concurrency = args.concurrency  # explicit override
    elif args.extreme:
        concurrency = EXTREME_CONCURRENCY
    else:
        concurrency = DEFAULT_CONCURRENCY

    if args.frame_seconds is not None:
        chunk_ms = int(args.frame_seconds * 1000)
    elif args.chunk_ms is not None:
        chunk_ms = args.chunk_ms
    elif args.extreme:
        chunk_ms = int(EXTREME_FRAME_SECONDS * 1000)
    else:
        chunk_ms = DEFAULT_CHUNK_MS

    if args.extreme:
        print(
            f"⚡ EXTREME mode: concurrency={concurrency}, frame_ms={chunk_ms} "
            f"({chunk_ms / 1000:.1f}s/frame). "
            f"Expect heavy dfs GPU load; monitor `nvidia-smi` on the target.",
        )

    audio_path = _resolve_audio(args.file)
    asyncio.run(
        _run_scan(
            args.target,
            audio_path,
            concurrency,
            chunk_ms,
            args.csv,
            args.scenario_id,
            vad_mode=args.vad_mode,
            vad_threshold_pct=args.vad_threshold,
            tail_strategy=args.tail_strategy,
            min_analysis_duration_s=args.min_analysis_duration,
        ),
    )


if __name__ == "__main__":
    cli()
