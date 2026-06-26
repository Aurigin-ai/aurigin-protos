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
import * as fs from "node:fs";
import * as net from "node:net";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const EXAMPLES_DIR = path.resolve(__dirname, "..");

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

// Ask the OS for an unused TCP port. There's a tiny TOCTOU window between
// here and when the server child binds, but it's vastly safer than a fixed
// port — Linux's default ephemeral range is 32768–60999, so any fixed port
// in there can be stolen by a transient outbound socket on a busy CI runner.
function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const s = net.createServer();
    s.once("error", reject);
    s.listen(0, "localhost", () => {
      const port = (s.address() as net.AddressInfo).port;
      s.close(() => resolve(port));
    });
  });
}

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

function startServer(port: number): ChildProcess {
  return spawn("npx", ["tsx", path.join(EXAMPLES_DIR, "server.ts")], {
    env: { ...process.env, PORT: String(port) },
    stdio: ["ignore", "pipe", "pipe"],
  });
}

function runProc(scriptPath: string, args: string[] = []): Promise<{ code: number | null; stdout: string; stderr: string }> {
  return new Promise((resolve) => {
    const proc = spawn("npx", ["tsx", scriptPath, ...args], { stdio: ["ignore", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    proc.stdout!.on("data", (d: Buffer) => { stdout += d.toString(); });
    proc.stderr!.on("data", (d: Buffer) => { stderr += d.toString(); });
    proc.on("close", (code: number | null) => resolve({ code, stdout, stderr }));
  });
}

async function killAndWait(proc: ChildProcess, timeoutMs = 2000): Promise<void> {
  if (proc.exitCode !== null || proc.signalCode !== null) return;
  const exited = new Promise<void>((resolve) => proc.once("exit", () => resolve()));
  proc.kill("SIGTERM");
  // tsx wraps the node child and doesn't always forward SIGTERM, so escalate
  // to SIGKILL if the process is still alive after the grace period — without
  // this, lingering server sockets keep the test runner from exiting.
  await Promise.race([
    exited,
    sleep(timeoutMs).then(() => {
      if (proc.exitCode === null && proc.signalCode === null) proc.kill("SIGKILL");
    }),
  ]);
  await exited;
}

async function withServer<T>(fn: (port: number) => Promise<T>): Promise<T> {
  const port = await freePort();
  const server = startServer(port);
  let serverOutput = "";
  server.stdout?.on("data", (d: Buffer) => { serverOutput += d.toString(); });
  server.stderr?.on("data", (d: Buffer) => { serverOutput += d.toString(); });
  try {
    const reachable = await waitForPort(port);
    assert.ok(reachable, `Server didn't bind on :${port} within 15 s.\n${serverOutput}`);
    return await fn(port);
  } finally {
    await killAndWait(server);
  }
}

test("client streams silence and roundtrips analyses", async () => {
  await withServer(async (port) => {
    const { code, stdout, stderr } = await runProc(
      path.join(EXAMPLES_DIR, "client.ts"),
      ["--target", `localhost:${port}`],
    );
    assert.equal(code, 0, `client failed: stderr=${stderr}`);
    assert.match(stdout, /Session: /, `missing session line in:\n${stdout}`);
    assert.match(stdout, /FINAL/);
    // Simulator issues per-session ids like 'sim-<8 hex>'. Pin the prefix only.
    assert.match(stdout, /sim-/, `missing simulator session id prefix in:\n${stdout}`);
    // NOTE: we deliberately don't assert AnalysisResult lines here. The
    // scenario-driven server emits curve samples at wallclock 1 s / 2 s / 3 s,
    // but the client streams its silence fallback as fast as gRPC can
    // serialize it, so the client typically closes its write side before any
    // sample fires. Analysis emission is covered by the phone-call test below,
    // which paces audio in real time.
  });
});

// One fixture per wire format. S16LE is the historical telephony case;
// F32LE catches regressions in the RIFF reader's format dispatch so a
// broken release of the IEEE-float path fails CI instead of prod.
const phoneCallFixtures: { name: string; file: string; header: RegExp }[] = [
  { name: "S16LE 8 kHz mono", file: "test_call.wav", header: /8000Hz\/1ch S16LE/ },
  { name: "F32LE 16 kHz mono", file: "test_call_f32le.wav", header: /16000Hz\/1ch F32LE/ },
];

for (const { name, file, header } of phoneCallFixtures) {
  test(`phone_call streams a ${name} WAV fixture and roundtrips analyses`, async () => {
    const repoRoot = path.resolve(EXAMPLES_DIR, "..", "..");
    const fixture = path.join(repoRoot, "examples", "audio", "fixtures", file);
    assert.ok(fs.existsSync(fixture), `missing test fixture: ${fixture}`);

    await withServer(async (port) => {
      const { code, stdout, stderr } = await runProc(
        path.join(EXAMPLES_DIR, "phone_call.ts"),
        ["--audio", fixture, "--duration", "1", "--chunk-ms", "100", "--target", `localhost:${port}`],
      );
      assert.equal(code, 0, `phone_call failed: stderr=${stderr}`);
      // Header confirms the WAV reader parsed sr/channels/format correctly.
      assert.match(stdout, header, `WAV reader didn't pick up ${name}`);
      assert.match(stdout, /📞 Session:/);
      assert.match(stdout, /Call ended/);
    });
  });
}

// Backend-simulation scenarios — pin the dfs config they mirror via
// --scenario-id and assert the on-wire emission shape matches what the real
// backend would produce for the same audio length + config.
const backendSimScenarios: {
  scenarioId: string;
  durationS: number;
  expectedAnalyses: number;
  extraSubstrings: string[];
}[] = [
  // tail_strategy=drop → 2 main windows fire, 1ms tail silently skipped.
  { scenarioId: "tail_dropped_below_min", durationS: 11, expectedAnalyses: 2, extraSubstrings: [] },
  // tail_strategy=extend → 2 emissions, second covers the 1ms tail.
  { scenarioId: "tail_extended_full_coverage", durationS: 11, expectedAnalyses: 2, extraSubstrings: [] },
  // tail_strategy=recompute → 2 emissions, second slides back (offset
  // shifts to audio time 5001ms, duration stays 5000ms).
  { scenarioId: "tail_recomputed_full_coverage", durationS: 11, expectedAnalyses: 2, extraSubstrings: [] },
  // silent_windows=[2] → one of the 5 emissions is the silence sentinel.
  // Loop the 10s fixture to fill the 15s scenario timeline.
  { scenarioId: "silence_gated_window", durationS: 16, expectedAnalyses: 5, extraSubstrings: ["label=silence"] },
];

for (const { scenarioId, durationS, expectedAnalyses, extraSubstrings } of backendSimScenarios) {
  test(`phone_call drives backend_simulation scenario '${scenarioId}'`, async () => {
    const repoRoot = path.resolve(EXAMPLES_DIR, "..", "..");
    const fixture = path.join(repoRoot, "examples", "audio", "fixtures", "test_call_10s_tail.wav");
    assert.ok(fs.existsSync(fixture), `missing test fixture: ${fixture}`);

    await withServer(async (port) => {
      const { code, stdout, stderr } = await runProc(
        path.join(EXAMPLES_DIR, "phone_call.ts"),
        [
          "--audio", fixture,
          "--duration", String(durationS),
          "--chunk-ms", "100",
          "--target", `localhost:${port}`,
          "--scenario-id", scenarioId,
        ],
      );
      assert.equal(code, 0, `phone_call failed: stderr=${stderr}`);
      assert.ok(
        stdout.includes(`analyses=${expectedAnalyses}`),
        `scenario ${scenarioId}: expected analyses=${expectedAnalyses}\nstdout:\n${stdout}`,
      );
      for (const needle of extraSubstrings) {
        assert.ok(
          stdout.includes(needle),
          `scenario ${scenarioId}: expected substring ${JSON.stringify(needle)}\nstdout:\n${stdout}`,
        );
      }
    });
  });
}
