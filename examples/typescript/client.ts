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
import { channelCredentials, transportLabel } from "./tls.js";
import { type ChunkRow, ResultCSV } from "./result_csv.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const DEFAULT_RATE = 16000;
const CHANNELS = 1;
const CHUNK_MS = 500;
// 5 s — matches the dfs default analysis_interval_s=5.0 so the fallback
// fires at least one analysis window in CI.
const SILENCE_CHUNKS = 10;

// WAVE format tags. PCM 16-bit goes out as S16LE; IEEE float 32-bit as
// F32LE — matching the formats deepfake-service's audio decoder accepts.
const WAVE_FORMAT_PCM = 0x0001;
const WAVE_FORMAT_IEEE_FLOAT = 0x0003;

interface WavData {
  sampleRate: number;
  channels: number;
  samples: Buffer; // raw PCM bytes (post-header)
  wireFormat: "S16LE" | "F32LE";
  bytesPerSample: number; // per *frame* component, before channel multiplication
}

function readWav(filePath: string): WavData {
  const buf = fs.readFileSync(filePath);
  if (buf.toString("ascii", 0, 4) !== "RIFF" || buf.toString("ascii", 8, 12) !== "WAVE") {
    throw new Error(`${filePath}: not a RIFF/WAVE file`);
  }
  // Walk RIFF chunks to find fmt + data (handles non-canonical orderings).
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
    offset += 8 + size + (size & 1); // pad to even
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

function* wavChunks(file: string): Generator<DetectDeepfakeRequest> {
  const { sampleRate, channels, samples, wireFormat, bytesPerSample } = readWav(file);
  const framesPerChunk = Math.floor((sampleRate * CHUNK_MS) / 1000);
  const bytesPerFrame = bytesPerSample * channels;
  const bytesPerChunk = framesPerChunk * bytesPerFrame;
  yield { createSessionRequest: {} };
  let ptsNs = 0n;
  for (let i = 0; i < samples.length; i += bytesPerChunk) {
    const chunk = samples.subarray(i, Math.min(i + bytesPerChunk, samples.length));
    const actualFrames = chunk.length / bytesPerFrame;
    const durationNs = BigInt(Math.round((actualFrames / sampleRate) * 1e9));
    yield {
      audio: {
        type: "audio/x-raw", format: wireFormat,
        channels, rate: sampleRate,
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
      await runSession(client, silentChunks(), "silence (3 s @ 16 kHz)", csv);
    } else {
      for (const wav of wavs) {
        // Pre-validate before opening the stream. If readWav throws
        // (mislabeled .wav file, unsupported format, broken header), we'd
        // otherwise surface a cryptic
        //   Error: 13 INTERNAL: Received RST_STREAM
        // because the throw fires after the gRPC call has started. Catching
        // here lets us print a clear skip line and keep going through the dir.
        try {
          readWav(wav);
        } catch (err) {
          console.log(`\n=== ${path.basename(wav)} ===\nSKIPPED: ${(err as Error).message}`);
          continue;
        }
        await runSession(client, wavChunks(wav), path.basename(wav), csv);
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
