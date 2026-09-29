#!/usr/bin/env bash
# Generate a server cert (private key + CSR + signed cert), signed by an
# existing CA. Use when your PKI is already set up and you just need to
# mint or rotate the server identity.
#
# Usage:
#   ./scripts/gen-mtls-server.sh <name> <sans> [<outdir>]
#
# Args:
#   name    Server identity — becomes both the cert Common Name and the
#           output filename prefix (e.g. "server", "deepfake-prod-01").
#   sans    Subject Alternative Names — comma-separated. Every hostname
#           and IP a client might use to connect must appear here.
#           Format: "IP:<ip>" or "DNS:<hostname>". **NO port numbers**
#           in IP entries — IP SANs are just the address, the port is
#           a connection detail.
#           Example: "IP:192.0.2.10,DNS:svc.example.internal,DNS:localhost,IP:127.0.0.1"
#   outdir  Where to write <name>.crt + <name>.key. Default: current dir.
#
# Required env:
#   CA_CRT  Path to the CA certificate that will sign this server cert.
#   CA_KEY  Path to the matching CA private key.
#
# Env overrides:
#   DAYS    Validity in days. Default: 365.
#   ORG     Organization (O=) in the cert subject. Default: Example.
#
# Output:
#   <outdir>/<name>.crt   Server certificate (public)
#   <outdir>/<name>.key   Server private key (SECRET — deploy to server nodes only)
#
# Example (using an existing CA at ./ca.crt / ./ca.key):
#   CA_CRT=./ca.crt CA_KEY=./ca.key \
#       ./scripts/gen-mtls-server.sh server-prod \
#       "IP:192.0.2.10,DNS:svc.example.internal,DNS:localhost,IP:127.0.0.1"
#   # → ./server-prod.{crt,key}

set -euo pipefail

if [[ $# -lt 2 ]]; then
    echo "usage: $0 <name> <sans> [<outdir>]" >&2
    echo "  sans example: IP:192.0.2.10,DNS:localhost,IP:127.0.0.1" >&2
    exit 2
fi

NAME="$1"
SANS="$2"
OUTDIR="${3:-.}"
DAYS="${DAYS:-365}"
ORG="${ORG:-Example}"
CA_CRT="${CA_CRT:?CA_CRT env var required (path to CA cert)}"
CA_KEY="${CA_KEY:?CA_KEY env var required (path to CA private key)}"

# Cheap sanity-check: catch the common "I included the port" mistake in
# an IP SAN entry. Openssl's error for this is cryptic.
if [[ "$SANS" =~ IP:[0-9.]+:[0-9]+ ]]; then
    echo "ERROR: SAN entry has a port number: $SANS" >&2
    echo "IP SANs are just the address — no port. Fix: remove ':<port>'" >&2
    exit 2
fi

for f in "$CA_CRT" "$CA_KEY"; do
    [[ -f "$f" ]] || { echo "not found: $f" >&2; exit 1; }
done

mkdir -p "$OUTDIR"

if [[ -f "$OUTDIR/$NAME.key" ]]; then
    echo "refusing to overwrite existing key: $OUTDIR/$NAME.key" >&2
    exit 1
fi

openssl req -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
    -keyout "$OUTDIR/$NAME.key" -out "$OUTDIR/$NAME.csr" \
    -subj "/CN=$NAME/O=$ORG" 2>/dev/null

openssl x509 -req -in "$OUTDIR/$NAME.csr" \
    -CA "$CA_CRT" -CAkey "$CA_KEY" -CAcreateserial \
    -out "$OUTDIR/$NAME.crt" -days "$DAYS" \
    -extfile <(printf "subjectAltName=%s\nextendedKeyUsage=serverAuth\n" "$SANS") \
    2>/dev/null

rm "$OUTDIR/$NAME.csr"
rm -f "$(dirname "$CA_CRT")/$(basename "$CA_CRT" .crt).srl" \
      "$(dirname "$CA_CRT")/ca.srl"
chmod 600 "$OUTDIR/$NAME.key"

echo "→ server cert issued:"
echo "  $OUTDIR/$NAME.crt   SANs: $SANS"
echo "  $OUTDIR/$NAME.key   (SECRET — deploy to backend nodes)"
echo ""
echo "Chain verify:"
openssl verify -CAfile "$CA_CRT" "$OUTDIR/$NAME.crt"
