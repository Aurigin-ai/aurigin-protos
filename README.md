# aurigin-protos

[![CI](https://github.com/Aurigin-ai/aurigin-protos/actions/workflows/ci.yml/badge.svg)](https://github.com/Aurigin-ai/aurigin-protos/actions/workflows/ci.yml)
[![Publish (CodeArtifact)](https://github.com/Aurigin-ai/aurigin-protos/actions/workflows/publish-codeartifact.yml/badge.svg)](https://github.com/Aurigin-ai/aurigin-protos/actions/workflows/publish-codeartifact.yml)
[![Publish (PyPI)](https://github.com/Aurigin-ai/aurigin-protos/actions/workflows/publish-pypi.yml/badge.svg)](https://github.com/Aurigin-ai/aurigin-protos/actions/workflows/publish-pypi.yml)
[![Publish (npm)](https://github.com/Aurigin-ai/aurigin-protos/actions/workflows/publish-npm.yml/badge.svg)](https://github.com/Aurigin-ai/aurigin-protos/actions/workflows/publish-npm.yml)
[![PyPI version](https://img.shields.io/pypi/v/aurigin-protos?label=pypi%20%E2%80%A2%20aurigin-protos&logo=pypi&logoColor=white)](https://pypi.org/project/aurigin-protos/)
[![npm version](https://img.shields.io/npm/v/@aurigin/protos?label=npm%20%E2%80%A2%20%40aurigin%2Fprotos&logo=npm&logoColor=white)](https://www.npmjs.com/package/@aurigin/protos)
[![Built with buf](https://img.shields.io/badge/built%20with-buf-1A2B49?logo=buf&logoColor=white)](https://buf.build)
[![Node ≥ 22](https://img.shields.io/badge/node-%E2%89%A522-339933?logo=node.js&logoColor=white)](https://nodejs.org)
[![Python ≥ 3.10](https://img.shields.io/badge/python-%E2%89%A53.10-3776AB?logo=python&logoColor=white)](https://www.python.org)

Source of truth for Aurigin's gRPC service definitions. Generates and publishes language-specific client/server stubs to public **PyPI** and **npm**.

- **TypeScript** package: [`@aurigin/protos`](https://www.npmjs.com/package/@aurigin/protos) on npm
- **Python** package: [`aurigin-protos`](https://pypi.org/project/aurigin-protos/) on PyPI

Aurigin services also have access to an internal AWS CodeArtifact mirror for pre-promotion (release-candidate) versions — see [`infra/aws/`](infra/aws/).

## Latest published version

The **PyPI** and **npm** badges above are the canonical source of truth for the latest version. They auto-update from the public registries:

- [`pypi.org/project/aurigin-protos`](https://pypi.org/project/aurigin-protos/) — Python wheel
- [`npmjs.com/package/@aurigin/protos`](https://www.npmjs.com/package/@aurigin/protos) — TypeScript / gRPC-JS package

Both packages are stamped with the same `vX.Y.Z` — PyPI via `publish-pypi.yml`, npm via `publish-npm.yml`. Dispatch them together for a coordinated release; the two badges then advance in lockstep. Because both workflows are manual promotions, the public versions may briefly lag the most recent `v*` tag while a release candidate is being smoke-tested internally — for Aurigin services that need the bleeding-edge tagged version, the AWS CodeArtifact channel always has it (see [Consuming the packages](#consuming-the-packages) below).

Quick install — no auth, no extra index, just:

```bash
# TypeScript
npm install @aurigin/protos @grpc/grpc-js

# Python
uv pip install aurigin-protos
```

## Layout

```
aurigin-protos/
├── proto/                    # .proto sources, mirrored by package path:
│   ├── aurigin/deepfake_detection/v1/deepfake_detection.proto
│   └── twilio/tme/extensions/common/v1/audio_buffer.proto  # vendored Twilio Media Extensions type
├── gen/
│   ├── ts/                   # TypeScript package (ts-proto + @grpc/grpc-js)
│   │   ├── package.json
│   │   ├── tsconfig.json
│   │   ├── README.md         # shipped to npm registry page
│   │   └── src/              # generated (gitignored)
│   └── py/                   # Python package (grpcio + protobuf)
│       ├── pyproject.toml
│       ├── README.md         # shipped to PyPI/CodeArtifact page
│       ├── aurigin/          # generated (gitignored)
│       └── twilio/           # generated (gitignored) — vendored Twilio types
├── examples/                 # consumer-side reference snippets
│   ├── audio/                # shared .wav fixtures (gitignored)
│   ├── scenarios/            # YAML scenarios driving the Python example simulator
│   │   ├── scenario.schema.json  # JSON Schema validating every YAML at load time
│   │   ├── default.yaml          # fallback when client sends no x-scenario-id
│   │   ├── happy/                # canonical bonafide/spoofed/curve flows
│   │   ├── edge/                 # oscillating confidence, duplicate detection, etc.
│   │   └── failure/              # event-level ERROR, gRPC UNAVAILABLE, DEADLINE_EXCEEDED
│   ├── python/               # uv-managed: `uv run server|client|phone-call`
│   │   └── sim/              # scenario-driven simulator (loader + curves + runner)
│   └── typescript/           # npm-managed: `npm run server|client`
├── infra/                    # AWS + public-registry runbooks (no IaC, just docs)
│   ├── aws/                  # OIDC + publisher role + CodeArtifact setup
│   └── public/               # PyPI / npm Trusted Publishers + visibility checklist
├── buf.yaml                  # buf config + lint rules
├── buf.gen.yaml              # codegen targets
├── Makefile                  # lint / generate / build / publish
├── LICENSE                   # Apache License 2.0
├── NOTICE                    # Apache 2.0 §4(d) attribution (Aurigin + vendored Twilio proto)
└── scripts/
    ├── publish-ts-codeartifact.sh  # CodeArtifact (npm)
    └── publish-py-codeartifact.sh  # CodeArtifact (twine)
```

> The `examples/audio/` dir is shared between the Python and TypeScript examples. Drop `.wav` fixtures in (gitignored) and the clients glob them automatically. `examples/audio/generate-conversation.sh` is a small `ffmpeg` helper that stitches the dir's other `.wav`s into a FreeSWITCH-style 8 kHz mono call.

## Prerequisites

For maintainers (publishing):

- `buf` — `brew install bufbuild/buf/buf`
- Node 22+ — used to run `ts-proto` and build the TS package
- Python 3.10+ with `build` (only — the publish workflows run `twine` themselves)
- AWS CLI v2 with credentials for the shared account, for local dry-runs of the CodeArtifact path. Not required for public publishing — `publish-pypi.yml` and `publish-npm.yml` run purely on OIDC tokens from GitHub.
- `gh` CLI authenticated against the `Aurigin-ai` org — required to cut a release (`gh workflow run release.yml -f version=X.Y.Z`, which dispatches all three publish workflows for you). Direct dispatch of `publish-codeartifact.yml` / `publish-pypi.yml` / `publish-npm.yml` is available for re-runs and recoveries.

The AWS + public-registry setup that backs the publish workflows is documented step-by-step in [`infra/`](infra/). Re-run those runbooks only if the underlying AWS / pypi.org / npmjs.com state is ever lost.

For consumers, see [Consuming the packages](#consuming-the-packages) below.

## Workflow

### 1. Add or edit a proto

Edit `.proto` files under `proto/`. Conventions:
- File path mirrors the package: `proto/<pkg-with-slashes>/<file>.proto`
  (e.g. `proto/aurigin/deepfake_detection/v1/deepfake_detection.proto` for package `aurigin.deepfake_detection.v1`)
- Each major API version lives in its own `.../v<N>/` directory
- Lint: `buf lint` enforces the `STANDARD` rule set, with `SERVICE_SUFFIX`
  exempted (see `buf.yaml`) so `DeepfakeDetection` keeps its current wire name

### 2. Lint and check breaking changes

```bash
make lint
make breaking      # compares HEAD against origin/main
```

### 3. Generate stubs

```bash
make generate
```

This installs `ts-proto` if needed, runs `buf generate` to produce both Python and TypeScript stubs, and creates `__init__.py` files for the Python package.

### 4. Release (single dispatch → tag + GitHub Release + all three publishers)

Versions live **only in tags**. `gen/ts/package.json` and `gen/py/pyproject.toml` stay at `0.0.0` on `main`; the publish workflows stamp the version from their `version` input at publish time.

```bash
gh workflow run release.yml -f version=0.1.0
```

This tags `main` as `v0.1.0`, creates a GitHub Release with auto-generated notes, and dispatches all three publish workflows below. The public-* workflows run in the `public-release` GitHub Environment so their OIDC tokens match the PyPI / npm Trusted Publisher bindings; there are no required reviewers, so all three run straight through.

| Output | Workflow | Triggered by |
|---|---|---|
| `@aurigin/protos@0.1.0` (npm) on AWS CodeArtifact | `.github/workflows/publish-codeartifact.yml` | **Manual dispatch only** (with `version` input — usually via `release.yml`) |
| `aurigin-protos==0.1.0` (PyPI) on AWS CodeArtifact | `.github/workflows/publish-codeartifact.yml` | **Manual dispatch only** (with `version` input — usually via `release.yml`) |
| `@aurigin/protos@0.1.0` on **public npmjs.com** | `.github/workflows/publish-npm.yml` | **Manual dispatch only** (with `version` input) |
| `aurigin-protos==0.1.0` on **public pypi.org** | `.github/workflows/publish-pypi.yml` | **Manual dispatch only** (with `version` input) |

CodeArtifact is the release-candidate lane: every tag lands there first and internal consumers pick it up. The public promotion is a separate, deliberate click — see `infra/public/` for the trust-publisher setup that backs it.

#### What you have to configure once

The CodeArtifact channel uses GitHub OIDC (not a static AWS key). The IAM role, CodeArtifact domain/repository, and the matching GitHub Actions secrets/variables are documented in [`infra/aws/`](infra/aws/) — that runbook is the source of truth for the internal-channel coordinates and is intended for Aurigin engineers.

The public channel (`publish-pypi.yml` + `publish-npm.yml`) requires no GitHub-side secrets at all. Trust is configured on pypi.org and npmjs.com themselves — short-lived OIDC tokens validated against per-package Trusted Publisher bindings, scoped via the `public-release` GitHub Environment so the `environment` claim matches. No reviewers on the env — it exists purely for the OIDC claim. See [`infra/public/`](infra/public/) for the step-by-step.

## Consuming the packages

The **default install path is the public registries** — `npmjs.com` for TypeScript, `pypi.org` for Python. No AWS credentials, no extra index URL, no `.npmrc`. Both packages ship with build provenance / sigstore attestations that downstream tooling can verify.

The internal **AWS CodeArtifact** channel is kept around for Aurigin services that need pre-release versions before they are promoted to the public registries. It's not the recommended path for anyone outside the Aurigin AWS org.

### TypeScript (downstream service)

```bash
npm install @aurigin/protos @grpc/grpc-js
```

```ts
import { credentials } from "@grpc/grpc-js";
import { DeepfakeDetectionClient } from "@aurigin/protos/aurigin/deepfake_detection/v1/deepfake_detection";

const client = new DeepfakeDetectionClient(
  "localhost:50051",
  credentials.createInsecure(),
);
```

Full server + client snippets: [examples/typescript/](examples/typescript/).

> *Aurigin engineers who need to install from the internal CodeArtifact channel (e.g. to pick up a tagged version before it has been promoted to public npm): see [`infra/aws/`](infra/aws/) for the connection details.*

### Python (downstream service, using `uv`)

```bash
uv pip install aurigin-protos
```

```python
from aurigin.deepfake_detection.v1 import deepfake_detection_pb2, deepfake_detection_pb2_grpc
from twilio.tme.extensions.common.v1 import audio_buffer_pb2
```

Full server + client snippets: [examples/python/](examples/python/).

> *Aurigin engineers who need to install from the internal CodeArtifact channel (e.g. to pick up a tagged version before it has been promoted to public PyPI, or to keep build inputs inside the AWS perimeter): see [`infra/aws/`](infra/aws/) for the connection details.*

## Git workflow

A few conventions enforced at the repo level — worth knowing before opening a PR:

- **PR-only.** Direct pushes to `main` are blocked by branch protection. Push your work to a feature branch and open a PR.
- **Squash-merge only.** "Create a merge commit" and "Rebase and merge" are disabled. Each merge produces exactly one commit on `main`, with the **PR title as the subject** and the **PR body as the message body** — pick descriptive PR titles, they become permanent history.
- **Branches auto-delete on merge.** Don't worry about cleanup; GitHub removes the source branch the moment the PR merges.
- **CI must be green.** `buf lint + breaking + format`, `TypeScript build`, `Python build + import smoke test`, and `shellcheck publish scripts` are required status checks. `buf breaking` runs against `main`, so a wire-incompatible proto change will fail CI unless it's intentional and reviewed.
- **PR template.** `.github/pull_request_template.md` pre-fills sections for summary, changes, wire/API impact (additive vs breaking — please be honest), and verification. Reviewers rely on the wire-impact tickbox to gate downstream upgrades.
- **Versioning happens in publish workflows, not on `main`.** `gen/ts/package.json` and `gen/py/pyproject.toml` stay at `0.0.0` in the source tree; `publish-codeartifact.yml`, `publish-pypi.yml`, and `publish-npm.yml` (all manual dispatch with a `version` input, typically driven by `release.yml`) stamp the version at publish time.

## Adding a new service

1. Create `proto/<package-path>/<service>.proto` (file path must mirror the proto `package`).
2. `make lint` — fail fast on naming, package, version-suffix and other STANDARD-rule violations before generating anything.
3. `make generate` — produce Python and TypeScript stubs.
4. Wire the new RPC into the example server and at least one example client in **both** languages (`examples/python/` and `examples/typescript/`). The Python server is a config-driven simulator (`examples/python/sim/`) — for a new RPC, extend `sim/runner.py` to handle its message types, and add one or more YAML scenarios under `examples/scenarios/` so consumers can exercise the new service end-to-end. The TypeScript server stays a thin stub. **Stub / scenario logic only — no real ML in this repo.**
5. Add an end-to-end smoke test for the new RPC in **both** test suites (`examples/python/tests/test_smoke.py`, `examples/typescript/tests/smoke.test.ts`). The existing `DetectDeepfake` test is the template.
6. Run everything locally before pushing:
   ```bash
   make lint && make generate && \
     pytest examples/python/tests/ && \
     (cd examples/typescript && npm test)
   ```
7. Open a PR. CI runs `buf lint + breaking + format`, both language builds, and both end-to-end smoke tests.
8. After merge, cut a release: `gh workflow run release.yml -f version=<x.y.z>`. The orchestrator tags `main` as `v<x.y.z>`, creates a GitHub Release with auto-generated notes, and dispatches `publish-codeartifact.yml`, `publish-pypi.yml`, and `publish-npm.yml` with that version. The public-* workflows run in the `public-release` GitHub Environment (no reviewers — used only for the OIDC `environment` claim that matches the Trusted Publisher bindings). Source files stay at `0.0.0`; the workflows stamp the version from the input.

## License

Released under the [Apache License 2.0](LICENSE) — includes an explicit patent grant, which matters for a proto / IDL library. The [`NOTICE`](NOTICE) file records the Aurigin copyright and the attribution for the vendored Twilio `audio_buffer.proto` (also Apache 2.0).
