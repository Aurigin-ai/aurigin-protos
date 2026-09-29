# Example TLS material — **DO NOT USE IN PRODUCTION**

Four self-signed ECDSA P-256 files committed to this repo so the example
server and clients enable TLS (and optionally mTLS) out of the box with
zero setup.

- **`server.crt`** — self-signed server certificate. SANs cover
  `localhost`, `127.0.0.1`, `::1` so any of those targets verify cleanly.
- **`server.key`** — matching private key for `server.crt`.
- **`client.crt`** — self-signed client certificate, used only when
  `MTLS=1`. Doubles as its own CA: the server is configured to verify
  client certs against this file directly, no intermediate CA in between.
- **`client.key`** — matching private key for `client.crt`.

**All four keys are public.** Anyone with a copy of this repo can
impersonate the example server *and* the example client. That's fine for
a getting-started example; it's a disaster for anything customer-facing.

## How it's wired up

**Plain TLS (default).** The simulator server
(`examples/simulator/deepfake/`) and the client examples (`client.py`
/ `client.ts` / `phone_call.{py,ts}`) all look at
`examples/certs/server.crt` (and `server.key` on the server side) at
startup. If both files exist — the default, since this directory is
committed — they switch to TLS automatically.

**mTLS (opt-in via `MTLS=1`).** Set `MTLS=1` on the server *and* the
client process. The server then demands a client cert chaining to
`examples/certs/client.crt`; the client presents `client.crt` +
`client.key`. With `MTLS=1` set on only one side the handshake fails
fast (`UNAVAILABLE` on the client). With `MTLS=1` set but the
`client.{crt,key}` files missing, both sides silently fall back to plain
TLS and print a notice in the transport header.

To force insecure mode, either:

- delete the server files (`rm examples/certs/server.{crt,key}`), or
- override the path with the env vars `TLS_CERT` / `TLS_KEY` / `TLS_CA`
  pointing at `/dev/null` or a non-existent path.

To toggle mTLS off without removing the cert files, just unset `MTLS`
(or set `MTLS=0`).

## Regenerating

```bash
# from examples/python/
just tls

# or from examples/typescript/
npm run tls
```

Both invoke the same OpenSSL commands and write all four files
(`server.{crt,key}` + `client.{crt,key}`). Re-run only if you want to
rotate the key pairs or change the SANs.

## When you ship anything real

Replace this directory's contents with a cert from a real CA (Let's Encrypt
for public, internal PKI for private), or skip TLS at the gRPC layer and
terminate TLS at a reverse proxy like Caddy / Traefik / Envoy. See the main
`examples/README.md` for the trust-model breakdown.

For an internal-PKI mTLS setup between the target gRPC server and its
callers (any mix of internal services and external clients), keep
reading — `just mtls-bundle` / `just mtls-client-csr` / `just mtls-sign-csr`
recipes cover the full lifecycle.

## Proper mTLS setup (CA-based)

Everything above (`just tls`, `client.{crt,key}` acting as its own CA) is
a shortcut for local dev. Real deployments need a CA that signs both the
server cert and per-caller client certs, so the trust chain is:

```
                      root CA (ca.crt / ca.key)
                                │
                ┌───────────────┼───────────────┐
                ▼               ▼               ▼
          server.crt      client-a.crt     client-b.crt   ...
          (on backend     (on caller A)    (on caller B)
           nodes)
```

The gRPC server is configured with `TLS_CLIENT_CA=<path>/ca.crt` — it
trusts **any client cert signed by that CA**, no per-client
configuration.

### Two trust models — pick per client

Depending on who runs the PKI for a given client, the trust chain looks
different. The server can accept both models **simultaneously** —
`TLS_CLIENT_CA` can hold multiple concatenated CA certs, so some clients
chain to your CA and others chain to their own.

| Model | Who runs the CA | Who issues client certs | When to use |
|---|---|---|---|
| **A — you issue** (default) | You (the CA operator) | You, via the CSR flow below | Small deployments, internal services, customers without their own PKI, or when you want unified CN naming |
| **B — customer brings their own CA** | The customer's org | The customer, using their own tooling — you never see their CSRs or issue their certs | Enterprise customers with existing corporate PKI (they refuse to use anyone else's CA), regulatory setups requiring a customer-controlled key ceremony, multi-tenant deployments where each tenant is its own trust domain |

The trust chain when both are active:

```
                          server's TLS_CLIENT_CA bundle
                          (ca-yours.crt + ca-customer.crt concatenated)
                                │
                    ┌───────────┴────────────┐
                    ▼                        ▼
              your root CA          customer's root CA
              (you run, ca.key       (customer runs,
               stays with you)        you never see their ca.key)
                    │                        │
             ┌──────┴──────┐          ┌──────┴──────┐
             ▼             ▼          ▼             ▼
       client-a.crt  client-b.crt  their-1.crt  their-2.crt
       (you signed)  (you signed)  (they signed)  (they signed)
```

Server accepts all four clients — the CN in each cert tells you which
caller is which, but the mTLS trust decision is per-CA, not per-client.

### Model B — customer brings their own CA

The customer runs their own PKI: they have their own root CA, they
generate keys on their own machines, they issue their own client certs.
You never touch their CSRs or their signing key. Your only involvement
is agreeing to trust their CA, one-time per customer.

Below is the end-to-end timeline. Every step names who acts and what
they hand over.

#### Phase 1 — onboarding a new customer (one-time, per customer CA)

**Step 1 — bilateral exchange.** Before touching anything, the two sides
swap what each needs from the other:

| Direction | Who → Who | What is handed over | Why |
|---|---|---|---|
| ← from customer | Customer → **you** | Their **root CA certificate** (`.crt` file, public — never their CA private key) | So your server can verify their client certs |
| ← from customer | Customer → **you** | Their intended CN naming convention (e.g. "we'll use `customer-a-<env>-<hostname>`") | Lets you configure a SAN allowlist later if you enforce one; also lets ops make sense of audit logs |
| → to customer | **You** → Customer | **Your** `ca.crt` | So their client can verify your server's cert (`TLS_CA` env on their side) |
| → to customer | **You** → Customer | Server endpoint (e.g. `192.0.2.10:50051`) | So they know where to connect |

Typical channel: email / customer portal. Everything exchanged in this
step is public (certs, hostnames, naming policies). No keys, no CSRs
change hands.

**Step 2 — you update the server's trust bundle.** On the CA operator
machine, append the customer's CA to your existing bundle:

```bash
# If you had only your own CA in the bundle:
cat ca-yours.crt customer-a-ca.crt > client-ca-bundle.pem

# Onboarding a second customer later — append theirs too:
cat ca-yours.crt customer-a-ca.crt customer-b-ca.crt > client-ca-bundle.pem
```

Order inside the bundle doesn't matter. OpenSSL treats it as a set of
trusted roots.

**⚠ Understand what this grants:** adding a CA to the bundle means
**every certificate that chains to that CA is accepted at the TLS
layer** — not just the ones the customer intends for your service. If
their CA is a narrow-purpose one (minted specifically for this
integration), that's fine. If it's a **broad enterprise root** they
also use for VPN / employee laptops / internal HTTPS / IoT / etc., you
just extended your server's trust to their entire PKI. Ask them what
the CA's scope is. If it's broader than what you want to accept, turn
on SAN allowlisting (see "Defense in depth" below) to filter which
CNs / SANs from that trust domain are actually allowed in.

**Step 3 — deploy the new bundle to each backend node and restart.**

```bash
scp client-ca-bundle.pem <backend-node>:/certs/client-ca-bundle.pem
# Then on the node:
systemctl restart <your-service>
```

Ensure the server's env points at the bundle:

```
TLS_CLIENT_CA=/certs/client-ca-bundle.pem
```

**⚠ Restart IS required here.** Unlike adding a client under an
already-trusted CA (zero-downtime), adding a *new CA* to the bundle
means the TLS stack has to re-read the trust anchors at boot. For a
multi-node cluster, do a rolling restart so the service stays up.

#### Phase 2 — steady state (customer onboards their own users)

Once Phase 1 is done, **you are no longer involved** for that customer.
The customer's flow, on their side, using their own tooling:

1. Customer's user requests access internally.
2. Customer's CA signs a new client cert against **their** CA
   (their CN, their validity policy, their process).
3. Customer gives the user their `<user>.crt` + `<user>.key`.
4. User configures their client:
   ```bash
   export MTLS=1
   export TLS_CA=<your ca.crt>                # from Phase 1, step 1
   export TLS_CLIENT_CERT=<their user.crt>    # signed by customer's CA
   export TLS_CLIENT_KEY=<their user.key>     # generated on user's machine
   ```
5. User connects. Server accepts because the customer's CA is in the
   bundle. Server logs show `x509_common_name: ["<their-CN>"]`.

**You never see the CSR, you never see the client cert, you don't
restart anything, you don't touch a config.** The customer scales their
own user base entirely on their own side.

#### Phase 3 — customer rotates their CA (rare, but plan for it)

Customers' CAs expire or get rotated too. When it happens:

| Direction | Who → Who | What is handed over |
|---|---|---|
| ← from customer | Customer → you | Their **new** root CA certificate |

You:

1. Append the new CA cert alongside the old one in the bundle (both
   during the overlap window):

   ```bash
   cat ca-yours.crt customer-a-ca-old.crt customer-a-ca-new.crt > client-ca-bundle.pem
   ```

2. Rolling-restart backends.
3. After all their client certs have migrated to the new CA (customer
   tells you when), remove the old CA and restart again:

   ```bash
   cat ca-yours.crt customer-a-ca-new.crt > client-ca-bundle.pem
   ```

#### Cheat sheet: what you receive from / send to a customer under Model B

| When | ← Receive from customer | → Send to customer |
|---|---|---|
| Phase 1 onboarding | Their root CA cert (once) | Your `ca.crt` (once), server endpoint (once) |
| Phase 2 steady state | Nothing | Nothing |
| Phase 3 CA rotation | Their new root CA cert (only when they rotate) | Nothing |

**Never in any phase**: you do not receive private keys, CSRs, or signed
client certs from the customer. You never send them signed certs or your
CA private key.

#### Trade-offs vs Model A

| Concern | Model A (you issue) | Model B (customer's CA) |
|---|---|---|
| CN naming convention | You control it — uniform | Customer chooses — expect variety |
| Rotation cadence for clients | You enforce (via `DAYS`) | Customer's own policy |
| Revocation of a specific client | Cert expires (no CRL here) | Customer's problem, out of your control |
| Onboarding a new client of that customer | You'd sign each CSR | Zero touch on your side |
| Server restart to trust a new client | No | No (they use existing trusted CA) |
| Server restart to onboard a new CA | Only for your own CA change | Yes — per Phase 1 above |
| Compromise blast radius | One bad `ca.key` — your whole trust domain | One bad customer CA — only that customer's clients |

#### Defense in depth for Model B

The trust bundle grants access at the CA level: any cert chaining to
the customer's CA passes the TLS handshake. If the customer's CA is
narrow-purpose ("we minted this only for the integration with you"),
that's exactly what you want. But if their CA is a broad enterprise
root used across their whole org — VPN, HTTPS, employee laptops, IoT —
you've inherited that entire trust surface. Their laptop VPN cert can
now handshake with your server.

Two layers of mitigation:

1. **Ask the customer for a narrow CA.** In Phase 1 step 1, if they can
   mint a dedicated intermediate CA scoped to just the client identities
   that should reach you, use that instead of their broad root. Many
   customers with proper PKI teams can do this on request.
2. **SAN allowlist on the server** (if supported by the deployment).
   Configure it with the CN or SAN patterns the customer told you in
   Phase 1 — the server then re-checks every accepted cert against the
   list and rejects anything not on it, even if the TLS chain was
   valid. This is the belt-and-suspenders answer when you can't get a
   narrow CA.

### Files produced by the mTLS workflow

Written to `examples/certs/mtls/` when you run `just mtls-bundle`:

| File | Role | Where it lives after deployment |
|---|---|---|
| `ca.crt` | Root CA cert (public) | Server(s) + every client |
| `ca.key` | Root CA private key ⚠ | **CA operator machine only** — never on servers or clients |
| `server.crt`, `server.key` | Server identity (SANs cover the LB address / DNS) | Every backend node (identical files) |
| `<cn>.crt`, `<cn>.key` | Per-client identity | The client machine only — `.key` NEVER shared |

### Initial bundle — one-time setup

Generate the CA, the server cert, and any initial client certs. Set
`SERVER_SANS` to include every hostname/IP the client will use to
connect. Use `DAYS` for the validity window (short = more rotation
work, long = weaker security posture).

```bash
cd examples/python

SERVER_SANS="IP:<PUBLIC-IP-OR-LB-VIP>,DNS:<PUBLIC-DNS>,DNS:localhost,IP:127.0.0.1" \
    DAYS=90 \
    just mtls-bundle
```

Other env overrides:

- `ORG` — Organization name baked into every cert's `O=` field. Default: `Example`.
- `CA_CN` — CA cert Common Name. Default: `mtls-ca`.
- `SERVER_CN` — server cert Common Name. Default: `server`.
- `CLIENT_COUNT` — how many `client-N.{crt,key}` pairs to pre-mint alongside the server. Default: `3`. Set to `0` if you only want CA + server and will onboard clients via the CSR flow below.

What to hand out:

- **To each backend node**: `ca.crt`, `server.crt`, `server.key`. Wire
  the env vars `TLS_CERT` / `TLS_KEY` / `TLS_CLIENT_CA` accordingly and
  restart the service.
- **Keep on the CA operator machine**: `ca.key` (never leaves).

### Atomic scripts — one operation at a time

`just mtls-bundle` is a convenience wrapper. It composes three
lower-level scripts you can also run standalone when you need to do
just one thing (rotate the server cert without touching the CA, mint an
extra internal client, keep the CA on a separate machine from the certs
it signs, etc.).

Each atomic script accepts a `<name>` that becomes **both the cert's
Common Name and the output filename prefix** — no coupling between "what
the cert says it is" and "where it lives on disk".

#### `just mtls-ca NAME [OUTDIR]` — create a root CA

Creates a self-signed root CA. Run **once per trust domain** — one for
your own PKI, one per customer that brings their own (Model B), etc.

| Parameter | What to supply | Example |
|---|---|---|
| `NAME` | CA identity — becomes the CN + filename base | `my-company-root`, `customer-a-ca`, `internal-mtls-ca` |
| `OUTDIR` | Optional. Where to write the two files | `/secure/pki`, `./certs` |

**Env overrides:**

| Var | Default | Notes |
|---|---|---|
| `DAYS` | `365` | Root CAs typically outlive individual certs — `3650` (10 years) is a common choice for a real root |
| `ORG` | `Example` | Baked into the cert's `O=` field |

**Output:** `<OUTDIR>/<NAME>.crt` (public) + `<OUTDIR>/<NAME>.key` (SECRET
— keep on the CA operator machine).

**Example:**

```bash
DAYS=3650 just mtls-ca my-company-root /secure/pki
# → /secure/pki/my-company-root.crt   (distribute to servers as TLS_CLIENT_CA + to clients as TLS_CA)
# → /secure/pki/my-company-root.key   (SECRET — never leaves the CA machine)
```

#### `just mtls-server-cert NAME SANS [OUTDIR]` — mint a server cert

Signs a server cert against an existing CA. Use to **rotate the server
identity** without regenerating the CA (its trust anchor is untouched,
existing clients keep working), or to issue server certs in a workflow
separate from CA creation.

| Parameter | What to supply | Example |
|---|---|---|
| `NAME` | Server identity — CN + filename base | `server`, `deepfake-prod-01`, `svc-eu-west` |
| `SANS` | Comma-separated `IP:<addr>` / `DNS:<name>` entries. **NO port numbers in IP entries** — SANs identify the host, the port is a connection detail | `"IP:192.0.2.10,DNS:svc.example.internal,DNS:localhost,IP:127.0.0.1"` |
| `OUTDIR` | Optional | `/secure/certs` |

**Required env (path to the signing CA):**

| Var | What to supply |
|---|---|
| `CA_CRT` | Path to the CA cert (created by `just mtls-ca`) |
| `CA_KEY` | Path to the matching CA private key |

**Env overrides:** `DAYS` (default `365`), `ORG` (default `Example`).

**Common mistake:** putting a port in an IP SAN entry (`IP:192.0.2.10:50051`).
The script catches this and rejects with a clear error before openssl
produces a cryptic one. Fix: drop the `:<port>` — SANs are about
identity, not endpoints.

**Example — rotate the server cert without regenerating CA:**

```bash
CA_CRT=/secure/pki/my-company-root.crt \
CA_KEY=/secure/pki/my-company-root.key \
DAYS=90 \
    just mtls-server-cert server-2026-q4 \
    "IP:192.0.2.10,DNS:svc.example.internal,DNS:localhost,IP:127.0.0.1" \
    /secure/certs
# → /secure/certs/server-2026-q4.{crt,key}
# Deploy the new pair to backend nodes, rolling-restart. Existing client
# certs keep working — CA is unchanged.
```

#### `just mtls-client-cert NAME [OUTDIR]` — mint a client cert (local shortcut)

Signs a client cert **and** generates the client's key on the local
machine. Use for internal services you own end-to-end, dev testing, or
initial rollout where you're bootstrapping several clients quickly.

**Not for onboarding real remote/customer clients** — their private key
would end up on your machine. For that, use the CSR round-trip below
(Model A, section further down).

**Why no SANs argument (unlike `mtls-server-cert`)?** TLS hostname
verification is one-directional: the client verifies the *server's* SANs
against the address it dialed, but the server does not verify the
client's SANs against anything — the client has no well-known address
to check. So client certs typically carry only a CN (which shows up in
the server's audit log) and no SANs. Edge cases where client SANs do
matter (server-side SAN allowlisting, workload-identity URI SANs,
human-identity email SANs) are rare enough that this script leaves them
out; use openssl directly if you need one.

| Parameter | What to supply | Example |
|---|---|---|
| `NAME` | Client identity — CN + filename base. Shows up in server audit logs | `internal-service-a`, `test-user-01` |
| `OUTDIR` | Optional | `/tmp/certs` |

**Required env** (same as `mtls-server`): `CA_CRT`, `CA_KEY`.

**Env overrides:** `DAYS` (default `365`), `ORG` (default `Example`).

**Example — mint a fresh client cert for an internal service:**

```bash
CA_CRT=/secure/pki/my-company-root.crt \
CA_KEY=/secure/pki/my-company-root.key \
    just mtls-client-cert my-internal-svc /secure/certs
# → /secure/certs/my-internal-svc.{crt,key}
# Ship both files to the client host, wire TLS_CLIENT_CERT/KEY,
# set MTLS=1. Server needs no changes (CA already trusted).
```

#### Recipes at a glance

| Recipe | Use when |
|---|---|
| `just mtls-bundle` | Fresh install / demo — CA + server + N clients in one shot with sensible defaults |
| `just mtls-ca NAME` | You want just a CA — perhaps to keep it on a separate machine from the certs it signs |
| `just mtls-server-cert NAME SANS` | Rotate the server cert (LB IP change, cert expiring, key compromise) without disturbing the CA or existing client certs |
| `just mtls-client-cert NAME` | Mint a client cert for an internal service you own — dev shortcut |
| `just mtls-client-csr CN` | **On the client's machine** — the customer / remote client runs this to generate their own key + CSR, sends CSR to you |
| `just mtls-sign-csr CSR` | **On your machine** — sign a customer-provided CSR, hand the signed cert back |

### Model A — you issue the client cert (CSR workflow)

Use this when the client doesn't bring their own PKI. The client generates
its own key pair, sends you only a CSR, you sign it, you send back the
signed cert. The client's private key never leaves their machine. **The
server does NOT need a restart or config change** — it already trusts the
CA, so any new cert signed by that CA is accepted on the next connection.

#### Step 1 — the client generates their key + CSR

On the **client's machine** (their laptop, their service host, wherever
the connection originates):

```bash
cd examples/python
just mtls-client-csr <cn> [<outdir>]
```

**You (CA operator) tell the client which parameters to use:**

| Parameter | What to supply | Example |
|---|---|---|
| `<cn>` | Common Name — a unique identifier for this client. Shows up in the server's `x509_common_name` audit log. Pick a naming convention (`<org>-<env>-<seq>`) and stick to it | `client-prod-01`, `customer-a-lab`, `external-caller-42` |
| `<outdir>` | Optional. Where to write the two output files. Defaults to current dir | `/tmp/certs`, `~/certs` |

Optional env override the client can set:

- `ORG` — Organization for the CSR's `O=` field. Default: `Example`.
  Only matters if you want a specific org name in the cert Subject.

The client also needs, from you, upfront:

- `ca.crt` (the CA cert — needed for `TLS_CA` env var so their client
  trusts your server)
- The server endpoint (e.g. `192.0.2.10:50051` or
  `svc.example.internal:50051`)

**Output:**

- `<outdir>/<cn>.key` — private key. **Client must keep this secret**
  and never send it anywhere.
- `<outdir>/<cn>.csr` — CSR. Public info, safe to send over any channel.

#### Step 2 — the client sends you the CSR

Email, S3 bucket, secure file transfer, whatever. CSRs are public — they
contain the client's public key + the identity they're claiming. The
sensitive part (private key) stays on their machine.

#### Step 3 — you sign the CSR

On **your (CA operator's) machine**, where `ca.crt` + `ca.key` live:

```bash
cd examples/python
just mtls-sign-csr <path-to-csr> [<outdir>]
```

**Parameters:**

| Parameter | What to supply | Example |
|---|---|---|
| `<path-to-csr>` | The CSR file the client sent you | `/tmp/incoming/client-prod-01.csr` |
| `<outdir>` | Optional. Where to write the signed cert. Defaults to the CSR's directory | `/tmp/signed` |

**Env overrides (usually not needed):**

| Env var | Default | When to set |
|---|---|---|
| `CA_CRT` | `examples/certs/mtls/ca.crt` | If your CA lives elsewhere |
| `CA_KEY` | `examples/certs/mtls/ca.key` | Same |
| `DAYS` | `365` | Match the client's rotation cadence (90 for short-lived, 365 for annual) |

**Before signing:** the script prints the CSR's declared Subject so you
can eyeball it. **CSRs are self-signed by the client** — meaning a client
could claim any CN. Your job as CA is the identity gate: only sign CSRs
where you've verified out-of-band (email confirmation, known-good
source, etc.) that the requester really is who they claim to be.

**Output:**

- `<outdir>/<cn>.crt` — signed cert. Send it back to the client.

#### Step 4 — send the signed cert back to the client

Any channel — certs are public. The client saves it next to their `.key`.

#### Step 5 — client wires it up

**No server-side change. No restart.** The client just points their app at
the new cert + their existing key + your CA:

```bash
export MTLS=1
export TLS_CA=<path>/ca.crt              # from you, step 1
export TLS_CLIENT_CERT=<path>/<cn>.crt   # from you, step 4
export TLS_CLIENT_KEY=<path>/<cn>.key    # generated locally, step 1
```

Next connection to the server works. Server logs show
`x509_common_name: ["<cn>"]` in the `session peer` (or equivalent) event
— you can now attribute every session to a specific caller.

### Convenience shortcut (dev/test only)

For local testing where you control both the CA machine and the "client"
machine (or during initial rollout), you can do steps 1 + 3 back-to-back
in one shell:

```bash
just mtls-client-csr <cn> examples/certs/mtls
just mtls-sign-csr    examples/certs/mtls/<cn>.csr examples/certs/mtls
rm                    examples/certs/mtls/<cn>.csr   # tidy up
```

**Do not use this pattern to onboard a real remote client** — the
client's private key ends up on your machine, which defeats the purpose
of the CSR workflow.

### Renewal

Three flavours of renewal, each with different blast radius. Pick the
one that matches what's expiring.

#### Rotating a single client cert

Cheapest — no coordination beyond that one client. Repeat steps 1-5 of
the CSR workflow above for that client. Same `<cn>` if you want
continuity, or bump a version suffix (`client-prod-02`) if you want the
old cert clearly rotated-by-name. The old cert stays valid until its
`notAfter` — no way to force-revoke without a CRL, which these scripts
don't cover.

#### Rotating the server cert (CA unchanged)

Use when the server cert is expiring, the LB address changed, or the
server private key was compromised — but the CA is still healthy.
Mint a new server cert against the existing CA, deploy to every
backend, rolling restart:

```bash
CA_CRT=/secure/pki/ca-yours.crt CA_KEY=/secure/pki/ca-yours.key DAYS=90 \
    just mtls-server-cert server-new "IP:<ip>,DNS:<dns>,DNS:localhost,IP:127.0.0.1" /secure/certs
# scp to backends, rolling restart
```

Clients don't need to change anything — the trust chain is unchanged
(same CA in their `TLS_CA`), the new server cert just presents a
different key that chains to that same CA.

#### Rotating an expiring CA (the big one)

This is the operationally largest procedure — every server AND every
client is touched. Do it in 5 phases with an overlap window so
nothing breaks mid-rotation. Start Phase 0 **at least 60 days before
the old CA expires**, budgeting weeks for client migration in Phase 4.

If you skip the overlap and just swap the CA on expiry day, every
client is broken until they get the new cert. Overlap = zero-downtime.

**Phase 0 — prep** (start ~60 days before old CA expires; deploy nothing
yet). Generate the new CA and a new server cert signed by it:

```bash
# New CA — long-lived
DAYS=3650 just mtls-ca ca-yours-new /secure/pki
# New server cert signed by the new CA
CA_CRT=/secure/pki/ca-yours-new.crt CA_KEY=/secure/pki/ca-yours-new.key DAYS=90 \
    just mtls-server-cert server-new \
    "IP:<ip>,DNS:<dns>,DNS:localhost,IP:127.0.0.1" /secure/certs
```

**Phase 1 — servers trust BOTH CAs.** Concatenate old + new CA into the
server's trust bundle, deploy to every backend, rolling restart:

```bash
cat ca-yours.crt ca-yours-new.crt > client-ca-bundle.pem
scp client-ca-bundle.pem <backend>:/certs/client-ca-bundle.pem
# rolling restart on each backend
```

Server now accepts client certs chaining to either CA. Nothing else is
touched yet.

**Phase 2 — every client trusts BOTH CAs.** Send `ca-yours-new.crt` to
every client operator (internal services + Model A customers + Model B
customers — everyone whose `TLS_CA` currently points at your old CA
needs the new one too, so they can still verify the server cert after
Phase 3). Their instruction: concat, don't replace:

```bash
cat ca-yours.crt ca-yours-new.crt > new-ca-bundle.crt
# then set TLS_CA=new-ca-bundle.crt
```

Now clients trust the server cert regardless of which CA signed it.

**Phase 3 — rotate the server cert to the new-CA-signed one.** Deploy
`server-new.crt` + `server-new.key` (from Phase 0) to every backend,
rolling restart. Clients accept because they trust both CAs from
Phase 2.

**Phase 4 — rotate every client cert to be signed by the new CA.** Over
weeks; each client swaps at their own pace. For every incoming CSR,
sign against the new CA:

```bash
CA_CRT=/secure/pki/ca-yours-new.crt CA_KEY=/secure/pki/ca-yours-new.key DAYS=365 \
    just mtls-sign-csr /path/to/incoming.csr
```

Server accepts new-CA-signed cert because the bundle still has both
CAs. Track migration status per client — you need to know when Phase 5
is safe to run.

**Phase 5 — cleanup, after every client has migrated.** Remove the old
CA from the server's bundle. Tell clients they can drop it from their
`TLS_CA`. Archive the old CA files for audit purposes (don't delete
outright — you may want to verify old signatures against historical
data).

```bash
# server-side: bundle now has only the new CA
cp ca-yours-new.crt client-ca-bundle.pem
scp client-ca-bundle.pem <backend>:/certs/
# rolling restart
```

Old CA can now expire without consequence — nothing trusts it anymore.

**Special case: CA private key compromise.** Skip the overlap. This is
an emergency — the old CA is actively dangerous, not just about to
expire. Regenerate + deploy in one rush; every legitimate client is
disrupted, but that's the correct trade-off vs continuing to trust a
compromised key. Have this runbook rehearsed before you need it.

**Model B customers during your CA rotation.** Their client certs chain
to *their* CA (which is untouched by your rotation), so those keep
working. But they need your new CA cert in *their* `TLS_CA` to verify
your rotated server cert. Send it to them in Phase 2 like any other
client.

### Security hygiene

- **`ca.key` is the crown jewel.** Anyone with it can mint client certs
  the server will accept. Keep it on the CA operator machine. Don't
  commit it. Don't put it on any server or client. Consider an offline
  / hardware-backed store for production PKI.
- **Every client's private key stays on its machine.** The whole point
  of the CSR workflow is that the private key never travels.
- **CSRs and certs are public.** Send them over email / Slack / whatever
  — no need for encrypted channels.
- **The server needs `ca.crt` only** — not `ca.key`. Same for every
  client. If any deployment surface (config, systemd unit, backup)
  holds `ca.key`, that's a leak.
