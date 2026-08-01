# @aurigin/protos

Generated gRPC TypeScript stubs for Aurigin services. Built from
[`aurigin-protos`](https://github.com/Aurigin-ai/aurigin-protos) using
[`ts-proto`](https://github.com/stephenh/ts-proto), compatible with
[`@grpc/grpc-js`](https://github.com/grpc/grpc-node/tree/master/packages/grpc-js).

## Install

```bash
npm install @aurigin/protos @grpc/grpc-js
```

Published from GitHub Actions via **npm Trusted Publisher** (OIDC,
no long-lived `NPM_TOKEN`) with `--provenance` — every published
tarball carries a **sigstore attestation** binding it to the exact
tag + workflow that produced it. See
[Verifying the release](#verifying-the-release).

Aurigin engineers who need a pre-promotion (release-candidate) version
can install from the internal AWS CodeArtifact mirror under the same
scope; see [`infra/aws/`](https://github.com/Aurigin-ai/aurigin-protos/tree/main/infra/aws)
in the repo.

## What's new in 0.3.0

- **`AudioFrame`** (`aurigin.media.v1`) — new Aurigin-native audio
  message, self-describes its codec + sample rate + channels on the
  wire. Recommended shape for every new integration.
- **`AudioCodec` enum** with 6 shipping codecs (S16LE / S24LE / S32LE /
  F32LE / PCMU / PCMA) + `AUDIO_CODEC_OPUS` reserved.
- **`AudioBuffer` is deprecated** — the ts-proto message still ships,
  the `DetectDeepfakeRequest.audio` oneof branch still works, but
  everything is scheduled for removal in **0.4.0**. New code should
  use `DetectDeepfakeRequest.audioFrame`.

## Usage

Minimal insecure-channel setup:

```ts
import { credentials } from "@grpc/grpc-js";
import { DeepfakeDetectionClient } from "@aurigin/protos/aurigin/deepfake_detection/v1/deepfake_detection";

const client = new DeepfakeDetectionClient(
  "localhost:50051",
  credentials.createInsecure(),
);
// DetectDeepfake is bidi-streaming — see the AudioFrame example below.
```

### Sending an `AudioFrame` (recommended)

```ts
import { credentials } from "@grpc/grpc-js";
import { DeepfakeDetectionClient } from "@aurigin/protos/aurigin/deepfake_detection/v1/deepfake_detection";
import { AudioCodec } from "@aurigin/protos/aurigin/media/v1/audio_frame";

const client = new DeepfakeDetectionClient(
  "localhost:50051",
  credentials.createInsecure(),
);
const call = client.detectDeepfake();

call.on("data", (response) => {
  if (response.createSessionResponse) {
    console.log(`Session ${response.createSessionResponse.sessionId}`);
  } else if (response.analysisResult) {
    const r = response.analysisResult;
    console.log(`@ ${r.audioOffsetMs}ms  label=${r.label}  score=${r.score.toFixed(3)}`);
  } else if (response.finalResult) {
    const f = response.finalResult;
    console.log(`FINAL  ${f.overallLabel}  score=${f.overallScore.toFixed(3)}`);
  }
});

// Frame 1 — open the session.
call.write({ createSessionRequest: {} });

// Then stream audio. Every AudioFrame is self-describing.
let ptsNs = 0n;
for (const chunk of chunks /* : Uint8Array[] */) {
  call.write({
    audioFrame: {
      codec: AudioCodec.AUDIO_CODEC_PCMU,   // G.711 μ-law (telco / NICE / Genesys / SIPREC)
      sampleRateHz: 8000,
      channels: 1,
      payload: chunk,                        // raw wire bytes — the server decodes
      ptsNs,                                 // optional; advisory
    },
  });
  ptsNs += BigInt(chunk.length) * 1_000_000_000n / 8000n;  // bytes → ns at 8k mono μ-law
}
call.end();
```

### `AudioCodec` values

| Enum value | Wire | Notes |
|---|---|---|
| `AUDIO_CODEC_UNSPECIFIED = 0` | — | **Reject sentinel.** Receivers return `INVALID_ARGUMENT` — proto3 injects 0 on unset scalars, and this catches clients that forgot to set the field. |
| `AUDIO_CODEC_S16LE = 1` | 16-bit signed linear PCM, LE | Teams Media Bot, FreeSWITCH `mod_audio_fork`, most SDKs. |
| `AUDIO_CODEC_S16BE = 2` | 16-bit signed linear PCM, **big-endian** | Wire-compatible with IETF L16 (RFC 3551, `audio/L16`) — Genesys AudioHook high-fidelity option. |
| `AUDIO_CODEC_S24LE = 3` | 24-bit signed linear PCM, LE | Pro-audio and broadcast WAVs. |
| `AUDIO_CODEC_S32LE = 4` | 32-bit signed linear PCM, LE | Same wire width as F32LE but integer, not float. |
| `AUDIO_CODEC_F32LE = 5` | 32-bit IEEE-float PCM, LE, [-1, +1] | `soundfile` / librosa export. |
| `AUDIO_CODEC_PCMU = 6`  | G.711 μ-law, 8-bit | Telco default — NICE VoiceStream, Genesys AudioHook default, SIPREC PT=0. |
| `AUDIO_CODEC_PCMA = 7`  | G.711 A-law, 8-bit | European PSTN trunks and SIPREC PT=8. |
| `AUDIO_CODEC_OPUS = 8`  | — (reserved) | Value stable from 0.3.0; decoder not shipped. Receivers reject with `UNIMPLEMENTED`. |

### Migrating from `AudioBuffer` (0.2.x → 0.3.0)

`AudioBuffer` still works — 0.2.x consumers don't have to change
anything. When you're ready to migrate:

| `AudioBuffer` field | `AudioFrame` equivalent |
|---|---|
| `format = "S16LE"` string | `codec = AudioCodec.AUDIO_CODEC_S16LE` |
| `format = "F32LE"` string | `codec = AudioCodec.AUDIO_CODEC_F32LE` |
| `rate` | `sampleRateHz` |
| `channels` | `channels` |
| `buffer` | `payload` |
| `ptsNs` | `ptsNs` (unchanged) |
| `durationNs` | derived by the receiver from `payload.length / bytesPerSample / channels / sampleRateHz` |
| `type = "audio/x-raw"` | dropped (always the same constant) |
| `size` | dropped (redundant with `payload.length`) |

### Runnable examples

Full runnable client + simulator server in the repo:

- **Client**: [`examples/typescript/client.ts`](https://github.com/Aurigin-ai/aurigin-protos/tree/main/examples/typescript/client.ts)
- **Live-call pattern**: [`examples/typescript/phone_call.ts`](https://github.com/Aurigin-ai/aurigin-protos/tree/main/examples/typescript/phone_call.ts)
- **Multi-call load**: [`examples/typescript/phone_call_burst.ts`](https://github.com/Aurigin-ai/aurigin-protos/tree/main/examples/typescript/phone_call_burst.ts)
- **Simulator server** (Python, drives both languages):
  [`examples/simulator/deepfake/`](https://github.com/Aurigin-ai/aurigin-protos/tree/main/examples/simulator/deepfake/)
  — Dockerfile + docker-compose

## `ts-proto` naming convention

`ts-proto` flattens nested types with underscores and suffixes service exports:

| Proto | TypeScript |
|---|---|
| `service DeepfakeDetection` | `DeepfakeDetectionService` / `DeepfakeDetectionServer` / `DeepfakeDetectionClient` |
| `oneof response { ... }` | discriminated optional fields on the message (e.g. `response.analysisResult`) |
| `oneof request { ... }` (input) | tagged union: `{ audioFrame: {...} }` or `{ createSessionRequest: {} }` |

Deep imports use the proto path:
`@aurigin/protos/aurigin/deepfake_detection/v1/deepfake_detection`.

## Package layout

Currently published modules:

| Module | Status |
|---|---|
| `@aurigin/protos/aurigin/deepfake_detection/v1/deepfake_detection` | Active — the `DeepfakeDetection` service |
| `@aurigin/protos/aurigin/media/v1/audio_frame` | **New in 0.3.0** — `AudioFrame` + `AudioCodec` |
| `@aurigin/protos/twilio/tme/extensions/common/v1/audio_buffer` | **Deprecated** — vendored Twilio message; scheduled for removal in 0.4.0 |

## Verifying the release

The tarball is built and published by
[`publish-npm.yml`](https://github.com/Aurigin-ai/aurigin-protos/blob/main/.github/workflows/publish-npm.yml)
running against a tagged commit on `main`. The workflow:

- Verifies the input version is strict semver.
- Checks out the `v<version>` tag and asserts it's reachable from `main`.
- Authenticates to npm via **Trusted Publisher (OIDC)** — no
  `NPM_TOKEN` exists in the repo.
- Runs `npm publish --provenance --access public`, which attaches a
  **sigstore** attestation signed with the same OIDC identity:
  `https://github.com/Aurigin-ai/aurigin-protos/.github/workflows/publish-npm.yml@refs/tags/v<X.Y.Z>`.

To verify the attestation of a package you've installed:

```bash
# Verify every installed package's provenance / signatures in one shot:
npm audit signatures

# Or inspect the provenance JSON directly:
npm view @aurigin/protos@0.3.0 --json  |  jq '.dist.attestations'
```

`npm audit signatures` returns non-zero when any package's attestation
fails to verify — worth wiring into CI for downstream consumers who
want to gate on it.

## Source

Generated. To change a service, edit the `.proto` files in
[aurigin-protos](https://github.com/Aurigin-ai/aurigin-protos), then
cut a release via `gh workflow run release.yml -f version=<x.y.z>`.
The orchestrator tags `main`, creates a GitHub Release, and dispatches
`publish-codeartifact.yml` (internal) + `publish-npm.yml` (public
npmjs.com). For local dry-runs: `make publish-ts-codeartifact`.
