// Shared TLS auto-detect for the example clients.
//
// Clients look at examples/certs/server.crt (committed to the repo so the
// example is TLS-by-default). Override with TLS_CA env var, or point it
// at a non-existent path to force insecure mode. mTLS is opt-in via
// MTLS=1 alongside TLS_CLIENT_CERT + TLS_CLIENT_KEY.
//
// The server-side helpers that used to live here (`serverCredentials`,
// `tlsAvailableForServer`, `mtlsAvailableForServer`) were dropped when
// the TypeScript example server was retired — the canonical simulator
// is now the Python `deepfake-simulator-service` under
// examples/simulator/deepfake/, which has its own TLS auto-detect in
// examples/simulator/deepfake/src/deepfake_simulator_service/server.py.

import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

import { credentials, type ChannelCredentials } from "@grpc/grpc-js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
// __dirname → examples/typescript/common/. Up twice to examples/, then certs/.
const DEFAULT_TLS_DIR = path.resolve(__dirname, "..", "..", "certs");

function mtlsRequested(): boolean {
  return ["1", "true", "yes"].includes((process.env.MTLS ?? "").toLowerCase());
}

function caPath(): string {
  return process.env.TLS_CA ?? path.join(DEFAULT_TLS_DIR, "server.crt");
}

function clientCertPath(): string {
  return process.env.TLS_CLIENT_CERT ?? path.join(DEFAULT_TLS_DIR, "client.crt");
}

function clientKeyPath(): string {
  return process.env.TLS_CLIENT_KEY ?? path.join(DEFAULT_TLS_DIR, "client.key");
}

export function tlsAvailableForClient(): boolean {
  return fs.existsSync(caPath());
}

function mtlsAvailableForClient(): boolean {
  return mtlsRequested() && fs.existsSync(clientCertPath()) && fs.existsSync(clientKeyPath());
}

export function channelCredentials(): ChannelCredentials {
  if (!tlsAvailableForClient()) return credentials.createInsecure();
  const rootCa = fs.readFileSync(caPath());
  if (mtlsAvailableForClient()) {
    return credentials.createSsl(
      rootCa,
      fs.readFileSync(clientKeyPath()),
      fs.readFileSync(clientCertPath()),
    );
  }
  return credentials.createSsl(rootCa);
}

export function transportLabel(): string {
  if (!tlsAvailableForClient()) {
    return "insecure (no examples/certs/server.crt found)";
  }
  if (mtlsAvailableForClient()) return "mTLS (self-signed, examples/certs/)";
  if (mtlsRequested()) {
    return "TLS (self-signed, examples/certs/) — MTLS=1 but client.{crt,key} missing, falling back";
  }
  return "TLS (self-signed, examples/certs/)";
}
