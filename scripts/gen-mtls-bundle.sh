#!/usr/bin/env bash
# Generate a CA-chained mTLS bundle for testing against a real deepfake
# service. Different from `just tls` (self-signed, client-is-its-own-CA
# simplified model) — this produces the shape production deepfake expects:
# one CA, one server cert chaining to it, N client certs chaining to it.
#
# Env overrides:
#   OUTDIR        default examples/certs/mtls (relative to repo root)
#   CLIENT_COUNT  default 3
#   SERVER_SANS   default "DNS:localhost,IP:127.0.0.1,IP:::1"
#   DAYS          default 365
#   CA_CN         Common Name for the CA cert. Default: mtls-ca.
#   SERVER_CN     Common Name for the server cert. Default: server.
#   ORG           Organization (O=) baked into every cert. Default: Example.
#
# Output files (under $OUTDIR):
#   ca.crt / ca.key                - root CA (self-signed, EC P-256)
#   server.crt / server.key        - server cert signed by CA, SANs from SERVER_SANS
#   client-1..N.crt / .key         - client certs signed by CA (CN=client-N)
#
# Wiring on the target gRPC server:
#   TLS_CERT       = <OUTDIR>/server.crt
#   TLS_KEY        = <OUTDIR>/server.key
#   TLS_CLIENT_CA  = <OUTDIR>/ca.crt
#
# Wiring on the client examples in this repo:
#   TLS_CA           = <OUTDIR>/ca.crt
#   MTLS             = 1
#   TLS_CLIENT_CERT  = <OUTDIR>/client-1.crt   (pick any of client-1..N)
#   TLS_CLIENT_KEY   = <OUTDIR>/client-1.key
#
# Typical remote-server invocation (replace IP with your server's public address):
#   SERVER_SANS="IP:192.0.2.10,DNS:localhost,IP:127.0.0.1" ./scripts/gen-mtls-bundle.sh

set -euo pipefail

OUTDIR="${OUTDIR:-examples/certs/mtls}"
CLIENT_COUNT="${CLIENT_COUNT:-3}"
SERVER_SANS="${SERVER_SANS:-DNS:localhost,IP:127.0.0.1,IP:::1}"
DAYS="${DAYS:-365}"
CA_CN="${CA_CN:-mtls-ca}"
SERVER_CN="${SERVER_CN:-server}"
ORG="${ORG:-Example}"

# Resolve to repo root so relative OUTDIR works regardless of cwd.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

mkdir -p "$OUTDIR"

echo "── generating mTLS bundle ─────────────────────────────────────────"
echo "  output   : $OUTDIR"
echo "  clients  : $CLIENT_COUNT"
echo "  server SANs: $SERVER_SANS"
echo "  validity : $DAYS days"
echo ""

# Compose the bundle from the three atomic scripts so there's one source
# of truth for the openssl invocations. If you want to run just one of
# these steps (e.g. rotate the server cert against the existing CA, or
# mint a new client), use the atomic scripts directly.
echo "[1/3] root CA"
DAYS="$DAYS" ORG="$ORG" \
    "$SCRIPT_DIR/gen-mtls-ca.sh" "$CA_CN" "$OUTDIR" >/dev/null

echo "[2/3] server cert"
CA_CRT="$OUTDIR/$CA_CN.crt" CA_KEY="$OUTDIR/$CA_CN.key" DAYS="$DAYS" ORG="$ORG" \
    "$SCRIPT_DIR/gen-mtls-server.sh" "$SERVER_CN" "$SERVER_SANS" "$OUTDIR" >/dev/null

# Guard against CLIENT_COUNT=0 — BSD's `seq 1 0` counts down and generates
# `1 0` rather than empty, so we'd end up with client-0 / client-1 files
# even when the caller explicitly asked for zero clients.
for i in $(if [[ "$CLIENT_COUNT" -ge 1 ]]; then seq 1 "$CLIENT_COUNT"; fi); do
    echo "[3/3] client-$i"
    CA_CRT="$OUTDIR/$CA_CN.crt" CA_KEY="$OUTDIR/$CA_CN.key" DAYS="$DAYS" ORG="$ORG" \
        "$SCRIPT_DIR/gen-mtls-client.sh" "client-$i" "$OUTDIR" >/dev/null
done

# Rename the CA files to the conventional ca.crt / ca.key regardless of
# CA_CN, so wiring instructions below (and the README) stay stable.
if [[ "$CA_CN" != "ca" ]]; then
    mv "$OUTDIR/$CA_CN.crt" "$OUTDIR/ca.crt"
    mv "$OUTDIR/$CA_CN.key" "$OUTDIR/ca.key"
fi
# Same for the server cert.
if [[ "$SERVER_CN" != "server" ]]; then
    mv "$OUTDIR/$SERVER_CN.crt" "$OUTDIR/server.crt"
    mv "$OUTDIR/$SERVER_CN.key" "$OUTDIR/server.key"
fi

# Enter OUTDIR for the summary printing below.
cd "$OUTDIR"

echo ""
echo "── bundle contents ────────────────────────────────────────────────"
ls -1
echo ""
echo "── wiring ─────────────────────────────────────────────────────────"
BUNDLE="$(pwd)"
cat <<EOF
Server side (gRPC service .env):
  TLS_CERT       = $BUNDLE/server.crt
  TLS_KEY        = $BUNDLE/server.key
  TLS_CLIENT_CA  = $BUNDLE/ca.crt

Client side (repo's client examples, or any grpc-py client):
  TLS_CA           = $BUNDLE/ca.crt
  MTLS             = 1
  TLS_CLIENT_CERT  = $BUNDLE/client-1.crt   (pick any: client-1..$CLIENT_COUNT)
  TLS_CLIENT_KEY   = $BUNDLE/client-1.key

Verify a client cert chains to the CA:
  openssl verify -CAfile $BUNDLE/ca.crt $BUNDLE/client-1.crt
EOF
