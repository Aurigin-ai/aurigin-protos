# replay-simulator-service

Random-score gRPC simulator for
`aurigin.replay_detection.v1.ReplayDetection.DetectReplay`.

Buffers the client's `AudioFrame` stream into fixed-size windows
(default 3000 ms — matches the halo-3 replay-detector training config
in `aurigin-replay`) and emits one `AnalysisResult` per completed
window plus a terminal `FinalResult` at end-of-stream.

Each window's `score` is a uniform draw from `[0, 1)` — no dependency
on the audio payload. Purpose is to exercise the wire shape + client-
side handling paths (verdict routing, label mapping, threshold logic)
with realistic per-window variance. Reach for the fingerprint
simulator's deterministic-embedding pattern instead when
repeatability matters (golden-fixture diffs, byte-level round-trips).

Intentionally slimmer than the deepfake simulator: **no scenarios, no
YAML, no fault injection.** Add a scenario system if / when
score-behaviour tests need it.

## Run — Docker (recommended)

```bash
cd examples/simulator/replay
docker compose up --build
```

Listens on `localhost:50051` insecurely (no `./certs` volume mounted).

## Run — directly with `uv` (dev)

```bash
cd examples/simulator/replay
uv sync
uv run replay-simulator-service
```

Default port `50051`; override with `PORT=…`. Auto-detects
`examples/certs/server.{crt,key}` for TLS-by-default when running from
the repo tree — the same committed self-signed certs the deepfake +
fingerprint simulators use.

## Configuration

| Env | Default | Purpose |
|---|---|---|
| `PORT` | `50051` | gRPC listen port. |
| `WINDOW_MS` | `3000` | Window length in ms. Matches the real replay-service's `ANALYSIS_INTERVAL_S * 1000`. The halo-3 model is trained on 3.0-s windows; changing this silently degrades accuracy for anything but simulator-side stream tests. |
| `DECISION_THRESHOLD` | `0.5` | `score >= this` → `REPLAY_LABEL_REPLAY`, below → `REPLAY_LABEL_GENUINE`. |
| `TLS_CERT` / `TLS_KEY` | `<repo>/examples/certs/server.{crt,key}` | Server TLS keypair. Present → TLS; absent → insecure. |
| `TLS_CLIENT_CA` | `<repo>/examples/certs/client.crt` | Client-cert trust anchor when `MTLS=1`. |
| `MTLS` | (unset) | `1` / `true` / `yes` → require client cert chaining to `TLS_CLIENT_CA`. |

## Wire

Accepts `DetectReplayRequest.oneof request`:

- `create_session_request` — server assigns a deterministic
  `sim-<8 hex>` session id from `sha256(peer)`.
- `audio_frame` — Aurigin-native `AudioFrame` (self-describing codec +
  rate + channels). Supported codecs: `S16LE`, `S16BE`, `S24LE`,
  `S32LE`, `F32LE`, `PCMU`, `PCMA`. `OPUS` returns `UNIMPLEMENTED`
  (matches the real service's codec support).

Emits `DetectReplayResponse.oneof response`:

- `create_session_response` — once, after `create_session_request`.
- `analysis_result` — one per completed 3000-ms window. `score` in
  `[0, 1)`, `label_raw` in `{"genuine", "replay"}`, `label` mirrors as
  the `ReplayLabel` enum. `confidence` is `min(1, |score − threshold| × 2)`.
- `final_result` — once, at stream close. Aggregates: mean score
  (`overall_score`), worst-case label (`overall_label` — REPLAY wins),
  and analysis counter.

## Ports

Standard gRPC listen port `:50051` (matches every other Aurigin
consumer service — in production each pod binds `50051` in its own
network namespace). Override with `PORT=…` for local runs that need
to coexist with something else on the loopback.

### Running side-by-side with the deepfake / fingerprint simulators

All three sims default to `:50051`. Change the host-side port mapping
in `docker-compose.yml` (`- "50053:50051"`) or override the internal
port with `PORT=50053 docker compose up --build`, and point your
client at the new port via `--target localhost:50053`.
