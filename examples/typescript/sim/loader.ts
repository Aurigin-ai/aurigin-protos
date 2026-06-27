// YAML scenario loader with JSON-Schema validation.
//
// Mirrors examples/python/sim/loader.py. Walks a directory tree of *.yaml
// scenarios, validates each against `examples/scenarios/scenario.schema.json`
// (Draft 2020-12), and returns a map keyed by scenario id.

import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

import * as yaml from "js-yaml";
// Ajv's CJS-shim default export doesn't satisfy TS's `new` check directly —
// pull the constructor off `.default` when needed.
import Ajv2020Module from "ajv/dist/2020.js";
const Ajv2020 = (Ajv2020Module as any).default ?? Ajv2020Module;

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SCHEMA_PATH = path.resolve(__dirname, "..", "..", "scenarios", "scenario.schema.json");

export interface StreamConfig {
  durationMs: number;
  chunkIntervalMs: number;
  sampleRateHz: number;
  channels: number;
  codec: string;
}

export interface NetworkConfig {
  baseLatencyMs: number;
  jitterMs: number;
  dropEventProbability: number;
  duplicateEventProbability: number;
  outOfOrderProbability: number;
}

export interface GrpcConfig {
  initialMetadata: Record<string, string>;
  trailingMetadata: Record<string, string>;
  terminateAtMs: number | null;
  statusCode: string | null;
  statusMessage: string | null;
}

export interface ScenarioEvent {
  atMs: number;
  type: string;
  name: string | null;
  payload: Record<string, any>;
}

// Declarative deepfake-service mirror — when present on a Scenario, the
// simulator computes the emission timeline from these knobs instead of using
// confidence_curve.emit_every_ms. Mirrors dfs config: analysis_interval_s ×
// 1000, min_analysis_duration_s × 1000.
export interface BackendSimulation {
  analysisIntervalMs: number;
  minAnalysisDurationMs: number;
  tailStrategy: "drop" | "extend" | "recompute";
  silentWindows: ReadonlyArray<number>;
  silenceConfidence: number;
}

export interface Scenario {
  id: string;
  description: string;
  stream: StreamConfig;
  network: NetworkConfig;
  grpc: GrpcConfig;
  randomSeed: number | null;
  confidenceCurve: Record<string, any> | null;
  events: ReadonlyArray<ScenarioEvent>;
  backendSimulation: BackendSimulation | null;
}

function loadSchema(): object {
  const raw = fs.readFileSync(SCHEMA_PATH, "utf-8");
  return JSON.parse(raw);
}

const VALIDATOR = new Ajv2020({ strict: false, allErrors: true }).compile(loadSchema());

function validate(doc: unknown, source: string): void {
  if (VALIDATOR(doc)) return;
  const lines = [`Scenario validation failed for ${source}:`];
  for (const err of VALIDATOR.errors ?? []) {
    const loc = err.instancePath || "<root>";
    lines.push(`  - ${loc}: ${err.message ?? "<no message>"}`);
  }
  throw new Error(lines.join("\n"));
}

function scenarioFromDoc(doc: any, _source: string): Scenario {
  const sc = doc.scenario;
  const st = doc.stream;
  const net = doc.network ?? {};
  const grpcCfg = doc.grpc ?? {};
  const rnd = doc.random ?? {};
  const events: ScenarioEvent[] = (doc.events ?? []).map((e: any) => ({
    atMs: e.at_ms,
    type: e.type,
    name: e.name ?? null,
    payload: { ...(e.payload ?? {}) },
  }));
  const bs = doc.backend_simulation;
  const backendSimulation: BackendSimulation | null = bs
    ? {
        analysisIntervalMs: Number(bs.analysis_interval_ms),
        minAnalysisDurationMs: Number(bs.min_analysis_duration_ms ?? 1000),
        tailStrategy: (bs.tail_strategy ?? "drop") as "drop" | "extend" | "recompute",
        silentWindows: Object.freeze(((bs.silent_windows ?? []) as number[]).map((n) => Number(n))),
        silenceConfidence: Number(bs.silence_confidence ?? 0.95),
      }
    : null;
  return {
    id: sc.id,
    description: sc.description ?? "",
    stream: {
      durationMs: st.duration_ms,
      chunkIntervalMs: st.chunk_interval_ms ?? 100,
      sampleRateHz: st.sample_rate_hz ?? 16000,
      channels: st.channels ?? 1,
      codec: st.codec ?? "pcm_s16le",
    },
    network: {
      baseLatencyMs: net.base_latency_ms ?? 0,
      jitterMs: net.jitter_ms ?? 0,
      dropEventProbability: net.drop_event_probability ?? 0,
      duplicateEventProbability: net.duplicate_event_probability ?? 0,
      outOfOrderProbability: net.out_of_order_probability ?? 0,
    },
    grpc: {
      initialMetadata: { ...(grpcCfg.initial_metadata ?? {}) },
      trailingMetadata: { ...(grpcCfg.trailing_metadata ?? {}) },
      terminateAtMs: grpcCfg.terminate_at_ms ?? null,
      statusCode: grpcCfg.status_code ?? null,
      statusMessage: grpcCfg.status_message ?? null,
    },
    randomSeed: rnd.seed ?? null,
    confidenceCurve: doc.confidence_curve ?? null,
    events,
    backendSimulation,
  };
}

function walkYaml(dir: string): string[] {
  const out: string[] = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      out.push(...walkYaml(full));
    } else if (entry.isFile() && full.endsWith(".yaml")) {
      out.push(full);
    }
  }
  return out;
}

export function loadScenarios(scenariosDir: string): Map<string, Scenario> {
  if (!fs.existsSync(scenariosDir) || !fs.statSync(scenariosDir).isDirectory()) {
    throw new Error(`Scenarios directory not found: ${scenariosDir}`);
  }
  const byId = new Map<string, Scenario>();
  const seenSource = new Map<string, string>();
  for (const yamlPath of walkYaml(scenariosDir).sort()) {
    const raw = fs.readFileSync(yamlPath, "utf-8");
    const doc = yaml.load(raw);
    if (doc == null) continue;
    validate(doc, yamlPath);
    const scenario = scenarioFromDoc(doc, yamlPath);
    if (seenSource.has(scenario.id)) {
      throw new Error(
        `Duplicate scenario id '${scenario.id}' in ${yamlPath} (already defined in ${seenSource.get(scenario.id)})`,
      );
    }
    seenSource.set(scenario.id, yamlPath);
    byId.set(scenario.id, scenario);
  }
  return byId;
}
