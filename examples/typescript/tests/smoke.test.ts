// End-to-end smoke test for the TS example clients.
//
// Spawns the Python deepfake-simulator-service (canonical location:
// examples/simulator/deepfake/) and runs the TypeScript client examples
// against it on a non-default port. The client falls back to streaming
// 3 s of silence when examples/audio/ is empty (always the case in CI),
// so this test exercises the full proto + gRPC wire path without needing
// fixtures.
//
// Catches anything that breaks the example: proto field renames, message
// removals, RPC name changes, ts-proto API shifts, simulator impl bugs.
//
// Requires `python3` and the aurigin-deepfake-simulator-service package
// importable on PYTHONPATH (or `uv sync`-ed at that location). CI does the
// same via a small pre-test shell block.
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
const REPO_ROOT = path.resolve(EXAMPLES_DIR, "..", "..");
const SIMULATOR_SRC = path.join(REPO_ROOT, "examples", "simulator", "deepfake", "src");
const GEN_PY = path.join(REPO_ROOT, "gen", "py");

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
  // Spawn the canonical Python simulator via `uv run --with …` so its
  // runtime deps (grpcio/protobuf/pyyaml/jsonschema) resolve in an
  // ephemeral env without needing a pre-install step. PYTHONPATH still
  // covers the generated protobuf stubs + the simulator package src/.
  // Mirrors the smoke-py Makefile target so both language jobs use the
  // same "no persistent Python env" invocation, PEP 668-safe on macOS
  // Homebrew Python 3.14 and clean on CI Ubuntu alike.
  return spawn(
    "uv",
    [
      "run", "--no-project", "--python", "3.11",
      "--with", "grpcio", "--with", "protobuf",
      "--with", "pyyaml", "--with", "jsonschema",
      "python", "-m", "deepfake_simulator_service",
    ],
    {
      env: {
        ...process.env,
        PORT: String(port),
        PYTHONPATH: [GEN_PY, SIMULATOR_SRC, process.env.PYTHONPATH ?? ""].join(path.delimiter),
      },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
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
    // Simulator issues per-session ids like 'sim-<32 hex>' (matches dfs's
    // 'pre-<32 hex>' shape). Pin the prefix only.
    assert.match(stdout, /sim-/, `missing simulator session id prefix in:\n${stdout}`);
    // NOTE: we deliberately don't assert AnalysisResult lines here. The
    // scenario-driven server emits curve samples at wallclock 1 s / 2 s / 3 s,
    // but the client streams its silence fallback as fast as gRPC can
    // serialize it, so the client typically closes its write side before any
    // sample fires. Analysis emission is covered by the phone-call test below,
    // which paces audio in real time.
  });
});

const phoneCallFixtures: { name: string; file: string; header: RegExp }[] = [
  // S16LE 8 kHz mono — the historical telephony fixture.
  { name: "S16LE 8 kHz mono", file: "test_call.wav", header: /8000Hz\/1ch S16LE/ },
  // F32LE 16 kHz mono — exercises the IEEE-float wire path so a regression
  // that breaks the RIFF reader's format dispatch fails CI.
  { name: "F32LE 16 kHz mono", file: "test_call_f32le.wav", header: /16000Hz\/1ch F32LE/ },
  // S16LE 16 kHz mono — the 10.001 s boundary-case fixture (also used by
  // backend_simulation tests below); default-scenario roundtrip coverage
  // here, separate from the tail-strategy assertions there.
  { name: "S16LE 16 kHz mono (10s tail)", file: "test_call_10s_tail.wav", header: /16000Hz\/1ch S16LE/ },
  // G.711 μ-law 8 kHz mono — exercises the new-in-0.3.0 PCMU codec
  // branch (both the WAV reader's format-tag dispatch AND
  // AudioFrame.codec=AUDIO_CODEC_PCMU on the wire).
  { name: "PCMU 8 kHz mono", file: "test_call_mulaw.wav", header: /8000Hz\/1ch PCMU/ },
  // G.711 A-law 8 kHz mono — same as above for the PCMA branch.
  { name: "PCMA 8 kHz mono", file: "test_call_alaw.wav", header: /8000Hz\/1ch PCMA/ },
  // 24-bit signed linear PCM 8 kHz mono — exercises AUDIO_CODEC_S24LE
  // + doubles as coverage for the WAVE_FORMAT_EXTENSIBLE (0xfffe)
  // unwrap path, since ffmpeg emits >16-bit PCM under the EXTENSIBLE
  // envelope by default (SubFormat GUID = KSDATAFORMAT_SUBTYPE_PCM).
  { name: "S24LE 8 kHz mono", file: "test_call_s24le.wav", header: /8000Hz\/1ch S24LE/ },
  // 32-bit signed linear PCM 8 kHz mono — AUDIO_CODEC_S32LE path,
  // also under an EXTENSIBLE envelope.
  { name: "S32LE 8 kHz mono", file: "test_call_s32le.wav", header: /8000Hz\/1ch S32LE/ },
  // WebRTC-shape S16LE 48 kHz mono — exercises the 48 kHz rate that
  // native WebRTC audio graphs (browsers, aurigin client SDKs) emit.
  { name: "WebRTC S16LE 48 kHz mono", file: "test_call_webrtc_48k.wav", header: /48000Hz\/1ch S16LE/ },
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

// ─── fingerprint simulator + client ────────────────────────────────────
//
// Same pattern as the deepfake tests above, but points at the fingerprint
// simulator (deterministic sha256-seeded synthetic embeddings — no
// scenarios, no YAML). Two tests:
//
//   1. Silence roundtrip — proto/wire smoke on the fallback path.
//   2. Determinism — the sim's core invariant is "same input → same
//      embedding". Run the silence client twice, assert the same
//      fingerprint_code both times. Catches non-deterministic drift in
//      _synthetic_embedding() (seed source, PRNG constants, normalisation
//      order) before it leaks into consumer test suites that rely on
//      stable codes.
//
// No fingerprint-specific WAV fixture matrix — the "same input → same
// code" pattern is already covered by the determinism test; adding the
// deepfake fixture matrix here would triple test runtime for zero extra
// fingerprint-side signal.

const FINGERPRINT_SIMULATOR_SRC = path.join(REPO_ROOT, "examples", "simulator", "fingerprint", "src");

function startFingerprintServer(port: number): ChildProcess {
  return spawn(
    "uv",
    [
      "run", "--no-project", "--python", "3.11",
      "--with", "grpcio", "--with", "protobuf",
      "python", "-m", "fingerprint_simulator_service",
    ],
    {
      env: {
        ...process.env,
        PORT: String(port),
        PYTHONPATH: [GEN_PY, FINGERPRINT_SIMULATOR_SRC, process.env.PYTHONPATH ?? ""].join(path.delimiter),
      },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
}

async function withFingerprintServer<T>(fn: (port: number) => Promise<T>): Promise<T> {
  const port = await freePort();
  const server = startFingerprintServer(port);
  let serverOutput = "";
  server.stdout?.on("data", (d: Buffer) => { serverOutput += d.toString(); });
  server.stderr?.on("data", (d: Buffer) => { serverOutput += d.toString(); });
  try {
    const reachable = await waitForPort(port);
    assert.ok(reachable, `Fingerprint server didn't bind on :${port} within 15 s.\n${serverOutput}`);
    return await fn(port);
  } finally {
    await killAndWait(server);
  }
}

test("fingerprint_client streams silence and roundtrips one EmbeddingResult", async () => {
  await withFingerprintServer(async (port) => {
    const { code, stdout, stderr } = await runProc(
      path.join(EXAMPLES_DIR, "fingerprint_client.ts"),
      ["--target", `localhost:${port}`],
    );
    assert.equal(code, 0, `fingerprint_client failed: stderr=${stderr}`);
    assert.match(stdout, /Session: /, `missing session line in:\n${stdout}`);
    assert.match(stdout, /sim-/, `missing simulator session id prefix in:\n${stdout}`);
    // 10 × 500 ms silence chunks fills one 5000 ms window exactly → one EmbeddingResult.
    assert.match(
      stdout,
      /Embedding \| offset=0ms \| duration=5000ms \| code=[0-9a-f]{16} \| dim=768 \| head=[0-9a-f]{16}/,
      `missing / malformed Embedding line in:\n${stdout}`,
    );
    assert.match(
      stdout,
      /FINAL\s+\| total=5000ms \| embeddings=1/,
      `missing / malformed FINAL line in:\n${stdout}`,
    );
  });
});

test("fingerprint sim produces identical embeddings for identical input across runs", async () => {
  // The sim's determinism invariant: sha256(payload[:64]) → seeded PRNG →
  // L2-normalise. Same silence payload → same code. Both codes must match.
  const codeRe = /Embedding \| .*? code=([0-9a-f]{16})/;
  await withFingerprintServer(async (port) => {
    const codes: string[] = [];
    for (let i = 0; i < 2; i++) {
      const { code, stdout, stderr } = await runProc(
        path.join(EXAMPLES_DIR, "fingerprint_client.ts"),
        ["--target", `localhost:${port}`],
      );
      assert.equal(code, 0, `fingerprint_client failed: stderr=${stderr}`);
      const match = stdout.match(codeRe);
      assert.ok(match, `missing Embedding line in:\n${stdout}`);
      codes.push(match[1]);
    }
    assert.equal(
      codes[0], codes[1],
      `fingerprint_code drifted across runs: ${codes[0]} != ${codes[1]}. ` +
      "This breaks the sim's determinism invariant — check server.py's " +
      "_synthetic_embedding() for non-deterministic changes (seed source, " +
      "PRNG constants, normalisation order).",
    );
  });
});
