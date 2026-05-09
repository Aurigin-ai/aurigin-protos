// Minimal gRPC server using the generated @aurigin/protos package.
// Implements DeepfakeDetection.DetectDeepfake (bidi streaming) with stub
// analysis logic. Replace _analyze() with real ML inference in production.
//
// Run after `npm install @aurigin/protos @grpc/grpc-js`.

import { Server, ServerCredentials, type ServerDuplexStream } from "@grpc/grpc-js";
import {
  DeepfakeDetectionService,
  type DeepfakeDetectionServer,
  type DetectDeepfakeRequest,
  type DetectDeepfakeResponse,
} from "@aurigin/protos/aurigin/deepfake_detection/v1/deepfake_detection";

function bytesToMs(audioBytes: number, channels: number, rate: number): number {
  if (rate <= 0 || channels <= 0) return 0;
  // S16LE PCM: 2 bytes per sample per channel.
  return Math.round((audioBytes / 2 / channels / rate) * 1000);
}

function analyze(_windowMs: number): { score: number; label: string; confidence: number } {
  // Stub: replace with real inference.
  return { score: 0.05, label: "bonafide", confidence: 1.0 };
}

const impl: DeepfakeDetectionServer = {
  detectDeepfake(call: ServerDuplexStream<DetectDeepfakeRequest, DetectDeepfakeResponse>) {
    let sessionStarted = false;
    let totalMs = 0;
    let count = 0;
    const scores: number[] = [];

    call.on("data", (req: DetectDeepfakeRequest) => {
      if (!sessionStarted) {
        if (!req.createSessionRequest) {
          call.emit("error", { code: 3, message: "Expected CreateSessionRequest first" });
          return;
        }
        sessionStarted = true;
        call.write({
          createSessionResponse: { sessionId: "demo-session-0001" },
        });
        return;
      }

      if (!req.audio) return;
      const buf = req.audio;
      const bufferLen = buf.buffer ? buf.buffer.length : 0;
      const windowMs = bytesToMs(bufferLen, buf.channels || 1, buf.rate || 16000);
      const { score, label, confidence } = analyze(windowMs);
      scores.push(score);
      count += 1;
      totalMs += windowMs;
      const ptsMs = Number(buf.ptsNs / 1_000_000n);
      call.write({
        analysisResult: {
          audioOffsetMs: BigInt(ptsMs),
          durationMs: BigInt(windowMs),
          score,
          label,
          confidence,
        },
      });
    });

    call.on("end", () => {
      const overall = scores.length ? scores.reduce((a, b) => a + b, 0) / scores.length : 0;
      call.write({
        finalResult: {
          totalAudioMs: BigInt(totalMs),
          overallScore: overall,
          overallLabel: overall < 0.4 ? "bonafide" : "spoofed",
          analysisCount: count,
        },
      });
      call.end();
    });
  },
};

const server = new Server();
server.addService(DeepfakeDetectionService, impl);

const port = Number(process.env.PORT ?? 50051);
server.bindAsync(
  `0.0.0.0:${port}`,
  ServerCredentials.createInsecure(),
  (err, boundPort) => {
    if (err) {
      console.error(err);
      process.exit(1);
    }
    console.log(`DeepfakeDetection server listening on :${boundPort}`);
  },
);
