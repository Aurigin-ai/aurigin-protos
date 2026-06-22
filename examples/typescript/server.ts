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
  type ServerDuplexStream,
} from "@grpc/grpc-js";
import {
  DeepfakeDetectionService,
  type DeepfakeDetectionServer,
  type DetectDeepfakeRequest,
  type DetectDeepfakeResponse,
} from "@aurigin/protos/aurigin/deepfake_detection/v1/deepfake_detection";

import { loadScenarios, runSession, type Scenario } from "./sim/index.js";
import { serverCredentials, transportLabel } from "./tls.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DEFAULT_SCENARIOS_DIR = path.resolve(__dirname, "..", "scenarios");

function pickScenario(
  metadata: Map<string, string>,
  scenarios: Map<string, Scenario>,
  defaultId: string,
): { scenario: Scenario; requested: string | undefined } {
  const requested = metadata.get("x-scenario-id");
  if (requested && scenarios.has(requested)) {
    return { scenario: scenarios.get(requested)!, requested };
  }
  return { scenario: scenarios.get(defaultId)!, requested };
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
      const { scenario, requested } = pickScenario(metadata, scenarios, defaultId);
      // Log the inbound call before the runner starts so the operator sees
      // *which* scenario the client asked for (and whether we honored it).
      // The runner's `start` line lands ~1 RTT later with the generated
      // session id once CreateSessionResponse is written.
      const peer = call.getPeer ? call.getPeer() : "?";
      let note: string;
      if (requested === undefined) note = `requested=none → default=${scenario.id}`;
      else if (requested === scenario.id) note = `scenario=${scenario.id}`;
      else note = `requested='${requested}' unknown → fallback=${scenario.id}`;
      console.error(`[incoming] peer=${peer} | ${note}`);
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
    serverCredentials(),
    (err, boundPort) => {
      if (err) {
        console.error(err);
        process.exit(1);
      }
      console.error(
        `DeepfakeDetection simulator listening on :${boundPort} | ${scenarios.size} scenarios loaded from ${scenariosDir} | default='${defaultId}' | transport=${transportLabel("server")}`,
      );
    },
  );

  // Graceful shutdown on Ctrl-C / SIGTERM. tryShutdown() lets in-flight RPCs
  // finish; a 2 s deadline triggers forceShutdown() so the process can't hang
  // on a stuck handler. Without this, the default behavior tears the loop
  // down mid-handler and leaves sockets / writes in a bad state.
  let shuttingDown = false;
  const shutdown = (signame: string): void => {
    if (shuttingDown) return;
    shuttingDown = true;
    process.stderr.write(`\nReceived ${signame}, shutting down...\n`);
    const forceTimer = setTimeout(() => {
      process.stderr.write("Grace period expired, force shutdown.\n");
      server.forceShutdown();
      process.exit(0);
    }, 2000);
    forceTimer.unref();
    server.tryShutdown((err) => {
      clearTimeout(forceTimer);
      if (err) process.stderr.write(`tryShutdown error: ${err.message}\n`);
      process.stderr.write("DeepfakeDetection simulator stopped.\n");
      process.exit(0);
    });
  };
  process.on("SIGINT", () => shutdown("SIGINT"));
  process.on("SIGTERM", () => shutdown("SIGTERM"));
}

serve();
