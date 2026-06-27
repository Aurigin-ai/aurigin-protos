// Shared TLS auto-detect for the example server + clients.
//
// Both sides look at examples/certs/server.{crt,key} (committed to the repo
// so the example is TLS-by-default). Override with TLS_CERT / TLS_KEY /
// TLS_CA env vars, or point them at non-existent paths to force insecure
// mode.

import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

import {
  ServerCredentials,
  credentials,
  type ChannelCredentials,
  type ServerCredentials as ServerCredentialsType,
} from "@grpc/grpc-js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
// __dirname → examples/typescript/common/.  Up twice to examples/, then certs/
// (one more `..` than when this file lived at examples/typescript/tls.ts).
const DEFAULT_TLS_DIR = path.resolve(__dirname, "..", "..", "certs");

function mtlsRequested(): boolean {
  return ["1", "true", "yes"].includes((process.env.MTLS ?? "").toLowerCase());
}

function certPath(): string {
  return process.env.TLS_CERT ?? path.join(DEFAULT_TLS_DIR, "server.crt");
}

function keyPath(): string {
  return process.env.TLS_KEY ?? path.join(DEFAULT_TLS_DIR, "server.key");
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

function clientCaPath(): string {
  return process.env.TLS_CLIENT_CA ?? path.join(DEFAULT_TLS_DIR, "client.crt");
}

export function tlsAvailableForServer(): boolean {
  return fs.existsSync(certPath()) && fs.existsSync(keyPath());
}

export function tlsAvailableForClient(): boolean {
  return fs.existsSync(caPath());
}

function mtlsAvailableForServer(): boolean {
  return mtlsRequested() && fs.existsSync(clientCaPath());
}

function mtlsAvailableForClient(): boolean {
  return mtlsRequested() && fs.existsSync(clientCertPath()) && fs.existsSync(clientKeyPath());
}

export function serverCredentials(): ServerCredentialsType {
  if (!tlsAvailableForServer()) return ServerCredentials.createInsecure();
  const requireClientCert = mtlsAvailableForServer();
  return ServerCredentials.createSsl(
    requireClientCert ? fs.readFileSync(clientCaPath()) : null,
    [{ private_key: fs.readFileSync(keyPath()), cert_chain: fs.readFileSync(certPath()) }],
    requireClientCert,
  );
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

export function transportLabel(side: "server" | "client"): string {
  if (side === "server") {
    if (!tlsAvailableForServer()) {
      return "insecure (no examples/certs/server.crt found)";
    }
    if (mtlsAvailableForServer()) return "mTLS (self-signed, examples/certs/)";
    if (mtlsRequested()) {
      return "TLS (self-signed, examples/certs/) — MTLS=1 but examples/certs/client.crt missing, falling back to plain TLS";
    }
    return "TLS (self-signed, examples/certs/)";
  }
  if (!tlsAvailableForClient()) {
    return "insecure (no examples/certs/server.crt found)";
  }
  if (mtlsAvailableForClient()) return "mTLS (self-signed, examples/certs/)";
  if (mtlsRequested()) {
    return "TLS (self-signed, examples/certs/) — MTLS=1 but client.{crt,key} missing, falling back";
  }
  return "TLS (self-signed, examples/certs/)";
}
