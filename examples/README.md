# Examples

Reference snippets showing how to consume the generated packages.

## Python (with `uv`)

[`uv`](https://github.com/astral-sh/uv) is the recommended Python package manager for this repo's consumers — it's a drop-in pip replacement that's ~10–100× faster and handles project venvs automatically.

Install once: `brew install uv`.

### Quick install + run

```bash
uv venv                                   # create .venv/
uv pip install aurigin-protos grpcio

uv run python examples/python/server.py   # in one terminal
uv run python examples/python/client.py   # in another
```

### Self-contained uv project

The `examples/python/` directory ships a `pyproject.toml` so you can run it as a self-contained uv project. `[project.scripts]` defines `server`, `client`, and `phone-call` entry points, mirroring the TypeScript example's `npm run server` / `npm run client`:

```bash
cd examples/python
uv sync                                   # creates .venv/, installs deps
uv run server                             # scenario-driven simulator on :50051
uv run client                             # client → localhost:50051
uv run phone-call                         # paced WAV streamer → localhost:50051
```

### `just` wrapper

`examples/python/Justfile` wraps the same `uv run …` commands so you don't have to remember the phone-call flags. `just --list` from `examples/python/` shows the full menu; the common cases:

```bash
cd examples/python

just sync                                 # uv sync
just server                               # scenario-driven simulator on :50051
just client                               # client → localhost:50051
just client 127.0.0.1:50051               # client → aurigin-router backend-simulator
just smoke                                # end-to-end pytest
```

### For a downstream service

Add `aurigin-protos` like any other public PyPI dependency — no extra index, no auth:

```toml
[project]
name = "my-service"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = [
    "aurigin-protos",
    "grpcio>=1.62",
]
```

> **Aurigin engineers** consuming a pre-promotion version from the internal AWS CodeArtifact mirror: see [`infra/aws/`](../infra/aws/) for the index URL and the `uv` configuration pattern.

Expected client output (against `examples/python/server.py` running the `default` scenario, with no WAVs in `audio/`):

```
=== silence (3 s @ 16 kHz) ===
Session: sim-1a2b3c4d
Analysis | offset=1000ms  | score=0.050 | label=bonafide
Analysis | offset=2000ms  | score=0.050 | label=bonafide
Analysis | offset=3000ms  | score=0.050 | label=bonafide
FINAL    | total=3000ms   | score=0.050 | label=bonafide
```

The session id is generated per session (`sim-<8 hex>`) and the cadence comes from the loaded scenario (1 s by default). Pass `--scenario-id <id>` to `phone-call` to load a different scenario from `examples/scenarios/`.

To run against real audio (real ML server required, e.g. backend-app's gRPC service), drop one or more `.wav` files (S16LE PCM, any sample rate, any channel count) into `examples/python/audio/` and re-run the client. It opens one session per file. The `audio/` dir is gitignored.

Files:
- `python/server.py` — scenario-driven simulator: loads YAML scenarios from `examples/scenarios/` at startup, picks one per session via the `x-scenario-id` request-metadata header, emits AnalysisResults from the scenario's confidence curve + events, optionally injects gRPC-level faults. Listens on `[::]:50051`. Env vars: `PORT`, `SCENARIOS_DIR`, `SCENARIO_DEFAULT`.
- `python/client.py` — streams every `.wav` in `examples/audio/` (one session per file). Falls back to 6 × 500 ms of silence when the dir is empty. Pass `--target HOST:PORT` to point at a non-default server (default `localhost:50051`).
- `python/phone_call.py` — simulates a live mobile call. Streams a WAV file looped to fill `--duration` (default 30 s) at real-time pace.

### Audio fixtures

The `examples/audio/` directory is shared between the Python and TypeScript examples — both clients glob it for `*.wav`. The directory is gitignored, so drop fixtures in locally without worrying about committing customer audio.

### Generating a FreeSWITCH-style conversation

`examples/audio/generate-conversation.sh` (colocated with the audio it produces) stitches every other `.wav` in the same dir into a single **8 kHz mono S16LE** WAV — the FreeSWITCH narrowband default — with brief silence between turns. Drives the phone-call simulator with realistic telephony cadence and bandwidth.

```bash
# Defaults: 500 ms gap between turns, no looping. Output: examples/audio/conversation_8khz.wav
bash examples/audio/generate-conversation.sh

# Longer gap, repeat the whole conversation 3 times
bash examples/audio/generate-conversation.sh --gap-ms 800 --repeat 3
```

Requires `ffmpeg` on `$PATH`. The output `.wav` is gitignored along with all other audio in the dir; the script itself is committed.

### Phone-call simulation

The phone-call example mimics the steady-state behaviour of a live audio source: it sends ~1 s of audio per second of wallclock, loops the input if it's shorter than the requested call duration, and prints `AnalysisResult` events the moment they come back from the server. Useful for validating that the streaming pipeline keeps up with real-time without backlog.

```bash
# Run against backend-app's gRPC server (assumes a real ML server on :50051)
uv run phone-call --duration 30 --chunk-ms 100 --audio audio/your_call.wav

# Or pick the first .wav in examples/audio/ automatically
uv run phone-call --duration 30

# Drive the scenario-driven simulator on :50051 with a specific scenario
uv run phone-call --duration 30 --scenario-id fake_detected_rising_curve
```

Sample output:

```
📞 Calling localhost:50051 | source=your_call.wav (4.16s @ 24000Hz/1ch) | duration=12.0s | frame=100ms
──────────────────────────────────────────────────────────────────────
📞 Session: 538a241b-ebdf-4e9a-83a2-259352bd0b01
   Analysis @   0.00s | score=0.945 | label=spoofed            | confidence=1.00
   Analysis @   2.90s | score=0.323 | label=bonafide           | confidence=1.00
   Analysis @   5.96s | score=0.240 | label=bonafide           | confidence=1.00
   Analysis @   9.01s | score=0.622 | label=partially_spoofed  | confidence=1.00
──────────────────────────────────────────────────────────────────────
☎️  Call ended | total=12.01s | score=0.532 | label=partially_spoofed | analyses=4
```

## TypeScript

The `examples/typescript/` directory has its own `package.json` so you can install and run directly — `@aurigin/protos` resolves from public npmjs.com, no auth:

```bash
cd examples/typescript
npm install

npm run server                         # scenario-driven simulator on :50051
npm run client                         # client → localhost:50051
npm run phone-call                     # paced WAV streamer → localhost:50051
npm run scenarios                      # list available scenarios
npm run call -- fake_detected_rising_curve       # 10 s call against that scenario
npm run call -- fake_detected_rising_curve --duration 30   # override duration
npm run burst -- --concurrency 5 --scenario-id fake_detected_rising_curve              # 5 simultaneous calls
npm run burst -- --concurrency 5 --scenario-id fake_detected_rising_curve --stagger-ms 500   # 5, 500ms apart
npm run tls                            # regenerate the committed self-signed certs (server + client)
MTLS=1 npm run server                  # same server, demands a client cert (see TLS section)
MTLS=1 npm run call -- default         # client presents its cert, transport=mTLS
npm test                               # end-to-end smoke test
```

> **Aurigin engineers** consuming a pre-promotion version from the internal AWS CodeArtifact mirror: see [`infra/aws/`](../infra/aws/) for the npm registry config.

Files:
- `typescript/server.ts` — TS twin of `python/server.py`: scenario-driven simulator that loads YAML scenarios from `examples/scenarios/`, picks one per session via the `x-scenario-id` request-metadata header, emits AnalysisResults from the scenario's confidence curve + events, optionally injects gRPC-level faults. Same env vars: `PORT`, `SCENARIOS_DIR`, `SCENARIO_DEFAULT`. Sim logic in `typescript/sim/{curves,loader,runner}.ts` mirrors `python/sim/`.
- `typescript/client.ts` — streams every `.wav` in `examples/audio/` (one session per file) using `DeepfakeDetectionClient.detectDeepfake()`; falls back to 6 × 500 ms of silence when the dir is empty. Pass `--target HOST:PORT` (e.g. `npm run client -- --target localhost:50051`) to point at a non-default server.
- `typescript/phone_call.ts` — TS twin of `python/phone_call.py`: streams a paced WAV looped to fill `--duration`. Run with `npm run phone-call -- --audio ../audio/your.wav`

### Notes on ts-proto naming

`ts-proto` flattens nested types with underscores and suffixes service exports:

| Proto | Generated TypeScript |
|---|---|
| `service DeepfakeDetection` | `DeepfakeDetectionService` (definition), `DeepfakeDetectionServer` (server interface), `DeepfakeDetectionClient` (client class) |
| `oneof response { ... }` | discriminated optional fields on the message (e.g. `response.analysisResult`) |

Deep imports use the proto path: `@aurigin/protos/aurigin/deepfake_detection/v1/deepfake_detection`.

## TLS (on by default) and mTLS (opt-in)

The example ships with four self-signed ECDSA P-256 files committed under [`certs/`](certs/):

| File | Used when | Purpose |
|---|---|---|
| `server.crt` + `server.key` | always (TLS-by-default) | Server's keypair. SANs cover `localhost`, `127.0.0.1`, `::1`. Doubles as the CA that clients trust. |
| `client.crt` + `client.key` | only when `MTLS=1` | Client's keypair. Doubles as the CA the server verifies presented client certs against. |

Both the Python and TypeScript servers + clients auto-detect these files. The transport mode shows in the startup header line:

```
# default (no env var) — plain TLS
DeepfakeDetection simulator listening on :50051 | ... | transport=TLS (self-signed, examples/certs/)
📞 Calling localhost:50051 | ... | transport=TLS (self-signed, examples/certs/)

# with MTLS=1 on both sides
DeepfakeDetection simulator listening on :50051 | ... | transport=mTLS (self-signed, examples/certs/)
📞 Calling localhost:50051 | ... | transport=mTLS (self-signed, examples/certs/)
```

> **All four committed keys are public — DO NOT USE IN PRODUCTION.** They exist so the example is TLS-by-default with zero setup. See [`certs/README.md`](certs/README.md) for the trust model when shipping anything real (Let's Encrypt, internal CA, edge termination).

### Plain TLS (default)

Nothing to set. Start the server and client; the cert auto-detect kicks in.

```bash
# Python
just server                            # transport=TLS
just call default                      # transport=TLS

# TypeScript
npm run server                         # transport=TLS
npm run call -- default                # transport=TLS
```

### mTLS (opt-in via `MTLS=1`)

Set `MTLS=1` on the **server** and the **client** process. Asymmetric configuration fails fast:

| Server `MTLS` | Client `MTLS` | Outcome |
|---|---|---|
| unset / 0 | unset / 0 | plain TLS — handshake succeeds, no client cert verified |
| **1** | unset / 0 | client gets `UNAVAILABLE` — server demands a cert the client doesn't present |
| unset / 0 | **1** | plain TLS — client sends a cert, server ignores it |
| **1** | **1** | mTLS — both sides verify each other |

```bash
# Python
MTLS=1 just server                     # transport=mTLS
MTLS=1 just call default               # transport=mTLS
MTLS=1 just burst 5 default            # 5 mTLS streams, all over the same channel

# TypeScript
MTLS=1 npm run server                  # transport=mTLS
MTLS=1 npm run call -- default         # transport=mTLS
```

Both languages also expose dedicated `mtls-*` wrappers as `just mtls-server`, `just mtls-call ID`, `just mtls-burst N ID` and `npm run mtls-server` / `npm run mtls-call -- ID` / `npm run mtls-burst -- --concurrency N --scenario-id ID`.

If `MTLS=1` is set but `client.{crt,key}` are missing (e.g. you deleted them), both sides fall back to plain TLS and the transport label calls it out: `transport=TLS (...) — MTLS=1 but client.{crt,key} missing, falling back`.

### Regenerating

```bash
# from examples/python/
just tls

# or from examples/typescript/
npm run tls
```

Both invoke the same OpenSSL commands and write all four files (`server.{crt,key}` + `client.{crt,key}`). Re-run only when you want to rotate keys or change SANs.

### Forcing insecure

Useful for benchmarking or for pointing the client at a server that's behind a TLS-terminating proxy:

```bash
rm examples/certs/server.{crt,key}                       # permanent — remove the cert from the tree
TLS_CERT=/dev/null TLS_KEY=/dev/null just server         # one-shot override (server)
TLS_CA=/dev/null just client                             # one-shot override (client / phone-call)
```

The server side reads `TLS_CERT` + `TLS_KEY` (TLS) and `TLS_CLIENT_CA` (mTLS). The client side reads `TLS_CA` (TLS) and `TLS_CLIENT_CERT` / `TLS_CLIENT_KEY` (mTLS). Pointing any of them at a non-existent path takes the insecure branch.

### Using a real cert

Drop your own `server.crt` and `server.key` (and `client.{crt,key}` if you want mTLS) into `examples/certs/`, overwriting the committed examples, and restart. The auto-detect logic doesn't care who signed them. For Let's Encrypt or internal CAs, see the trust-model breakdown in [`certs/README.md`](certs/README.md).

## Configuring the server

Both `python/server.py` and `typescript/server.ts` are the same scenario-driven simulator — same env vars, same `x-scenario-id` metadata selector, same YAML scenario format. Everything below applies to either implementation.

### Env vars

| Var | Default | Purpose |
|---|---|---|
| `PORT` | `50051` | gRPC listen port. |
| `SCENARIOS_DIR` | `<repo>/examples/scenarios` | Directory the server walks at startup. Every `*.yaml` under it (recursive) is loaded and validated against `scenario.schema.json`. Duplicate `scenario.id` is a startup error. |
| `SCENARIO_DEFAULT` | `default` | Scenario id used when the client doesn't send `x-scenario-id` or sends an unknown id. Must match one of the loaded scenarios or the server exits at startup. |

Example — point both servers at a custom directory on a non-default port:

```bash
PORT=50061 SCENARIOS_DIR=$HOME/my-scenarios uv run server                 # Python
PORT=50061 SCENARIOS_DIR=$HOME/my-scenarios npm run server                # TypeScript
```

### Selecting a scenario per session

Clients pick a scenario by setting the `x-scenario-id` gRPC request-metadata header. Unknown or missing ids fall back to `SCENARIO_DEFAULT`. The selection happens at session creation, so every `CreateSessionRequest` can hit a different scenario on the same server.

Python:
```python
import grpc
metadata = [("x-scenario-id", "fake_detected_rising_curve")]
stub.DetectDeepfake(request_iter(), metadata=metadata)
```

TypeScript:
```ts
import { Metadata } from "@grpc/grpc-js";
const md = new Metadata();
md.set("x-scenario-id", "fake_detected_rising_curve");
client.detectDeepfake(md);
```

### Server-side logs

Both implementations write structured per-session logs to **stderr** so process supervisors and pipe redirections don't drop them. Three log shapes; you'll see them in this order for a normal session, plus a fourth on fault-injection scenarios:

```
[incoming] peer=ipv6:[::1]:64813 | scenario=fake_detected_rising_curve
[sim-1ab652ba] start | scenario=fake_detected_rising_curve | duration_target=30000ms | seed=42
[sim-1ab652ba] end   | total=30000ms | analyses=29 | score=0.945 | label=spoofed
```

| Line | Emitted | What it tells you |
|---|---|---|
| `[incoming] peer=… \| …` | Right when the RPC arrives, before the runner spins up | The client's address and which scenario the server resolved. Three sub-shapes: `scenario=<id>` (client asked for a known id), `requested='<id>' unknown → fallback=<default>` (client asked for something we don't have), `requested=none → default=<default>` (no header). |
| `[<sid>] start \| …` | After the server emits `CreateSessionResponse` | Session id (used to grep concurrent calls apart), scenario chosen, scenario's `duration_target`, RNG `seed` if pinned. |
| `[<sid>] fault \| …` | When `grpc.terminate_at_ms` fires | The wallclock offset, the gRPC status code, the configured message, and how many analyses had already fired. |
| `[<sid>] end   \| …` | Right before `FinalResult` is yielded | Total audio ms, count of `AnalysisResult`s emitted, the last score, the resolved overall label. |

Per-emission logs (one line per `AnalysisResult` write) are opt-in via `SIM_LOG_ANALYSES=1` since they get noisy fast at default cadence.

### Bundled scenarios

Drop-in YAML files under `examples/scenarios/`. Names match the `scenario.id` field, not the path.

| id | When to use |
|---|---|
| `default` | Flat low score, bonafide throughout. Safe baseline. Used when no `x-scenario-id` is sent. |
| `confidence_linear_ramp` | Linear climb from 0.05 → 0.94 over 30 s. No explicit fake-detected event — client must infer detection from the threshold crossing. |
| `fake_detected_rising_curve` | Canonical "happy fake": sigmoid curve crosses the threshold around 12 s, then a `FAKE_DETECTED` event with `reason=vocoder_artifacts` is emitted. |
| `real_audio_detected_real` | Bonafide audio. Score stays in 0.02–0.10 with small jitter. Use to verify the happy path doesn't false-positive. |
| `confidence_oscillating` | Confidence wobbles around 0.5 for 30 s. Use to test debounce / hysteresis logic. |
| `duplicate_detection` | Same `FAKE_DETECTED` event emitted twice (8 s and 8.05 s). Verifies the client deduplicates by content rather than blindly trusting the wire. |
| `stream_ends_no_verdict` | Curve disabled, no events. `FinalResult` arrives with `overall_label=unknown`, `analysis_count=0`. |
| `grpc_deadline_exceeded` | gRPC `DEADLINE_EXCEEDED` at 8 s before the first curve sample fires. Tests premature-termination handling. |
| `grpc_unavailable_midstream` | gRPC `UNAVAILABLE` at 15 s after ~14 analysis samples. Tests client reconnect / retry. |
| `model_timeout_event_error` | Event-level `ERROR` mid-stream (not a gRPC fault). Stream stays open; curve continues afterwards. |

### Writing a custom scenario

1. Drop a `*.yaml` file anywhere under `SCENARIOS_DIR`. Subdirectories are walked recursively.
2. Validate against [`scenarios/scenario.schema.json`](scenarios/scenario.schema.json) (Draft 2020-12). The server runs the same validation at startup and refuses to start on any failure.
3. Pick a unique `scenario.id` matching `^[a-z0-9][a-z0-9_-]*$`. Duplicates across files are a startup error.

Minimum viable shape:
```yaml
version: 1
scenario:
  id: my_scenario
  description: One-line summary.
stream:
  duration_ms: 10000
confidence_curve:
  type: linear
  emit_every_ms: 1000
  from: { at_ms: 0,     fake_probability: 0.10 }
  to:   { at_ms: 10000, fake_probability: 0.90 }
```

Optional blocks the schema accepts: `random.seed` (deterministic RNG), `network.{base_latency_ms,jitter_ms,...}` (per-emission latency), `grpc.{initial_metadata,trailing_metadata,terminate_at_ms,status_code,status_message}` (fault injection), `events[]` (explicit `CONFIDENCE_UPDATE` / `FAKE_DETECTED` / `ERROR` at specific `at_ms`). See the bundled scenarios for working examples of each.

VS Code with the Red Hat YAML extension auto-validates against the schema if the workspace `yaml.schemas` setting maps `examples/scenarios/**/*.yaml` to `examples/scenarios/scenario.schema.json` (already configured in the workspace settings).
