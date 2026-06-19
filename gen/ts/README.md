# @aurigin/protos

Generated gRPC TypeScript stubs for Aurigin services. Built from [`aurigin-protos`](https://github.com/Aurigin-ai/aurigin-protos) using [`ts-proto`](https://github.com/stephenh/ts-proto), compatible with [`@grpc/grpc-js`](https://github.com/grpc/grpc-node/tree/master/packages/grpc-js).

## Install

```bash
npm install @aurigin/protos @grpc/grpc-js
```

Ships with sigstore-backed provenance — installers and registries can verify the published tarball was built from the tagged commit on the repository's `publish-npm.yml` workflow.

Aurigin services that need a pre-promotion (release-candidate) version can install from the internal AWS CodeArtifact mirror under the same scope; see the [`infra/aws/`](https://github.com/Aurigin-ai/aurigin-protos/tree/main/infra/aws) runbook in the repo for the connection details.

## Usage

Deep-import the service module you need:

```ts
import { credentials } from "@grpc/grpc-js";
import { DeepfakeDetectionClient } from "@aurigin/protos/aurigin/deepfake_detection/v1/deepfake_detection";

const client = new DeepfakeDetectionClient(
  "localhost:50051",
  credentials.createInsecure(),
);

// DetectDeepfake is bidi-streaming — see examples/typescript/client.ts for a runnable demo.
const call = client.detectDeepfake();
call.write({ createSessionRequest: {} });
```

Server-side: import `DeepfakeDetectionService` (the service definition) and `DeepfakeDetectionServer` (the impl interface) from the same module.

## Naming convention

`ts-proto` flattens nested types with underscores and suffixes service exports:

| Proto | TypeScript |
|---|---|
| `service DeepfakeDetection` | `DeepfakeDetectionService` / `DeepfakeDetectionServer` / `DeepfakeDetectionClient` |
| `oneof response { ... }` | discriminated optional fields on the message (e.g. `response.analysisResult`) |

Currently published modules:

- `@aurigin/protos/aurigin/deepfake_detection/v1/deepfake_detection`
- `@aurigin/protos/twilio/tme/extensions/common/v1/audio_buffer`

## Source

This package is generated. To add or change a service, edit the `.proto` files in [aurigin-protos](https://github.com/Aurigin-ai/aurigin-protos), then cut a release via `gh workflow run release.yml -f version=<x.y.z>`. The orchestrator tags `main`, creates a GitHub Release, and dispatches `publish-codeartifact.yml` (internal) + `publish-npm.yml` (public npmjs.com, runs in the `public-release` env for the OIDC claim — no reviewer gate). For local dry-runs, `make publish-ts-codeartifact`.
