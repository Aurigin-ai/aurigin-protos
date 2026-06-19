// List every loadable scenario from ../scenarios as `<id>  <description>`.
// Uses the same loader the server uses, so the list always matches what the
// server would accept via the x-scenario-id metadata header.
//
// Mirrors `just scenarios` on the Python side.

import * as path from "node:path";
import { fileURLToPath } from "node:url";

import { loadScenarios } from "./sim/index.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SCENARIOS_DIR = path.resolve(__dirname, "..", "scenarios");

const scenarios = loadScenarios(SCENARIOS_DIR);
const sorted = [...scenarios.values()].sort((a, b) => a.id.localeCompare(b.id));
for (const s of sorted) {
  console.log(`${s.id.padEnd(38)}  ${s.description}`);
}
