# Examples

Reference snippets showing how to consume the generated packages.

> **Note**: the scenario-driven simulator server used to live at
> `python/server.py` + `typescript/server.ts`. It's been extracted to a
> single canonical Python implementation at
> [`simulator/deepfake/`](simulator/deepfake/), packaged as
> `aurigin-deepfake-simulator-service` with a Dockerfile and
> docker-compose. The `python/` and `typescript/` trees under here are
> now **client-only** — the smoke tests in both languages spawn the
> Python simulator as a subprocess. Wherever this README says "run the
> server", read: `cd examples/simulator/deepfake && uv run
> deepfake-simulator-service` (or `docker compose up`).

## Python (with `uv`)

[`uv`](https://github.com/astral-sh/uv) is the recommended Python package manager for this repo's consumers — it's a drop-in pip replacement that's ~10–100× faster and handles project venvs automatically.

Install once: `brew install uv`.

### Quick install + run

```bash
uv venv                                   # create .venv/
uv pip install aurigin-protos grpcio

uv run python -m deepfake_simulator_service                # in one terminal (see simulator/deepfake/)
uv run python examples/python/client.py                    # in another
```

### Self-contained uv project

The `examples/python/` directory ships a `pyproject.toml` so you can run it as a self-contained uv project. `[project.scripts]` defines `client`, `phone-call`, and `phone-call-burst` entry points, mirroring the TypeScript example's `npm run client` / etc. The scenario-driven simulator server lives in `examples/simulator/deepfake/` — see that directory for `uv run deepfake-simulator-service`.

```bash
# Terminal 1 — start the simulator
cd examples/simulator/deepfake
uv sync
uv run deepfake-simulator-service          # scenario-driven simulator on :50051

# Terminal 2 — run a client
cd examples/python
uv sync                                    # creates .venv/, installs deps
uv run client                              # batch client → localhost:50051
uv run phone-call                          # single live call (the integration pattern)
uv run phone-call-burst -c 5               # N concurrent calls (load test / multi-call architecture)
uv run scan-file --file long.wav           # offline parallel scan of one long file (throughput mode)
```

Shared helpers (WAV reader, CSV writer, TLS auto-detect, signal-handler) live under `examples/python/common/` and re-export through `from common import …` — the three CLI scripts above stay focused on what they're demonstrating, not on infra glue.

### `just` wrapper

`examples/python/Justfile` wraps the same `uv run …` commands so you don't have to remember the phone-call flags. `just --list` from `examples/python/` shows the full menu; the common cases:

```bash
cd examples/python

just sync                                 # uv sync
# The `just server` recipe is gone — the simulator lives in
# examples/simulator/deepfake/ and is run from there. See that
# directory's README for `uv run deepfake-simulator-service`
# or `docker compose up`.
just client                               # client → localhost:50051
just client 127.0.0.1:50051               # client → aurigin-router backend-simulator
just scan-file --file long.wav            # offline batch scan (parallel slices, no pacing)
just scan-file-extreme --file long.wav    # 90 × 40 s slices — saturates dfs GPU on batched inference
just scan-file-bench-no-vad --file long.wav   # extreme + per-session VAD off (GPU-only bench floor)
just smoke                                # end-to-end pytest (spawns the simulator as a subprocess)
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

Expected client output (against `deepfake-simulator-service` from `examples/simulator/deepfake/` running the `default` scenario, with no WAVs in `audio/`):

```
=== silence (3 s @ 16 kHz) ===
Session: sim-1a2b3c4d
Analysis | offset=1000ms  | score=0.050 | label=bonafide
Analysis | offset=2000ms  | score=0.050 | label=bonafide
Analysis | offset=3000ms  | score=0.050 | label=bonafide
FINAL    | total=3000ms   | score=0.050 | label=bonafide
```

The session id is generated per session (`sim-<8 hex>`) and the cadence comes from the loaded scenario (1 s by default). Pass `--scenario-id <id>` to `phone-call` to load a different scenario from `examples/scenarios/`.

To run against real audio (real ML server required, e.g. backend-app's gRPC service), drop one or more `.wav` files into `examples/audio/` and re-run the client. Both 16-bit PCM (`S16LE`) and 32-bit IEEE-float (`F32LE`) WAVs are accepted, at any sample rate and channel count — the client reads the format tag from the RIFF header and stamps the outgoing message's codec accordingly (`AudioFrame.codec` on the new 0.3.0 wire, or legacy `AudioBuffer.format` on the deprecated 0.2.x path — see [Wire messages](#wire-messages--audioframe-new-in-030-and-audiobuffer-deprecated) below). It opens one session per file. The `audio/` dir is gitignored.

Files:
- `simulator/deepfake/` — **canonical scenario-driven simulator**, extracted here so both Python and TypeScript clients drive the same reference implementation. Packaged as `aurigin-deepfake-simulator-service` with a Dockerfile + docker-compose. Loads YAML scenarios from `examples/scenarios/` at startup, picks one per session via the `x-scenario-id` request-metadata header, emits AnalysisResults from the scenario's confidence curve + events, optionally injects gRPC-level faults. Listens on `[::]:50051`. Env vars: `PORT`, `SCENARIOS_DIR`, `SCENARIO_DEFAULT`. See its own README for `uv run deepfake-simulator-service` and `docker compose up --build`.
- `python/client.py` — streams every `.wav` in `examples/audio/` (one session per file). Falls back to 6 × 500 ms of silence when the dir is empty. Pass `--target HOST:PORT` to point at a non-default server (default `localhost:50051`). Pass `--csv PATH` to additionally write per-chunk results to a CSV — see [CSV export](#csv-export) below.
- `python/phone_call.py` — **minimal worked example** of the FreeSWITCH-fork integration pattern. Single live call: open bidi → real-time-paced sender + concurrent receiver → close. Heavily commented at the send loop because that's exactly the line that becomes `for await frame in fork_socket: ...` in a real `mod_audio_fork` / Twilio Media Stream / SIPREC integration.
- `python/phone_call_burst.py` — the **recommended multi-call architecture**: one long-lived gRPC channel multiplexing N concurrent bidi streams (vs N separate channels). Same per-call building blocks (imported from `phone_call.py`), plus `--concurrency N` / `--stagger-ms` / per-stream `call-NN` labels / graceful shutdown across all streams / summary aggregation. Use it to find the connection-count knee on a real backend or to capture per-chunk results across many concurrent sessions (`--csv PATH`).
- `python/scan_file.py` — **offline batch scan** of one long file. Splits the file into N contiguous 5-s-aligned slices and streams them as N concurrent sessions on one warm gRPC channel, no real-time pacing. Explicitly NOT a live-call simulator: the goal is minimum wall time on a long recording, not fidelity to a real phone-call cadence. Includes `--extreme` preset (90 × 40 s slices) sized so every stream fills one server-side `BATCH_SIZE=8` batch, plus per-session `--vad-mode` / `--vad-threshold` overrides (aurigin-protos ≥ 0.4) so you can benchmark the GPU-only inference floor without editing dfs env vars. Reports per-slice timing percentiles (send→open / open→first / first→last / last→final) computed from `time.perf_counter_ns()` timestamps so successive runs are honestly comparable.
- `python/common/` — shared helpers (`wav_reader`, `result_csv`, `tls`, `shutdown`). The four CLI scripts above import from here so the files themselves stay focused on what they're demonstrating.

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

### Phone-call: single live call (the FreeSWITCH-fork integration pattern)

`phone_call.py` / `phone_call.ts` is the **minimal worked example** of plugging a real-time audio source into the gRPC bidi: send loop paced at wallclock (~1 s of audio per second of real time), receive loop running concurrently, single call, exits cleanly. The send loop's docstring/comment is the part to read — in a production integration the `samples.subarray(cursor, …)` slicing becomes `for await frame in fork_socket: …`, and the wallclock `asyncio.sleep` / `setTimeout` pacing goes away (the socket IS the clock).

```bash
# Against backend-app's gRPC server (assumes a real ML server on :50051)
uv run phone-call --duration 30 --chunk-ms 20 --audio audio/your_call.wav

# Or pick the first .wav in examples/audio/ automatically
uv run phone-call --duration 30

# Drive the scenario-driven simulator with a specific scenario
uv run phone-call --duration 30 --scenario-id fake_detected_rising_curve
```

Sample output:

```
📞 Calling localhost:50051 | source=your_call.wav (4.16s @ 24000Hz/1ch S16LE) | duration=12.0s | frame=100ms | transport=TLS (self-signed, examples/certs/)
──────────────────────────────────────────────────────────────────────
📞 Session: 538a241b-ebdf-4e9a-83a2-259352bd0b01
   Analysis @   0.00s | score=0.945 | label=spoofed            | confidence=1.00
   Analysis @   5.00s | score=0.240 | label=bonafide           | confidence=1.00
   Analysis @  10.00s | score=0.622 | label=partially_spoofed  | confidence=1.00
──────────────────────────────────────────────────────────────────────
☎️  Call ended | total=12.01s | score=0.532 | label=partially_spoofed | analyses=3
```

### Phone-call burst: N concurrent calls (recommended multi-call architecture)

`phone_call_burst.py` / `phone_call_burst.ts` mirrors how a PBX (FreeSWITCH instance running `mod_audio_fork`, etc.) handling many calls simultaneously should shape its gRPC plumbing: **one** long-lived channel multiplexing N concurrent bidi streams — *not* N separate channels. The per-call work is the exact same `send_call` / `recv_call` imported from `phone_call.py` (so the pattern reads as "do it N times"), wrapped with `--concurrency`, `--stagger-ms`, per-stream labels, graceful Ctrl-C across all streams, and a summary. Use it to find the connection-count knee on a real backend, or with `--csv` to capture per-chunk results across every concurrent call in one file.

```bash
# 5 concurrent calls of the rising-curve scenario, all starting at t=0
uv run phone-call-burst -c 5 --duration 30 --scenario-id fake_detected_rising_curve

# Same but stagger starts 200 ms apart (mimics arriving calls vs thundering herd)
uv run phone-call-burst -c 5 --stagger-ms 200 --duration 30 --scenario-id fake_detected_rising_curve

# Capture per-chunk results across all 10 streams into one CSV — for load
# tuning / cross-revision regression comparison
uv run phone-call-burst -c 10 --duration 60 --target real-backend:50051 --csv /tmp/burst.csv

# Cap wall time per call so a slow-pacing client can't drag sessions past
# the target duration. Also enables the pacing-quality summary at the end,
# which flags whether the client actually produced a realtime workload
# (see "Interpreting the pacing summary" below).
uv run phone-call-burst -c 128 --duration 120 --max-wall-s 150 --target real-backend:50051 --csv /tmp/burst.csv
```

Sample output (3 concurrent calls):

```
📞 Calling localhost:50051 | source=your_call.wav (4.16s @ 24000Hz/1ch S16LE) | duration=10.0s | frame=100ms | concurrency=3 | stagger=200ms | transport=TLS (...)
──────────────────────────────────────────────────────────────────────
[call-01] 📞 Session: a72b...
[call-02] 📞 Session: 5d11...
[call-03] 📞 Session: 9f08...
[call-01]    Analysis @   5.00s | score=0.945 | label=spoofed            | confidence=1.00
[call-02]    Analysis @   5.00s | score=0.943 | label=spoofed            | confidence=1.00
[call-03]    Analysis @   5.00s | score=0.946 | label=spoofed            | confidence=1.00
...
[call-01] ☎️  Call ended | total=10.01s | score=0.901 | label=spoofed | analyses=2
[call-02] ☎️  Call ended | total=10.01s | score=0.902 | label=spoofed | analyses=2
[call-03] ☎️  Call ended | total=10.01s | score=0.899 | label=spoofed | analyses=2
──────────────────────────────────────────────────────────────────────
Summary: 3/3 streams OK, 0 failed
Pacing: audio_per_wall p50=1.00 p05=0.99 (1.00 = perfect realtime; <0.95 = client too slow) — under-paced sessions: 0/3, wall-capped: 0/3
```

#### Interpreting the pacing summary

The `Pacing:` line answers **"did the client actually produce the workload we intended?"** A real telco call delivers one 20 ms frame every 20 ms wall-clock, forever. A load-generator that drifts (single asyncio/event-loop starving under too many concurrent streams, GC pauses, network hiccups) will burst frames or fall behind — neither faithful to a real call.

- **`audio_per_wall`** — ratio of audio-time-sent to wall-time-elapsed per session. `1.00` = perfect realtime, `<0.95` = client too slow. Matches the deepfake service's server-side `rtf` field on `session ended`, so you can cross-check.
- **`under-paced sessions: N/M`** — count of sessions where `audio_per_wall < 0.95`. Any non-zero count means those sessions' server-side measurements aren't representative of a real call — treat them as invalid data points for capacity claims.
- **`wall-capped: N/M`** — count of sessions that hit `--max-wall-s` before naturally finishing. Under-paced sessions typically also get wall-capped when the flag is set.

**When you see `⚠ Client-side pacing degraded` after the summary**, the load-generator is the bottleneck, not the server. Shard the run across multiple processes at a lower `--concurrency` per process:

```bash
# 4 processes × 32 concurrent = 128 total, each process well within its
# single-event-loop pacing budget. Concat CSVs afterward.
for i in 0 1 2 3; do
    uv run phone-call-burst -c 32 --duration 120 --max-wall-s 150 \
        --stagger-ms 40 --target real-backend:50051 --csv /tmp/burst-shard-$i.csv &
done
wait
awk 'NR==1 || !/^file_name,/' /tmp/burst-shard-*.csv > /tmp/burst-total.csv
```

Rule of thumb: one Python asyncio loop or one Node event loop reliably sustains ~1500-2000 gRPC sends/sec (≈ 30-40 concurrent 20 ms streams). Past that, you'll start seeing `audio_per_wall < 1.0` and need to shard.

### Scan-file: parallel offline batch scanning (long-file throughput)

`scan_file.py` is the offline counterpart to `phone_call.py` /
`phone_call_burst.py`. Instead of simulating live-call pacing, it splits
one long recording into N contiguous slices and streams them as N
concurrent sessions on ONE warm gRPC channel, each at full speed
(no `asyncio.sleep`). This lets the server-side batcher fill batches
across slices — a single-session `client.py` run only gets batch=1 on
every window because windows from one stream arrive every 5 s (way
longer than the 10 ms batch flush interval).

**Slice alignment.** Every slice length is rounded DOWN to a multiple of
the server's `ANALYSIS_INTERVAL_S` (5 s by default); the last slice
absorbs the residual so no audio is dropped. This eliminates the
tail-window artifact where a 1–2 s partial window gets scored by a
5-s-trained model and produces spurious high spoof scores.

**Throughput presets.**

```bash
# Conservative default — 8 slices, 100ms frames. Safe on any dfs.
uv run scan-file --file audio/long.wav

# --extreme preset — 90 slices × 40 s per AudioFrame. Every stream
# fills exactly one BATCH_SIZE=8 server-side batch. 90 concurrent
# windows arrive within the batcher's flush interval → GPU saturates
# on batched inference. Use with ≥60 min files.
uv run scan-file --file audio/long.wav --extreme

# Same preset, but tune concurrency past 90 to sweep the curve on
# beefier GPUs (server MAX_CONCURRENT_STREAMS must be raised too).
uv run scan-file --file audio/long.wav --extreme --concurrency 128
```

**Per-session `DetectionConfig` overrides (aurigin-protos ≥ 0.4).**
`CreateSessionRequest.config` lets the client override server-side
knobs without touching env vars. `scan-file` surfaces the two most
useful ones as CLI flags:

```bash
# Force VAD off per-session — measures the GPU-only inference floor
# (no CPU-side silence gating). Useful on speech-heavy audio where
# VAD's per-window cost outweighs its silence-skip savings, and for
# benchmark runs that need a stable model-only inference number.
uv run scan-file --file audio/long.wav --extreme --vad-mode off

# Tune the silence-skip percentage per-session (0-100). Unset =
# server env default (SILENCE_THRESHOLD_PCT, typically 80).
uv run scan-file --file audio/long.wav --extreme --vad-threshold 90
```

The server logs `vad_source=per-session` and the effective values on
the session-start log line for every slice, so you can grep the dfs
logs to confirm the override landed.

**Precision timing report.** Every slice's send-start, session-open,
first-result, last-result, and final-result timestamps are captured via
`time.perf_counter_ns()`. The scan-complete summary reports min / p50 /
p95 / max for each phase, so back-to-back runs against the same file are
honestly comparable:

```
═══ Scan complete ═══
  wall_time:  24.45 s
  audio:      3700.00 s
  RTF:        151.31× (90-slice parallel · vs single-session baseline)
  slices:     90 (all completed)
  analyses:   740 windows across all slices
  ...
  ── per-slice timing (n=90/90) ──
  channel_ready              78.3  (one-shot, TLS handshake shared)
  send_start→open        min=   12.0  p50=   18.5  p95=   34.2  max=   47.1   (ms)
  open→first_result      min=  510.4  p50=  620.8  p95=  735.9  max=  801.2   (ms)
  first→last_result      min=15200.1  p50=17840.6  p95=19120.4  max=21055.9   (ms)
  last→final_result      min=   45.2  p50=   61.7  p95=   88.3  max=  110.5   (ms)
  send_start→final       min=16300.5  p50=18821.6  p95=20155.9  max=22014.7   (ms)
```

Interpretation:

  - `channel_ready` — TLS/mTLS handshake amortised across all N slices
    (one warm channel, not N cold ones).
  - `send_start→open` — server-side session accept + register. Rises
    with dfs contention; a p95 blowing past 100 ms means the server is
    overloaded before the first frame lands.
  - `open→first_result` — time to first analysis window (audio buffered
    → VAD → model → wire). Rises with GPU queue depth.
  - `first→last_result` — bulk of the work; roughly proportional to
    slice length × per-window inference cost.
  - `last→final_result` — tail flush + finalise. Should be small (tens
    of ms); a large tail hints at slow session-close on the server.

**Boundary loss.** Slices are contiguous, no overlap. At each N-1 slice
boundaries up to one analysis window may be lost to alignment. For a
60-minute file with `--concurrency 8` that's ~35 s out of 3600 s
(~1 %); with `--concurrency 90` and 40 s slices it's ~7 min (~12 %) —
noticeable. Use fewer, longer slices when coverage matters more than
raw throughput.

**Requirements for `--extreme`.**

  - dfs has GPU headroom (`nvidia-smi` idle before the run).
  - HTTP/2 `MAX_CONCURRENT_STREAMS` on the server ≥ 90 (default: 100).
  - gRPC max message size ≥ frame_seconds × rate × 2 bytes. `scan-file`
    bumps its channel-side limit to 16 MB automatically; the server
    may need matching `grpc.max_receive_message_length` (dfs default
    is 16 MB, matches).

CSV export works the same as `client.py` / `phone_call_burst.py` —
pass `--csv PATH` and every per-window `AnalysisResult` is written
with its absolute-timeline `chunk_offset` (the client adds each
slice's offset to the server-reported per-slice offset).

### CSV export

Both `client.py` and `client.ts` accept `--csv PATH` to write per-chunk analysis results to a CSV file alongside the normal console output. One row per `AnalysisResult`, grouped by session — handy for diffing two runs (e.g. comparing the same fixtures against two model revisions, or against `tail_strategy=drop` vs `extend` vs `recompute`).

```bash
# Python
uv run client --csv /tmp/results.csv

# TypeScript
npm run client -- --csv /tmp/results.csv
```

The file is **overwritten** on each run and the header is always written. Columns (in order):

| Column | Source | Notes |
|---|---|---|
| `file_name` | the streamed `.wav` filename (or `"silence (3 s @ 16 kHz)"` for the silence-fallback session) | |
| `prediction_id` | `CreateSessionResponse.session_id` | server-issued |
| `chunk_id` | 0-indexed within session | |
| `chunk_offset` | `AnalysisResult.audio_offset_ms` | ms |
| `chunk_confidence` | `AnalysisResult.confidence` | `0.000000`–`1.000000` |
| `chunk_result` | `AnalysisResult.label` | `bonafide` / `spoofed` / `partially_spoofed` / `silence` / `error` |
| `chunk_duration` | `AnalysisResult.duration_ms` | ms |
| `audio_duration` | `FinalResult.total_audio_ms` | ms; repeated on every row |
| `chunks_count` | `len(chunks)` (matches `FinalResult.analysis_count`) | repeated on every row |
| `processing_time_ms` | wallclock from session-open to `FinalResult`-received | ms, 1-decimal; user-perceived latency for this file. Distinct from `audio_duration` (which is the audio length itself). |
| `global_confidence` | mean of per-chunk `confidence` across the session | matches `backend-app /predict`'s `avg_confidence` |
| `global_result` | `FinalResult.overall_label` | repeated on every row |
| `created_at` | ISO 8601 UTC timestamp when the row block was written | one timestamp per session |

Both implementations share the column list — the column constant lives in [`examples/python/common/result_csv.py`](python/common/result_csv.py) and [`examples/typescript/common/result_csv.ts`](typescript/common/result_csv.ts), and parity is asserted in PRs. Data columns are byte-identical for the same inputs; only `created_at` differs (Python uses the `+00:00` UTC suffix, TS uses `Z` — both valid ISO 8601).

`--csv` is supported on `client` (batch over a directory) and `phone-call-burst` (per-chunk capture across N concurrent calls — the typical "compare model A vs model B against the same audio set" or "what does the result distribution look like at load N=10" use case). `phone-call` (single live call) deliberately omits it since the console output is enough for one call.

## TypeScript

The `examples/typescript/` directory has its own `package.json` so you can install and run directly — `@aurigin/protos` resolves from public npmjs.com, no auth:

```bash
# Terminal 1 — start the simulator (Python; see examples/simulator/deepfake/)
cd examples/simulator/deepfake && uv run deepfake-simulator-service
# or: docker compose up --build

# Terminal 2 — TypeScript client
cd examples/typescript
npm install

npm run client                         # client → localhost:50051
npm run phone-call                     # paced WAV streamer → localhost:50051
npm run call -- fake_detected_rising_curve       # 10 s call against that scenario
npm run call -- fake_detected_rising_curve --duration 30   # override duration
npm run burst -- --concurrency 5 --scenario-id fake_detected_rising_curve              # 5 simultaneous calls
npm run burst -- --concurrency 5 --scenario-id fake_detected_rising_curve --stagger-ms 500   # 5, 500ms apart
npm run tls                            # regenerate the committed self-signed certs (server + client)
MTLS=1 npm run call -- default         # client presents its cert, transport=mTLS
npm test                               # end-to-end smoke test (spawns the Python simulator as a subprocess)
```

> The `server` and `scenarios` npm scripts (and their `mtls-server`
> variant) are gone — the scenario-driven simulator lives in
> `examples/simulator/deepfake/` now. Run it from there or via
> `docker compose up --build`.

> **Aurigin engineers** consuming a pre-promotion version from the internal AWS CodeArtifact mirror: see [`infra/aws/`](../infra/aws/) for the npm registry config.

Files:
- `simulator/deepfake/` — the same canonical simulator both language clients target (see the Python section above for the full description). No TS twin: the previous `typescript/server.ts` and `typescript/sim/` were deleted when the simulator was extracted.
- `typescript/client.ts` — streams every `.wav` in `examples/audio/` (one session per file) using `DeepfakeDetectionClient.detectDeepfake()`; falls back to 6 × 500 ms of silence when the dir is empty. Pass `--target HOST:PORT` (e.g. `npm run client -- --target localhost:50051`) to point at a non-default server. Pass `--csv PATH` to additionally write per-chunk results to a CSV — see [CSV export](#csv-export) below.
- `typescript/phone_call.ts` — TS twin of `python/phone_call.py`: single live call, the FreeSWITCH-fork integration pattern. Run with `npm run phone-call -- --audio ../audio/your.wav`.
- `typescript/phone_call_burst.ts` — TS twin of `python/phone_call_burst.py`: N concurrent calls (recommended multi-call architecture). Run with `npm run phone-call-burst -- -c 5`. Supports `--csv PATH`.
- `typescript/common/` — shared helpers (`wav_reader`, `result_csv`, `tls`, `shutdown`) mirroring `python/common/`.

### Notes on ts-proto naming

`ts-proto` flattens nested types with underscores and suffixes service exports:

| Proto | Generated TypeScript |
|---|---|
| `service DeepfakeDetection` | `DeepfakeDetectionService` (definition), `DeepfakeDetectionServer` (server interface), `DeepfakeDetectionClient` (client class) |
| `oneof response { ... }` | discriminated optional fields on the message (e.g. `response.analysisResult`) |

Deep imports use the proto path: `@aurigin/protos/aurigin/deepfake_detection/v1/deepfake_detection`.

## Fingerprint client + simulator

New in 0.3.1: `aurigin.fingerprint.v1.Fingerprint.ExtractFingerprint` —
the consumer contract for `aurigin-fingerprint`, a GPU-only WavLM
feature extractor. Same bidi shape as deepfake (`stream AudioFrame` in,
`stream EmbeddingResult` out plus a terminal `FinalResult`); different
response payload (768-d L2-normalised float32 embedding per window, no
label / score / verdict — that's caller-side composition).

### Simulator

Minimal Python simulator at
[`simulator/fingerprint/`](simulator/fingerprint/) — packaged as
`aurigin-fingerprint-simulator-service` with a Dockerfile + docker-
compose. Deterministic embeddings from `sha256(payload[:64])` so the
same input produces the same 768-d unit vector on any machine
(smoke-test-friendly). No scenarios, no fault injection — see that
directory's README for the rationale and env-var reference
(`PORT`, `WINDOW_MS`, `EMBEDDING_DIM`).

```bash
# Terminal 1 — start the simulator
cd examples/simulator/fingerprint
uv sync
uv run fingerprint-simulator-service                 # :50051
# or:
docker compose up --build                            # same, containerised

# Run alongside the deepfake simulator on a different port:
PORT=50052 uv run fingerprint-simulator-service      # :50052
```

### Python client

```bash
cd examples/python
uv sync                                              # if not already done
uv run fingerprint-client                            # → localhost:50051 (TLS auto)
uv run fingerprint-client --target localhost:50052   # side-by-side with deepfake sim
just fingerprint-client                              # same, via justfile
just insecure-fingerprint-client                     # plaintext (docker-compose default)
just mtls-fingerprint-client                         # with client cert
```

Sample output (silence fallback):

```
# transport=TLS (self-signed, examples/certs/)

=== silence (5 s @ 16 kHz) ===
Session: sim-a1b2c3d4
Embedding | offset=0ms | duration=5000ms | code=3e7f9c1a2b4d5e6f | dim=768 | head=8e3f22c19d4a7b0e
FINAL     | total=5000ms | embeddings=1
```

### TypeScript client

```bash
cd examples/typescript
npm install                                          # if not already done
npm run fingerprint-client                           # → localhost:50051 (TLS auto)
npm run fingerprint-client -- --target localhost:50052
MTLS=1 npm run fingerprint-client                    # with client cert
```

Same `EmbeddingResult` shape as the Python client. The generated TS
client class name is `FingerprintClient`; the RPC is `extractFingerprint`
(ts-proto camelCases the proto's `ExtractFingerprint`).

### Files

- `simulator/fingerprint/` — minimal deterministic sim. See its README
  for env vars, Docker usage, and the "run alongside deepfake simulator"
  side-by-side pattern.
- `python/fingerprint_client.py` — streams every `.wav` in
  `examples/audio/` (one session per file). Falls back to 5 s of
  silence when the dir is empty. Prints per-window `EmbeddingResult`
  with a hex preview of the first 8 bytes of `embedding` so you can
  eyeball vector differences without dumping 3072 bytes per window.
  Reuses the same `common/` helpers (`wav_reader`, `tls`) as the
  deepfake client — nothing fingerprint-specific there.
- `typescript/fingerprint_client.ts` — TS twin. Same shape.

### Deferred (not shipped with the first-cut examples)

- Fingerprint-flavoured `phone_call` / `phone_call_burst` / `scan_file`
  variants — add when there's real demand. The single-session client
  covers the demo case.
- Scenario YAML system — fingerprint's deterministic-hash approach
  doesn't need scenarios. Adding one later is a compatible extension.
- Smoke test suite for the fingerprint sim in `tests/`.

## Wire messages — `AudioFrame` (new in 0.3.0) and `AudioBuffer` (deprecated)

`DetectDeepfakeRequest.oneof request` accepts three alternatives — one
for session setup and **two shapes for audio frames**:

```proto
message DetectDeepfakeRequest {
  oneof request {
    CreateSessionRequest              create_session_request = 1;
    twilio.tme.extensions.common.v1.AudioBuffer  audio       = 2 [deprecated = true];  // legacy 0.2.x shape
    aurigin.media.v1.AudioFrame                  audio_frame = 3;                       // new in 0.3.0 — preferred
  }
}
```

### `AudioFrame` — the recommended shape

Self-describing: `codec` + `sample_rate_hz` + `channels` ride on every
message, so no session-open coordination or free-form format string is
needed. The codec may even change mid-stream (e.g. Teams SDP renegotiation)
without a wire break.

```proto
message AudioFrame {
  AudioCodec codec        = 1;
  uint32     sample_rate_hz = 2;   // 8000 / 16000 / 48000
  uint32     channels      = 3;   // 1 at Aurigin edge today; kept for headroom
  bytes      payload       = 4;   // the audio bytes

  optional uint64 pts_ns   = 5;   // presentation timestamp, advisory
  optional uint64 sequence = 6;   // monotonic per-stream, gap detection
}
```

### `AudioCodec` — the enum

| Value | Notes |
|---|---|
| `AUDIO_CODEC_UNSPECIFIED = 0` | **Reject sentinel.** proto3 injects 0 when the client forgets to set the field; the simulator (and every real deepfake receiver) returns `INVALID_ARGUMENT` on receipt so the bug surfaces on frame 1. Never a valid runtime codec. |
| `AUDIO_CODEC_S16LE = 1` | 16-bit signed linear PCM, little-endian. What Teams' Media Bot host, FreeSWITCH `mod_audio_fork`, and most SDKs emit after their own decode. 2 bytes/sample. |
| `AUDIO_CODEC_S16BE = 2` | 16-bit signed linear PCM, **big-endian**. Wire-compatible with IETF L16 (RFC 3551, `audio/L16`) — Genesys AudioHook's high-fidelity option. 2 bytes/sample. |
| `AUDIO_CODEC_S24LE = 3` | 24-bit signed linear PCM, little-endian, packed 3-bytes-per-sample. Common in pro-audio and broadcast WAVs. Deepfake decodes via a vectorised numpy pad-to-int32 + astype-to-float32 pass. 3 bytes/sample. |
| `AUDIO_CODEC_S32LE = 4` | 32-bit signed linear PCM, little-endian. Same wire width as F32LE but different interpretation (integer, not float). Deepfake decodes via a single vectorised numpy int32→float32 pass. 4 bytes/sample. |
| `AUDIO_CODEC_F32LE = 5` | 32-bit IEEE-float PCM, little-endian, samples in `[-1, +1]`. What `soundfile` / librosa export for high-precision recordings. 4 bytes/sample. |
| `AUDIO_CODEC_PCMU = 6`  | G.711 μ-law, 8-bit — telco default (NICE VoiceStream, Genesys AudioHook default, SIPREC PT=0). 1 byte/sample. |
| `AUDIO_CODEC_PCMA = 7`  | G.711 A-law, 8-bit — European PSTN trunks and SIPREC PT=8. 1 byte/sample. |
| `AUDIO_CODEC_OPUS = 8`  | Opus (RFC 6716). **Reserved from 0.3.0** so the enum value is stable for future clients; the decoder is not shipped in this wave. Receivers reject with `UNIMPLEMENTED`. |

### `AudioBuffer` — deprecated

The Twilio-vendored `AudioBuffer` message still works — 0.2.x consumers
don't have to change anything to keep talking to a 0.3.0+ server. It's
marked `[deprecated = true]` and scheduled for removal in **0.4.0**.
Its free-form `format` string field carries codec identity in the old
shape (`"S16LE"` / `"F32LE"` only — telco codecs were never accepted
through this path).

### Building an `AudioFrame` — Python

```python
from aurigin.deepfake_detection.v1 import deepfake_detection_pb2 as pb
from aurigin.media.v1 import audio_frame_pb2 as af

req = pb.DetectDeepfakeRequest(
    audio_frame=af.AudioFrame(
        codec=af.AUDIO_CODEC_PCMU,       # G.711 μ-law
        sample_rate_hz=8000,
        channels=1,
        payload=ulaw_bytes,              # raw wire bytes — no client-side decode
        # pts_ns / sequence optional
    ),
)
```

The `examples/python/common/wav_reader.py` `WavData` helper exposes an
`audio_codec` property that returns the matching `AudioCodec` value
(S16LE or F32LE) so client code doesn't have to repeat the
format→enum mapping.

### Building an `AudioFrame` — TypeScript

```ts
import { DetectDeepfakeRequest } from "@aurigin/protos/aurigin/deepfake_detection/v1/deepfake_detection";
import { AudioCodec } from "@aurigin/protos/aurigin/media/v1/audio_frame";

const req: DetectDeepfakeRequest = {
  request: {
    $case: "audioFrame",
    audioFrame: {
      codec: AudioCodec.AUDIO_CODEC_PCMU,
      sampleRateHz: 8000,
      channels: 1,
      payload: ulawBytes,
    },
  },
};
```

### Migration guidance

- **New integrations**: use `audio_frame`. Full stop.
- **Existing 0.2.x consumers**: keep working with `audio` (AudioBuffer)
  until you're ready to migrate. Field-for-field mapping:

  | AudioBuffer field | AudioFrame equivalent |
  |---|---|
  | `format = "S16LE"` string | `codec = AUDIO_CODEC_S16LE` enum |
  | `format = "F32LE"` string | `codec = AUDIO_CODEC_F32LE` enum |
  | `rate` | `sample_rate_hz` |
  | `channels` | `channels` |
  | `buffer` | `payload` |
  | `pts_ns` | `pts_ns` (unchanged) |
  | `duration_ns` | *derived by the receiver* from `len(payload) / bytes_per_sample / channels / sample_rate_hz` |
  | `type = "audio/x-raw"` | *dropped* (was always the same constant) |
  | `size` | *dropped* (redundant with `len(payload)`) |

- **Simulator behaviour**: `examples/simulator/deepfake/` accepts both
  wire shapes on the same server, so a mixed-consumer environment
  (some clients on 0.2.x, others on 0.3.0) works without a coordinated
  cut-over.

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

Nothing to set. Start the simulator and client; the cert auto-detect kicks in.

```bash
# Simulator (Python — canonical, drives both language clients)
cd examples/simulator/deepfake
uv run deepfake-simulator-service      # transport=TLS

# Python client
cd examples/python
just call default                      # transport=TLS

# TypeScript client
cd examples/typescript
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
# Simulator (Python — canonical)
cd examples/simulator/deepfake
MTLS=1 uv run deepfake-simulator-service   # transport=mTLS

# Python client
cd examples/python
MTLS=1 just call default               # transport=mTLS
MTLS=1 just burst 5 default            # 5 mTLS streams, all over the same channel

# TypeScript client
cd examples/typescript
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

# One-shot override (simulator)
cd examples/simulator/deepfake
TLS_CERT=/dev/null TLS_KEY=/dev/null uv run deepfake-simulator-service

# One-shot override (client / phone-call — Python or TypeScript)
TLS_CA=/dev/null just client
```

The server side reads `TLS_CERT` + `TLS_KEY` (TLS) and `TLS_CLIENT_CA` (mTLS). The client side reads `TLS_CA` (TLS) and `TLS_CLIENT_CERT` / `TLS_CLIENT_KEY` (mTLS). Pointing any of them at a non-existent path takes the insecure branch.

### Using a real cert

Drop your own `server.crt` and `server.key` (and `client.{crt,key}` if you want mTLS) into `examples/certs/`, overwriting the committed examples, and restart. The auto-detect logic doesn't care who signed them. For Let's Encrypt or internal CAs, see the trust-model breakdown in [`certs/README.md`](certs/README.md).

## Configuring the simulator

The simulator lives in [`simulator/deepfake/`](simulator/deepfake/) and
is packaged as `aurigin-deepfake-simulator-service`. Both the Python and
TypeScript client examples target it — the previous per-language server
implementations were retired. Everything below applies to that one
canonical service.

### Env vars

| Var | Default | Purpose |
|---|---|---|
| `PORT` | `50051` | gRPC listen port. |
| `SCENARIOS_DIR` | `<repo>/examples/scenarios` | Directory the server walks at startup. Every `*.yaml` under it (recursive) is loaded and validated against `scenario.schema.json`. Duplicate `scenario.id` is a startup error. |
| `SCENARIO_DEFAULT` | `default` | Scenario id used when the client doesn't send `x-scenario-id` or sends an unknown id. Must match one of the loaded scenarios or the server exits at startup. |

Example — point the simulator at a custom directory on a non-default port:

```bash
cd examples/simulator/deepfake
PORT=50061 SCENARIOS_DIR=$HOME/my-scenarios uv run deepfake-simulator-service

# Or via docker-compose (same env vars work — either export them or
# add them to the `environment:` block in docker-compose.yml):
PORT=50061 SCENARIOS_DIR=/scenarios docker compose up --build
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
