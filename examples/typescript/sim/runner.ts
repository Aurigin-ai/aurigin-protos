// Per-session scenario runner.
//
// Drives a single DetectDeepfake bidi RPC from a Scenario:
// - emits CreateSessionResponse with a generated session_id,
// - schedules curve samples + explicit events along the scenario's timeline,
// - applies network latency/jitter on each emission,
// - aborts the stream with the configured gRPC status if grpc.terminate_at_ms is set,
// - drains the client's audio chunks (content ignored),
// - emits FinalResult on stream close (with the last computed score and the
//   scenario's chosen label).
//
// Mirrors examples/python/sim/runner.py. Native asyncio doesn't have a direct
// equivalent in @grpc/grpc-js (which is event/callback-based), so the queue
// pump is implemented with setTimeout for scheduled emissions plus a single
// async tail to wait for client end / fault.

import { randomBytes } from "node:crypto";
import { Metadata, status as GrpcStatus, type ServerDuplexStream } from "@grpc/grpc-js";

import type {
  DetectDeepfakeRequest,
  DetectDeepfakeResponse,
} from "@aurigin/protos/aurigin/deepfake_detection/v1/deepfake_detection";

import * as curves from "./curves.js";
import type { Scenario, ScenarioEvent } from "./loader.js";

type Call = ServerDuplexStream<DetectDeepfakeRequest, DetectDeepfakeResponse>;
type Rng = curves.Rng;

// Opt-in per-emission logging. Always-on logs (start / fault / end) are
// emitted below in runSession — this flag only gates the (potentially noisy)
// one-line-per-AnalysisResult output.
const LOG_ANALYSES = ["1", "true", "yes"].includes(
  (process.env.SIM_LOG_ANALYSES ?? "").toLowerCase(),
);

function log(sessionId: string, message: string): void {
  // Prefix every server-side log line with the session id for grep-ability.
  // Use console.error (stderr) for parity with the Python side — process
  // supervisors are less likely to buffer stderr than stdout.
  console.error(`[${sessionId}] ${message}`);
}

const STATUS_CODE_BY_NAME: Record<string, number> = {
  OK: GrpcStatus.OK,
  CANCELLED: GrpcStatus.CANCELLED,
  UNKNOWN: GrpcStatus.UNKNOWN,
  INVALID_ARGUMENT: GrpcStatus.INVALID_ARGUMENT,
  DEADLINE_EXCEEDED: GrpcStatus.DEADLINE_EXCEEDED,
  NOT_FOUND: GrpcStatus.NOT_FOUND,
  ALREADY_EXISTS: GrpcStatus.ALREADY_EXISTS,
  PERMISSION_DENIED: GrpcStatus.PERMISSION_DENIED,
  RESOURCE_EXHAUSTED: GrpcStatus.RESOURCE_EXHAUSTED,
  FAILED_PRECONDITION: GrpcStatus.FAILED_PRECONDITION,
  ABORTED: GrpcStatus.ABORTED,
  OUT_OF_RANGE: GrpcStatus.OUT_OF_RANGE,
  UNIMPLEMENTED: GrpcStatus.UNIMPLEMENTED,
  INTERNAL: GrpcStatus.INTERNAL,
  UNAVAILABLE: GrpcStatus.UNAVAILABLE,
  DATA_LOSS: GrpcStatus.DATA_LOSS,
  UNAUTHENTICATED: GrpcStatus.UNAUTHENTICATED,
};

interface SessionState {
  sessionId: string;
  startedAt: number; // ms since process start (performance.now)
  accumulatedAudioMs: number;
  lastScore: number;
  lastLabel: string;
  analysisCount: number;
}

interface TimelineItem {
  kind: "curve_sample" | "event" | "computed_emission";
  atMs: number;
  event?: ScenarioEvent;
  // Set for kind=computed_emission (backend_simulation-driven).
  durationMs?: number;
  isSilent?: boolean;
  silenceConfidence?: number;
  // offsetMs usually equals atMs (fire-wallclock-time == audio offset under
  // the existing simulator convention), but tail_strategy='recompute'
  // slides it back so the offset reports where the slid window starts
  // rather than where it ended. Falls back to atMs when absent.
  offsetMs?: number;
}

// Bytes per sample for the AudioBuffer wire formats the deepfake-service
// decoder accepts. Used by the audio drainer's fallback duration-from-bytes
// calc when the client doesn't populate duration_ns.
const BYTES_PER_SAMPLE: Record<string, number> = { S16LE: 2, F32LE: 4 };

function makeSessionId(): string {
  // "sim-" prefix identifies this session as simulator-generated, matching
  // deepfake-service's "pre-" prefix (= prediction). Cross-cutting log
  // searches can tell at a glance whether a session id came from the real
  // backend or the scenario-driven simulator. 16 random bytes → 32 hex
  // chars (mirrors uuid4 hex; we don't need RFC 4122 compliance here).
  return `sim-${randomBytes(16).toString("hex")}`;
}

function mulberry32(seed: number): Rng {
  let a = seed >>> 0;
  return function (): number {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function applyNetwork(net: Scenario["network"], rng: Rng): Promise<void> {
  let delayMs = net.baseLatencyMs;
  if (net.jitterMs) {
    // Match Python's randint(-jitter, jitter) — integer in [-j, +j].
    const j = net.jitterMs;
    delayMs += Math.floor(rng() * (2 * j + 1)) - j;
  }
  if (delayMs > 0) {
    await sleep(delayMs);
  }
}

function buildTimeline(scenario: Scenario): TimelineItem[] {
  const items: TimelineItem[] = [];

  if (scenario.backendSimulation != null) {
    items.push(...emissionsFromBackendSimulation(scenario));
  } else {
    const curve = scenario.confidenceCurve;
    if (curve != null) {
      const every = (curve.emit_every_ms ?? 1000) as number;
      for (let t = every; t <= scenario.stream.durationMs; t += every) {
        items.push({ kind: "curve_sample", atMs: t });
      }
    }
  }

  for (const ev of scenario.events) {
    items.push({ kind: "event", atMs: ev.atMs, event: ev });
  }

  items.sort((a, b) => a.atMs - b.atMs);
  return items;
}

// Mirror dfs's window-emission rules over a finite stream. Walks
// [0, duration_ms] in `analysis_interval_ms` steps and emits one
// computed_emission per main window. Residual after the last main window
// follows `tail_strategy`:
//   - drop: residual < min_analysis_duration_ms → silently skipped
//           residual ≥ min_analysis_duration_ms → short tail window
//   - extend: residual < min_analysis_duration_ms → folded into prior window
//             (last emission shifts to t=duration_ms, longer duration)
//   - recompute: residual < min_analysis_duration_ms → last main emission
//                slides back to end at end-of-stream (offset shifts to
//                duration_ms - analysis_interval_ms, duration stays
//                analysis_interval_ms). Mirrors backend-app HTTP
//                /predict chunk_audio byte-for-byte.
//   - extend|recompute with residual ≥ min_analysis_duration_ms → same as
//     drop's else branch (standalone short tail).
function emissionsFromBackendSimulation(scenario: Scenario): TimelineItem[] {
  const bs = scenario.backendSimulation!;
  const durationMs = scenario.stream.durationMs;
  const interval = bs.analysisIntervalMs;
  const minAnalysis = bs.minAnalysisDurationMs;
  const silentSet = new Set(bs.silentWindows);
  const nMain = Math.floor(durationMs / interval);
  const residual = durationMs - nMain * interval;
  const canAbsorbTail =
    residual > 0 &&
    residual < minAnalysis &&
    nMain > 0 &&
    (bs.tailStrategy === "extend" || bs.tailStrategy === "recompute");

  const items: TimelineItem[] = [];
  for (let i = 1; i <= nMain; i++) {
    let atMs = i * interval;
    let windowDur = interval;
    let offsetMs = atMs;
    if (i === nMain && canAbsorbTail) {
      if (bs.tailStrategy === "extend") {
        atMs = durationMs;
        windowDur = interval + residual;
        offsetMs = atMs;
      } else {
        // recompute — fire at end-of-stream, but report the audio offset
        // as where the slid-back window starts (durationMs - interval).
        atMs = durationMs;
        windowDur = interval;
        offsetMs = durationMs - interval;
      }
    }
    items.push({
      kind: "computed_emission",
      atMs,
      durationMs: windowDur,
      isSilent: silentSet.has(i),
      silenceConfidence: bs.silenceConfidence,
      offsetMs,
    });
  }

  if (residual > 0 && !canAbsorbTail && residual >= minAnalysis) {
    const tailIdx = nMain + 1;
    items.push({
      kind: "computed_emission",
      atMs: durationMs,
      durationMs: residual,
      isSilent: silentSet.has(tailIdx),
      silenceConfidence: bs.silenceConfidence,
      offsetMs: durationMs,
    });
  }

  return items;
}

function makeAnalysisResult(
  state: SessionState,
  tMs: number,
  score: number,
  label: string,
  confidence: number,
  durationMs: number,
): DetectDeepfakeResponse {
  state.lastScore = score;
  state.lastLabel = label;
  state.analysisCount += 1;
  return {
    analysisResult: {
      audioOffsetMs: BigInt(tMs),
      durationMs: BigInt(durationMs),
      score,
      label,
      confidence,
    },
  } as DetectDeepfakeResponse;
}

function materialise(
  item: TimelineItem,
  scenario: Scenario,
  state: SessionState,
  rng: Rng,
  atMs: number,
): DetectDeepfakeResponse | null {
  // AnalysisResult.duration_ms resolution order, finest grained wins:
  //   per-event payload.duration_ms > curve.analysis_window_ms > stream.chunk_interval_ms
  // Lets one scenario emit windows of varying lengths (e.g. tail_extended_
  // full_coverage where the last window is longer than the rest).
  // Mirrors examples/python/sim/runner.py._materialise.
  const curve = scenario.confidenceCurve ?? {};
  const curveWindowMs = ((curve as any).analysis_window_ms as number | undefined) ?? scenario.stream.chunkIntervalMs;

  if (item.kind === "computed_emission") {
    // backend_simulation-driven emission. Silent windows skip the curve and
    // emit a sentinel; otherwise the curve (if any) computes the score the
    // same way curve_sample does.
    //
    // offsetMs is the wire audio_offset_ms. Usually equals atMs (fire-time ==
    // audio-offset under the existing simulator convention), but recompute
    // slides it back so the offset reports where the slid window starts.
    const emitOffset = item.offsetMs ?? atMs;
    const dur = item.durationMs ?? curveWindowMs;
    if (item.isSilent) {
      return makeAnalysisResult(
        state, emitOffset, 0, "silence", item.silenceConfidence ?? 0.95, dur,
      );
    }
    let score = 0;
    let label = "bonafide";
    if (scenario.confidenceCurve) {
      score = curves.evaluate(scenario.confidenceCurve, rng, atMs);
      label = curves.labelFor(scenario.confidenceCurve, score);
    }
    const confidence = Math.round((0.85 + rng() * 0.14) * 1000) / 1000;
    return makeAnalysisResult(state, emitOffset, score, label, confidence, dur);
  }

  if (item.kind === "curve_sample") {
    const c = scenario.confidenceCurve!;
    const score = curves.evaluate(c, rng, atMs);
    const label = curves.labelFor(c, score);
    const confidence = Math.round((0.85 + rng() * 0.14) * 1000) / 1000;
    return makeAnalysisResult(state, atMs, score, label, confidence, curveWindowMs);
  }

  const ev = item.event!;
  if (ev.type === "STREAM_STARTED" || ev.type === "STREAM_ENDED") {
    // Lifecycle markers are implicit on the wire (session/final messages).
    return null;
  }
  if (ev.type === "ERROR") {
    // Event-level errors are surfaced as a sentinel AnalysisResult with
    // label='error' and score 0; clients should treat label='error' as
    // non-actionable. (Future: dedicated proto message.)
    const dur = (ev.payload.duration_ms as number | undefined) ?? curveWindowMs;
    return makeAnalysisResult(state, atMs, 0, "error", 0, dur);
  }
  // CONFIDENCE_UPDATE or FAKE_DETECTED — both map to AnalysisResult.
  const payload = ev.payload;
  const score = (payload.fake_probability ?? state.lastScore) as number;
  const label = (payload.label ?? curves.labelFor(scenario.confidenceCurve ?? {}, score)) as string;
  const confidence = (payload.confidence ?? Math.round((0.85 + rng() * 0.14) * 1000) / 1000) as number;
  const dur = (payload.duration_ms as number | undefined) ?? curveWindowMs;
  return makeAnalysisResult(state, atMs, score, label, confidence, dur);
}

function audioChunkMs(audio: {
  durationNs?: bigint;
  rate?: number;
  channels?: number;
  buffer?: Uint8Array;
  format?: string;
}): number {
  const durationNs = audio.durationNs ?? 0n;
  if (durationNs > 0n) {
    return Number(durationNs / 1_000_000n);
  }
  const rate = audio.rate ?? 0;
  const channels = audio.channels ?? 0;
  const buf = audio.buffer;
  if (rate && channels && buf) {
    const bps = BYTES_PER_SAMPLE[audio.format ?? ""] ?? 2;
    return Math.round((buf.length / bps / channels / rate) * 1000);
  }
  return 0;
}

export async function runSession(scenario: Scenario, call: Call): Promise<void> {
  const rng = scenario.randomSeed != null ? mulberry32(scenario.randomSeed) : Math.random;

  // Initial metadata.
  if (Object.keys(scenario.grpc.initialMetadata).length > 0) {
    const md = new Metadata();
    for (const [k, v] of Object.entries(scenario.grpc.initialMetadata)) md.set(k, v);
    call.sendMetadata(md);
  }

  // 1. Require CreateSessionRequest first. Eagerly attach the data listener
  //    so we don't lose subsequent audio chunks while we await the first one.
  const state: SessionState = {
    sessionId: makeSessionId(),
    startedAt: performance.now(),
    accumulatedAudioMs: 0,
    lastScore: 0,
    lastLabel: "bonafide",
    analysisCount: 0,
  };

  let firstResolved = false;
  let firstResolve: (req: DetectDeepfakeRequest) => void;
  let firstReject: (err: unknown) => void;
  const firstMessage = new Promise<DetectDeepfakeRequest>((res, rej) => {
    firstResolve = res;
    firstReject = rej;
  });

  let clientEnded = false;
  let clientEndResolve: () => void;
  const clientEndedPromise = new Promise<void>((res) => {
    clientEndResolve = res;
  });

  let loggedFormat = false;
  call.on("data", (msg: DetectDeepfakeRequest) => {
    if (!firstResolved) {
      firstResolved = true;
      firstResolve(msg);
      return;
    }
    if (msg.audio) {
      if (!loggedFormat) {
        const a = msg.audio as any;
        log(
          state.sessionId,
          `audio | format=${a.format || "?"} | rate=${a.rate ?? 0} | channels=${a.channels ?? 0}`,
        );
        loggedFormat = true;
      }
      state.accumulatedAudioMs += audioChunkMs(msg.audio as any);
    }
  });

  call.on("end", () => {
    if (!firstResolved) {
      firstResolved = true;
      firstReject(new Error("Stream ended before CreateSessionRequest"));
    }
    clientEnded = true;
    clientEndResolve();
  });

  call.on("error", () => {
    // The transport surfaces errors via 'error'; treat as a client-end so we
    // tear down cleanly. The fault-injection path emits its own error after.
    clientEnded = true;
    clientEndResolve();
  });

  let first: DetectDeepfakeRequest;
  try {
    first = await firstMessage;
  } catch {
    return;
  }
  if (!first.createSessionRequest) {
    call.emit("error", {
      code: GrpcStatus.INVALID_ARGUMENT,
      message: "Expected CreateSessionRequest first",
    });
    return;
  }

  call.write({ createSessionResponse: { sessionId: state.sessionId } } as DetectDeepfakeResponse);
  // Verb width 5 chars — kept in sync with 'end  ' and 'fault' below so '|'
  // aligns when grepping per-session log output.
  log(
    state.sessionId,
    `start | scenario=${scenario.id} | duration_target=${scenario.stream.durationMs}ms` +
      (scenario.randomSeed != null ? ` | seed=${scenario.randomSeed}` : ""),
  );

  // 2. Schedule timeline emissions and fault. Each emission applies network
  //    delay before writing.
  const timeline = buildTimeline(scenario);
  let faultFired = false;
  const timers: NodeJS.Timeout[] = [];

  for (const item of timeline) {
    const t = setTimeout(async () => {
      if (clientEnded || faultFired) return;
      await applyNetwork(scenario.network, rng);
      if (clientEnded || faultFired) return;
      const response = materialise(item, scenario, state, rng, item.atMs);
      if (response) {
        if (LOG_ANALYSES && response.analysisResult) {
          const r = response.analysisResult;
          const offsetS = (Number(r.audioOffsetMs) / 1000).toFixed(2).padStart(6);
          log(
            state.sessionId,
            `analysis @ ${offsetS}s | score=${r.score.toFixed(3)} | ` +
              `label=${r.label} | confidence=${r.confidence.toFixed(2)}`,
          );
        }
        call.write(response);
      }
    }, item.atMs);
    timers.push(t);
  }

  if (scenario.grpc.terminateAtMs != null) {
    const t = setTimeout(() => {
      if (clientEnded) return;
      faultFired = true;
      const codeName = scenario.grpc.statusCode ?? "INTERNAL";
      const code = STATUS_CODE_BY_NAME[codeName] ?? GrpcStatus.INTERNAL;
      const message = scenario.grpc.statusMessage ?? "simulated fault";
      log(
        state.sessionId,
        // 'fault' is verb width 5 — kept in sync with 'start' / 'end  '.
        `fault | at=${scenario.grpc.terminateAtMs}ms | code=${codeName} | message=${JSON.stringify(message)} ` +
          `| analyses_so_far=${state.analysisCount}`,
      );
      call.emit("error", { code, message });
    }, scenario.grpc.terminateAtMs);
    timers.push(t);
  }

  // 3. Wait for the client to close, or for the timeline (+ stream duration)
  //    to fully elapse. Whichever comes first.
  const timelineEnd = sleep(scenario.stream.durationMs).then(() => "timeline" as const);
  const clientEnd = clientEndedPromise.then(() => "client" as const);
  await Promise.race([timelineEnd, clientEnd]);

  // If the timeline finished first, still wait for the client to close so we
  // drain remaining audio metadata and don't write FinalResult mid-stream.
  if (!clientEnded) {
    await clientEndedPromise;
  }

  // Stop any pending scheduled emissions.
  for (const t of timers) clearTimeout(t);

  if (faultFired) return;

  if (Object.keys(scenario.grpc.trailingMetadata).length > 0) {
    const md = new Metadata();
    for (const [k, v] of Object.entries(scenario.grpc.trailingMetadata)) md.set(k, v);
    // @grpc/grpc-js attaches trailers via the call's `sendMetadata`-like
    // mechanism through `call.end(metadata?)` — not all versions support it,
    // so we attach as best-effort to the response object instead.
    (call as any).trailingMetadata = md;
  }

  const totalAudioMs = Math.max(state.accumulatedAudioMs, scenario.stream.durationMs);
  const overallLabel = state.analysisCount > 0 ? state.lastLabel : "unknown";
  log(
    state.sessionId,
    // 'end  ' padded to verb width 5 — matches 'start' / 'fault'.
    `end   | total=${totalAudioMs}ms | analyses=${state.analysisCount} ` +
      `| score=${state.lastScore.toFixed(3)} | label=${overallLabel}`,
  );
  call.write({
    finalResult: {
      totalAudioMs: BigInt(totalAudioMs),
      overallScore: state.lastScore,
      overallLabel,
      analysisCount: state.analysisCount,
    },
  } as DetectDeepfakeResponse);
  call.end();
}
