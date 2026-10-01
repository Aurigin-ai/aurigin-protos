# recording-simulator-service

Byte-counting gRPC simulator for
`aurigin.recording.v1.Recording.Record`.

Pure sink — reads the client's `AudioFrame` stream, discards the
payload, tracks a running byte + duration count, and emits a
`RecordingComplete` at end-of-stream. No actual blob is written; the
simulator returns a synthetic `blob_uri` (`sim://rec-<hex>.wav`) and
a real SHA-256 over the received payload bytes so downstream integrity-
verification paths can be exercised end-to-end without provisioning
blob storage.

Intentionally slim: **no scenarios, no YAML, no fault injection, no
disk write.** Add a scenario system if / when behaviour tests need
one.

## Run — Docker (recommended)

```bash
cd examples/simulator/recording
docker compose up --build
```

Listens on `localhost:50051` insecurely (no `./certs` volume mounted).

## Run — directly with `uv` (dev)

```bash
cd examples/simulator/recording
uv sync
uv run recording-simulator-service
```

Default port `50051`; override with `PORT=…`. Auto-detects
`examples/certs/server.{crt,key}` for TLS-by-default when running from
the repo tree — the same committed self-signed certs the deepfake +
fingerprint + replay simulators use.

## Configuration

| Env | Default | Purpose |
|---|---|---|
| `PORT` | `50051` | gRPC listen port. |
| `OUTPUT_FORMAT` | `wav` | Container suffix declared in the synthetic `blob_uri`. Cosmetic — the simulator never actually encodes anything. |
| `TLS_CERT` / `TLS_KEY` | `<repo>/examples/certs/server.{crt,key}` | Server TLS keypair. Present → TLS; absent → insecure. |
| `TLS_CLIENT_CA` | `<repo>/examples/certs/client.crt` | Client-cert trust anchor when `MTLS=1`. |
| `MTLS` | (unset) | `1` / `true` / `yes` → require client cert chaining to `TLS_CLIENT_CA`. |

## Wire

Accepts `RecordRequest.oneof request`:

- `create_session_request` — server assigns a fresh `rec-<32 hex>`
  session id and returns a `planned_blob_uri = sim://<id>.<format>`.
- `audio_frame` — Aurigin-native `AudioFrame` (self-describing codec +
  rate + channels). Supported codecs: `S16LE`, `S16BE`, `S24LE`,
  `S32LE`, `F32LE`, `PCMU`, `PCMA`. `OPUS` returns `UNIMPLEMENTED`.

Emits `RecordResponse.oneof response`:

- `create_session_response` — once, after `create_session_request`,
  carrying `recording_id` + `planned_blob_uri`.
- `progress` — never (reserved on the proto for a future heartbeat).
- `complete` — once, at stream close. Carries the final `recording_id`,
  `blob_uri` (equal to the planned one here — the simulator doesn't
  rotate), `duration_ms`, `bytes`, `sha256`, `codec`, `sample_rate_hz`.

## Ports

Standard gRPC listen port `:50051` (matches every other Aurigin
consumer service — in production each pod binds `50051` in its own
network namespace). Override with `PORT=…` for local runs that need
to coexist with something else on the loopback.

### Running side-by-side with the deepfake / fingerprint / replay simulators

All four sims default to `:50051`. Change the host-side port mapping
in `docker-compose.yml` (`- "50054:50051"`) or override the internal
port with `PORT=50054 docker compose up --build`, and point your
client at the new port via `--target localhost:50054`.
