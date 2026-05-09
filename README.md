# aurigin-protos

[![CI](https://github.com/Aurigin-ai/aurigin-protos/actions/workflows/ci.yml/badge.svg)](https://github.com/Aurigin-ai/aurigin-protos/actions/workflows/ci.yml)
[![Publish (GitHub Packages)](https://github.com/Aurigin-ai/aurigin-protos/actions/workflows/publish.yml/badge.svg)](https://github.com/Aurigin-ai/aurigin-protos/actions/workflows/publish.yml)
[![Publish (CodeArtifact)](https://github.com/Aurigin-ai/aurigin-protos/actions/workflows/publish-codeartifact.yml/badge.svg)](https://github.com/Aurigin-ai/aurigin-protos/actions/workflows/publish-codeartifact.yml)
[![Latest release](https://img.shields.io/github/v/release/Aurigin-ai/aurigin-protos?sort=semver)](https://github.com/Aurigin-ai/aurigin-protos/releases/latest)
[![Built with buf](https://img.shields.io/badge/built%20with-buf-1A2B49?logo=buf&logoColor=white)](https://buf.build)
[![Node ≥ 22](https://img.shields.io/badge/node-%E2%89%A522-339933?logo=node.js&logoColor=white)](https://nodejs.org)
[![Python ≥ 3.10](https://img.shields.io/badge/python-%E2%89%A53.10-3776AB?logo=python&logoColor=white)](https://www.python.org)

Source of truth for Aurigin's gRPC service definitions. Generates and publishes language-specific client/server stubs to AWS CodeArtifact and GitHub Packages.

- **TypeScript** package: `@aurigin/protos` (CodeArtifact) · `@aurigin-ai/protos` (GitHub Packages)
- **Python** package: `aurigin-protos` (CodeArtifact) · wheel attached to GitHub Releases

## Latest published version

The version stamped into the published packages always matches the most recent `v*` tag — see the **Latest release** badge above (it auto-updates from the [GitHub Releases page](https://github.com/Aurigin-ai/aurigin-protos/releases/latest)).

Quick install (replace nothing — the registries always serve the latest):

```bash
# TypeScript via CodeArtifact (after `aws codeartifact login --tool npm ...`)
npm install @aurigin/protos

# TypeScript via GitHub Packages (after .npmrc setup, see "Consuming" below)
npm install @aurigin-ai/protos

# Python via CodeArtifact (after `aws codeartifact login --tool pip ...`)
uv pip install aurigin-protos

# Python wheel pinned to a specific GH Release tag (no PyPI on GitHub)
gh release download v<x.y.z> -R Aurigin-ai/aurigin-protos -p '*.whl' \
  && uv pip install ./aurigin_protos-*.whl
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
│   ├── python/               # uv-managed: `uv run server|client|phone-call`
│   └── typescript/           # npm-managed: `npm run server|client`
├── buf.yaml                  # buf config + lint rules
├── buf.gen.yaml              # codegen targets
├── Makefile                  # lint / generate / build / publish
└── scripts/
    ├── publish-ts-codeartifact.sh  # CodeArtifact (npm)
    ├── publish-py-codeartifact.sh  # CodeArtifact (twine)
    ├── publish-ts-github.sh        # GitHub Packages (npm)
    └── publish-py-github.sh        # GitHub Release asset (wheel + sdist)
```

> The `examples/audio/` dir is shared between the Python and TypeScript examples. Drop `.wav` fixtures in (gitignored) and the clients glob them automatically. `examples/audio/generate-conversation.sh` is a small `ffmpeg` helper that stitches the dir's other `.wav`s into a FreeSWITCH-style 8 kHz mono call.

## Prerequisites

For maintainers (publishing):

- `buf` — `brew install bufbuild/buf/buf`
- Node 22+ — used to run `ts-proto` and build the TS package
- Python 3.10+ with `build` + `twine` — `uv pip install build twine` (or `pip install build twine`)
- AWS CLI v2 with credentials for the CodeArtifact account (CodeArtifact path only)
- `gh` CLI authenticated against the `Aurigin-ai` org (GitHub Packages / Releases path only)

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

### 4. Release (tag → both workflows fire in parallel)

Versions live **only in tags**. `gen/ts/package.json` and `gen/py/pyproject.toml` stay at `0.0.0` on `main`; the publish workflows stamp the version from the `v*` tag at publish time. Pushing one tag publishes to **all four** outputs simultaneously:

```bash
git tag v0.1.0
git push origin v0.1.0
```

| Output | Workflow | Triggered by |
|---|---|---|
| `aurigin-protos@0.1.0` (npm) on AWS CodeArtifact | `.github/workflows/publish-codeartifact.yml` | `v*` tag push |
| `aurigin-protos==0.1.0` (PyPI) on AWS CodeArtifact | `.github/workflows/publish-codeartifact.yml` | `v*` tag push |
| `@<repo-owner>/protos@0.1.0` on GitHub Packages | `.github/workflows/publish.yml` | `v*` tag push |
| `aurigin_protos-0.1.0-py3-none-any.whl` + `.tar.gz` attached to GitHub Release `v0.1.0` | `.github/workflows/publish.yml` | `v*` tag push |

Both workflows also support manual runs via the Actions → Run workflow button (with a version input).

#### Why two workflows

GitHub Packages doesn't host a Python registry, so the Python wheel/sdist take the **GitHub Release asset** path instead. The TS scope is also rewritten at publish time: source `package.json` says `@aurigin/protos` (CodeArtifact-friendly); the GH Packages workflow rewrites it to `@aurigin-ai/protos` (matches the GitHub Packages scope rule) without touching the file on `main`.

#### What you have to configure once

For `publish-codeartifact.yml` to authenticate (it uses GitHub OIDC, not a static AWS key), add at repo Settings → Secrets and variables → Actions:

- **Secret:** `AWS_ROLE_TO_ASSUME` — IAM role ARN trusting GitHub OIDC, scoped to `repo:Aurigin-ai/aurigin-protos:ref:refs/tags/v*` (release path) **and** `repo:Aurigin-ai/aurigin-protos:ref:refs/heads/main` (manual `workflow_dispatch` from main). The role's permissions policy needs `codeartifact:GetAuthorizationToken` / `GetRepositoryEndpoint` / `ReadFromRepository` / `PublishPackageVersion` / `PutPackageMetadata`, plus `sts:GetServiceBearerToken` (with `StringEquals` condition `sts:AWSServiceName = codeartifact.amazonaws.com`).
- **Variables:** `AWS_REGION = eu-west-1`, `AURIGIN_CA_DOMAIN = aurigin-ai-domain`, `AURIGIN_CA_DOMAIN_OWNER = 717279723333`, `AURIGIN_CA_REPO = aurigin-shared`.

`publish.yml` only needs the workflow's auto-issued `GITHUB_TOKEN` — no setup.

#### Local dry-runs

Useful when CI is unavailable or you want to test a publish script change before tagging. Each script reads env vars and shells out the same way the workflow would:

```bash
# AWS CodeArtifact (values for this repo's setup)
export AURIGIN_CA_DOMAIN=aurigin-ai-domain \
       AURIGIN_CA_DOMAIN_OWNER=717279723333 \
       AURIGIN_CA_REPO=aurigin-shared \
       AWS_REGION=eu-west-1
make publish-codeartifact      # = publish-ts-codeartifact + publish-py-codeartifact

# GitHub Packages + Release
export GITHUB_TOKEN=$(gh auth token) \
       GITHUB_REPO=Aurigin-ai/aurigin-protos GITHUB_TAG=v0.1.0
make publish-github            # = publish-ts-github + publish-py-github

# Both at once (CodeArtifact first, then GitHub)
make publish
```

## Consuming the packages

### TypeScript (downstream service)

**From AWS CodeArtifact** (`@aurigin/protos`):

```bash
aws codeartifact login --tool npm \
  --domain aurigin-ai-domain \
  --domain-owner 717279723333 \
  --repository aurigin-shared \
  --region eu-west-1

npm install @aurigin/protos
```

**From GitHub Packages** (`@aurigin-ai/protos`):

Add to your project's `.npmrc`:

```
@aurigin-ai:registry=https://npm.pkg.github.com
//npm.pkg.github.com/:_authToken=${GITHUB_TOKEN}
```

then:

```bash
npm install @aurigin-ai/protos
```

Either install gives the same generated code under the registry's scope. Substitute the scope you used when you import:

```ts
// CodeArtifact:
import { DeepfakeDetectionClient } from "@aurigin/protos/aurigin/deepfake_detection/v1/deepfake_detection";

// GitHub Packages:
import { DeepfakeDetectionClient } from "@aurigin-ai/protos/aurigin/deepfake_detection/v1/deepfake_detection";
```

Full server + client snippets: [examples/typescript/](examples/typescript/).

### Python (downstream service, using `uv`)

**From AWS CodeArtifact** (`aurigin-protos`):

```bash
aws codeartifact login --tool pip \
  --domain aurigin-ai-domain \
  --domain-owner 717279723333 \
  --repository aurigin-shared \
  --region eu-west-1

uv venv
uv pip install aurigin-protos
```

For project-managed deps, declare the CodeArtifact index in `pyproject.toml` — see [examples/README.md](examples/README.md#option-b--project-managed-pyprojecttoml) for the full snippet.

**From a GitHub Release** (no PyPI registry on GitHub Packages). Pick a tag from [the Releases page](https://github.com/Aurigin-ai/aurigin-protos/releases/latest) (badge at the top of this README is always current) and substitute it for `<x.y.z>`:

```bash
uv pip install \
  "https://github.com/Aurigin-ai/aurigin-protos/releases/download/v<x.y.z>/aurigin_protos-<x.y.z>-py3-none-any.whl"
```

Or in `pyproject.toml`:

```toml
[project]
dependencies = [
    "aurigin-protos @ https://github.com/Aurigin-ai/aurigin-protos/releases/download/v<x.y.z>/aurigin_protos-<x.y.z>-py3-none-any.whl",
]
```

For private repos (or to always grab the latest without hardcoding), use `gh release download`:

```bash
gh release download v<x.y.z> -R Aurigin-ai/aurigin-protos -p '*.whl' \
  && uv pip install ./aurigin_protos-*.whl
# Or, omit the tag to download from the latest release:
gh release download -R Aurigin-ai/aurigin-protos -p '*.whl' \
  && uv pip install ./aurigin_protos-*.whl
```

```python
from aurigin.deepfake_detection.v1 import deepfake_detection_pb2, deepfake_detection_pb2_grpc
from twilio.tme.extensions.common.v1 import audio_buffer_pb2
```

Full server + client snippets: [examples/python/](examples/python/).

## Git workflow

A few conventions enforced at the repo level — worth knowing before opening a PR:

- **PR-only.** Direct pushes to `main` are blocked by branch protection. Push your work to a feature branch and open a PR.
- **Squash-merge only.** "Create a merge commit" and "Rebase and merge" are disabled. Each merge produces exactly one commit on `main`, with the **PR title as the subject** and the **PR body as the message body** — pick descriptive PR titles, they become permanent history.
- **Branches auto-delete on merge.** Don't worry about cleanup; GitHub removes the source branch the moment the PR merges.
- **CI must be green.** `buf lint + breaking + format`, `TypeScript build`, `Python build + import smoke test`, and `shellcheck publish scripts` are required status checks. `buf breaking` runs against `main`, so a wire-incompatible proto change will fail CI unless it's intentional and reviewed.
- **PR template.** `.github/pull_request_template.md` pre-fills sections for summary, changes, wire/API impact (additive vs breaking — please be honest), and verification. Reviewers rely on the wire-impact tickbox to gate downstream upgrades.
- **Versioning happens in publish workflows, not on `main`.** `gen/ts/package.json` and `gen/py/pyproject.toml` stay at `0.0.0` in the source tree; the version is stamped in by `publish.yml` / `publish-codeartifact.yml` from the `v*` tag that triggered the run.

## Adding a new service

1. Create `proto/<package-path>/<service>.proto` (file path must mirror the proto `package`).
2. `make lint` — fail fast on naming, package, version-suffix and other STANDARD-rule violations before generating anything.
3. `make generate` — produce Python and TypeScript stubs.
4. Wire the new RPC into the example server and at least one example client in **both** languages (`examples/python/` and `examples/typescript/`). Stub logic only — no real ML in this repo.
5. Add an end-to-end smoke test for the new RPC in **both** test suites (`examples/python/tests/test_smoke.py`, `examples/typescript/tests/smoke.test.ts`). The existing `DetectDeepfake` test is the template.
6. Run everything locally before pushing:
   ```bash
   make lint && make generate && \
     pytest examples/python/tests/ && \
     (cd examples/typescript && npm test)
   ```
7. Open a PR. CI runs `buf lint + breaking + format`, both language builds, and both end-to-end smoke tests.
8. After merge, tag the release: `git tag v<x.y.z> && git push --tags`. Both publish workflows fire in parallel — the same tag pushes to **all four** outputs (CodeArtifact npm + Py, GitHub Packages npm, Python wheel as GH Release asset). Source files stay at `0.0.0`; the workflows stamp the version from the tag.
