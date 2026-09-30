// Minimal Replay-Detection gRPC client using the generated @aurigin/protos package.
//
// If `examples/audio/` contains .wav files, opens one session per file
// and streams its PCM through `ReplayDetection.DetectReplay`. Otherwise
// streams 3 s of silence as a connectivity smoke-test (still emits one
// AnalysisResult against a 3000 ms window — the halo-3 training length).
//
// CLI:
//   npm run replay-client -- [--target HOST:PORT]
//   tsx replay_client.ts [--target HOST:PORT]

import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { AudioCodec } from "@aurigin/protos/aurigin/media/v1/audio_frame";
import {
  type DetectReplayRequest,
  type DetectReplayResponse,
  ReplayDetectionClient,
} from "@aurigin/protos/aurigin/replay_detection/v1/replay_detection";
import { channelCredentials, readWav, transportLabel, type WavData } from "./common/index.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const DEFAULT_RATE = 16000;
const CHANNELS = 1;
const CHUNK_MS = 500;
// 3000 ms of silence at 16 kHz — matches the halo-3 replay-detector
// training window so the fallback fires at least one AnalysisResult in CI.
const SILENCE_CHUNKS = 6;

function* silentChunks(): Generator<DetectReplayRequest> {
  yield { createSessionRequest: {} };
  let ptsNs = 0n;
  for (let i = 0; i < SILENCE_CHUNKS; i++) {
    const samples = Math.floor((DEFAULT_RATE * CHUNK_MS) / 1000);
    const chunk = Buffer.alloc(samples * CHANNELS * 2);
    yield {
      audioFrame: {
        codec: AudioCodec.AUDIO_CODEC_S16LE,
        sampleRateHz: DEFAULT_RATE,
        channels: CHANNELS,
        payload: chunk,
        ptsNs,
      },
    };
    ptsNs += BigInt(CHUNK_MS) * 1_000_000n;
  }
}

function* wavChunks(wav: WavData): Generator<DetectReplayRequest> {
  const framesPerChunk = Math.floor((wav.rate * CHUNK_MS) / 1000);
  const bytesPerFrame = wav.bytesPerSample * wav.channels;
  const bytesPerChunk = framesPerChunk * bytesPerFrame;
  yield { createSessionRequest: {} };
  let ptsNs = 0n;
  for (let i = 0; i < wav.samples.length; i += bytesPerChunk) {
    const chunk = wav.samples.subarray(i, Math.min(i + bytesPerChunk, wav.samples.length));
    const actualFrames = chunk.length / bytesPerFrame;
    yield {
      audioFrame: {
        codec: wav.audioCodec,
        sampleRateHz: wav.rate,
        channels: wav.channels,
        payload: chunk,
        ptsNs,
      },
    };
    ptsNs += BigInt(Math.round((actualFrames / wav.rate) * 1e9));
  }
}

function runSession(
  client: ReplayDetectionClient,
  iter: Iterable<DetectReplayRequest>,
  label: string,
): Promise<void> {
  return new Promise((resolve, reject) => {
    console.log(`\n=== ${label} ===`);
    const call = client.detectReplay();
    call.on("data", (response: DetectReplayResponse) => {
      if (response.createSessionResponse) {
        console.log(`Session: ${response.createSessionResponse.sessionId}`);
      } else if (response.analysisResult) {
        const r = response.analysisResult;
        console.log(
          `Analysis | offset=${r.audioOffsetMs}ms | duration=${r.durationMs}ms | ` +
            `score=${r.score.toFixed(3)} | label=${r.labelRaw || r.label} | ` +
            `confidence=${r.confidence.toFixed(2)}`,
        );
      } else if (response.finalResult) {
        const f = response.finalResult;
        console.log(
          `FINAL    | total=${f.totalAudioMs}ms | score=${f.overallScore.toFixed(3)} | ` +
            `label=${f.overallLabelRaw || f.overallLabel} | analyses=${f.analysisCount}`,
        );
      }
    });
    call.on("end", resolve);
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
  const argv = process.argv.slice(2);
  const target = parseTarget(argv);
  const audioDir = path.join(__dirname, "..", "audio");
  const wavs = fs.existsSync(audioDir)
    ? fs
        .readdirSync(audioDir)
        .filter((f) => f.endsWith(".wav"))
        .sort()
        .map((f) => path.join(audioDir, f))
    : [];

  console.error(`# transport=${transportLabel()}`);

  const client = new ReplayDetectionClient(target, channelCredentials());
  try {
    if (wavs.length === 0) {
      await runSession(client, silentChunks(), "silence (3 s @ 16 kHz)");
    } else {
      for (const wavPath of wavs) {
        // Pre-validate before opening the stream — same reasoning as
        // client.ts / fingerprint_client.ts: a readWav throw inside the
        // request generator after the gRPC call has started surfaces as
        // an opaque `Error: 13 INTERNAL: Received RST_STREAM`. Catching
        // here yields a clean skip line + continues the dir scan.
        let wav: WavData;
        try {
          wav = readWav(wavPath);
        } catch (err) {
          console.log(`\n=== ${path.basename(wavPath)} ===\nSKIPPED: ${(err as Error).message}`);
          continue;
        }
        await runSession(client, wavChunks(wav), path.basename(wavPath));
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
