# deepfake-simulator-service

Scenario-driven gRPC simulator for `aurigin.deepfake_detection.v1.DeepfakeDetection.DetectDeepfake`.

Reads YAML scenarios at startup, validates them against
`examples/scenarios/scenario.schema.json`, and replays the chosen
scenario's timeline for every session. Clients select a scenario per
call via the `x-scenario-id` request metadata header; missing/unknown
ids fall back to `SCENARIO_DEFAULT`.

Extracted from `examples/python/server.py` (which no longer exists) so
the same simulator drives both the Python and TypeScript example clients
and any downstream integration test that needs a stand-in for the real
deepfake service.

## Run — Docker (recommended)

```bash
cd examples/simulator/deepfake
docker compose up --build
```

Listens on `localhost:50051` insecurely. Scenarios come from
`examples/scenarios/` baked into the image; override at runtime by
mounting your own tree over `/scenarios` (see the commented block in
`docker-compose.yml`).

## Run — directly with `uv` (dev)

```bash
cd examples/simulator/deepfake
uv sync
uv run deepfake-simulator-service
```

Default port `50051`; override with `PORT=…`. Scenarios resolve to
`../../scenarios` relative to the package, so running from the repo
tree picks up the shared example scenarios without extra config.

## Configuration

| Env | Default | Purpose |
|---|---|---|
| `PORT` | `50051` | gRPC listen port. |
| `SCENARIOS_DIR` | `<repo>/examples/scenarios` (dev), `/scenarios` (Docker) | Directory of `*.yaml` scenarios. Each file must validate against `scenario.schema.json`. |
| `SCENARIO_DEFAULT` | `default` | Fallback scenario when the client either omits `x-scenario-id` or names an unknown id. Must exist in `SCENARIOS_DIR`. |
| `TLS_CERT` / `TLS_KEY` | `<repo>/examples/certs/server.{crt,key}` | Server TLS keypair. Present → TLS; absent → insecure. |
| `TLS_CLIENT_CA` | `<repo>/examples/certs/client.crt` | Client-cert trust anchor when `MTLS=1`. |
| `MTLS` | (unset) | `1` / `true` / `yes` → require client cert chaining to `TLS_CLIENT_CA`. |

## Wire

Accepts both wire shapes on `DetectDeepfakeRequest.oneof request`:

- `audio` — Twilio-vendored `AudioBuffer` (deprecated as of aurigin-protos 0.3.0)
- `audio_frame` — Aurigin-native `AudioFrame` (recommended; self-describing codec + rate + channels)

The simulator drains audio bytes for pacing/duration accounting but
does not decode content — verdicts come from the loaded scenario, not
the audio.

## Scenario shape

See `examples/scenarios/scenario.schema.json` and `examples/scenarios/default.yaml`.
Scenarios describe:

- A per-window score curve (linear, cosine, step, exponential-decay, etc.).
- Explicit inline events (analysis results at specific timestamps).
- Final label + score at stream close.
- Optional gRPC-level fault injection (mid-stream abort with a specific status).
- Optional network-latency simulation (base + jitter per outbound message).

## Ports

Standard gRPC listen port `:50051`. Consumers configure their client
target as `localhost:50051` (dev) or `<host>:50051` (compose network /
Kubernetes service).
