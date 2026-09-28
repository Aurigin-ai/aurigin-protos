#!/usr/bin/env bash
# CA-OPERATOR utility: take a client-provided CSR, sign it with the CA
# key, produce a client cert. Give the .crt back to the client (over any
# channel — the cert is public). The client already has the matching
# private key; you never touch it.
#
# Usage:
#   ./scripts/sign-client-csr.sh <csr> [<outdir>]
#
# Args:
#   csr       Path to the CSR file received from the client
#   outdir    Where to write the signed cert. Default: same dir as the CSR.
#
# Env overrides:
#   CA_CRT    default: examples/certs/mtls/ca.crt (relative to repo root)
#   CA_KEY    default: examples/certs/mtls/ca.key
#   DAYS      default: 365
#
# Output:
#   <outdir>/<basename>.crt   signed cert to send back to the client
#
# The server does NOT need a restart or config change to accept the new
# client — as long as CA_CRT is already the file referenced by the
# server's TLS_CLIENT_CA, the new client is trusted immediately (mTLS
# trusts the whole CA chain, not per-client certs).
#
# Example:
#   ./scripts/sign-client-csr.sh /tmp/incoming/client-a-prod.csr /tmp/signed
#   # → /tmp/signed/client-a-prod.crt
#   # → return .crt to the client, they wire it in as TLS_CLIENT_CERT

set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "usage: $0 <csr> [<outdir>]" >&2
    exit 2
fi

CSR="$1"
OUTDIR="${2:-$(dirname "$CSR")}"
DAYS="${DAYS:-365}"

# Default CA location relative to repo root.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CA_CRT="${CA_CRT:-$REPO_ROOT/examples/certs/mtls/ca.crt}"
CA_KEY="${CA_KEY:-$REPO_ROOT/examples/certs/mtls/ca.key}"

if [[ ! -f "$CSR" ]]; then
    echo "CSR not found: $CSR" >&2
    exit 1
fi
if [[ ! -f "$CA_CRT" ]] || [[ ! -f "$CA_KEY" ]]; then
    echo "CA not found — expected $CA_CRT + $CA_KEY" >&2
    echo 'Generate one with `just mtls-bundle` or override CA_CRT/CA_KEY.' >&2
    exit 1
fi

mkdir -p "$OUTDIR"
BASENAME="$(basename "$CSR" .csr)"
CRT="$OUTDIR/$BASENAME.crt"

echo "── signing CSR ────────────────────────────────────────────────────"
echo "  CSR    : $CSR"
echo "  CA cert: $CA_CRT"
echo "  output : $CRT"
echo "  validity: $DAYS days"
echo ""

# Show the CSR's declared identity so the operator can eyeball it before
# signing — CSRs are self-signed, so the client can claim any CN they want;
# the operator's job is to confirm the CN matches what they promised.
echo "── CSR identity (verify this matches what the requester claimed) ──"
openssl req -in "$CSR" -noout -subject
echo ""

openssl x509 -req -in "$CSR" \
    -CA "$CA_CRT" -CAkey "$CA_KEY" -CAcreateserial \
    -out "$CRT" -days "$DAYS" \
    -extfile <(printf "extendedKeyUsage=clientAuth\n") \
    2>/dev/null

# Tidy up openssl scratch file.
rm -f "$(dirname "$CA_CRT")/ca.srl"

echo "── signed cert ────────────────────────────────────────────────────"
openssl x509 -in "$CRT" -noout -subject -issuer -dates -ext extendedKeyUsage
echo ""
echo "── chain verify ───────────────────────────────────────────────────"
# If CA_CRT is self-signed (a root), we can verify directly against it.
# If it's an intermediate (subject != issuer), openssl needs the root to
# build the chain up to a trust anchor. Two modes:
#   - ROOT_CRT env is set → do a full-chain verify (root + intermediate + leaf)
#   - ROOT_CRT unset → skip the verify (signing already succeeded; this
#     is only a post-hoc sanity check)
CA_SUBJECT=$(openssl x509 -in "$CA_CRT" -noout -subject -nameopt RFC2253 | sed 's/^subject=//')
CA_ISSUER=$(openssl x509 -in "$CA_CRT" -noout -issuer -nameopt RFC2253 | sed 's/^issuer=//')
if [[ "$CA_SUBJECT" == "$CA_ISSUER" ]]; then
    # CA_CRT is self-signed (root) — direct verify works
    openssl verify -CAfile "$CA_CRT" "$CRT" || true
elif [[ -n "${ROOT_CRT:-}" ]] && [[ -f "$ROOT_CRT" ]]; then
    # CA_CRT is an intermediate; caller supplied the root — full-chain verify
    openssl verify -CAfile "$ROOT_CRT" -untrusted "$CA_CRT" "$CRT" || true
else
    # CA_CRT is an intermediate but no ROOT_CRT supplied. The signing
    # step already succeeded (we got here — openssl x509 -req exited 0);
    # the verify is just a post-hoc sanity check. Skip cleanly with a
    # note explaining how to run it manually if needed.
    echo "  (skipped — CA is an intermediate. To verify the full chain, either:)"
    echo "    - re-run with ROOT_CRT=<path/to/root.crt> in the env, or"
    echo "    - openssl verify -CAfile <root.crt> -untrusted $CA_CRT $CRT"
fi
echo ""
echo "Send $CRT back to the requester. Server needs no changes — as long"
echo "as $CA_CRT (and its parent chain, if any) is already trusted by the"
echo "server's TLS_CLIENT_CA bundle, the new client cert is accepted on"
echo "the next connection (no server restart required)."
