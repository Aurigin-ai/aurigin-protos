// Minimal Fingerprint gRPC client using the generated @aurigin/protos package.
//
// If `examples/audio/` contains .wav files, opens one session per file
// and streams its PCM through `Fingerprint.ExtractFingerprint`. Otherwise
// streams 5 s of silence as a connectivity smoke-test.
//
// CLI:
//   npm run fingerprint-client -- [--target HOST:PORT]
//   tsx fingerprint_client.ts [--target HOST:PORT]

import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import {
  type ExtractFingerprintRequest,
  type ExtractFingerprintResponse,
  FingerprintClient,
} from "@aurigin/protos/aurigin/fingerprint/v1/fingerprint";
import { AudioCodec } from "@aurigin/protos/aurigin/media/v1/audio_frame";
import { channelCredentials, readWav, transportLabel, type WavData } from "./common/index.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const DEFAULT_RATE = 16000;
const CHANNELS = 1;
const CHUNK_MS = 500;
// 5000 ms of silence at 16 kHz — matches the fingerprint window so the
// fallback fires at least one EmbeddingResult in CI.
const SILENCE_CHUNKS = 10;

function* silentChunks(): Generator<ExtractFingerprintRequest> {
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

function* wavChunks(wav: WavData): Generator<ExtractFingerprintRequest> {
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
  client: FingerprintClient,
  iter: Iterable<ExtractFingerprintRequest>,
  label: string,
): Promise<void> {
  return new Promise((resolve, reject) => {
    console.log(`\n=== ${label} ===`);
    const call = client.extractFingerprint();
    call.on("data", (response: ExtractFingerprintResponse) => {
      if (response.createSessionResponse) {
        console.log(`Session: ${response.createSessionResponse.sessionId}`);
      } else if (response.embeddingResult) {
        const r = response.embeddingResult;
        // Hex preview of the first 8 bytes of the embedding — enough to
        // eyeball that the vector actually changed between windows without
        // dumping 3072 bytes to the terminal. The full embedding rides
        // `r.embedding` for callers to hash / base64 / Qdrant-insert.
        const head = Buffer.from(r.embedding.slice(0, 8)).toString("hex");
        console.log(
          `Embedding | offset=${r.audioOffsetMs}ms | duration=${r.durationMs}ms | ` +
            `code=${r.fingerprintCode} | dim=${r.dim} | head=${head}`,
        );
      } else if (response.finalResult) {
        const f = response.finalResult;
        console.log(`FINAL     | total=${f.totalAudioMs}ms | embeddings=${f.embeddingCount}`);
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

  const client = new FingerprintClient(target, channelCredentials());
  try {
    if (wavs.length === 0) {
      await runSession(client, silentChunks(), "silence (5 s @ 16 kHz)");
    } else {
      for (const wavPath of wavs) {
        // Pre-validate before opening the stream — same reasoning as
        // client.ts: a readWav throw inside the request generator after
        // the gRPC call has started surfaces as an opaque
        //   Error: 13 INTERNAL: Received RST_STREAM
        // Catching here yields a clean skip line + continues the dir scan.
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
