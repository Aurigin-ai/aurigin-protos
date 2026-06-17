// Simulate one or more mobile phone calls by streaming audio to DetectDeepfake.
//
// Default mode reads a WAV file (looped to fill --duration, paced in real
// time). With --fifo, reads from a named pipe instead — designed for a
// FreeSWITCH `record_session <fifo>` source feeding G.711 μ-law at 8 kHz.
//
// In both modes analysis events print as they arrive (the client's "data"
// listener fires concurrently with sends).
//
// With --concurrency N, opens N concurrent bidi streams over a single
// gRPC channel. Each stream gets a client-side label (`call-01`, `call-02`,
// ...) that prefixes every log line so interleaved output stays readable.
// FIFO mode is single-stream by nature and is rejected with --concurrency > 1.
//
// CLI:
//   # File mode (default)
//   tsx phone_call.ts [--audio path/to.wav] [--duration 30]
//                     [--chunk-ms 100] [--target localhost:50051]
//                     [--concurrency 1]
//
//   # FIFO mode
//   tsx phone_call.ts --fifo /var/lib/freeswitch/recordings/live.r16
//                     [--codec mulaw|pcm16] [--chunk-ms 100]
//                     [--target localhost:50051]
//
// Defaults:
//   - File mode: if --audio is omitted, picks the first .wav in
//     `examples/audio/` (gitignored — drop a fixture in).
//   - FIFO mode: --codec defaults to `mulaw`. `pcm16` skips the decode.

import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { credentials, type ClientDuplexStream } from "@grpc/grpc-js";
import {
  DeepfakeDetectionClient,
  type DetectDeepfakeRequest,
  type DetectDeepfakeResponse,
} from "@aurigin/protos/aurigin/deepfake_detection/v1/deepfake_detection";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const DEFAULT_CHUNK_MS = 100;
const DEFAULT_DURATION_S = 30;
const DEFAULT_CONCURRENCY = 1;
const FIFO_SAMPLE_RATE = 8000;
const FIFO_CHANNELS = 1;

type Codec = "mulaw" | "pcm16";

// ─── μ-law → S16LE lookup table (precomputed once at module load) ──────
const ULAW_TO_PCM16 = new Int16Array(256);
for (let i = 0; i < 256; i++) {
  const u = ~i & 0xff;
  const sign = u & 0x80;
  const exp = (u >> 4) & 0x07;
  const mant = u & 0x0f;
  let s = ((mant << 3) + 0x84) << exp;
  s -= 0x84;
  ULAW_TO_PCM16[i] = sign ? -s : s;
}

function ulawToPcm16(data: Buffer): Buffer {
  const out = Buffer.alloc(data.length * 2);
  for (let i = 0; i < data.length; i++) {
    out.writeInt16LE(ULAW_TO_PCM16[data[i]], i * 2);
  }
  return out;
}

// ─── Per-stream logger ────────────────────────────────────────────────
function log(label: string, message: string): void {
  console.log(`[${label}] ${message}`);
}

// ─── WAV reader (16-bit PCM only) ─────────────────────────────────────
interface WavData {
  sampleRate: number;
  channels: number;
  pcm: Buffer;
}

function readWav(filePath: string): WavData {
  const buf = fs.readFileSync(filePath);
  if (buf.toString("ascii", 0, 4) !== "RIFF" || buf.toString("ascii", 8, 12) !== "WAVE") {
    throw new Error(`${filePath}: not a RIFF/WAVE file`);
  }
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
    offset += 8 + size + (size & 1);
  }
  if (dataStart < 0) throw new Error(`${filePath}: no data chunk`);
  if (bitsPerSample !== 16) throw new Error(`${filePath}: expected 16-bit PCM, got ${bitsPerSample}-bit`);
  return { sampleRate, channels, pcm: buf.subarray(dataStart, dataStart + dataLen) };
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
        "Drop a 16-bit PCM WAV in examples/audio/ or pass one with --audio.",
    );
  }
  return path.join(audioDir, wavs[0]);
}

// ─── Senders ──────────────────────────────────────────────────────────

type Call = ClientDuplexStream<DetectDeepfakeRequest, DetectDeepfakeResponse>;

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

async function sendFile(
  call: Call,
  _label: string,
  wav: WavData,
  durationS: number,
  chunkMs: number,
): Promise<void> {
  const { sampleRate, channels, pcm } = wav;
  const bytesPerSample = 2 * channels;
  const bytesPerChunk = Math.max(1, Math.floor((sampleRate * chunkMs) / 1000) * bytesPerSample);
  const chunkS = chunkMs / 1000;

  call.write({ createSessionRequest: {} });

  let nextSend = Date.now();
  let ptsNs = 0n;
  let cursor = 0;
  let elapsedS = 0;

  while (elapsedS < durationS) {
    const end = Math.min(cursor + bytesPerChunk, pcm.length);
    const chunk = pcm.subarray(cursor, end);
    cursor = end;
    if (cursor >= pcm.length) cursor = 0; // loop the file
    const actualFrames = chunk.length / bytesPerSample;
    const durationNs = BigInt(Math.round((actualFrames / sampleRate) * 1e9));

    call.write({
      audio: {
        type: "audio/x-raw",
        format: "S16LE",
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

function sendFifo(
  call: Call,
  label: string,
  fifoPath: string,
  codec: Codec,
  chunkMs: number,
): Promise<void> {
  const samplesPerChunk = Math.floor((FIFO_SAMPLE_RATE * chunkMs) / 1000);
  const bytesPerInputChunk =
    codec === "mulaw" ? samplesPerChunk * FIFO_CHANNELS : samplesPerChunk * FIFO_CHANNELS * 2;

  call.write({ createSessionRequest: {} });

  log(label, `⏳ Waiting for writer on ${fifoPath} (open() blocks)...`);
  const stream = fs.createReadStream(fifoPath, { highWaterMark: bytesPerInputChunk });

  return new Promise<void>((resolve, reject) => {
    let ptsNs = 0n;
    let connected = false;

    stream.on("data", (data) => {
      if (!connected) {
        log(label, ">>> Writer connected, streaming audio");
        connected = true;
      }
      const buf = data as Buffer;
      const pcm = codec === "mulaw" ? ulawToPcm16(buf) : buf;
      const actualFrames = pcm.length / (2 * FIFO_CHANNELS);
      const durationNs = BigInt(Math.round((actualFrames / FIFO_SAMPLE_RATE) * 1e9));

      call.write({
        audio: {
          type: "audio/x-raw",
          format: "S16LE",
          channels: FIFO_CHANNELS,
          rate: FIFO_SAMPLE_RATE,
          durationNs,
          ptsNs,
          size: BigInt(pcm.length),
          buffer: pcm,
        },
      });
      ptsNs += durationNs;
    });

    stream.on("end", () => {
      log(label, "<<< FIFO closed by writer");
      call.end();
      resolve();
    });
    stream.on("error", reject);
  });
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
  fifo: string | null;
  codec: Codec;
  duration: number;
  chunkMs: number;
  target: string;
  concurrency: number;
}

function parseArgs(argv: string[]): Args {
  const out: Args = {
    audio: null,
    fifo: null,
    codec: "mulaw",
    duration: DEFAULT_DURATION_S,
    chunkMs: DEFAULT_CHUNK_MS,
    target: "localhost:50051",
    concurrency: DEFAULT_CONCURRENCY,
  };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    const next = () => argv[++i];
    switch (arg) {
      case "--audio": out.audio = next(); break;
      case "--fifo": out.fifo = next(); break;
      case "--codec": {
        const v = next();
        if (v !== "mulaw" && v !== "pcm16") throw new Error(`--codec must be mulaw|pcm16, got ${v}`);
        out.codec = v;
        break;
      }
      case "--duration": out.duration = Number(next()); break;
      case "--chunk-ms": out.chunkMs = Number(next()); break;
      case "--target": out.target = next(); break;
      case "-c": case "--concurrency": out.concurrency = Number(next()); break;
      case "-h": case "--help":
        console.log("Usage: tsx phone_call.ts [--audio FILE | --fifo PATH] [--codec mulaw|pcm16]");
        console.log("                         [--duration SEC] [--chunk-ms MS] [--target HOST:PORT]");
        console.log("                         [--concurrency N | -c N]");
        process.exit(0);
      default: throw new Error(`Unknown arg: ${arg}`);
    }
  }
  if (!Number.isInteger(out.concurrency) || out.concurrency < 1) {
    throw new Error("--concurrency must be a positive integer");
  }
  if (out.fifo && out.concurrency > 1) {
    throw new Error("--fifo is single-stream by nature; --concurrency > 1 is incompatible.");
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
  senderFactory: (call: Call, label: string) => Promise<void>,
): Promise<CallResult> {
  const call = client.detectDeepfake();
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
  const client = new DeepfakeDetectionClient(args.target, credentials.createInsecure());
  const labels = makeLabels(args.concurrency);

  let results: CallResult[];
  try {
    if (args.fifo) {
      console.log(
        `📞 Calling ${args.target} | source=${args.fifo} (FIFO, ${args.codec}, ` +
          `${FIFO_SAMPLE_RATE}Hz/${FIFO_CHANNELS}ch) | frame=${args.chunkMs}ms`,
      );
      console.log("─".repeat(70));
      const senderFactory = (call: Call, label: string) =>
        sendFifo(call, label, args.fifo!, args.codec, args.chunkMs);
      results = await Promise.all(labels.map((label) => runOneCall(client, label, senderFactory)));
    } else {
      const audioPath = resolveAudio(args.audio);
      const wav = readWav(audioPath);
      const fileDur = wav.pcm.length / (wav.sampleRate * 2 * wav.channels);
      console.log(
        `📞 Calling ${args.target} | source=${path.basename(audioPath)} ` +
          `(${fileDur.toFixed(2)}s @ ${wav.sampleRate}Hz/${wav.channels}ch) ` +
          `| duration=${args.duration.toFixed(1)}s | frame=${args.chunkMs}ms | ` +
          `concurrency=${args.concurrency}`,
      );
      console.log("─".repeat(70));
      const senderFactory = (call: Call, label: string) =>
        sendFile(call, label, wav, args.duration, args.chunkMs);
      results = await Promise.all(labels.map((label) => runOneCall(client, label, senderFactory)));
    }
  } finally {
    client.close();
  }

  const failures = results.filter((r) => r.error !== null);
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
