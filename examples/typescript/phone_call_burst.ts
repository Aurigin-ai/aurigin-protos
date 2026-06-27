// N concurrent live phone calls → DetectDeepfake.
//
// The recommended multi-call integration pattern: when a single PBX (e.g.
// a FreeSWITCH instance running `mod_audio_fork`) is handling many calls
// simultaneously, you want **one** long-lived gRPC channel multiplexing
// N concurrent bidi streams — not N separate channels. This file shows
// exactly that shape:
//
//   - One `new DeepfakeDetectionClient(target, ...)` for the whole run.
//   - N concurrent `client.detectDeepfake()` bidi streams over that channel,
//     each running the same `sendCall` / `recvCall` loop that
//     `phone_call.ts` documents in detail (imported, not duplicated).
//   - Per-stream `call-NN` label prefixes every log line so interleaved
//     output is grep-friendly.
//   - Optional `--stagger-ms` to spread call starts out (mimics a real
//     PBX where calls arrive over time, not all at exactly t=0).
//   - Graceful SIGINT/SIGTERM via `common.installSignalShutdown` cancels
//     every in-flight stream cleanly — no orphan bidi sockets, summary
//     still prints.
//   - Optional `--csv PATH` captures per-chunk results across all streams
//     in one file — useful for finding the connection-count knee on a
//     real backend.
//
// CLI:
//   tsx phone_call_burst.ts [--audio FILE] [--target localhost:50051]
//                           [--chunk-ms 100] [--duration 30]
//                           [-c|--concurrency 5] [--stagger-ms 500]
//                           [--scenario-id ID] [--csv PATH]

import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { Metadata, type ServiceError } from "@grpc/grpc-js";
import { DeepfakeDetectionClient } from "@aurigin/protos/aurigin/deepfake_detection/v1/deepfake_detection";

import {
  ResultCSV,
  type WavData,
  channelCredentials,
  durationS as wavDurationS,
  installSignalShutdown,
  readWav,
  transportLabel,
} from "./common/index.js";
// The per-call building blocks. `phone_call_burst` is "sendCall + recvCall
// instantiated N times over one channel" — importing keeps that
// relationship explicit and prevents drift between the two files' loops.
import { type Call, type ResponseSink, recvCall, sendCall } from "./phone_call.js";

const DEFAULT_CHUNK_MS = 100;
const DEFAULT_DURATION_S = 30;
const DEFAULT_CONCURRENCY = 1;

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

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

function makeLabels(n: number): string[] {
  const width = Math.max(2, String(n).length);
  return Array.from({ length: n }, (_, i) => `call-${String(i + 1).padStart(width, "0")}`);
}

interface CallResult {
  label: string;
  error: unknown | null;
}

async function runOneCall(
  client: DeepfakeDetectionClient,
  label: string,
  wav: WavData,
  chunkMs: number,
  durationS: number,
  metadata: Metadata | undefined,
  delayMs: number,
  csv: ResultCSV | null,
  fileName: string,
  registerCall: (call: Call) => void,
): Promise<CallResult> {
  // Stagger-driven delay before opening the stream — used by --stagger-ms
  // to spread call starts across wallclock.
  if (delayMs > 0) await sleep(delayMs);
  const call: Call = metadata ? client.detectDeepfake(metadata) : client.detectDeepfake();
  registerCall(call);
  const sink: ResponseSink = {
    sessionId: "", chunks: [], audioDurationMs: 0, globalResult: "unknown",
  };
  const tStart = performance.now();
  try {
    await Promise.all([
      sendCall(call, wav, chunkMs, durationS),
      recvCall(call, { label, sink }),
    ]);
    return { label, error: null };
  } catch (err) {
    return { label, error: err };
  } finally {
    if (csv) {
      csv.writeSession(
        fileName, sink.sessionId, sink.chunks,
        sink.audioDurationMs, sink.globalResult,
        performance.now() - tStart,
      );
    }
  }
}

// ─── argv parser ────────────────────────────────────────────────────────────

interface Args {
  audio: string | null;
  duration: number;
  chunkMs: number;
  target: string;
  concurrency: number;
  staggerMs: number;
  scenarioId: string | null;
  csv: string | null;
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
    csv: null,
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
      case "--csv": out.csv = next(); break;
      case "-h": case "--help":
        console.log("Usage: tsx phone_call_burst.ts [--audio FILE] [--duration SEC]");
        console.log("                               [--chunk-ms MS] [--target HOST:PORT]");
        console.log("                               [-c|--concurrency N] [--stagger-ms MS]");
        console.log("                               [--scenario-id ID] [--csv PATH]");
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

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const audioPath = resolveAudio(args.audio);
  const wav = readWav(audioPath);
  const labels = makeLabels(args.concurrency);

  const metadata = args.scenarioId ? new Metadata() : undefined;
  if (metadata && args.scenarioId) metadata.set("x-scenario-id", args.scenarioId);
  const scenarioSuffix = args.scenarioId ? ` | scenario=${args.scenarioId}` : "";
  const staggerSuffix = args.staggerMs ? ` | stagger=${args.staggerMs}ms` : "";

  const csv: ResultCSV | null = args.csv ? new ResultCSV(args.csv) : null;
  if (csv) console.error(`# csv=${args.csv}`);

  console.log(
    `📞 Calling ${args.target} | source=${path.basename(audioPath)} ` +
      `(${wavDurationS(wav).toFixed(2)}s @ ${wav.rate}Hz/${wav.channels}ch ${wav.wireFormat}) ` +
      `| duration=${args.duration.toFixed(1)}s | frame=${args.chunkMs}ms | ` +
      `concurrency=${args.concurrency}${staggerSuffix}${scenarioSuffix} ` +
      `| transport=${transportLabel("client")}`,
  );
  console.log("─".repeat(70));

  const activeCalls: Call[] = [];
  const shutdown = installSignalShutdown(activeCalls);

  const client = new DeepfakeDetectionClient(args.target, channelCredentials());
  let results: CallResult[];
  try {
    results = await Promise.all(
      labels.map((label, i) =>
        runOneCall(
          client, label, wav, args.chunkMs, args.duration,
          metadata, i * args.staggerMs, csv, path.basename(audioPath),
          (c) => activeCalls.push(c),
        ),
      ),
    );
  } finally {
    client.close();
    if (csv) await csv.close();
  }

  const failures = results.filter((r) => r.error !== null);
  if (shutdown.seen) {
    process.stderr.write("─".repeat(70) + "\n");
    process.stderr.write(
      `Shutdown complete (${args.concurrency - failures.length}/${args.concurrency} streams finished cleanly).\n`,
    );
    return;
  }
  console.log("─".repeat(70));
  const ok = args.concurrency - failures.length;
  console.log(`Summary: ${ok}/${args.concurrency} streams OK, ${failures.length} failed`);
  for (const { label, error } of failures) {
    const e = error as ServiceError | Error;
    const code = (e as ServiceError).code !== undefined ? `code=${(e as ServiceError).code} ` : "";
    console.error(`  [${label}] ${code}${e.message ?? String(e)}`);
  }
  if (failures.length > 0) process.exit(1);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
