# orchestrator-simulator-service

Minimal gRPC simulator for `aurigin.client.v1.AudioVerification` — the
SDK-facing surface that macOS / Windows / TypeScript SDKs + developer
API-key streaming clients target.

Answers `Stream` with a canned `Verdict` (BONAFIDE, score=0.5) every
5 s while the stream is open, then emits a terminal `FinalResult` when
the client half-closes. Useful as a connectivity + wire-shape smoke-test
target while a real orchestrator is being brought up.

## What it does not do

- **No scenarios / YAML** — a single hardcoded response loop. If you
  need scenario-driven behaviour, mirror the `deepfake` simulator's
  scenario loader.
- **No downstream dispatch** — the real orchestrator fans audio out to
  deepfake / fingerprint / recorder consumers over gRPC; this sim just
  replays canned messages in-process.
- **`Verify` is UNIMPLEMENTED** — unary file verification lands with the
  real orchestrator; SDK smoke tests use `Stream` + half-close.
- **No TLS** — insecure by default. Real deployments terminate TLS at
  an L7 gRPC-aware ingress in front of the orchestrator.

## Run — Docker (recommended)

```bash
cd examples/simulator/orchestrator
docker compose up --build
```

Listens on `localhost:50053` insecurely.

## Run — directly with `uv` (dev)

```bash
cd examples/simulator/orchestrator
uv sync
uv run orchestrator-simulator-service
```

Default port `50053`; override with `PORT=…`.

## Auth (mandatory by default)

Every `Stream` (and `Verify`) call MUST carry a Bearer token in gRPC
metadata — same header the real orchestrator will require, per
`aurigin.common.v1.Principal` (both JWT and API-key callers use it):

```
authorization: Bearer <token>
```

Missing / malformed / unrecognised tokens abort with `UNAUTHENTICATED`
BEFORE any application state is allocated.

Set `AUTH_ENABLED=false` to bypass the check entirely — every request
is accepted, no metadata is inspected. Intended for quick local smokes
only; never disable in a shared or public deployment.

The simulator accepts **two** demo tokens out of the box — one JWT-shaped
and one API-key-shaped — so SDK developers can exercise both the
`CALLER_TYPE_USER` (JWT) and `CALLER_TYPE_CLIENT` (API key) code paths
against the same server:

| Env var | Default (baked into the image) | Represents |
|---|---|---|
| `ORCHESTRATOR_SIM_JWT` | `eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJkZW1vLXVzZXIiLCJ0ZW5hbnQiOiJkZW1vLXRlbmFudCIsImlhdCI6MTcwMDAwMDAwMH0.aurigin-sim-demo-signature-not-verified` | Demo user JWT — `CALLER_TYPE_USER`. Signature is NOT verified — the simulator does exact string matching, not real JWT validation. |
| `ORCHESTRATOR_SIM_API_KEY` | `sk_test_aurigin_sim_demo_0000000000000000` | Demo API key — `CALLER_TYPE_CLIENT`. Matches the `sk_…` prefix the platform uses for developer keys. |

Override either env var to smoke-test against your own token; set to an
empty string to disable that flavour.

## Configuration

| Env | Default | Purpose |
|---|---|---|
| `PORT` | `50053` | gRPC listen port. |
| `AUTH_ENABLED` | `true` | When `false`, the simulator accepts every request unchecked (dev only). |
| `ORCHESTRATOR_SIM_JWT` | (see above) | Demo JWT accepted in `Bearer` metadata. Empty = disable JWT auth. Ignored when `AUTH_ENABLED=false`. |
| `ORCHESTRATOR_SIM_API_KEY` | (see above) | Demo API key accepted in `Bearer` metadata. Empty = disable API-key auth. Ignored when `AUTH_ENABLED=false`. |

## Wire

Accepts `aurigin.client.v1.AudioVerification.Stream` — bidi streaming.

Session lifecycle:

1. Client sends `CreateSessionRequest` (with optional `ClientSessionConfig`
   — the simulator ignores the config).
2. Server responds with `CreateSessionResponse` carrying a minted
   `session_id`.
3. Client streams `AudioFrame` messages. The simulator silently drains
   them — the audio content doesn't affect the response.
4. Every 5 s while the stream is open, server emits a canned `Verdict`
   (`consumer_name="deepfake"`, `label=BONAFIDE`, `score=0.5`,
   `confidence=0.5`).
5. On client half-close, server emits `FinalResult` with an aggregate
   per-consumer summary, then closes the stream.

## Ports

Standard AudioVerification listen port `:50053` — distinct from the
`deepfake` simulator (`:50051`) and `fingerprint` simulator (`:50052`)
so all three can run side-by-side on one host.

## Example client

See [`examples/python/orchestrator_client.py`](../../python/orchestrator_client.py)
for a minimal Python client that opens a session, streams either
synthesised silence (`--duration`), a single WAV (`--audio-file`),
or every `*.wav` in a directory (`--audio-dir`), and prints every
`Verdict` + `EmbeddingVerdict` + `FinalResult` it receives. A TypeScript
counterpart with matching `--audio-file` support lives in
[`examples/typescript/orchestrator_client.ts`](../../typescript/orchestrator_client.ts).

    # silence smoke-test
    uv run orchestrator-client --token <jwt> --target host.example:443

    # real audio (single file)
    uv run orchestrator-client --token <jwt> --target host.example:443 \
        --audio-file ../audio/922.wav

    # iterate the whole sample directory
    uv run orchestrator-client --token <jwt> --target host.example:443 \
        --audio-dir ../audio
