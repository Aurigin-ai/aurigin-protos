// Simulate one or more mobile phone calls by streaming audio to DetectDeepfake.
//
// Reads a WAV file (looped to fill --duration, paced in real time) and prints
// analysis events as they arrive (the client's "data" listener fires
// concurrently with sends).
//
// With --concurrency N, opens N concurrent bidi streams over a single
// gRPC channel. Each stream gets a client-side label (`call-01`, `call-02`,
// ...) that prefixes every log line so interleaved output stays readable.
//
// CLI:
//   tsx phone_call.ts [--audio path/to.wav] [--duration 30]
//                     [--chunk-ms 100] [--target localhost:50051]
//                     [--concurrency 1] [--scenario-id ID]
//
// Defaults:
//   - if --audio is omitted, picks the first .wav in `examples/audio/`
//     (gitignored — drop a fixture in).

import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { Metadata, type ClientDuplexStream } from "@grpc/grpc-js";
import {
  DeepfakeDetectionClient,
  type DetectDeepfakeRequest,
  type DetectDeepfakeResponse,
} from "@aurigin/protos/aurigin/deepfake_detection/v1/deepfake_detection";
import { channelCredentials, transportLabel } from "./tls.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const DEFAULT_CHUNK_MS = 100;
const DEFAULT_DURATION_S = 30;
const DEFAULT_CONCURRENCY = 1;

// ─── Per-stream logger ────────────────────────────────────────────────
function log(label: string, message: string): void {
  console.log(`[${label}] ${message}`);
}

// ─── WAV reader (S16LE PCM and F32LE IEEE float) ──────────────────────
const WAVE_FORMAT_PCM = 0x0001;
const WAVE_FORMAT_IEEE_FLOAT = 0x0003;

interface WavData {
  sampleRate: number;
  channels: number;
  samples: Buffer;
  wireFormat: "S16LE" | "F32LE";
  bytesPerSample: number;
}

function readWav(filePath: string): WavData {
  const buf = fs.readFileSync(filePath);
  if (buf.toString("ascii", 0, 4) !== "RIFF" || buf.toString("ascii", 8, 12) !== "WAVE") {
    throw new Error(`${filePath}: not a RIFF/WAVE file`);
  }
  let offset = 12;
  let audioFormat = 0;
  let sampleRate = 0;
  let channels = 0;
  let bitsPerSample = 0;
  let dataStart = -1;
  let dataLen = 0;
  while (offset + 8 <= buf.length) {
    const id = buf.toString("ascii", offset, offset + 4);
    const size = buf.readUInt32LE(offset + 4);
    if (id === "fmt ") {
      audioFormat = buf.readUInt16LE(offset + 8);
      channels = buf.readUInt16LE(offset + 10);
      sampleRate = buf.readUInt32LE(offset + 12);
      bitsPerSample = buf.readUInt16LE(offset + 22);
    } else if (id === "data") {
      dataStart = offset + 8;
      dataLen = size;
      break;
    }
    offset += 8 + size + (size & 1);
  }
  if (dataStart < 0) throw new Error(`${filePath}: no data chunk`);
  let wireFormat: "S16LE" | "F32LE";
  if (audioFormat === WAVE_FORMAT_PCM && bitsPerSample === 16) {
    wireFormat = "S16LE";
  } else if (audioFormat === WAVE_FORMAT_IEEE_FLOAT && bitsPerSample === 32) {
    wireFormat = "F32LE";
  } else {
    throw new Error(
      `${filePath}: unsupported WAV (format tag ${audioFormat}, ${bitsPerSample}-bit) — ` +
        `expected 16-bit PCM or 32-bit IEEE float`,
    );
  }
  return {
    sampleRate,
    channels,
    samples: buf.subarray(dataStart, dataStart + dataLen),
    wireFormat,
    bytesPerSample: bitsPerSample / 8,
  };
}

function resolveAudio(arg: string | null): string {
  if (arg) {
    if (!fs.existsSync(arg)) throw new Error(`No such file: ${arg}`);
    return arg;
  }
  const audioDir = path.join(__dirname, "..", "audio");
  if (!fs.existsSync(audioDir)) {
    throw new Error("No --audio supplied and examples/audio/ does not exist.");
  }
  const wavs = fs.readdirSync(audioDir).filter((f) => f.endsWith(".wav")).sort();
  if (wavs.length === 0) {
    throw new Error(
      "No --audio supplied and no .wav files found in examples/audio/. " +
        "Drop a 16-bit PCM or 32-bit IEEE-float WAV in examples/audio/ or pass one with --audio.",
    );
  }
  return path.join(audioDir, wavs[0]);
}

// ─── Sender ───────────────────────────────────────────────────────────

type Call = ClientDuplexStream<DetectDeepfakeRequest, DetectDeepfakeResponse>;

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

async function sendFile(
  call: Call,
  _label: string,
  wav: WavData,
  durationS: number,
  chunkMs: number,
): Promise<void> {
  const { sampleRate, channels, samples, wireFormat, bytesPerSample } = wav;
  const bytesPerFrame = bytesPerSample * channels;
  const bytesPerChunk = Math.max(1, Math.floor((sampleRate * chunkMs) / 1000) * bytesPerFrame);
  const chunkS = chunkMs / 1000;

  call.write({ createSessionRequest: {} });

  let nextSend = Date.now();
  let ptsNs = 0n;
  let cursor = 0;
  let elapsedS = 0;

  while (elapsedS < durationS) {
    const end = Math.min(cursor + bytesPerChunk, samples.length);
    const chunk = samples.subarray(cursor, end);
    cursor = end;
    if (cursor >= samples.length) cursor = 0; // loop the file
    const actualFrames = chunk.length / bytesPerFrame;
    const durationNs = BigInt(Math.round((actualFrames / sampleRate) * 1e9));

    call.write({
      audio: {
        type: "audio/x-raw",
        format: wireFormat,
        channels,
        rate: sampleRate,
        durationNs,
        ptsNs,
        size: BigInt(chunk.length),
        buffer: chunk,
      },
    });
    ptsNs += durationNs;
    elapsedS += chunkS;

    // Deadline-based pacing: ~1 s of audio per second of wallclock,
    // robust to single-write jitter.
    nextSend += chunkMs;
    const sleepFor = nextSend - Date.now();
    if (sleepFor > 0) await sleep(sleepFor);
  }

  call.end();
}

// ─── Receiver (concurrent with sender) ────────────────────────────────

function listen(call: Call, label: string): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    call.on("data", (response: DetectDeepfakeResponse) => {
      if (response.createSessionResponse) {
        log(label, `📞 Session: ${response.createSessionResponse.sessionId}`);
      } else if (response.analysisResult) {
        const r = response.analysisResult;
        const offsetS = (Number(r.audioOffsetMs) / 1000).toFixed(2).padStart(6);
        log(
          label,
          `   Analysis @ ${offsetS}s | score=${r.score.toFixed(3)} | ` +
            `label=${r.label.padEnd(18)} | confidence=${r.confidence.toFixed(2)}`,
        );
      } else if (response.finalResult) {
        const f = response.finalResult;
        log(
          label,
          `☎️  Call ended | total=${(Number(f.totalAudioMs) / 1000).toFixed(2)}s | ` +
            `score=${f.overallScore.toFixed(3)} | label=${f.overallLabel} | ` +
            `analyses=${f.analysisCount}`,
        );
      }
    });
    call.on("end", resolve);
    call.on("error", reject);
  });
}

// ─── Tiny argv parser ─────────────────────────────────────────────────

interface Args {
  audio: string | null;
  duration: number;
  chunkMs: number;
  target: string;
  concurrency: number;
  staggerMs: number;
  scenarioId: string | null;
}

function parseArgs(argv: string[]): Args {
  const out: Args = {
    audio: null,
    duration: DEFAULT_DURATION_S,
    chunkMs: DEFAULT_CHUNK_MS,
    target: "localhost:50051",
    concurrency: DEFAULT_CONCURRENCY,
    staggerMs: 0,
    scenarioId: null,
  };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    const next = () => argv[++i];
    switch (arg) {
      case "--audio": out.audio = next(); break;
      case "--duration": out.duration = Number(next()); break;
      case "--chunk-ms": out.chunkMs = Number(next()); break;
      case "--target": out.target = next(); break;
      case "-c": case "--concurrency": out.concurrency = Number(next()); break;
      case "--stagger-ms": out.staggerMs = Number(next()); break;
      case "--scenario-id": out.scenarioId = next(); break;
      case "-h": case "--help":
        console.log("Usage: tsx phone_call.ts [--audio FILE] [--duration SEC]");
        console.log("                         [--chunk-ms MS] [--target HOST:PORT]");
        console.log("                         [--concurrency N | -c N] [--stagger-ms MS]");
        console.log("                         [--scenario-id ID]");
        process.exit(0);
      default: throw new Error(`Unknown arg: ${arg}`);
    }
  }
  if (!Number.isInteger(out.concurrency) || out.concurrency < 1) {
    throw new Error("--concurrency must be a positive integer");
  }
  if (!Number.isFinite(out.staggerMs) || out.staggerMs < 0) {
    throw new Error("--stagger-ms must be >= 0");
  }
  return out;
}

function makeLabels(n: number): string[] {
  const width = Math.max(2, String(n).length);
  return Array.from({ length: n }, (_, i) => `call-${String(i + 1).padStart(width, "0")}`);
}

// ─── Per-stream runner ────────────────────────────────────────────────

interface CallResult {
  label: string;
  error: unknown | null;
}

async function runOneCall(
  client: DeepfakeDetectionClient,
  label: string,
  metadata: Metadata | undefined,
  senderFactory: (call: Call, label: string) => Promise<void>,
  delayMs: number = 0,
  isShuttingDown: () => boolean = () => false,
  registerCall: (call: Call) => void = () => {},
): Promise<CallResult> {
  // With delayMs > 0, sleep before opening the stream — used by --stagger-ms
  // to fan out start times across concurrent calls.
  if (delayMs > 0) await sleep(delayMs);
  // If a SIGINT/SIGTERM arrived during the stagger wait, skip opening this
  // stream entirely. Without this we'd open a fresh call onto a channel that
  // is about to be closed.
  if (isShuttingDown()) return { label, error: new Error("Cancelled before start (signal)") };
  const call = metadata ? client.detectDeepfake(metadata) : client.detectDeepfake();
  registerCall(call);
  try {
    await Promise.all([senderFactory(call, label), listen(call, label)]);
    return { label, error: null };
  } catch (err) {
    return { label, error: err };
  }
}

// ─── main ─────────────────────────────────────────────────────────────

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const client = new DeepfakeDetectionClient(args.target, channelCredentials());
  const transportSuffix = ` | transport=${transportLabel("client")}`;
  const labels = makeLabels(args.concurrency);

  let metadata: Metadata | undefined;
  if (args.scenarioId) {
    metadata = new Metadata();
    metadata.set("x-scenario-id", args.scenarioId);
  }
  const scenarioSuffix = args.scenarioId ? ` | scenario=${args.scenarioId}` : "";
  const staggerSuffix = args.staggerMs ? ` | stagger=${args.staggerMs}ms` : "";

  // Graceful Ctrl-C: cancel every in-flight stream. listen()'s "error" handler
  // rejects, runOneCall catches, returns {label, error} — Promise.all still
  // resolves and the summary prints normally.
  const activeCalls: Call[] = [];
  let shuttingDown = false;
  const shutdown = (signame: string): void => {
    if (shuttingDown) return;
    shuttingDown = true;
    process.stderr.write(`\nReceived ${signame}, cancelling streams...\n`);
    for (const c of activeCalls) {
      try { c.cancel(); } catch { /* best-effort */ }
    }
  };
  process.on("SIGINT", () => shutdown("SIGINT"));
  process.on("SIGTERM", () => shutdown("SIGTERM"));

  let results: CallResult[];
  try {
    const audioPath = resolveAudio(args.audio);
    const wav = readWav(audioPath);
    const fileDur = wav.samples.length / (wav.sampleRate * wav.bytesPerSample * wav.channels);
    console.log(
      `📞 Calling ${args.target} | source=${path.basename(audioPath)} ` +
        `(${fileDur.toFixed(2)}s @ ${wav.sampleRate}Hz/${wav.channels}ch ${wav.wireFormat}) ` +
        `| duration=${args.duration.toFixed(1)}s | frame=${args.chunkMs}ms | ` +
        `concurrency=${args.concurrency}${staggerSuffix}${scenarioSuffix}${transportSuffix}`,
    );
    console.log("─".repeat(70));
    const senderFactory = (call: Call, label: string) =>
      sendFile(call, label, wav, args.duration, args.chunkMs);
    results = await Promise.all(
      labels.map((label, i) =>
        runOneCall(
          client, label, metadata, senderFactory, i * args.staggerMs,
          () => shuttingDown, (c) => activeCalls.push(c),
        ),
      ),
    );
  } finally {
    client.close();
  }

  const failures = results.filter((r) => r.error !== null);
  if (shuttingDown) {
    // Cancellation paths look like errors; treat them as a clean exit since
    // SIGINT was intentional.
    process.stderr.write("─".repeat(70) + "\n");
    process.stderr.write(
      `Shutdown complete (${args.concurrency - failures.length}/${args.concurrency} streams finished cleanly).\n`,
    );
    return;
  }
  if (args.concurrency > 1) {
    console.log("─".repeat(70));
    const ok = args.concurrency - failures.length;
    console.log(`Summary: ${ok}/${args.concurrency} streams OK, ${failures.length} failed`);
    for (const { label, error } of failures) {
      console.error(`  [${label}] ${(error as Error).message ?? String(error)}`);
    }
    if (failures.length > 0) process.exit(1);
  } else if (failures.length > 0) {
    throw failures[0].error;
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
