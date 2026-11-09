// Minimal Transcription gRPC client using the generated @aurigin/protos package.
//
// If `examples/audio/` contains .wav files, opens one session per file
// and streams its PCM through `Transcription.Transcribe`. Otherwise
// streams 4 s of silence as a connectivity smoke-test (produces 2
// synthetic TranscriptTurns against the simulator's default 2000 ms
// turn length).
//
// CLI:
//   npm run asr-client -- [--target HOST:PORT] [--audio-dir DIR]
//   tsx asr_client.ts [--target HOST:PORT] [--audio-dir DIR]

import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import {
  type TranscribeRequest,
  type TranscribeResponse,
  TranscriptionClient,
} from "@aurigin/protos/aurigin/asr/v1/asr";
import { AudioCodec } from "@aurigin/protos/aurigin/media/v1/audio_frame";
import { channelCredentials, readWav, transportLabel, type WavData } from "./common/index.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const DEFAULT_RATE = 16000;
const CHANNELS = 1;
const CHUNK_MS = 500;
// 4000 ms of silence at 16 kHz — enough to produce 2 TranscriptTurns at
// the simulator's default 2000 ms turn length, so CI sees the per-turn
// path exercised at least twice before FinalResult.
const SILENCE_CHUNKS = 8;

function* silentChunks(): Generator<TranscribeRequest> {
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

function* wavChunks(wav: WavData): Generator<TranscribeRequest> {
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
  client: TranscriptionClient,
  iter: Iterable<TranscribeRequest>,
  label: string,
): Promise<void> {
  return new Promise((resolve, reject) => {
    console.log(`\n=== ${label} ===`);
    const call = client.transcribe();
    call.on("data", (response: TranscribeResponse) => {
      if (response.createSessionResponse) {
        const ack = response.createSessionResponse;
        console.log(`Session: ${ack.sessionId}`);
        if (ack.modelName || ack.modelVersion) {
          console.log(`Model:   ${ack.modelName || "?"}@${ack.modelVersion || "?"}`);
        }
      } else if (response.transcriptTurn) {
        const t = response.transcriptTurn;
        console.log(
          `Turn ${String(t.turn).padStart(3)} | ` +
            `${String(t.t0Ms).padStart(6)}-${String(t.t1Ms).padEnd(6)}ms | ` +
            `conf=${t.confidence.toFixed(2)} | silence=${t.silencePct.toFixed(1)}% | ` +
            `${JSON.stringify(t.text)}`,
        );
      } else if (response.finalResult) {
        const f = response.finalResult;
        console.log(
          `FINAL     | total=${f.totalAudioMs}ms | turns=${f.turnCount} | ` +
            `wall=${f.wallMs}ms | rtf=${f.rtf.toFixed(2)} | ` +
            `silence_mean=${f.silencePctMean.toFixed(1)}%`,
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

function parseAudioDir(argv: string[]): string {
  const i = argv.indexOf("--audio-dir");
  return i >= 0 && i + 1 < argv.length ? argv[i + 1] : path.join(__dirname, "..", "audio");
}

async function main() {
  const argv = process.argv.slice(2);
  const target = parseTarget(argv);
  const audioDir = parseAudioDir(argv);
  const wavs = fs.existsSync(audioDir)
    ? fs
        .readdirSync(audioDir)
        .filter((f) => f.endsWith(".wav"))
        .sort()
        .map((f) => path.join(audioDir, f))
    : [];

  console.error(`# transport=${transportLabel()}`);

  const client = new TranscriptionClient(target, channelCredentials());
  try {
    if (wavs.length === 0) {
      await runSession(client, silentChunks(), "silence (4 s @ 16 kHz)");
    } else {
      for (const wavPath of wavs) {
        // Pre-validate before opening the stream — same reasoning as
        // client.ts / fingerprint_client.ts / replay_client.ts: a readWav
        // throw inside the request generator after the gRPC call has
        // started surfaces as an opaque `Error: 13 INTERNAL: Received
        // RST_STREAM`. Catching here yields a clean skip line + continues
        // the dir scan.
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
