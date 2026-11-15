// Minimal AudioVerification.Stream client — SDK smoke-test target.
//
// Opens one bidi session against `aurigin.client.v1.AudioVerification.Stream`,
// streams audio for a configurable duration, and prints every `Verdict` /
// `EmbeddingVerdict` / `TranscriptTurn` / `FraudVerdict` / `FinalResult`
// the server responds with.
//
// Source of audio (in priority order):
//   * Any 2-or-3 of `--near-end-file` / `--far-end-file` / `--mixed-file` —
//     stream the WAVs interleaved on one session, each frame tagged
//     with its endpoint via ClientAudioFrame. Covers every per-leg
//     scenario (NEAR+FAR 1-on-1, NEAR+MIXED Teams-style,
//     NEAR+FAR+MIXED conference).
//   * `--audio-file PATH`  → the WAV at PATH (S16LE / F32LE / PCMU / PCMA).
//                             `--duration` is ignored.
//   * default              → synthesised silence (`--duration`).
//
// Endpoint attribution:
//   * `--endpoint {near_end,far_end,mixed}` — tag every frame with this
//     endpoint (uses the ClientAudioFrame wrapper variant).
//   * `--track-id STR` — opaque, stable per session; surfaces on every
//     Verdict / EmbeddingVerdict / TranscriptTurn / FraudVerdict.
//   * With no `--endpoint` / `--track-id` / dual-file flags, falls back
//     to the raw AudioFrame wire shape (backward-compatible path).
//
// Points at the orchestrator simulator by default (`localhost:50053`).
// Override `--target` to hit any AudioVerification server.
//
// Auth is MANDATORY — the SDK-facing gRPC surface expects
// `authorization: Bearer <token>` in metadata.
//
// TLS: `--tls auto` (default) picks insecure for `localhost:*` and any
// `:80` target, secure for everything else.
//
// CLI:
//   tsx orchestrator_client.ts --token sk_test_aurigin_sim_demo_0000000000000000
//   tsx orchestrator_client.ts --token <jwt> --target host.example:443 \
//       --audio-file ../audio/922.wav --endpoint far_end
//   tsx orchestrator_client.ts --token <jwt> --target host.example:443 \
//       --near-end-file mic.wav --far-end-file speaker.wav --track-id demo-call
//   tsx orchestrator_client.ts --token <jwt> --target host.example:443 \
//       --near-end-file mic.wav --mixed-file conf_mix.wav --track-id teams-call

import { Metadata, credentials } from "@grpc/grpc-js";
import { AudioVerificationClient } from "@aurigin/protos/aurigin/client/v1/audio_verification";
import { SessionType } from "@aurigin/protos/aurigin/common/v1/session";
import { Endpoint } from "@aurigin/protos/aurigin/common/v1/endpoint";
import { AudioCodec } from "@aurigin/protos/aurigin/media/v1/audio_frame";
import { readWav, type WavData } from "./common/index.js";

// Same demo token the orchestrator simulator ships with — kept here so
// `tsx orchestrator_client.ts` (no flags) still works against a
// fresh `docker compose up` of the sim.
const DEMO_API_KEY = "sk_test_aurigin_sim_demo_0000000000000000";

const RATE = 16000;
const CHANNELS = 1;
const CHUNK_MS = 100;
const BYTES_PER_SAMPLE = 2; // S16LE
const SAMPLES_PER_CHUNK = Math.floor((RATE * CHUNK_MS) / 1000);

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

interface Args {
  target: string;
  token: string;
  duration: number;
  tls: "auto" | "always" | "never";
  audioFile: string | null;
  nearEndFile: string | null;
  farEndFile: string | null;
  mixedFile: string | null;
  endpoint: Endpoint | null;
  trackId: string | null;
}

function parseArgs(): Args {
  const argv = process.argv.slice(2);
  const args: Args = {
    target: "localhost:50053",
    token: DEMO_API_KEY,
    duration: 15,
    tls: "auto",
    audioFile: null,
    nearEndFile: null,
    farEndFile: null,
    mixedFile: null,
    endpoint: null,
    trackId: null,
  };
  for (let i = 0; i < argv.length; i++) {
    const flag = argv[i];
    const next = argv[i + 1];
    if (flag === "--target" && next) { args.target = next; i++; }
    else if (flag === "--token" && next) { args.token = next; i++; }
    else if (flag === "--duration" && next) { args.duration = Number(next); i++; }
    else if (flag === "--tls" && (next === "auto" || next === "always" || next === "never")) {
      args.tls = next; i++;
    }
    else if (flag === "--audio-file" && next) { args.audioFile = next; i++; }
    else if (flag === "--near-end-file" && next) { args.nearEndFile = next; i++; }
    else if (flag === "--far-end-file" && next) { args.farEndFile = next; i++; }
    else if (flag === "--mixed-file" && next) { args.mixedFile = next; i++; }
    else if (flag === "--endpoint" && next) {
      const ep = ENDPOINT_BY_NAME[next];
      if (ep === undefined) {
        console.error(`unknown --endpoint ${next}; choices: near_end, far_end, mixed`);
        process.exit(2);
      }
      args.endpoint = ep; i++;
    }
    else if (flag === "--track-id" && next) { args.trackId = next; i++; }
  }

  // Multi-file mode (any 2 or 3 of near/far/mixed) is mutually
  // exclusive with single-source flags.
  const multiCount = [args.nearEndFile, args.farEndFile, args.mixedFile]
    .filter((f) => f !== null).length;
  if (multiCount === 1) {
    console.error(
      "--near-end-file / --far-end-file / --mixed-file need at least two of " +
      "them together — a single endpoint input belongs on --audio-file (with --endpoint)",
    );
    process.exit(2);
  }
  if (multiCount >= 2 && args.audioFile) {
    console.error("multi-file mode conflicts with --audio-file");
    process.exit(2);
  }
  if (multiCount >= 2 && args.endpoint !== null) {
    console.error("--endpoint is implied by multi-file mode; don't set both");
    process.exit(2);
  }

  return args;
}

// `always` / `never` are explicit; `auto` picks insecure for localhost
// or any `:80` target, secure for everything else.
function useTls(target: string, mode: Args["tls"]): boolean {
  if (mode === "always") return true;
  if (mode === "never") return false;
  const idx = target.lastIndexOf(":");
  const host = idx >= 0 ? target.slice(0, idx) : target;
  const port = idx >= 0 ? target.slice(idx + 1) : "";
  if (host === "localhost" || host === "127.0.0.1" || host === "::1" || port === "80") return false;
  return true;
}

// Build a StreamRequest for one audio frame. Switches between raw
// AudioFrame and ClientAudioFrame depending on whether any attribution
// was supplied — so the no-flag path stays wire-identical to pre-0.3.8.
function makeFrameRequest(
  codec: AudioCodec,
  rate: number,
  channels: number,
  payload: Buffer,
  ptsNs: bigint,
  endpoint: Endpoint | null,
  trackId: string | null,
): object {
  const frame = {
    codec,
    sampleRateHz: rate,
    channels,
    payload,
    ptsNs,
  };
  if (endpoint === null && !trackId) {
    return { audioFrame: frame };
  }
  const clientAudioFrame: Record<string, unknown> = { frame };
  if (endpoint !== null) clientAudioFrame.endpoint = endpoint;
  if (trackId) clientAudioFrame.trackId = trackId;
  return { clientAudioFrame };
}

function attrSuffix(endpoint: number | undefined, trackId: string | undefined): string {
  const parts: string[] = [];
  if (endpoint && endpoint !== Endpoint.ENDPOINT_UNSPECIFIED) {
    parts.push(`endpoint=${ENDPOINT_LABEL[endpoint] ?? endpoint}`);
  }
  if (trackId) parts.push(`track=${trackId}`);
  return parts.length ? ` ${parts.join(" ")}` : "";
}

interface WavChunk { payload: Buffer; ptsNs: bigint }

function chunkWav(wav: WavData): WavChunk[] {
  const framesPerChunk = Math.floor((wav.rate * CHUNK_MS) / 1000);
  const bytesPerFrame = wav.bytesPerSample * wav.channels;
  const bytesPerChunk = framesPerChunk * bytesPerFrame;
  const chunks: WavChunk[] = [];
  let ptsNs = 0n;
  for (let i = 0; i < wav.samples.length; i += bytesPerChunk) {
    const payload = wav.samples.subarray(i, Math.min(i + bytesPerChunk, wav.samples.length));
    if (payload.length === 0) break;
    chunks.push({ payload, ptsNs });
    const actualFrames = payload.length / bytesPerFrame;
    ptsNs += BigInt(Math.round((actualFrames / wav.rate) * 1e9));
  }
  return chunks;
}

async function run(args: Args): Promise<void> {
  const tls = useTls(args.target, args.tls);
  // Pre-read WAVs so bad paths fail BEFORE opening the stream.
  const multiSources: Array<[Endpoint, WavData]> = [];
  if (args.nearEndFile) multiSources.push([Endpoint.ENDPOINT_NEAR_END, readWav(args.nearEndFile)]);
  if (args.farEndFile)  multiSources.push([Endpoint.ENDPOINT_FAR_END,  readWav(args.farEndFile)]);
  if (args.mixedFile)   multiSources.push([Endpoint.ENDPOINT_MIXED,    readWav(args.mixedFile)]);
  const singleWav = args.audioFile ? readWav(args.audioFile) : null;

  const src = multiSources.length >= 2
    ? `multi=${[args.nearEndFile, args.farEndFile, args.mixedFile].filter(Boolean).join("+")}`
    : args.audioFile ? `file=${args.audioFile}` : `duration=${args.duration}s`;
  console.log(
    `# target=${args.target} tls=${tls} ${src} ` +
    `token_prefix=${args.token.slice(0, 10)}… ` +
    `endpoint=${args.endpoint !== null ? ENDPOINT_LABEL[args.endpoint] : "untagged"} ` +
    `track_id=${args.trackId ?? "(none)"}`
  );

  const client = new AudioVerificationClient(
    args.target,
    tls ? credentials.createSsl() : credentials.createInsecure(),
  );

  const meta = new Metadata();
  meta.set("authorization", `Bearer ${args.token}`);

  const call = client.stream(meta);

  // Print every response as it arrives.
  const done = new Promise<void>((resolve, reject) => {
    call.on("data", (resp) => {
      if (resp.createSessionResponse) {
        console.log(`SESSION | id=${resp.createSessionResponse.sessionId}`);
      } else if (resp.verdict) {
        const v = resp.verdict;
        console.log(
          `VERDICT | consumer=${v.consumerName} offset=${v.audioOffsetMs}ms ` +
          `label=${v.labelRaw || v.label} score=${v.score.toFixed(3)} ` +
          `confidence=${v.confidence.toFixed(3)}` +
          attrSuffix(v.endpoint, v.trackId)
        );
      } else if (resp.embeddingVerdict) {
        const e = resp.embeddingVerdict;
        console.log(
          `EMBED   | consumer=${e.consumerName} offset=${e.audioOffsetMs}ms ` +
          `dim=${e.dim} fingerprint=${e.fingerprintCode}` +
          attrSuffix(e.endpoint, e.trackId)
        );
      } else if (resp.transcriptTurn) {
        const t = resp.transcriptTurn;
        console.log(
          `TURN    | turn=${t.turn} t0=${t.t0Ms}ms t1=${t.t1Ms}ms ` +
          `text=${JSON.stringify(t.text)} confidence=${t.confidence.toFixed(3)}` +
          attrSuffix(t.endpoint, t.trackId)
        );
      } else if (resp.fraudVerdict) {
        const fv = resp.fraudVerdict;
        console.log(
          `FRAUD   | turn=${fv.turn} score=${fv.score.toFixed(3)} ` +
          `decision=${fv.decision} family=${fv.family || "-"} ` +
          `type=${fv.type || "-"}` +
          attrSuffix(fv.endpoint, fv.trackId)
        );
      } else if (resp.notification) {
        const n = resp.notification;
        console.log(`NOTICE  | type=${n.type} message=${JSON.stringify(n.message)}`);
      } else if (resp.finalResult) {
        const f = resp.finalResult;
        console.log(`FINAL   | total_audio_ms=${f.totalAudioMs}`);
        for (const pc of f.perConsumer) {
          let line =
            `        | consumer=${pc.consumerName} ` +
            `label=${pc.overallLabelRaw || pc.overallLabel} ` +
            `score=${pc.overallScore.toFixed(3)} count=${pc.analysisCount}`;
          if (pc.modelVersion) line += ` model_version=${pc.modelVersion}`;
          console.log(line);
        }
      }
    });
    call.on("error", (err) => {
      console.log(`ERROR   | ${err.message}`);
      reject(err);
    });
    call.on("end", () => resolve());
  });

  // Send CreateSessionRequest.
  const label = multiSources.length >= 2
    ? `multi-${[args.nearEndFile, args.farEndFile, args.mixedFile].filter(Boolean).join("+")}`
    : args.audioFile ?? `silence-${args.duration}s`;
  call.write({
    createSessionRequest: {
      config: {
        sessionType: SessionType.SESSION_TYPE_USER_STREAM,
        attributes: {
          example: "orchestrator_client.ts",
          source: label,
        },
      },
    },
  });

  // Multi-file mode: interleave 2-or-3 endpoint-tagged streams in one
  // session. Each tick walks every endpoint slot once and emits one
  // frame per side that still has audio. Files of different lengths
  // continue independently until all drain.
  if (multiSources.length >= 2) {
    const chunked = multiSources.map(([ep, wav]) => ({ ep, wav, chunks: chunkWav(wav) }));
    const maxLen = Math.max(...chunked.map(({ chunks }) => chunks.length));
    for (let i = 0; i < maxLen; i++) {
      for (const { ep, wav, chunks } of chunked) {
        if (i < chunks.length) {
          const { payload, ptsNs } = chunks[i];
          call.write(makeFrameRequest(
            wav.audioCodec, wav.rate, wav.channels,
            payload, ptsNs, ep, args.trackId,
          ));
        }
      }
      await new Promise((r) => setTimeout(r, CHUNK_MS));
    }
  } else if (singleWav) {
    for (const { payload, ptsNs } of chunkWav(singleWav)) {
      call.write(makeFrameRequest(
        singleWav.audioCodec, singleWav.rate, singleWav.channels,
        payload, ptsNs, args.endpoint, args.trackId,
      ));
      await new Promise((r) => setTimeout(r, CHUNK_MS));
    }
  } else {
    const silence = Buffer.alloc(SAMPLES_PER_CHUNK * CHANNELS * BYTES_PER_SAMPLE, 0);
    const totalChunks = Math.floor((args.duration * 1000) / CHUNK_MS);
    let ptsNs = 0n;
    for (let i = 0; i < totalChunks; i++) {
      call.write(makeFrameRequest(
        AudioCodec.AUDIO_CODEC_S16LE, RATE, CHANNELS,
        silence, ptsNs, args.endpoint, args.trackId,
      ));
      ptsNs += BigInt(CHUNK_MS * 1_000_000);
      await new Promise((r) => setTimeout(r, CHUNK_MS));
    }
  }
  call.end();

  await done.catch(() => {});
}

run(parseArgs()).catch((err) => {
  console.error(err);
  process.exit(1);
});
