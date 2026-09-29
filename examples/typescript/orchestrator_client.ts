// Minimal AudioVerification.Stream client — SDK smoke-test target.
//
// Opens one bidi session against `aurigin.client.v1.AudioVerification.Stream`,
// streams synthesised silence for a configurable duration, and prints every
// `Verdict` + `EmbeddingVerdict` + `FinalResult` the server responds with.
//
// Points at the orchestrator simulator by default (`localhost:50053`).
// Override `--target` to hit any AudioVerification server.
//
// Auth is MANDATORY — the SDK-facing gRPC surface expects
// `authorization: Bearer <token>` in metadata (spec: aurigin.common.v1.Principal
// CallerType comment — same header for JWTs and API keys). Pick one of the
// two demo tokens the simulator accepts (see its README) via
// `--token <literal>` or supply your own for a real server.
//
// TLS: `--tls auto` (default) picks insecure for `localhost:*` and any
// `:80` target, secure for everything else. Override with
// `--tls always` / `--tls never` when the target hostname doesn't give
// it away.
//
// CLI:
//   tsx orchestrator_client.ts --token sk_test_aurigin_sim_demo_0000000000000000
//   tsx orchestrator_client.ts --token <jwt> --target host.example:443
//   tsx orchestrator_client.ts --token <jwt> --target host.example:50053 --duration 30

import { Metadata, credentials } from "@grpc/grpc-js";
import { AudioVerificationClient } from "@aurigin/protos/aurigin/client/v1/audio_verification";
import { SessionType } from "@aurigin/protos/aurigin/common/v1/session";
import { AudioCodec } from "@aurigin/protos/aurigin/media/v1/audio_frame";

// Same demo token the orchestrator simulator ships with — kept here so
// `tsx orchestrator_client.ts` (no flags) still works against a
// fresh `docker compose up` of the sim.
const DEMO_API_KEY = "sk_test_aurigin_sim_demo_0000000000000000";

const RATE = 16000;
const CHANNELS = 1;
const CHUNK_MS = 100;
const BYTES_PER_SAMPLE = 2; // S16LE
const SAMPLES_PER_CHUNK = Math.floor((RATE * CHUNK_MS) / 1000);

interface Args {
  target: string;
  token: string;
  duration: number;
  tls: "auto" | "always" | "never";
}

function parseArgs(): Args {
  const argv = process.argv.slice(2);
  const args: Args = { target: "localhost:50053", token: DEMO_API_KEY, duration: 15, tls: "auto" };
  for (let i = 0; i < argv.length; i++) {
    const flag = argv[i];
    const next = argv[i + 1];
    if (flag === "--target" && next) { args.target = next; i++; }
    else if (flag === "--token" && next) { args.token = next; i++; }
    else if (flag === "--duration" && next) { args.duration = Number(next); i++; }
    else if (flag === "--tls" && (next === "auto" || next === "always" || next === "never")) {
      args.tls = next; i++;
    }
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

async function run(args: Args): Promise<void> {
  const tls = useTls(args.target, args.tls);
  console.log(
    `# target=${args.target} tls=${tls} duration=${args.duration}s ` +
    `token_prefix=${args.token.slice(0, 10)}…`
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
          `confidence=${v.confidence.toFixed(3)}`
        );
      } else if (resp.embeddingVerdict) {
        const e = resp.embeddingVerdict;
        console.log(
          `EMBED   | consumer=${e.consumerName} offset=${e.audioOffsetMs}ms ` +
          `dim=${e.dim} fingerprint=${e.fingerprintCode}`
        );
      } else if (resp.notification) {
        const n = resp.notification;
        console.log(`NOTICE  | type=${n.type} message=${JSON.stringify(n.message)}`);
      } else if (resp.finalResult) {
        const f = resp.finalResult;
        console.log(`FINAL   | total_audio_ms=${f.totalAudioMs}`);
        for (const pc of f.perConsumer) {
          console.log(
            `        | consumer=${pc.consumerName} ` +
            `label=${pc.overallLabelRaw || pc.overallLabel} ` +
            `score=${pc.overallScore.toFixed(3)} count=${pc.analysisCount}`
          );
        }
      }
    });
    call.on("error", (err) => {
      console.log(`ERROR   | ${err.message}`);
      reject(err);
    });
    call.on("end", () => resolve());
  });

  // Send CreateSessionRequest + silence chunks paced to wall-clock so the
  // server has time to emit interleaved Verdicts on its own timer.
  call.write({
    createSessionRequest: {
      config: {
        sessionType: SessionType.SESSION_TYPE_USER_STREAM,
        attributes: { example: "orchestrator_client.ts" },
      },
    },
  });

  const silence = Buffer.alloc(SAMPLES_PER_CHUNK * CHANNELS * BYTES_PER_SAMPLE, 0);
  const totalChunks = Math.floor((args.duration * 1000) / CHUNK_MS);
  let ptsNs = 0n;
  for (let i = 0; i < totalChunks; i++) {
    call.write({
      audioFrame: {
        codec: AudioCodec.AUDIO_CODEC_S16LE,
        sampleRateHz: RATE,
        channels: CHANNELS,
        payload: silence,
        ptsNs,
      },
    });
    ptsNs += BigInt(CHUNK_MS * 1_000_000);
    await new Promise((r) => setTimeout(r, CHUNK_MS));
  }
  call.end();

  await done.catch(() => {});
}

run(parseArgs()).catch((err) => {
  console.error(err);
  process.exit(1);
});
