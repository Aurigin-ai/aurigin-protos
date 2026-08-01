# aurigin-protos

Generated gRPC Python stubs for Aurigin services. Built from
[`aurigin-protos`](https://github.com/Aurigin-ai/aurigin-protos) via
buf's `protocolbuffers/python` and `grpc/python` remote plugins.

## Install

```bash
uv pip install aurigin-protos
# or:  pip install aurigin-protos
```

Published from GitHub Actions via **PyPI Trusted Publisher** (OIDC,
no long-lived API tokens) and shipped with **PEP 740 sigstore
attestations**. See [Verifying the release](#verifying-the-release).

Aurigin engineers who need a pre-promotion (release-candidate) version
can install from the internal AWS CodeArtifact mirror under the same
name; see [`infra/aws/`](https://github.com/Aurigin-ai/aurigin-protos/tree/main/infra/aws)
in the repo.

## What's new in 0.3.0

- **`aurigin.media.v1.AudioFrame`** — new Aurigin-native audio message
  that self-describes its codec + sample rate + channels on the wire.
  Recommended shape for every new integration.
- **`AudioCodec` enum** with 6 shipping codecs (S16LE / S24LE / S32LE /
  F32LE / PCMU / PCMA) + `AUDIO_CODEC_OPUS` reserved for future use.
- **`twilio.tme.extensions.common.v1.AudioBuffer` is now deprecated**
  (`[deprecated = true]`). Existing 0.2.x code keeps working —
  `DetectDeepfakeRequest.audio` is still accepted — but the message is
  scheduled for removal in **0.4.0**. New code should use
  `DetectDeepfakeRequest.audio_frame`.

## Usage

Minimal insecure-channel setup:

```python
import grpc
from aurigin.deepfake_detection.v1 import deepfake_detection_pb2 as pb
from aurigin.deepfake_detection.v1 import deepfake_detection_pb2_grpc as pb_grpc

with grpc.insecure_channel("localhost:50051") as channel:
    stub = pb_grpc.DeepfakeDetectionStub(channel)
    # DetectDeepfake is bidi-streaming — see the AudioFrame example below.
```

### Sending an `AudioFrame` (recommended)

```python
from aurigin.deepfake_detection.v1 import deepfake_detection_pb2 as pb
from aurigin.media.v1 import audio_frame_pb2 as af

def requests(chunks):
    # Frame 1 — open the session.
    yield pb.DetectDeepfakeRequest(create_session_request=pb.CreateSessionRequest())

    # Then stream audio. Every AudioFrame is self-describing — no
    # session-open coordination needed for the codec, and it can even
    # change mid-stream (rare in practice, but on the wire).
    pts_ns = 0
    for chunk in chunks:
        yield pb.DetectDeepfakeRequest(
            audio_frame=af.AudioFrame(
                codec=af.AUDIO_CODEC_PCMU,   # G.711 μ-law (telco / NICE / Genesys / SIPREC)
                sample_rate_hz=8000,
                channels=1,
                payload=chunk,               # raw wire bytes — the server decodes
                pts_ns=pts_ns,               # optional; advisory
            ),
        )
        pts_ns += len(chunk) * 1_000_000_000 // 8000  # bytes → ns at 8k mono μ-law

for response in stub.DetectDeepfake(requests(my_audio_chunks)):
    kind = response.WhichOneof("response")
    if kind == "create_session_response":
        print(f"Session {response.create_session_response.session_id}")
    elif kind == "analysis_result":
        r = response.analysis_result
        print(f"@ {r.audio_offset_ms}ms  label={r.label}  score={r.score:.3f}")
    elif kind == "final_result":
        f = response.final_result
        print(f"FINAL  {f.overall_label}  score={f.overall_score:.3f}")
```

### `AudioCodec` values

| Enum value | Wire | Notes |
|---|---|---|
| `AUDIO_CODEC_UNSPECIFIED = 0` | — | **Reject sentinel.** Receivers return `INVALID_ARGUMENT` — proto3 injects 0 on unset scalars, and this catches clients that forgot to set the field. |
| `AUDIO_CODEC_S16LE = 1` | 16-bit signed linear PCM, LE | Teams Media Bot, FreeSWITCH `mod_audio_fork`, most SDKs. |
| `AUDIO_CODEC_F32LE = 2` | 32-bit IEEE-float PCM, LE, [-1, +1] | `soundfile` / librosa export. |
| `AUDIO_CODEC_L16 = 3`   | 16-bit signed linear PCM, **big-endian** | IETF L16 (RFC 3551) — Genesys AudioHook high-fidelity option. |
| `AUDIO_CODEC_PCMU = 4`  | G.711 μ-law, 8-bit | Telco default — NICE VoiceStream, Genesys AudioHook default, SIPREC PT=0. |
| `AUDIO_CODEC_PCMA = 5`  | G.711 A-law, 8-bit | European PSTN trunks and SIPREC PT=8. |
| `AUDIO_CODEC_OPUS = 6`  | — (reserved) | Value stable from 0.3.0; decoder not shipped. Receivers reject with `UNIMPLEMENTED`. |
| `AUDIO_CODEC_S24LE = 7` | 24-bit signed linear PCM, LE | Pro-audio and broadcast WAVs. |
| `AUDIO_CODEC_S32LE = 8` | 32-bit signed linear PCM, LE | Same wire width as F32LE but integer, not float. |

### Migrating from `AudioBuffer` (0.2.x → 0.3.0)

`AudioBuffer` still works — 0.2.x consumers don't have to change
anything. When you're ready to migrate:

| `AudioBuffer` field | `AudioFrame` equivalent |
|---|---|
| `format = "S16LE"` string | `codec = AUDIO_CODEC_S16LE` enum |
| `format = "F32LE"` string | `codec = AUDIO_CODEC_F32LE` enum |
| `rate` | `sample_rate_hz` |
| `channels` | `channels` |
| `buffer` | `payload` |
| `pts_ns` | `pts_ns` (unchanged) |
| `duration_ns` | derived by the receiver from `len(payload) / bytes_per_sample / channels / sample_rate_hz` |
| `type = "audio/x-raw"` | dropped (was always the same constant) |
| `size` | dropped (redundant with `len(payload)`) |

### Runnable examples

Full runnable client + simulator server in the repo:

- **Client**: [`examples/python/client.py`](https://github.com/Aurigin-ai/aurigin-protos/tree/main/examples/python/client.py)
  (streams `.wav` files, falls back to silence)
- **Live-call pattern**: [`examples/python/phone_call.py`](https://github.com/Aurigin-ai/aurigin-protos/tree/main/examples/python/phone_call.py)
  (real-time-paced send loop — the FreeSWITCH / Twilio Media Stream
  integration shape)
- **Multi-call load**: [`examples/python/phone_call_burst.py`](https://github.com/Aurigin-ai/aurigin-protos/tree/main/examples/python/phone_call_burst.py)
  (N concurrent bidi streams over a single channel)
- **Simulator server**: [`examples/simulator/deepfake/`](https://github.com/Aurigin-ai/aurigin-protos/tree/main/examples/simulator/deepfake/)
  (scenario-driven, Dockerfile + docker-compose; the same simulator
  drives both language smoke suites)

## Package layout

Proto packages map 1:1 to Python import paths. Each `.proto` file
produces two modules:

- `<package_path>.<file>_pb2` — message classes
- `<package_path>.<file>_pb2_grpc` — service stub + servicer base class

Currently published modules:

| Module | Status |
|---|---|
| `aurigin.deepfake_detection.v1.deepfake_detection_pb2[_grpc]` | Active — the `DeepfakeDetection` service |
| `aurigin.media.v1.audio_frame_pb2` | **New in 0.3.0** — `AudioFrame` + `AudioCodec` |
| `twilio.tme.extensions.common.v1.audio_buffer_pb2` | **Deprecated** — vendored Twilio message; scheduled for removal in 0.4.0 |

## Verifying the release

The wheel and sdist are built and uploaded by
[`publish-pypi.yml`](https://github.com/Aurigin-ai/aurigin-protos/blob/main/.github/workflows/publish-pypi.yml)
running against a tagged commit on `main`. The workflow:

- Verifies the input version is strict semver.
- Checks out the `v<version>` tag and asserts it's reachable from `main`.
- Authenticates to PyPI via **Trusted Publisher (OIDC)** — no
  long-lived API token exists in the repo.
- Runs `pypa/gh-action-pypi-publish@release/v1` with
  `attestations: true`, which attaches a **PEP 740** attestation
  bundle to every uploaded file. The signer identity is
  `https://github.com/Aurigin-ai/aurigin-protos/.github/workflows/publish-pypi.yml@refs/tags/v<X.Y.Z>`
  with OIDC issuer `https://token.actions.githubusercontent.com`.

To verify a downloaded wheel:

```bash
# Automatic (pip 24.2+ verifies PEP 740 attestations if present).
pip install aurigin-protos

# Manual, via sigstore-python:
uvx sigstore verify identity \
  --cert-identity 'https://github.com/Aurigin-ai/aurigin-protos/.github/workflows/publish-pypi.yml@refs/tags/v0.3.0' \
  --cert-oidc-issuer 'https://token.actions.githubusercontent.com' \
  --bundle aurigin_protos-0.3.0-py3-none-any.whl.publish.attestation \
  aurigin_protos-0.3.0-py3-none-any.whl
```

A non-zero exit means the wheel was not built by our `publish-pypi.yml`
workflow at that tag — refuse to install.

## Source

Generated. To change a service, edit the `.proto` files in
[aurigin-protos](https://github.com/Aurigin-ai/aurigin-protos), then
cut a release via `gh workflow run release.yml -f version=<x.y.z>`.
The orchestrator tags `main`, creates a GitHub Release, and dispatches
`publish-codeartifact.yml` (internal) + `publish-pypi.yml` (public
pypi.org). For local dry-runs: `make publish-py-codeartifact`.
