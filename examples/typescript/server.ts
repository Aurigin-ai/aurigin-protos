// Scenario-driven gRPC simulator for DeepfakeDetection.DetectDeepfake.
//
// Loads YAML scenarios from a directory at startup, validates each against
// the JSON Schema in `examples/scenarios/scenario.schema.json`, and serves
// them per session.
//
// Client selects a scenario via the `x-scenario-id` request metadata header.
// If the header is missing or names an unknown scenario, the server falls
// back to the scenario whose id matches `SCENARIO_DEFAULT` (default `default`).
//
// Env vars:
//     PORT              gRPC listen port              (default 50051)
//     SCENARIOS_DIR     directory of *.yaml scenarios (default <repo>/examples/scenarios)
//     SCENARIO_DEFAULT  id of the fallback scenario   (default "default")
//
// Mirrors examples/python/server.py.

import * as path from "node:path";
import { fileURLToPath } from "node:url";

import {
  Server,
  ServerCredentials,
  type ServerDuplexStream,
} from "@grpc/grpc-js";
import {
  DeepfakeDetectionService,
  type DeepfakeDetectionServer,
  type DetectDeepfakeRequest,
  type DetectDeepfakeResponse,
} from "@aurigin/protos/aurigin/deepfake_detection/v1/deepfake_detection";

import { loadScenarios, runSession, type Scenario } from "./sim/index.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DEFAULT_SCENARIOS_DIR = path.resolve(__dirname, "..", "scenarios");

function pickScenario(
  metadata: Map<string, string>,
  scenarios: Map<string, Scenario>,
  defaultId: string,
): Scenario {
  const requested = metadata.get("x-scenario-id");
  if (requested && scenarios.has(requested)) {
    return scenarios.get(requested)!;
  }
  return scenarios.get(defaultId)!;
}

function metadataToMap(
  call: ServerDuplexStream<DetectDeepfakeRequest, DetectDeepfakeResponse>,
): Map<string, string> {
  const md = new Map<string, string>();
  for (const [k, v] of Object.entries(call.metadata.toJSON())) {
    if (Array.isArray(v) && v.length > 0) {
      md.set(k.toLowerCase(), String(v[0]));
    }
  }
  return md;
}

function buildImpl(
  scenarios: Map<string, Scenario>,
  defaultId: string,
): DeepfakeDetectionServer {
  return {
    detectDeepfake(call: ServerDuplexStream<DetectDeepfakeRequest, DetectDeepfakeResponse>) {
      const metadata = metadataToMap(call);
      const scenario = pickScenario(metadata, scenarios, defaultId);
      runSession(scenario, call).catch((err) => {
        // runSession only throws on programming errors — gRPC-level faults
        // are emitted as `error` events on the call. Log and end cleanly.
        console.error("runSession failed:", err);
        try {
          call.end();
        } catch {
          // ignore
        }
      });
    },
  };
}

function serve(): void {
  const port = Number(process.env.PORT ?? 50051);
  const scenariosDir = process.env.SCENARIOS_DIR ?? DEFAULT_SCENARIOS_DIR;
  const defaultId = process.env.SCENARIO_DEFAULT ?? "default";

  const scenarios = loadScenarios(scenariosDir);
  if (!scenarios.has(defaultId)) {
    const available = [...scenarios.keys()].sort();
    console.error(
      `Default scenario id '${defaultId}' not found in ${scenariosDir}. Available: ${JSON.stringify(available)}`,
    );
    process.exit(1);
  }

  const server = new Server();
  server.addService(DeepfakeDetectionService, buildImpl(scenarios, defaultId));

  server.bindAsync(
    `0.0.0.0:${port}`,
    ServerCredentials.createInsecure(),
    (err, boundPort) => {
      if (err) {
        console.error(err);
        process.exit(1);
      }
      console.log(
        `DeepfakeDetection simulator listening on :${boundPort} | ${scenarios.size} scenarios loaded from ${scenariosDir} | default='${defaultId}'`,
      );
    },
  );
}

serve();
