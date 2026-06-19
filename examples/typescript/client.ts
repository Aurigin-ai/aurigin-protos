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

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const DEFAULT_RATE = 16000;
const CHANNELS = 1;
const CHUNK_MS = 500;
const SILENCE_CHUNKS = 6;

interface WavData {
  sampleRate: number;
  channels: number;
  pcm: Buffer; // S16LE PCM samples (post-header)
}

function readWav(filePath: string): WavData {
  const buf = fs.readFileSync(filePath);
  if (buf.toString("ascii", 0, 4) !== "RIFF" || buf.toString("ascii", 8, 12) !== "WAVE") {
    throw new Error(`${filePath}: not a RIFF/WAVE file`);
  }
  // Walk RIFF chunks to find fmt + data (handles non-canonical orderings).
  let offset = 12;
  let sampleRate = 0;
  let channels = 0;
  let bitsPerSample = 0;
  let dataStart = -1;
  let dataLen = 0;
  while (offset + 8 <= buf.length) {
    const id = buf.toString("ascii", offset, offset + 4);
    const size = buf.readUInt32LE(offset + 4);
    if (id === "fmt ") {
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
  if (bitsPerSample !== 16) throw new Error(`${filePath}: expected 16-bit PCM, got ${bitsPerSample}-bit`);
  return { sampleRate, channels, pcm: buf.subarray(dataStart, dataStart + dataLen) };
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
  const { sampleRate, channels, pcm } = readWav(file);
  const framesPerChunk = Math.floor((sampleRate * CHUNK_MS) / 1000);
  const bytesPerChunk = framesPerChunk * channels * 2;
  yield { createSessionRequest: {} };
  let ptsNs = 0n;
  for (let i = 0; i < pcm.length; i += bytesPerChunk) {
    const chunk = pcm.subarray(i, Math.min(i + bytesPerChunk, pcm.length));
    const actualFrames = chunk.length / (channels * 2);
    const durationNs = BigInt(Math.round((actualFrames / sampleRate) * 1e9));
    yield {
      audio: {
        type: "audio/x-raw", format: "S16LE",
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
): Promise<void> {
  return new Promise((resolve, reject) => {
    console.log(`\n=== ${label} ===`);
    const call = client.detectDeepfake();
    call.on("data", (response: DetectDeepfakeResponse) => {
      if (response.createSessionResponse) {
        console.log(`Session: ${response.createSessionResponse.sessionId}`);
      } else if (response.analysisResult) {
        const r = response.analysisResult;
        console.log(`Analysis | offset=${r.audioOffsetMs}ms | score=${r.score.toFixed(3)} | label=${r.label} | confidence=${r.confidence.toFixed(2)}`);
      } else if (response.finalResult) {
        const f = response.finalResult;
        console.log(`FINAL    | total=${f.totalAudioMs}ms | score=${f.overallScore.toFixed(3)} | label=${f.overallLabel} | analyses=${f.analysisCount}`);
      }
    });
    call.on("end", () => resolve());
    call.on("error", reject);
    for (const req of iter) call.write(req);
    call.end();
  });
}

function parseTarget(argv: string[]): string {
  const i = argv.indexOf("--target");
  return i >= 0 && i + 1 < argv.length ? argv[i + 1] : "localhost:50051";
}

async function main() {
  const target = parseTarget(process.argv.slice(2));
  const audioDir = path.join(__dirname, "..", "audio");
  const wavs = fs.existsSync(audioDir)
    ? fs.readdirSync(audioDir).filter((f) => f.endsWith(".wav")).sort().map((f) => path.join(audioDir, f))
    : [];

  console.error(`# transport=${transportLabel("client")}`);
  const client = new DeepfakeDetectionClient(target, channelCredentials());
  try {
    if (wavs.length === 0) {
      await runSession(client, silentChunks(), "silence (3 s @ 16 kHz)");
    } else {
      for (const wav of wavs) {
        await runSession(client, wavChunks(wav), path.basename(wav));
      }
    }
  } finally {
    client.close();
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
