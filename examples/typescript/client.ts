// Minimal gRPC client using the generated @aurigin/protos package.
//
// If `examples/audio/` contains .wav files, opens one session per file
// and streams its PCM through DetectDeepfake. Otherwise streams 3 s of
// silence as a connectivity smoke-test.
//
// CLI:
//   npm run client -- [--target HOST:PORT]
//   tsx client.ts [--target HOST:PORT]

import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import {
  DeepfakeDetectionClient,
  type DetectDeepfakeRequest,
  type DetectDeepfakeResponse,
} from "@aurigin/protos/aurigin/deepfake_detection/v1/deepfake_detection";
import {
  type ChunkRow, ResultCSV, type WavData,
  channelCredentials, readWav, transportLabel,
} from "./common/index.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const DEFAULT_RATE = 16000;
const CHANNELS = 1;
const CHUNK_MS = 500;
// 5 s — matches the dfs default analysis_interval_s=5.0 so the fallback
// fires at least one analysis window in CI.
const SILENCE_CHUNKS = 10;

function* silentChunks(): Generator<DetectDeepfakeRequest> {
  yield { createSessionRequest: {} };
  let ptsNs = 0n;
  for (let i = 0; i < SILENCE_CHUNKS; i++) {
    const samples = Math.floor((DEFAULT_RATE * CHUNK_MS) / 1000);
    const chunk = Buffer.alloc(samples * CHANNELS * 2);
    const durationNs = BigInt(CHUNK_MS) * 1_000_000n;
    yield {
      audio: {
        type: "audio/x-raw", format: "S16LE",
        channels: CHANNELS, rate: DEFAULT_RATE,
        durationNs, ptsNs, size: BigInt(chunk.length), buffer: chunk,
      },
    };
    ptsNs += durationNs;
  }
}

function* wavChunks(wav: WavData): Generator<DetectDeepfakeRequest> {
  const framesPerChunk = Math.floor((wav.rate * CHUNK_MS) / 1000);
  const bytesPerFrame = wav.bytesPerSample * wav.channels;
  const bytesPerChunk = framesPerChunk * bytesPerFrame;
  yield { createSessionRequest: {} };
  let ptsNs = 0n;
  for (let i = 0; i < wav.samples.length; i += bytesPerChunk) {
    const chunk = wav.samples.subarray(i, Math.min(i + bytesPerChunk, wav.samples.length));
    const actualFrames = chunk.length / bytesPerFrame;
    const durationNs = BigInt(Math.round((actualFrames / wav.rate) * 1e9));
    yield {
      audio: {
        type: "audio/x-raw", format: wav.wireFormat,
        channels: wav.channels, rate: wav.rate,
        durationNs, ptsNs, size: BigInt(chunk.length), buffer: chunk,
      },
    };
    ptsNs += durationNs;
  }
}

function runSession(
  client: DeepfakeDetectionClient,
  iter: Iterable<DetectDeepfakeRequest>,
  label: string,
  csv: ResultCSV | null = null,
): Promise<void> {
  return new Promise((resolve, reject) => {
    console.log(`\n=== ${label} ===`);
    let sessionId = "";
    const chunks: ChunkRow[] = [];
    let audioDurationMs = 0;
    let globalResult = "unknown";
    // Wallclock from right before the bidi opens to FinalResult-received.
    // Captures network + server-side work + client-side iteration cost — the
    // "user-perceived" latency for processing this file.
    const tStart = performance.now();

    const call = client.detectDeepfake();
    call.on("data", (response: DetectDeepfakeResponse) => {
      if (response.createSessionResponse) {
        sessionId = response.createSessionResponse.sessionId;
        console.log(`Session: ${sessionId}`);
      } else if (response.analysisResult) {
        const r = response.analysisResult;
        chunks.push({
          offsetMs: Number(r.audioOffsetMs),
          durationMs: Number(r.durationMs),
          confidence: r.confidence,
          label: r.label,
        });
        console.log(`Analysis | offset=${r.audioOffsetMs}ms | score=${r.score.toFixed(3)} | label=${r.label} | confidence=${r.confidence.toFixed(2)}`);
      } else if (response.finalResult) {
        const f = response.finalResult;
        audioDurationMs = Number(f.totalAudioMs);
        globalResult = f.overallLabel;
        console.log(`FINAL    | total=${f.totalAudioMs}ms | score=${f.overallScore.toFixed(3)} | label=${f.overallLabel} | analyses=${f.analysisCount}`);
      }
    });
    call.on("end", () => {
      const processingTimeMs = performance.now() - tStart;
      if (csv) {
        csv.writeSession(
          label, sessionId, chunks, audioDurationMs, globalResult,
          processingTimeMs,
        );
      }
      resolve();
    });
    call.on("error", reject);
    for (const req of iter) call.write(req);
    call.end();
  });
}

function parseTarget(argv: string[]): string {
  const i = argv.indexOf("--target");
  return i >= 0 && i + 1 < argv.length ? argv[i + 1] : "localhost:50051";
}

function parseCsv(argv: string[]): string | null {
  const i = argv.indexOf("--csv");
  return i >= 0 && i + 1 < argv.length ? argv[i + 1] : null;
}

async function main() {
  const argv = process.argv.slice(2);
  const target = parseTarget(argv);
  const csvPath = parseCsv(argv);
  const audioDir = path.join(__dirname, "..", "audio");
  const wavs = fs.existsSync(audioDir)
    ? fs.readdirSync(audioDir).filter((f) => f.endsWith(".wav")).sort().map((f) => path.join(audioDir, f))
    : [];

  console.error(`# transport=${transportLabel("client")}`);

  let csv: ResultCSV | null = null;
  if (csvPath) {
    csv = new ResultCSV(csvPath);
    console.error(`# csv=${csvPath}`);
  }

  const client = new DeepfakeDetectionClient(target, channelCredentials());
  try {
    if (wavs.length === 0) {
      await runSession(client, silentChunks(), "silence (5 s @ 16 kHz)", csv);
    } else {
      for (const wavPath of wavs) {
        // Pre-validate before opening the stream. If readWav throws
        // (mislabeled .wav file, unsupported format, broken header), we'd
        // otherwise surface a cryptic
        //   Error: 13 INTERNAL: Received RST_STREAM
        // because the throw fires after the gRPC call has started. Catching
        // here lets us print a clear skip line and keep going through the dir.
        let wav: WavData;
        try {
          wav = readWav(wavPath);
        } catch (err) {
          console.log(`\n=== ${path.basename(wavPath)} ===\nSKIPPED: ${(err as Error).message}`);
          continue;
        }
        await runSession(client, wavChunks(wav), path.basename(wavPath), csv);
      }
    }
  } finally {
    client.close();
    if (csv) await csv.close();
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
