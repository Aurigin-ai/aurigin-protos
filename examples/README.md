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
- `python/server.py` — `DeepfakeDetectionServicer` impl with stub analysis, listens on `[::]:50051`
- `python/client.py` — streams every `.wav` in `examples/audio/` (one session per file). Falls back to 6 × 500 ms of silence when the dir is empty. Pass `--target HOST:PORT` to point at a non-default server (default `localhost:50051`).
- `python/phone_call.py` — simulates a live mobile call. Two input modes:
  - **File mode** (default): streams a WAV file looped to fill `--duration` (default 30 s) at real-time pace.
  - **FIFO mode** (`--fifo /path/to/pipe` `--codec mulaw|pcm16`): reads from a named pipe until the writer closes — designed for FreeSWITCH `record_session` G.711 μ-law output. Pacing is implicit (writer-driven), no `--duration` cap by default.

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

# FIFO mode — tail a FreeSWITCH record_session pipe (G.711 μ-law @ 8 kHz)
uv run phone-call --fifo /var/lib/freeswitch/recordings/live.r16 --codec mulaw

# Local FIFO smoke-test using ffmpeg as the writer
mkfifo /tmp/test.fifo
ffmpeg -re -i examples/audio/conversation_8khz.wav -f mulaw -ar 8000 -ac 1 /tmp/test.fifo &
uv run phone-call --fifo /tmp/test.fifo --codec mulaw
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

npm run server    # in one terminal
npm run client    # in another
```

> **Aurigin engineers** consuming a pre-promotion version from the internal AWS CodeArtifact mirror: see [`infra/aws/`](../infra/aws/) for the npm registry config.

Files:
- `typescript/server.ts` — `Server` from `@grpc/grpc-js` + `addService(DeepfakeDetectionService, impl)` with bidi stream handling
- `typescript/client.ts` — streams every `.wav` in `examples/audio/` (one session per file) using `DeepfakeDetectionClient.detectDeepfake()`; falls back to 6 × 500 ms of silence when the dir is empty. Pass `--target HOST:PORT` (e.g. `npm run client -- --target localhost:50051`) to point at a non-default server.
- `typescript/phone_call.ts` — TS twin of `python/phone_call.py`: file mode (paced WAV looped to fill `--duration`) and FIFO mode (`--fifo PATH` `--codec mulaw|pcm16`). Built-in μ-law lookup table replaces `audioop`. Run with `npm run phone-call -- --audio ../audio/your.wav`

### Notes on ts-proto naming

`ts-proto` flattens nested types with underscores and suffixes service exports:

| Proto | Generated TypeScript |
|---|---|
| `service DeepfakeDetection` | `DeepfakeDetectionService` (definition), `DeepfakeDetectionServer` (server interface), `DeepfakeDetectionClient` (client class) |
| `oneof response { ... }` | discriminated optional fields on the message (e.g. `response.analysisResult`) |

Deep imports use the proto path: `@aurigin/protos/aurigin/deepfake_detection/v1/deepfake_detection`.
