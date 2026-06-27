// Single live phone call → DetectDeepfake.
//
// A minimal worked example of the **integration pattern** for a real-time
// audio source (FreeSWITCH `mod_audio_fork`, Twilio Media Streams, a SIPREC
// tap, etc.) piped over a gRPC bidi stream. Reads a WAV file from disk and
// streams its PCM in real time so the example is self-contained; in a
// production integration you'd replace the WAV reader with your media-fork
// socket reader — the rest of the file (bidi setup, paced send loop,
// concurrent response reader) stays the same.
//
// For load-testing N concurrent calls against a real backend (the
// recommended multi-call architecture — finding the connection-count
// knee, comparing fp16/fp32 throughput, etc.), see the sibling
// `phone_call_burst.ts`.
//
// CLI:
//   tsx phone_call.ts [--audio FILE] [--target localhost:50051]
//                     [--chunk-ms 100] [--duration 30]
//                     [--scenario-id ID]
//
// Defaults:
//   - if --audio is omitted, picks the first .wav in `examples/audio/`
//     (gitignored — drop a fixture in).

import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import {
  Metadata,
  type ClientDuplexStream,
} from "@grpc/grpc-js";
import {
  DeepfakeDetectionClient,
  type DetectDeepfakeRequest,
  type DetectDeepfakeResponse,
} from "@aurigin/protos/aurigin/deepfake_detection/v1/deepfake_detection";

import {
  type ChunkRow,
  type WavData,
  channelCredentials,
  durationS as wavDurationS,
  installSignalShutdown,
  readWav,
  transportLabel,
} from "./common/index.js";

const DEFAULT_CHUNK_MS = 100;
const DEFAULT_DURATION_S = 30;

const __dirname = path.dirname(fileURLToPath(import.meta.url));

export type Call = ClientDuplexStream<DetectDeepfakeRequest, DetectDeepfakeResponse>;

export interface ResponseSink {
  sessionId: string;
  chunks: ChunkRow[];
  audioDurationMs: number;
  globalResult: string;
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

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

// Stream `wav` in real-time-paced AudioBuffer chunks until `durationS` is up.
//
// Also imported by phone_call_burst.ts — same loop, just instantiated N times.
//
// THIS LOOP IS THE INTEGRATION PATTERN. For a FreeSWITCH `mod_audio_fork`
// or Twilio Media Stream, replace the `samples.subarray(cursor, …)` slicing
// with a `for await (const frame of forkSocket) { call.write(...) }` —
// no manual setTimeout pacing needed in that version, the socket IS the
// clock. We loop a finite WAV here just so the example self-contains.
export async function sendCall(
  call: Call, wav: WavData, chunkMs: number, durationS: number,
): Promise<void> {
  const bytesPerFrame = wav.bytesPerSample * wav.channels;
  const bytesPerChunk = Math.max(1, Math.floor((wav.rate * chunkMs) / 1000) * bytesPerFrame);
  const chunkS = chunkMs / 1000;

  call.write({ createSessionRequest: {} });

  let nextSend = Date.now();
  let ptsNs = 0n;
  let cursor = 0;
  let elapsedS = 0;

  while (elapsedS < durationS) {
    const end = Math.min(cursor + bytesPerChunk, wav.samples.length);
    const chunk = wav.samples.subarray(cursor, end);
    cursor = end;
    if (cursor >= wav.samples.length) cursor = 0;  // loop the file
    const actualFrames = chunk.length / bytesPerFrame;
    const durationNs = BigInt(Math.round((actualFrames / wav.rate) * 1e9));

    call.write({
      audio: {
        type: "audio/x-raw", format: wav.wireFormat,
        channels: wav.channels, rate: wav.rate,
        durationNs, ptsNs, size: BigInt(chunk.length), buffer: chunk,
      },
    });
    ptsNs += durationNs;
    elapsedS += chunkS;

    // Deadline-based wallclock pacing — drift-free: if a single write
    // blocks longer than chunkMs, the next sleepFor shrinks rather than
    // compounding.
    nextSend += chunkMs;
    const sleepFor = nextSend - Date.now();
    if (sleepFor > 0) await sleep(sleepFor);
  }

  call.end();
}

// Receive every server response as it arrives. Runs concurrently with the
// sender via Promise.all — that's the other half of the bidi pattern.
//
// Optional `label` / `sink` exist so phone_call_burst.ts can reuse this
// same function: `label` prefixes every log line with the per-stream id,
// and `sink` captures session_id + chunks + final aggregates so the
// caller can flush a CSV row block after the call ends.
export function recvCall(
  call: Call,
  opts: { label?: string; sink?: ResponseSink } = {},
): Promise<void> {
  const { label, sink } = opts;
  const log = (message: string): void => {
    console.log(label ? `[${label}] ${message}` : message);
  };
  return new Promise<void>((resolve, reject) => {
    call.on("data", (response: DetectDeepfakeResponse) => {
      if (response.createSessionResponse) {
        const sessionId = response.createSessionResponse.sessionId;
        if (sink) sink.sessionId = sessionId;
        log(`📞 Session: ${sessionId}`);
      } else if (response.analysisResult) {
        const r = response.analysisResult;
        if (sink) {
          sink.chunks.push({
            offsetMs: Number(r.audioOffsetMs),
            durationMs: Number(r.durationMs),
            confidence: r.confidence,
            label: r.label,
          });
        }
        const offsetS = (Number(r.audioOffsetMs) / 1000).toFixed(2).padStart(6);
        log(
          `   Analysis @ ${offsetS}s | score=${r.score.toFixed(3)} | ` +
            `label=${r.label.padEnd(18)} | confidence=${r.confidence.toFixed(2)}`,
        );
      } else if (response.finalResult) {
        const f = response.finalResult;
        if (sink) {
          sink.audioDurationMs = Number(f.totalAudioMs);
          sink.globalResult = f.overallLabel;
        }
        log(
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

// ─── argv parser ────────────────────────────────────────────────────────────

interface Args {
  audio: string | null;
  duration: number;
  chunkMs: number;
  target: string;
  scenarioId: string | null;
}

function parseArgs(argv: string[]): Args {
  const out: Args = {
    audio: null,
    duration: DEFAULT_DURATION_S,
    chunkMs: DEFAULT_CHUNK_MS,
    target: "localhost:50051",
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
      case "--scenario-id": out.scenarioId = next(); break;
      case "-h": case "--help":
        console.log("Usage: tsx phone_call.ts [--audio FILE] [--duration SEC]");
        console.log("                         [--chunk-ms MS] [--target HOST:PORT]");
        console.log("                         [--scenario-id ID]");
        process.exit(0);
      default: throw new Error(`Unknown arg: ${arg}`);
    }
  }
  return out;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const audioPath = resolveAudio(args.audio);
  const wav = readWav(audioPath);

  const metadata = args.scenarioId ? new Metadata() : undefined;
  if (metadata && args.scenarioId) metadata.set("x-scenario-id", args.scenarioId);
  const scenarioSuffix = args.scenarioId ? ` | scenario=${args.scenarioId}` : "";

  console.log(
    `📞 Calling ${args.target} | source=${path.basename(audioPath)} ` +
      `(${wavDurationS(wav).toFixed(2)}s @ ${wav.rate}Hz/${wav.channels}ch ${wav.wireFormat}) ` +
      `| duration=${args.duration.toFixed(1)}s | frame=${args.chunkMs}ms` +
      `${scenarioSuffix} | transport=${transportLabel("client")}`,
  );
  console.log("─".repeat(70));

  const client = new DeepfakeDetectionClient(args.target, channelCredentials());
  try {
    const call: Call = metadata ? client.detectDeepfake(metadata) : client.detectDeepfake();

    // Graceful Ctrl-C: cancel the in-flight stream cleanly. installSignalShutdown
    // is the same helper phone_call_burst.ts uses for its N-call list.
    installSignalShutdown([call]);

    // Send + receive concurrently: this is the bidi pattern. sendCall
    // writes AudioBuffer messages at real-time pace; recvCall reads
    // AnalysisResult / FinalResult messages as the server emits them.
    await Promise.all([sendCall(call, wav, args.chunkMs, args.duration), recvCall(call)]);
  } finally {
    client.close();
  }
}

// Only run main() when this file is the process entry point. Without this
// guard, importing send_call / recv_call from phone_call_burst.ts would also
// trigger main() (the TS equivalent of Python's `if __name__ == "__main__":`).
if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => {
    console.error(err);
    process.exit(1);
  });
}
