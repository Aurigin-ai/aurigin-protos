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
  kind: "curve_sample" | "event";
  atMs: number;
  event?: ScenarioEvent;
}

function makeSessionId(): string {
  return `sim-${randomBytes(4).toString("hex")}`;
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

  const curve = scenario.confidenceCurve;
  if (curve != null) {
    const every = (curve.emit_every_ms ?? 1000) as number;
    for (let t = every; t <= scenario.stream.durationMs; t += every) {
      items.push({ kind: "curve_sample", atMs: t });
    }
  }

  for (const ev of scenario.events) {
    items.push({ kind: "event", atMs: ev.atMs, event: ev });
  }

  items.sort((a, b) => a.atMs - b.atMs);
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
  if (item.kind === "curve_sample") {
    const curve = scenario.confidenceCurve!;
    const score = curves.evaluate(curve, rng, atMs);
    const label = curves.labelFor(curve, score);
    const confidence = Math.round((0.85 + rng() * 0.14) * 1000) / 1000;
    return makeAnalysisResult(state, atMs, score, label, confidence, scenario.stream.chunkIntervalMs);
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
    return makeAnalysisResult(state, atMs, 0, "error", 0, scenario.stream.chunkIntervalMs);
  }
  // CONFIDENCE_UPDATE or FAKE_DETECTED — both map to AnalysisResult.
  const payload = ev.payload;
  const score = (payload.fake_probability ?? state.lastScore) as number;
  const label = (payload.label ?? curves.labelFor(scenario.confidenceCurve ?? {}, score)) as string;
  const confidence = (payload.confidence ?? Math.round((0.85 + rng() * 0.14) * 1000) / 1000) as number;
  return makeAnalysisResult(state, atMs, score, label, confidence, scenario.stream.chunkIntervalMs);
}

function audioChunkMs(audio: {
  durationNs?: bigint;
  rate?: number;
  channels?: number;
  buffer?: Uint8Array;
}): number {
  const durationNs = audio.durationNs ?? 0n;
  if (durationNs > 0n) {
    return Number(durationNs / 1_000_000n);
  }
  const rate = audio.rate ?? 0;
  const channels = audio.channels ?? 0;
  const buf = audio.buffer;
  if (rate && channels && buf) {
    return Math.round((buf.length / 2 / channels / rate) * 1000);
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

  call.on("data", (msg: DetectDeepfakeRequest) => {
    if (!firstResolved) {
      firstResolved = true;
      firstResolve(msg);
      return;
    }
    if (msg.audio) {
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
      if (response) call.write(response);
    }, item.atMs);
    timers.push(t);
  }

  if (scenario.grpc.terminateAtMs != null) {
    const t = setTimeout(() => {
      if (clientEnded) return;
      faultFired = true;
      const code = STATUS_CODE_BY_NAME[scenario.grpc.statusCode ?? "INTERNAL"] ?? GrpcStatus.INTERNAL;
      call.emit("error", {
        code,
        message: scenario.grpc.statusMessage ?? "simulated fault",
      });
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

  call.write({
    finalResult: {
      totalAudioMs: BigInt(Math.max(state.accumulatedAudioMs, scenario.stream.durationMs)),
      overallScore: state.lastScore,
      overallLabel: state.analysisCount > 0 ? state.lastLabel : "unknown",
      analysisCount: state.analysisCount,
    },
  } as DetectDeepfakeResponse);
  call.end();
}
