#!/usr/bin/env bash
# CLIENT-SIDE utility: generate a private key + CSR to send to the CA
# operator. The .key file stays on this machine; only the .csr is sent
# to the CA. When you get the signed .crt back, put it next to the .key
# and point your client at both.
#
# Usage:
#   ./scripts/gen-client-csr.sh <cn> [<outdir>]
#
# Args:
#   cn        Common Name for the client cert (e.g. "client-prod-01",
#             "customer-a-lab", "external-caller-2"). Ends up in the cert's
#             Subject and in the server's `x509_common_name` audit log.
#   outdir    Where to write <cn>.key and <cn>.csr. Default: current dir.
#
# Env overrides:
#   ORG        Organization (O=) in the CSR subject. Default: Example.
#   REUSE_KEY  Path to an existing private key. When set, no new key is
#              generated — the CSR is signed with the existing key
#              instead. Use for "rotate the cert but keep the same key"
#              scenarios (see gotchas below).
#
# Output:
#   <outdir>/<cn>.key   private key — KEEP SECRET, DO NOT SHARE
#                       (skipped when REUSE_KEY is set — existing key
#                        is left untouched at its current path)
#   <outdir>/<cn>.csr   CSR — send this to the CA operator
#
# Default (fresh key) — the standard flow:
#   ./scripts/gen-client-csr.sh client-prod-01 /tmp/certs
#   # → /tmp/certs/client-prod-01.{key,csr}
#
# Reuse existing key (rotate cert, keep same crypto identity):
#   REUSE_KEY=/tmp/certs/client-prod-01.key \
#       ./scripts/gen-client-csr.sh client-prod-01 /tmp/certs
#   # → /tmp/certs/client-prod-01.csr        (new CSR bound to existing key)
#   # → /tmp/certs/client-prod-01.key         (unchanged)
#
# When to use REUSE_KEY:
#   - Client key is in a hardware store you can't easily rotate
#   - Something downstream pins the public-key fingerprint
#   - Recovery from an accidental cert expiry, faster path back to service
#
# When NOT to use REUSE_KEY (rotate key + cert together — the safer
# default that ACME et al. use):
#   - Regular scheduled renewals
#   - Suspected or confirmed key compromise
#   - No specific reason to preserve the key
# Delete the old .key and re-run without REUSE_KEY to get a fresh key pair.

set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "usage: $0 <cn> [<outdir>]" >&2
    echo "  cn      Common Name (identity) for the client cert" >&2
    echo "  outdir  default: current dir" >&2
    echo "  REUSE_KEY=<path>  (env) reuse existing key, skip keygen" >&2
    exit 2
fi

CN="$1"
OUTDIR="${2:-.}"
ORG="${ORG:-Example}"
REUSE_KEY="${REUSE_KEY:-}"

mkdir -p "$OUTDIR"
KEY_OUT="$OUTDIR/$CN.key"
CSR="$OUTDIR/$CN.csr"

# ── Reuse-existing-key mode ────────────────────────────────────────
if [[ -n "$REUSE_KEY" ]]; then
    if [[ ! -f "$REUSE_KEY" ]]; then
        echo "REUSE_KEY not found: $REUSE_KEY" >&2
        exit 1
    fi
    echo "── generating CSR from existing key ───────────────────────────────"
    echo "  CN     : $CN"
    echo "  O      : $ORG"
    echo "  key    : $REUSE_KEY  (reused — file untouched)"
    echo "  CSR    : $CSR  (send this to the CA operator)"
    echo ""
    openssl req -new -key "$REUSE_KEY" -out "$CSR" \
        -subj "/CN=$CN/O=$ORG" 2>/dev/null

    echo "── CSR summary ────────────────────────────────────────────────────"
    openssl req -in "$CSR" -noout -subject -verify 2>&1 | grep -v "unable to"
    echo ""
    echo "Next: send $CSR to the CA operator. Your key stays at:"
    echo "  $REUSE_KEY"
    exit 0
fi

# ── Default: fresh keypair + CSR ───────────────────────────────────
if [[ -f "$KEY_OUT" ]]; then
    echo "refusing to overwrite existing key: $KEY_OUT" >&2
    echo "" >&2
    echo "Two ways forward:" >&2
    echo "  1. Rotate the key (recommended for regular renewals):" >&2
    echo "       rm $KEY_OUT" >&2
    echo "       $0 $CN $OUTDIR" >&2
    echo "  2. Keep the existing key, generate only a new CSR:" >&2
    echo "       REUSE_KEY=$KEY_OUT $0 $CN $OUTDIR" >&2
    exit 1
fi

echo "── generating client key + CSR ────────────────────────────────────"
echo "  CN     : $CN"
echo "  O      : $ORG"
echo "  key    : $KEY_OUT  (SECRET — do not share)"
echo "  CSR    : $CSR  (send this to the CA operator)"
echo ""

openssl req -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
    -keyout "$KEY_OUT" -out "$CSR" \
    -subj "/CN=$CN/O=$ORG" 2>/dev/null

# Lock down the private key — read-only for the owner, nothing for anyone else.
chmod 600 "$KEY_OUT"

echo "── CSR summary ────────────────────────────────────────────────────"
openssl req -in "$CSR" -noout -subject -verify 2>&1 | grep -v "unable to"
echo ""
echo "Next steps:"
echo "  1. Send $CSR to the CA operator (email / secure channel)"
echo "  2. Keep $KEY_OUT on this machine — never send it anywhere"
echo "  3. When you receive the signed .crt back, save it as $OUTDIR/$CN.crt"
echo "  4. Configure the client:"
echo "       export MTLS=1"
echo "       export TLS_CA=<ca.crt from CA operator>"
echo "       export TLS_CLIENT_CERT=$OUTDIR/$CN.crt"
echo "       export TLS_CLIENT_KEY=$KEY_OUT"
