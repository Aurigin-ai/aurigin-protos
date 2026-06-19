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

**Plain TLS (default).** `server.py` / `server.ts` and `client.py` /
`client.ts` / `phone_call.{py,ts}` all look at `examples/certs/server.crt`
(and `server.key` on the server side) at startup. If both files exist —
the default, since this directory is committed — they switch to TLS
automatically.

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
rotate the keypairs or change the SANs.

## When you ship anything real

Replace this directory's contents with a cert from a real CA (Let's Encrypt
for public, internal PKI for private), or skip TLS at the gRPC layer and
terminate TLS at a reverse proxy like Caddy / Traefik / Envoy. See the main
`examples/README.md` for the trust-model breakdown.
