#!/usr/bin/env bash
# Generate an intermediate CA — a subordinate CA signed by an existing
# (root or higher-level) CA. Use for delegated / time-boxed trust:
#
#   root CA (long-lived, offline)
#          │  signs
#          ▼
#   intermediate CA (short-lived, e.g. 30 days for a customer trial)
#          │  signs
#          ▼
#   leaf certs (client / server)
#
# The intermediate has `basicConstraints=CA:TRUE,pathlen:0` so it CAN
# sign leaf certs but CANNOT sign further intermediates — bounds the
# delegation depth.
#
# Usage:
#   ./scripts/gen-mtls-intermediate.sh <name> [<outdir>]
#
# Args:
#   name    Intermediate CA identity — becomes both CN and filename base.
#           Examples: "customer-a-trial-ca", "wit-trial-30d", "sub-ca-eu".
#   outdir  Where to write <name>.crt + <name>.key. Default: current dir.
#
# Required env (paths to the SIGNING parent CA):
#   CA_CRT  Path to the parent CA cert (usually your root CA cert).
#   CA_KEY  Path to the matching parent CA private key.
#
# Env overrides:
#   DAYS    Validity in days. Default: 30. Intermediates are typically
#           SHORT-LIVED — that's the whole point of using one. Overshoot
#           by a few days if you're spanning a trial window (e.g. 33 to
#           cover a 30-day sales trial plus 3 days grace).
#   ORG     Organization (O=) in the cert subject. Default: Example.
#
# Output:
#   <outdir>/<name>.crt   Intermediate CA cert (public — ship in every
#                         cert chain signed under this intermediate, so
#                         the server can build client → intermediate →
#                         root during TLS handshake).
#   <outdir>/<name>.key   Intermediate CA private key. Handling depends
#                         on who signs certs under it:
#                           - You keep it: you sign the customer's leaf
#                             certs yourself, ship them the intermediate
#                             .crt only.
#                           - You give it to the customer: they can mint
#                             their own leaf certs under this intermediate
#                             for the trial window (delegated sub-CA).
#
# Example — mint a 30-day trial intermediate for a customer:
#   CA_CRT=/secure/pki/my-root.crt CA_KEY=/secure/pki/my-root.key DAYS=30 \
#       ./scripts/gen-mtls-intermediate.sh customer-a-trial-ca /secure/trials

set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "usage: $0 <name> [<outdir>]" >&2
    exit 2
fi

NAME="$1"
OUTDIR="${2:-.}"
DAYS="${DAYS:-30}"
ORG="${ORG:-Example}"
CA_CRT="${CA_CRT:?CA_CRT env var required (path to parent CA cert)}"
CA_KEY="${CA_KEY:?CA_KEY env var required (path to parent CA private key)}"

for f in "$CA_CRT" "$CA_KEY"; do
    [[ -f "$f" ]] || { echo "not found: $f" >&2; exit 1; }
done

mkdir -p "$OUTDIR"

if [[ -f "$OUTDIR/$NAME.key" ]]; then
    echo "refusing to overwrite existing key: $OUTDIR/$NAME.key" >&2
    exit 1
fi

# Step 1: keypair + CSR for the intermediate.
openssl req -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
    -keyout "$OUTDIR/$NAME.key" -out "$OUTDIR/$NAME.csr" \
    -subj "/CN=$NAME/O=$ORG/OU=intermediate CA" 2>/dev/null

# Step 2: parent CA signs the CSR into an intermediate cert.
# CA extensions:
#   basicConstraints=critical,CA:TRUE,pathlen:0
#       → this cert can sign leaves; pathlen:0 means it cannot sign
#         further intermediates (bounds delegation depth)
#   keyUsage=critical,keyCertSign,cRLSign
#       → the two usages an issuing CA needs; anything else denied
#   subjectKeyIdentifier + authorityKeyIdentifier
#       → standard hygiene for path building; not strictly required
#         but produces cleaner cert chains
openssl x509 -req -in "$OUTDIR/$NAME.csr" \
    -CA "$CA_CRT" -CAkey "$CA_KEY" -CAcreateserial \
    -out "$OUTDIR/$NAME.crt" -days "$DAYS" \
    -extfile <(cat <<EOF
basicConstraints=critical,CA:TRUE,pathlen:0
keyUsage=critical,keyCertSign,cRLSign
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid,issuer
EOF
) 2>/dev/null

rm "$OUTDIR/$NAME.csr"
rm -f "$(dirname "$CA_CRT")/$(basename "$CA_CRT" .crt).srl" \
      "$(dirname "$CA_CRT")/ca.srl"
chmod 600 "$OUTDIR/$NAME.key"

echo "→ intermediate CA issued:"
echo "  $OUTDIR/$NAME.crt   (public — ship with every leaf cert as a chain)"
echo "  $OUTDIR/$NAME.key   (SECRET — keep, OR delegate to the party that will mint leaves)"
echo ""
echo "── chain summary ──"
openssl x509 -in "$OUTDIR/$NAME.crt" -noout -subject -issuer -dates \
    -ext basicConstraints -ext keyUsage
echo ""
echo "── chain verify (leaf → intermediate → root) ──"
openssl verify -CAfile "$CA_CRT" "$OUTDIR/$NAME.crt"
echo ""
echo "Next: sign leaf certs against this intermediate."
echo "  # Client leaf, then ship (client.crt + intermediate.crt) as chain to caller:"
echo "  CA_CRT=$OUTDIR/$NAME.crt CA_KEY=$OUTDIR/$NAME.key DAYS=365 \\"
echo "      ./scripts/sign-client-csr.sh <path-to-csr>"
echo ""
echo "  cat <leaf>.crt $OUTDIR/$NAME.crt > <leaf>-fullchain.crt"
echo "  # then TLS_CLIENT_CERT=<leaf>-fullchain.crt on the client side"
