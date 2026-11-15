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
  type TranscriptionConfig,
  TranscriptionClient,
} from "@aurigin/protos/aurigin/asr/v1/asr";
import { Endpoint } from "@aurigin/protos/aurigin/common/v1/endpoint";
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

const ENDPOINT_BY_NAME: Record<string, Endpoint> = {
  near_end: Endpoint.ENDPOINT_NEAR_END,
  far_end: Endpoint.ENDPOINT_FAR_END,
  mixed: Endpoint.ENDPOINT_MIXED,
};

const ENDPOINT_LABEL: Record<number, string> = {
  [Endpoint.ENDPOINT_UNSPECIFIED]: "unspecified",
  [Endpoint.ENDPOINT_NEAR_END]: "near_end",
  [Endpoint.ENDPOINT_FAR_END]: "far_end",
  [Endpoint.ENDPOINT_MIXED]: "mixed",
};

function createSessionRequest(
  endpoint: Endpoint | null,
  trackId: string | null,
): { config?: TranscriptionConfig } {
  if (endpoint === null && !trackId) return {};
  const config: Partial<TranscriptionConfig> = {};
  if (endpoint !== null) config.endpoint = endpoint;
  if (trackId) config.trackId = trackId;
  return { config: config as TranscriptionConfig };
}

function attrSuffix(endpoint: number | undefined, trackId: string | undefined): string {
  const parts: string[] = [];
  if (endpoint && endpoint !== Endpoint.ENDPOINT_UNSPECIFIED) {
    parts.push(`ep=${ENDPOINT_LABEL[endpoint] ?? endpoint}`);
  }
  if (trackId) parts.push(`track=${trackId}`);
  return parts.length ? ` ${parts.join(" ")}` : "";
}

function* silentChunks(
  endpoint: Endpoint | null,
  trackId: string | null,
): Generator<TranscribeRequest> {
  yield { createSessionRequest: createSessionRequest(endpoint, trackId) };
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

function* wavChunks(
  wav: WavData,
  endpoint: Endpoint | null,
  trackId: string | null,
): Generator<TranscribeRequest> {
  const framesPerChunk = Math.floor((wav.rate * CHUNK_MS) / 1000);
  const bytesPerFrame = wav.bytesPerSample * wav.channels;
  const bytesPerChunk = framesPerChunk * bytesPerFrame;
  yield { createSessionRequest: createSessionRequest(endpoint, trackId) };
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
            `conf=${t.confidence.toFixed(2)} | silence=${t.silencePct.toFixed(1)}% |` +
            `${attrSuffix(t.endpoint, t.trackId)} ${JSON.stringify(t.text)}`,
        );
      } else if (response.finalResult) {
        const f = response.finalResult;
        console.log(
          `FINAL     | total=${f.totalAudioMs}ms | turns=${f.turnCount} | ` +
            `wall=${f.wallMs}ms | rtf=${f.rtf.toFixed(2)} | ` +
            `silence_mean=${f.silencePctMean.toFixed(1)}%` +
            attrSuffix(f.endpoint, f.trackId),
        );
      }
    });
    call.on("end", resolve);
    call.on("error", reject);
    for (const req of iter) call.write(req);
    call.end();
  });
}

function parseFlag(argv: string[], flag: string): string | null {
  const i = argv.indexOf(flag);
  return i >= 0 && i + 1 < argv.length ? argv[i + 1] : null;
}

async function main() {
  const argv = process.argv.slice(2);
  const target = parseFlag(argv, "--target") ?? "localhost:50051";
  const audioDir = parseFlag(argv, "--audio-dir") ?? path.join(__dirname, "..", "audio");
  const endpointName = parseFlag(argv, "--endpoint");
  const trackId = parseFlag(argv, "--track-id");

  let endpoint: Endpoint | null = null;
  if (endpointName !== null) {
    const ep = ENDPOINT_BY_NAME[endpointName];
    if (ep === undefined) {
      console.error(`unknown --endpoint ${endpointName}; choices: near_end, far_end, mixed`);
      process.exit(2);
    }
    endpoint = ep;
  }

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
      await runSession(
        client,
        silentChunks(endpoint, trackId),
        "silence (4 s @ 16 kHz)",
      );
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
        await runSession(client, wavChunks(wav, endpoint, trackId), path.basename(wavPath));
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
