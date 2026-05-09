// End-to-end smoke test for the TS example.
//
// Spawns examples/typescript/server.ts and runs examples/typescript/client.ts
// against it on a non-default port. The client falls back to streaming 3 s
// of silence when examples/audio/ is empty (always the case in CI), so this
// test exercises the full proto + gRPC wire path without needing fixtures.
//
// Catches anything that breaks the example: proto field renames, message
// removals, RPC name changes, ts-proto API shifts, server impl bugs.
//
// Run with: node --import tsx --test tests/smoke.test.ts

import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn, type ChildProcess } from "node:child_process";
import * as net from "node:net";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const EXAMPLES_DIR = path.resolve(__dirname, "..");
const PORT = 50062; // avoid clashing with a dev server on 50051 / Python test on 50061

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

async function waitForPort(port: number, timeoutMs = 15_000): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const reachable = await new Promise<boolean>((resolve) => {
      const s = net.createConnection(port, "localhost");
      s.once("connect", () => { s.destroy(); resolve(true); });
      s.once("error", () => { s.destroy(); resolve(false); });
    });
    if (reachable) return true;
    await sleep(100);
  }
  return false;
}

function startServer(): ChildProcess {
  return spawn("npx", ["tsx", path.join(EXAMPLES_DIR, "server.ts")], {
    env: { ...process.env, PORT: String(PORT) },
    stdio: ["ignore", "pipe", "pipe"],
  });
}

function runClient(): Promise<{ code: number | null; stdout: string; stderr: string }> {
  return new Promise((resolve) => {
    const proc = spawn("npx", ["tsx", path.join(EXAMPLES_DIR, "client.ts"), `localhost:${PORT}`], {
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    proc.stdout.on("data", (d) => (stdout += d.toString()));
    proc.stderr.on("data", (d) => (stderr += d.toString()));
    proc.on("close", (code) => resolve({ code, stdout, stderr }));
  });
}

test("client roundtrip against example server", async () => {
  const server = startServer();
  let serverOutput = "";
  server.stdout?.on("data", (d) => (serverOutput += d.toString()));
  server.stderr?.on("data", (d) => (serverOutput += d.toString()));

  try {
    const reachable = await waitForPort(PORT);
    assert.ok(reachable, `Server didn't bind on :${PORT} within 15 s.\n${serverOutput}`);

    const { code, stdout, stderr } = await runClient();
    assert.equal(code, 0, `client failed: stderr=${stderr}`);
    assert.match(stdout, /Session: demo-session-0001/);
    assert.match(stdout, /Analysis \| offset=/);
    assert.match(stdout, /FINAL/);
  } finally {
    server.kill();
    // Give it a moment to release the port for the next test run.
    await sleep(200);
  }
});
