"""Per-session scenario runner.

Drives a single DetectDeepfake bidi RPC from a Scenario:
- emits CreateSessionResponse with a generated session_id,
- schedules curve samples + explicit events along the scenario's timeline,
- applies network latency/jitter on each emission,
- aborts the stream with the configured gRPC status if grpc.terminate_at_ms is set,
- drains the client's audio chunks in the background (content ignored),
- emits FinalResult on stream close (with the last computed score and the
  scenario's chosen label).
"""

from __future__ import annotations

import asyncio
import os
import random
import sys
import uuid
from dataclasses import dataclass
from typing import Any

import grpc

from aurigin.deepfake_detection.v1 import deepfake_detection_pb2 as pb

from . import curves
from .loader import Scenario

_STATUS_CODE_BY_NAME = {code.name: code for code in grpc.StatusCode}

# Opt-in per-emission logging. Always-on logs (start / fault / end) are below
# in run_session — this flag only gates the (potentially noisy) one-line-per-
# AnalysisResult output.
_LOG_ANALYSES = os.environ.get("SIM_LOG_ANALYSES", "").lower() in ("1", "true", "yes")


def _log(session_id: str, message: str) -> None:
    """Prefix every server-side log line with the session id for grep-ability.

    Writes to sys.stderr (not stdout) and bypasses Python's print() so
    process-manager wrappers (uv, hatch entry-point shims, just) can't
    silently buffer the destination. The startup line in server.py works
    the same way for the same reason.
    """
    sys.stderr.write(f"[{session_id}] {message}\n")
    sys.stderr.flush()

# Sentinel marking the end of the scheduled timeline. Consumed by the
# main generator loop to know it can stop pulling from the queue.
_TIMELINE_DONE = object()

# Bytes per sample for the AudioBuffer wire formats the deepfake-service
# decoder accepts. Used by _drain_audio's defensive duration-from-bytes
# fallback when the client doesn't populate duration_ns.
_BYTES_PER_SAMPLE = {"S16LE": 2, "F32LE": 4}


@dataclass
class _SessionState:
    session_id: str
    started_at: float                  # asyncio loop time
    accumulated_audio_ms: int = 0
    last_score: float = 0.0
    last_label: str = "bonafide"
    analysis_count: int = 0

    def now_ms(self, loop: asyncio.AbstractEventLoop) -> int:
        return int((loop.time() - self.started_at) * 1000)


def _make_session_id() -> str:
    return f"sim-{uuid.uuid4().hex[:8]}"


def _make_analysis_result(
    state: _SessionState,
    t_ms: int,
    score: float,
    label: str,
    confidence: float,
    duration_ms: int,
) -> pb.DetectDeepfakeResponse:
    state.last_score = score
    state.last_label = label
    state.analysis_count += 1
    return pb.DetectDeepfakeResponse(
        analysis_result=pb.AnalysisResult(
            audio_offset_ms=t_ms,
            duration_ms=duration_ms,
            score=score,
            label=label,
            confidence=confidence,
        ),
    )


async def _apply_network(network, rng: random.Random) -> None:
    delay_ms = network.base_latency_ms
    if network.jitter_ms:
        delay_ms += rng.randint(-network.jitter_ms, network.jitter_ms)
    if delay_ms > 0:
        await asyncio.sleep(delay_ms / 1000)


async def _drain_audio(request_iterator, state: _SessionState) -> None:
    """Consume audio chunks so the client can finish writing. Content ignored
    by the simulator — the scenario drives output, not the audio bytes."""
    logged_format = False
    async for msg in request_iterator:
        if msg.HasField("audio"):
            buf = msg.audio
            if not logged_format:
                _log(
                    state.session_id,
                    f"audio | format={buf.format or '?'} | rate={buf.rate} | channels={buf.channels}",
                )
                logged_format = True
            if buf.duration_ns > 0:
                state.accumulated_audio_ms += int(buf.duration_ns // 1_000_000)
            elif buf.rate and buf.channels:
                bps = _BYTES_PER_SAMPLE.get(buf.format, 2)
                state.accumulated_audio_ms += int(
                    len(buf.buffer) / bps / buf.channels / buf.rate * 1000
                )


async def _emit_timeline(
    scenario: Scenario,
    state: _SessionState,
    rng: random.Random,
    loop: asyncio.AbstractEventLoop,
    out_queue: asyncio.Queue,
) -> None:
    """Walk the scheduled timeline in order, sleeping between emissions."""
    timeline = _build_timeline(scenario)
    for at_ms, item in timeline:
        sleep_for = (at_ms / 1000.0) - (loop.time() - state.started_at)
        if sleep_for > 0:
            await asyncio.sleep(sleep_for)
        await _apply_network(scenario.network, rng)
        response = _materialise(item, scenario, state, rng, at_ms)
        if response is not None:
            await out_queue.put(response)
    await out_queue.put(_TIMELINE_DONE)


def _build_timeline(scenario: Scenario) -> list[tuple[int, dict[str, Any]]]:
    """Combine emissions (from backend_simulation or curve) + explicit events.

    Two scheduling modes:
      - **backend_simulation** (declarative): emissions are computed from
        (stream.duration_ms, analysis_interval_ms, min_chunk_duration_ms,
        tail_strategy). Mirrors how deepfake-service actually windows a
        finite-length audio stream. Curve still consulted for score values.
      - **curve.emit_every_ms** (scripted): emissions on a fixed wallclock
        grid driven by the curve. Pre-existing behavior; unchanged.

    Events are always overlaid on top.
    """
    items: list[tuple[int, dict[str, Any]]] = []

    bs = scenario.backend_simulation
    if bs is not None:
        items.extend(_emissions_from_backend_simulation(scenario))
    else:
        curve = scenario.confidence_curve
        if curve is not None:
            every = curve.get("emit_every_ms", 1000)
            for t in range(every, scenario.stream.duration_ms + 1, every):
                items.append((t, {"kind": "curve_sample", "at_ms": t}))

    for ev in scenario.events:
        items.append((ev.at_ms, {"kind": "event", "event": ev}))

    items.sort(key=lambda x: x[0])
    return items


def _emissions_from_backend_simulation(
    scenario: Scenario,
) -> list[tuple[int, dict[str, Any]]]:
    """Mirror dfs's window-emission rules over a finite stream.

    Walks the [0, duration_ms] interval in `analysis_interval_ms` steps and
    emits one `computed_emission` per main window. Residual after the last
    main window is handled per `tail_strategy`:

      - drop: residual < min_chunk_duration_ms → silently skipped
              residual ≥ min_chunk_duration_ms → emitted as a short tail
              window with duration_ms = residual
      - extend: residual < min_chunk_duration_ms → folded into the prior
                window (last main emission shifts to t=duration_ms with
                duration_ms = analysis_interval_ms + residual)
                residual ≥ min_chunk_duration_ms → same as drop's else branch
    """
    bs = scenario.backend_simulation
    assert bs is not None
    duration_ms = scenario.stream.duration_ms
    interval = bs.analysis_interval_ms
    min_chunk = bs.min_chunk_duration_ms
    silent_set = set(bs.silent_windows)
    n_main = duration_ms // interval
    residual = duration_ms - n_main * interval
    fold_residual = (
        residual > 0
        and residual < min_chunk
        and bs.tail_strategy == "extend"
        and n_main > 0
    )

    items: list[tuple[int, dict[str, Any]]] = []
    for i in range(1, n_main + 1):
        at_ms = i * interval
        window_dur = interval
        # Last main window absorbs the residual when extending.
        if i == n_main and fold_residual:
            at_ms = duration_ms
            window_dur = interval + residual
        items.append((at_ms, {
            "kind": "computed_emission",
            "at_ms": at_ms,
            "duration_ms": window_dur,
            "is_silent": i in silent_set,
            "silence_confidence": bs.silence_confidence,
        }))

    # Standalone tail emission (only when not folded into the prior window
    # and large enough to clear the min_chunk floor).
    if residual > 0 and not fold_residual and residual >= min_chunk:
        tail_idx = n_main + 1
        items.append((duration_ms, {
            "kind": "computed_emission",
            "at_ms": duration_ms,
            "duration_ms": residual,
            "is_silent": tail_idx in silent_set,
            "silence_confidence": bs.silence_confidence,
        }))

    return items


def _materialise(
    item: dict[str, Any],
    scenario: Scenario,
    state: _SessionState,
    rng: random.Random,
    at_ms: int,
) -> pb.DetectDeepfakeResponse | None:
    # AnalysisResult.duration_ms resolution order, finest grained wins:
    #   per-event payload.duration_ms > curve.analysis_window_ms > stream.chunk_interval_ms
    # Lets one scenario emit windows of varying lengths (e.g. tail_extended_
    # full_coverage where the last window is longer than the rest).
    curve = scenario.confidence_curve or {}
    curve_window_ms = curve.get("analysis_window_ms", scenario.stream.chunk_interval_ms)

    if item["kind"] == "computed_emission":
        # backend_simulation-driven emission. Silent windows skip the curve
        # entirely and emit a sentinel; otherwise curve (if any) computes
        # the score the same way curve_sample does.
        if item["is_silent"]:
            return _make_analysis_result(
                state,
                t_ms=at_ms,
                score=0.0,
                label="silence",
                confidence=float(item["silence_confidence"]),
                duration_ms=int(item["duration_ms"]),
            )
        if curve:
            score = curves.evaluate(curve, rng, at_ms)
            label = curves.label_for(curve, score)
        else:
            score = 0.0
            label = "bonafide"
        confidence = round(rng.uniform(0.85, 0.99), 3)
        return _make_analysis_result(
            state,
            t_ms=at_ms,
            score=score,
            label=label,
            confidence=confidence,
            duration_ms=int(item["duration_ms"]),
        )

    if item["kind"] == "curve_sample":
        score = curves.evaluate(curve, rng, at_ms)
        label = curves.label_for(curve, score)
        confidence = round(rng.uniform(0.85, 0.99), 3)
        return _make_analysis_result(
            state,
            t_ms=at_ms,
            score=score,
            label=label,
            confidence=confidence,
            duration_ms=curve_window_ms,
        )

    ev = item["event"]
    if ev.type in ("STREAM_STARTED", "STREAM_ENDED"):
        # Lifecycle markers are implicit on the wire (session/final messages).
        return None
    if ev.type == "ERROR":
        # Event-level errors are surfaced as a sentinel AnalysisResult with
        # label='error' and score 0; clients should treat label='error' as
        # non-actionable. (Future: dedicated proto message.)
        return _make_analysis_result(
            state,
            t_ms=at_ms,
            score=0.0,
            label="error",
            confidence=0.0,
            duration_ms=ev.payload.get("duration_ms", curve_window_ms),
        )
    # CONFIDENCE_UPDATE or FAKE_DETECTED — both map to AnalysisResult.
    payload = ev.payload
    score = float(payload.get("fake_probability", state.last_score))
    label = payload.get("label", curves.label_for(curve, score))
    confidence = float(payload.get("confidence", round(rng.uniform(0.85, 0.99), 3)))
    return _make_analysis_result(
        state,
        t_ms=at_ms,
        score=score,
        label=label,
        confidence=confidence,
        duration_ms=int(payload.get("duration_ms", curve_window_ms)),
    )


async def run_session(scenario: Scenario, request_iterator, context):
    """Async generator yielding DetectDeepfakeResponse for one bidi RPC."""
    loop = asyncio.get_running_loop()
    rng = random.Random(scenario.random_seed) if scenario.random_seed is not None else random.Random()

    # Set initial metadata if configured.
    if scenario.grpc.initial_metadata:
        await context.send_initial_metadata(list(scenario.grpc.initial_metadata.items()))

    # 1. Require CreateSessionRequest first.
    first = await request_iterator.__anext__()
    if not first.HasField("create_session_request"):
        await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Expected CreateSessionRequest first")
        return

    state = _SessionState(session_id=_make_session_id(), started_at=loop.time())
    yield pb.DetectDeepfakeResponse(
        create_session_response=pb.CreateSessionResponse(session_id=state.session_id),
    )
    _log(
        state.session_id,
        f"start | scenario={scenario.id} | duration_target={scenario.stream.duration_ms}ms"
        + (f" | seed={scenario.random_seed}" if scenario.random_seed is not None else ""),
    )  # verb width 5 chars — kept in sync with 'end  ' and 'fault' below

    # 2. Background tasks: drain audio, emit timeline, optional fault.
    out_queue: asyncio.Queue = asyncio.Queue()
    audio_task = asyncio.create_task(_drain_audio(request_iterator, state))
    emit_task = asyncio.create_task(_emit_timeline(scenario, state, rng, loop, out_queue))

    fault_task: asyncio.Task | None = None
    if scenario.grpc.terminate_at_ms is not None:
        fault_task = asyncio.create_task(asyncio.sleep(scenario.grpc.terminate_at_ms / 1000))

    try:
        # 3. Pump queue → wire until the timeline ends, the client closes
        #    its write side, or a fault fires.
        timeline_finished = False
        client_finished = False
        while not (timeline_finished and client_finished):
            queue_get = asyncio.create_task(out_queue.get())
            waiters = [queue_get]
            if not client_finished:
                waiters.append(audio_task)
            if fault_task:
                waiters.append(fault_task)

            done, _pending = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)

            if fault_task and fault_task in done:
                queue_get.cancel()
                code_name = scenario.grpc.status_code or "INTERNAL"
                code = _STATUS_CODE_BY_NAME.get(code_name, grpc.StatusCode.INTERNAL)
                message = scenario.grpc.status_message or "simulated fault"
                _log(
                    state.session_id,
                    f"fault | at={state.now_ms(loop)}ms | code={code_name} | message={message!r} "
                    f"| analyses_so_far={state.analysis_count}",
                )  # verb width 5 chars — kept in sync with 'start' and 'end  '
                await context.abort(code, message)
                return

            if audio_task in done and not client_finished:
                # Client closed the write side. Stop emitting and finalise.
                client_finished = True
                queue_get.cancel()
                emit_task.cancel()
                break

            item = await queue_get
            if item is _TIMELINE_DONE:
                timeline_finished = True
                continue
            if _LOG_ANALYSES and item.HasField("analysis_result"):
                r = item.analysis_result
                _log(
                    state.session_id,
                    f"analysis @ {r.audio_offset_ms / 1000:6.2f}s | score={r.score:.3f} "
                    f"| label={r.label} | confidence={r.confidence:.2f}",
                )
            yield item

        # 4. If the timeline finished first, still wait for the client to close.
        if not client_finished:
            await audio_task

        if scenario.grpc.trailing_metadata:
            context.set_trailing_metadata(list(scenario.grpc.trailing_metadata.items()))

        total_audio_ms = max(state.accumulated_audio_ms, scenario.stream.duration_ms)
        overall_label = state.last_label if state.analysis_count > 0 else "unknown"
        _log(
            state.session_id,
            f"end   | total={total_audio_ms}ms | analyses={state.analysis_count} "
            f"| score={state.last_score:.3f} | label={overall_label}",
        )  # 'end  ' padded to verb width 5 — matches 'start' and 'fault'
        yield pb.DetectDeepfakeResponse(
            final_result=pb.FinalResult(
                total_audio_ms=total_audio_ms,
                overall_score=state.last_score,
                overall_label=overall_label,
                analysis_count=state.analysis_count,
            ),
        )
    finally:
        for t in (audio_task, emit_task, fault_task):
            if t is not None and not t.done():
                t.cancel()
