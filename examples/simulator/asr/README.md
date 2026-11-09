# asr-simulator-service

Deterministic gRPC simulator for
`aurigin.asr.v1.Transcription.Transcribe`.

Buffers the client's `AudioFrame` stream and emits one
`TranscriptTurn` every `TURN_MS` of audio (default 2000 ms) plus a
terminal `FinalResult` at end-of-stream.

Each turn's `text` is drawn deterministically from a fixed vocabulary
cycled by turn index — repeated runs over the same audio produce
identical transcripts. Purpose is to exercise the wire shape +
client-side handling paths (turn routing, text extraction, FinalResult
aggregation) with realistic cadence, not to actually transcribe audio.
Point a real `aurigin-asr` instance at your client when you need real
STT output.

Intentionally slim: **no scenarios, no YAML, no fault injection.** Add
a scenario system if / when transcript-behaviour tests need it.

## Run — Docker (recommended)

```bash
cd examples/simulator/asr
docker compose up --build
```

Listens on `localhost:50051` insecurely (no `./certs` volume mounted).

## Run — directly with `uv` (dev)

```bash
cd examples/simulator/asr
uv sync
uv run asr-simulator-service
```

Default port `50051`; override with `PORT=…`. Auto-detects
`examples/certs/server.{crt,key}` for TLS-by-default when running from
the repo tree — the same committed self-signed certs the sibling
simulators use.

## Configuration

| Env | Default | Purpose |
|---|---|---|
| `PORT` | `50051` | gRPC listen port. |
| `TURN_MS` | `2000` | Audio duration per emitted `TranscriptTurn`, in ms. Simulates the turn cadence a real VAD would produce. |
| `MODEL_NAME` | `parakeet-sim` | Value echoed on `CreateSessionResponse.model_name`. |
| `MODEL_VERSION` | `0.0.0-sim` | Value echoed on `CreateSessionResponse.model_version` and `FinalResult.model_version`. |
| `TLS_CERT` | `examples/certs/server.crt` | Server certificate for TLS. Set to a non-existent path to force insecure mode. |
| `TLS_KEY` | `examples/certs/server.key` | Server private key for TLS. |
| `MTLS` | unset | When `1` / `true`, requires client certificates signed by `TLS_CLIENT_CA`. |
| `TLS_CLIENT_CA` | `examples/certs/client.crt` | CA bundle used to verify client certs when `MTLS=1`. |

## What it does NOT simulate

- **Real VAD / turn-cutting.** Turns are emitted every `TURN_MS` of
  buffered audio regardless of speech content. The real
  `aurigin-asr` uses Silero VAD + the handover's turn-cutter state
  machine (pause / max-length / pre-roll / min-speech rules).
- **Empty-text skip.** Real ASR drops turns whose transcription came
  back empty; the simulator always emits non-empty text, so the
  consumer's "skip empty" branch is not exercised here.
- **Confidence variance / silence-fraction populated per turn.**
  Fixed `confidence=0.95`, `silence_pct=0.0`. Add a scenario system
  when consumer tests need either.
