# @aurigin/protos

Generated gRPC TypeScript stubs for Aurigin services. Built from [`aurigin-protos`](https://github.com/Aurigin-ai/aurigin-protos) using [`ts-proto`](https://github.com/stephenh/ts-proto), compatible with [`@grpc/grpc-js`](https://github.com/grpc/grpc-node/tree/master/packages/grpc-js).

## Install

This package is published in two places. Pick whichever your team authenticates against. The package scope differs by registry; the import path under that scope is identical.

### Option A — AWS CodeArtifact (`@aurigin/protos`)

```bash
aws codeartifact login --tool npm \
  --domain $AURIGIN_CA_DOMAIN \
  --domain-owner $AURIGIN_CA_DOMAIN_OWNER \
  --repository $AURIGIN_CA_REPO \
  --region $AWS_REGION

npm install @aurigin/protos @grpc/grpc-js
```

### Option B — GitHub Packages (`@aurigin-ai/protos`)

Add to your project's `.npmrc`:

```
@aurigin-ai:registry=https://npm.pkg.github.com
//npm.pkg.github.com/:_authToken=${GITHUB_TOKEN}
```

Then:

```bash
npm install @aurigin-ai/protos @grpc/grpc-js
```

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

Currently published modules — same files under either scope, depending on which registry you installed from:

| Registry | Scope | Deepfake detection module | Twilio AudioBuffer module |
|---|---|---|---|
| AWS CodeArtifact | `@aurigin` | `@aurigin/protos/aurigin/deepfake_detection/v1/deepfake_detection` | `@aurigin/protos/twilio/tme/extensions/common/v1/audio_buffer` |
| GitHub Packages | `@aurigin-ai` | `@aurigin-ai/protos/aurigin/deepfake_detection/v1/deepfake_detection` | `@aurigin-ai/protos/twilio/tme/extensions/common/v1/audio_buffer` |

## Source

This package is generated. To add or change a service, edit the `.proto` files in [aurigin-protos](https://github.com/Aurigin-ai/aurigin-protos), tag a release (`git tag v<x.y.z> && git push --tags`); both publish workflows fire in parallel and ship to CodeArtifact + GitHub Packages. For local dry-runs, `make publish-ts-codeartifact` (CodeArtifact) or `make publish-ts-github` (GitHub Packages).
