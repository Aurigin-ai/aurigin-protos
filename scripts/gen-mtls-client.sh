#!/usr/bin/env bash
# Generate a client cert (key + CSR + signed cert) in one shot, signed
# by an existing CA. This is the LOCAL shortcut where the CA operator
# also generates the client's key — appropriate for dev, initial
# rollout, or clients you own end-to-end. For a real customer / remote
# client, use the CSR round-trip instead (gen-client-csr.sh on their
# side + sign-client-csr.sh on yours) so their private key never leaves
# their machine.
#
# Usage:
#   ./scripts/gen-mtls-client.sh <name> [<outdir>]
#
# Args:
#   name    Client identity — becomes both the cert Common Name and the
#           output filename prefix. Shows up in the server's audit log.
#           Examples: "client-prod-01", "internal-service-a", "test-user".
#   outdir  Where to write <name>.crt + <name>.key. Default: current dir.
#
# Required env:
#   CA_CRT  Path to the CA cert that will sign this client cert.
#   CA_KEY  Path to the matching CA private key.
#
# Env overrides:
#   DAYS    Validity in days. Default: 365.
#   ORG     Organization (O=) in the cert subject. Default: Example.
#
# Output:
#   <outdir>/<name>.crt   Client certificate (public)
#   <outdir>/<name>.key   Client private key (SECRET — move to the client's machine)
#
# Example:
#   CA_CRT=./ca.crt CA_KEY=./ca.key \
#       ./scripts/gen-mtls-client.sh internal-service-a
#   # → ./internal-service-a.{crt,key}
#   # → ship both files to the client host, wire TLS_CLIENT_CERT/KEY

set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "usage: $0 <name> [<outdir>]" >&2
    exit 2
fi

NAME="$1"
OUTDIR="${2:-.}"
DAYS="${DAYS:-365}"
ORG="${ORG:-Example}"
CA_CRT="${CA_CRT:?CA_CRT env var required (path to CA cert)}"
CA_KEY="${CA_KEY:?CA_KEY env var required (path to CA private key)}"

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
    -extfile <(printf "extendedKeyUsage=clientAuth\n") \
    2>/dev/null

rm "$OUTDIR/$NAME.csr"
rm -f "$(dirname "$CA_CRT")/$(basename "$CA_CRT" .crt).srl" \
      "$(dirname "$CA_CRT")/ca.srl"
chmod 600 "$OUTDIR/$NAME.key"

echo "→ client cert issued:"
echo "  $OUTDIR/$NAME.crt   CN=$NAME"
echo "  $OUTDIR/$NAME.key   (SECRET — ship to the client host)"
echo ""
echo "Chain verify:"
openssl verify -CAfile "$CA_CRT" "$OUTDIR/$NAME.crt"
