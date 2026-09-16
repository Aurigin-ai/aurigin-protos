# fingerprint-simulator-service

Deterministic gRPC simulator for
`aurigin.fingerprint.v1.Fingerprint.ExtractFingerprint`.

Buffers the client's `AudioFrame` stream into fixed-size windows
(default 5000 ms — matches the WavLM fine-tune training config in
`aurigin-fingerprint`) and emits one `EmbeddingResult` per completed
window plus a terminal `FinalResult` at end-of-stream.

Embeddings are derived deterministically from the first 64 bytes of the
window payload: `sha256(payload[:64])` seeds a PRNG that fills a 768-d
float32 vector, L2-normalised. Same input → same embedding on any
machine — useful for smoke tests that assert on `fingerprint_code`,
golden-fixture diffs across releases, and client-side integration tests
that exercise the full stream shape without needing a real GPU-backed
backend.

Intentionally slimmer than the deepfake simulator: **no scenarios, no
YAML, no fault injection.** Fingerprint's output is a vector, not a
classification decision, so scenario-shaped behaviour variation isn't
the right tool for embedding-behaviour tests. If we ever need it,
adding a scenario system later is a compatible extension.

## Run — Docker (recommended)

```bash
cd examples/simulator/fingerprint
docker compose up --build
```

Listens on `localhost:50051` insecurely (no `./certs` volume mounted).

## Run — directly with `uv` (dev)

```bash
cd examples/simulator/fingerprint
uv sync
uv run fingerprint-simulator-service
```

Default port `50051`; override with `PORT=…`. Auto-detects
`examples/certs/server.{crt,key}` for TLS-by-default when running from
the repo tree — the same committed self-signed certs the deepfake
simulator uses.

## Configuration

| Env | Default | Purpose |
|---|---|---|
| `PORT` | `50051` | gRPC listen port. |
| `WINDOW_MS` | `5000` | Window length in ms. Matches the real fingerprint-service's `MODEL_WINDOW_SECONDS * 1000`. Change only for testing client-side windowing behaviour. |
| `EMBEDDING_DIM` | `768` | Dimensionality of the returned unit vectors. Matches `microsoft/wavlm-base-plus`. |
| `TLS_CERT` / `TLS_KEY` | `<repo>/examples/certs/server.{crt,key}` | Server TLS keypair. Present → TLS; absent → insecure. |
| `TLS_CLIENT_CA` | `<repo>/examples/certs/client.crt` | Client-cert trust anchor when `MTLS=1`. |
| `MTLS` | (unset) | `1` / `true` / `yes` → require client cert chaining to `TLS_CLIENT_CA`. |

## Wire

Accepts `ExtractFingerprintRequest.oneof request`:

- `create_session_request` — server assigns a deterministic
  `sim-<8 hex>` session id from `sha256(peer)`.
- `audio_frame` — Aurigin-native `AudioFrame` (self-describing codec +
  rate + channels). Supported codecs: `S16LE`, `S16BE`, `S24LE`,
  `S32LE`, `F32LE`, `PCMU`, `PCMA`. `OPUS` returns `UNIMPLEMENTED`
  (matches the real service's codec support). No legacy
  `twilio.AudioBuffer` fork — the fingerprint proto never carried it.

## Ports

Standard gRPC listen port `:50051` (matches every other Aurigin
consumer service — in production each pod binds `50051` in its own
network namespace). Override with `PORT=…` for local runs that need
to coexist with something else on the loopback.

### Running side-by-side with the deepfake simulator

Both sims default to `:50051`. Two ways to bring them up together:

**Direct (uv):**

```bash
# Terminal 1 — deepfake on the default port
cd examples/simulator/deepfake
uv run deepfake-simulator-service                # :50051

# Terminal 2 — fingerprint on a different port
cd examples/simulator/fingerprint
PORT=50052 uv run fingerprint-simulator-service  # :50052

# Clients — point each at its sim's port
cd examples/python
uv run client              --target localhost:50051     # → deepfake
uv run fingerprint-client  --target localhost:50052     # → fingerprint
```

**Docker Compose:**

Change this simulator's `ports:` line to `"50052:50051"` (host-side
50052 → container-internal 50051), leave the deepfake sim on
`"50051:50051"`. Bring both up:

```bash
(cd examples/simulator/deepfake     && docker compose up --build -d)
(cd examples/simulator/fingerprint  && docker compose up --build -d)
```

Neither the client nor the proto cares which port — `--target
HOST:PORT` on any example client points it wherever the sim listens.
