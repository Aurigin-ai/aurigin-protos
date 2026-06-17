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
import random
import time
import uuid
from dataclasses import dataclass
from typing import Any

import grpc

from aurigin.deepfake_detection.v1 import deepfake_detection_pb2 as pb

from . import curves
from .loader import Scenario

_STATUS_CODE_BY_NAME = {code.name: code for code in grpc.StatusCode}

# Sentinel marking the end of the scheduled timeline. Consumed by the
# main generator loop to know it can stop pulling from the queue.
_TIMELINE_DONE = object()


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
    async for msg in request_iterator:
        if msg.HasField("audio"):
            buf = msg.audio
            if buf.duration_ns > 0:
                state.accumulated_audio_ms += int(buf.duration_ns // 1_000_000)
            elif buf.rate and buf.channels:
                state.accumulated_audio_ms += int(
                    len(buf.buffer) / 2 / buf.channels / buf.rate * 1000
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
    """Combine curve sample points + explicit events into one ordered list."""
    items: list[tuple[int, dict[str, Any]]] = []

    curve = scenario.confidence_curve
    if curve is not None:
        every = curve.get("emit_every_ms", 1000)
        for t in range(every, scenario.stream.duration_ms + 1, every):
            items.append((t, {"kind": "curve_sample", "at_ms": t}))

    for ev in scenario.events:
        items.append((ev.at_ms, {"kind": "event", "event": ev}))

    items.sort(key=lambda x: x[0])
    return items


def _materialise(
    item: dict[str, Any],
    scenario: Scenario,
    state: _SessionState,
    rng: random.Random,
    at_ms: int,
) -> pb.DetectDeepfakeResponse | None:
    if item["kind"] == "curve_sample":
        curve = scenario.confidence_curve
        score = curves.evaluate(curve, rng, at_ms)
        label = curves.label_for(curve, score)
        confidence = round(rng.uniform(0.85, 0.99), 3)
        return _make_analysis_result(
            state,
            t_ms=at_ms,
            score=score,
            label=label,
            confidence=confidence,
            duration_ms=scenario.stream.chunk_interval_ms,
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
            duration_ms=scenario.stream.chunk_interval_ms,
        )
    # CONFIDENCE_UPDATE or FAKE_DETECTED — both map to AnalysisResult.
    payload = ev.payload
    score = float(payload.get("fake_probability", state.last_score))
    label = payload.get("label", curves.label_for(scenario.confidence_curve or {}, score))
    confidence = float(payload.get("confidence", round(rng.uniform(0.85, 0.99), 3)))
    return _make_analysis_result(
        state,
        t_ms=at_ms,
        score=score,
        label=label,
        confidence=confidence,
        duration_ms=scenario.stream.chunk_interval_ms,
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
                code = _STATUS_CODE_BY_NAME.get(scenario.grpc.status_code or "INTERNAL", grpc.StatusCode.INTERNAL)
                await context.abort(code, scenario.grpc.status_message or "simulated fault")
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
            yield item

        # 4. If the timeline finished first, still wait for the client to close.
        if not client_finished:
            await audio_task

        if scenario.grpc.trailing_metadata:
            context.set_trailing_metadata(list(scenario.grpc.trailing_metadata.items()))

        yield pb.DetectDeepfakeResponse(
            final_result=pb.FinalResult(
                total_audio_ms=max(state.accumulated_audio_ms, scenario.stream.duration_ms),
                overall_score=state.last_score,
                overall_label=state.last_label if state.analysis_count > 0 else "unknown",
                analysis_count=state.analysis_count,
            ),
        )
    finally:
        for t in (audio_task, emit_task, fault_task):
            if t is not None and not t.done():
                t.cancel()
