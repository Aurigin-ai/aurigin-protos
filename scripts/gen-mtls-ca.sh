#!/usr/bin/env bash
# Create a root CA — the trust anchor. Run this ONCE per trust domain
# (e.g. once for "your own PKI", once per customer that brings their own).
#
# Usage:
#   ./scripts/gen-mtls-ca.sh <name> [<outdir>]
#
# Args:
#   name    CA identity — used as both the cert's Common Name AND the
#           output filename prefix. Pick a stable descriptor like
#           "my-company-root", "customer-a-ca", "internal-mtls-ca".
#   outdir  Where to write <name>.crt + <name>.key. Default: current dir.
#
# Env overrides:
#   DAYS    Validity in days. Default: 365. Root CAs typically live longer;
#           consider 3650 (10 years) for a real root that anchors many certs.
#   ORG     Organization (O=) in the cert subject. Default: Example.
#
# Output:
#   <outdir>/<name>.crt   CA certificate — public, distributed to servers
#                         (as TLS_CLIENT_CA) and to clients (as TLS_CA)
#   <outdir>/<name>.key   CA private key — SECRET, stays on this machine
#                         only. Anyone with this can mint client certs the
#                         server will accept.
#
# Example:
#   ./scripts/gen-mtls-ca.sh my-company-root /secure/pki
#   # → /secure/pki/my-company-root.{crt,key}

set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "usage: $0 <name> [<outdir>]" >&2
    exit 2
fi

NAME="$1"
OUTDIR="${2:-.}"
DAYS="${DAYS:-365}"
ORG="${ORG:-Example}"

mkdir -p "$OUTDIR"

if [[ -f "$OUTDIR/$NAME.key" ]]; then
    echo "refusing to overwrite existing CA key: $OUTDIR/$NAME.key" >&2
    echo "delete it first if you're intentionally rotating." >&2
    exit 1
fi

openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
    -keyout "$OUTDIR/$NAME.key" -out "$OUTDIR/$NAME.crt" -days "$DAYS" \
    -subj "/CN=$NAME/O=$ORG/OU=root CA" 2>/dev/null

chmod 600 "$OUTDIR/$NAME.key"

echo "→ CA generated:"
echo "  $OUTDIR/$NAME.crt   (public — distribute)"
echo "  $OUTDIR/$NAME.key   (SECRET — keep on this machine)"
echo ""
echo "Use as the CA for server or client certs:"
echo "  CA_CRT=$OUTDIR/$NAME.crt CA_KEY=$OUTDIR/$NAME.key \\"
echo "      ./scripts/gen-mtls-server.sh <server-name> <sans> $OUTDIR"
