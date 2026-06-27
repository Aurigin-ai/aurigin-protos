// Shared helpers for the aurigin-protos TypeScript example clients/server.
//
// Four small modules — all "infra glue" rather than the actual gRPC
// example, so they live here together to keep the call-site files
// focused on what they're demonstrating:
//
//   - wav_reader  — WavData type + readWav() (S16LE / F32LE)
//   - result_csv  — ResultCSV writer for per-chunk run captures
//   - tls         — TLS auto-detect for example clients (server-cert + mTLS)
//   - shutdown    — graceful SIGINT/SIGTERM handler for grpc-js calls
//
// Top-level re-exports so call sites can do:
//   import { readWav, ResultCSV, channelCredentials } from "./common/index.js";

export type { WavData, WireFormat } from "./wav_reader.js";
export { bytesPerFrame, durationS, readWav } from "./wav_reader.js";

export type { ChunkRow } from "./result_csv.js";
export { CSV_COLUMNS, ResultCSV } from "./result_csv.js";

export {
  channelCredentials,
  serverCredentials,
  tlsAvailableForClient,
  tlsAvailableForServer,
  transportLabel,
} from "./tls.js";

export type { Shutdown } from "./shutdown.js";
export { installSignalShutdown } from "./shutdown.js";
