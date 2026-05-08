# aurigin-protos

[![CI](https://github.com/Aurigin-ai/aurigin-protos/actions/workflows/ci.yml/badge.svg)](https://github.com/Aurigin-ai/aurigin-protos/actions/workflows/ci.yml)
[![Publish (GitHub Packages)](https://github.com/Aurigin-ai/aurigin-protos/actions/workflows/publish.yml/badge.svg)](https://github.com/Aurigin-ai/aurigin-protos/actions/workflows/publish.yml)
[![Publish (CodeArtifact)](https://github.com/Aurigin-ai/aurigin-protos/actions/workflows/publish-codeartifact.yml/badge.svg)](https://github.com/Aurigin-ai/aurigin-protos/actions/workflows/publish-codeartifact.yml)
[![Latest release](https://img.shields.io/github/v/release/Aurigin-ai/aurigin-protos?sort=semver)](https://github.com/Aurigin-ai/aurigin-protos/releases/latest)
[![Built with buf](https://img.shields.io/badge/built%20with-buf-1A2B49?logo=buf&logoColor=white)](https://buf.build)
[![Node ≥ 20](https://img.shields.io/badge/node-%E2%89%A520-339933?logo=node.js&logoColor=white)](https://nodejs.org)
[![Python ≥ 3.10](https://img.shields.io/badge/python-%E2%89%A53.10-3776AB?logo=python&logoColor=white)](https://www.python.org)

Source of truth for Aurigin's gRPC service definitions. Generates and publishes language-specific client/server stubs to AWS CodeArtifact and GitHub Packages.

- **TypeScript** package: `@aurigin/protos` (CodeArtifact) · `@<owner>/protos` (GitHub Packages)
- **Python** package: `aurigin-protos` (CodeArtifact) · wheel attached to GitHub Releases

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
│       └── aurigin/          # generated (gitignored)
├── examples/                 # consumer-side reference snippets
│   ├── python/               # uv-managed: `uv run server` / `uv run client`
│   └── typescript/           # npm-managed: `npm run server` / `npm run client`
├── buf.yaml                  # buf config + lint rules
├── buf.gen.yaml              # codegen targets
├── Makefile                  # lint / generate / build / publish
└── scripts/
    ├── publish-ts.sh
    └── publish-py.sh
```

## Prerequisites

For maintainers (publishing):

- `buf` — `brew install bufbuild/buf/buf`
- Node 20+ — used to run `ts-proto` and build the TS package
- Python 3.10+ with `build` + `twine` — `uv pip install build twine` (or `pip install build twine`)
- AWS CLI v2 with credentials for the CodeArtifact account

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

### 4. Bump version

Update both:
- `gen/ts/package.json` → `version`
- `gen/py/pyproject.toml` → `version`

(They should track each other so consumers can coordinate upgrades.)

### 5. Publish

There are two registries supported. Pick whichever your downstream services authenticate against — the same source tree drives both.

#### Option A — AWS CodeArtifact

Set the CodeArtifact env vars (replace placeholders with real values, or export them in your shell):

```bash
export AURIGIN_CA_DOMAIN=<domain-name>
export AURIGIN_CA_DOMAIN_OWNER=<aws-account-id>
export AURIGIN_CA_REPO=<repo-name>
export AWS_REGION=eu-west-1
export AWS_PROFILE=aurigin-shared        # whichever account hosts CodeArtifact
```

Then:

```bash
make publish-ts
make publish-py
# or both:
make publish
```

The publish scripts call `aws codeartifact login` to write the auth token into `~/.npmrc` / `~/.pypirc`, then `npm publish` / `twine upload`. The TS package is published as `@aurigin/protos`.

#### Option B — GitHub Packages + Releases (preferred for CI)

GitHub Packages hosts the npm registry; **GitHub does not have a Python registry**, so the Python wheel + sdist are attached to a GitHub Release instead and consumers install via the release URL.

The recommended path is the workflow at `.github/workflows/publish.yml`, which fires on `v*` tag push or via the Actions "Run workflow" button:

```bash
git tag v0.1.0
git push origin v0.1.0
```

That single tag publishes:
- **TS:** `@<repo-owner>/protos@0.1.0` to `https://npm.pkg.github.com` (the workflow rewrites the package name from `@aurigin/protos` to `@<owner>/protos` so it matches the GitHub Packages scope rule, without touching the source `package.json`).
- **Python:** `aurigin_protos-0.1.0-py3-none-any.whl` + `aurigin_protos-0.1.0.tar.gz` attached to the `v0.1.0` GitHub Release.

For local dry-runs, use:

```bash
export GITHUB_TOKEN=<PAT with write:packages>     # or `gh auth token`
export GITHUB_REPO=<owner>/<repo>                 # e.g. Aurigin-ai/aurigin-protos
export GITHUB_TAG=v0.1.0
make publish-github
```

## Consuming the packages

### TypeScript (downstream service)

**From AWS CodeArtifact** (`@aurigin/protos`):

```bash
aws codeartifact login --tool npm \
  --domain $AURIGIN_CA_DOMAIN \
  --domain-owner $AURIGIN_CA_DOMAIN_OWNER \
  --repository $AURIGIN_CA_REPO \
  --region $AWS_REGION

npm install @aurigin/protos
```

**From GitHub Packages** (`@<owner>/protos`):

Add to your project's `.npmrc`:

```
@<owner>:registry=https://npm.pkg.github.com
//npm.pkg.github.com/:_authToken=${GITHUB_TOKEN}
```

then:

```bash
npm install @<owner>/protos
```

In both cases:

```ts
import { DeepfakeDetectionClient } from "@<scope>/protos/aurigin/deepfake_detection/v1/deepfake_detection";
```

Full server + client snippets: [examples/typescript/](examples/typescript/).

### Python (downstream service, using `uv`)

**From AWS CodeArtifact** (`aurigin-protos`):

```bash
aws codeartifact login --tool pip \
  --domain $AURIGIN_CA_DOMAIN \
  --domain-owner $AURIGIN_CA_DOMAIN_OWNER \
  --repository $AURIGIN_CA_REPO \
  --region $AWS_REGION

uv venv
uv pip install aurigin-protos
```

For project-managed deps, declare the CodeArtifact index in `pyproject.toml` — see [examples/README.md](examples/README.md#option-b--project-managed-pyprojecttoml) for the full snippet.

**From a GitHub Release** (no PyPI registry on GitHub Packages):

```bash
uv pip install \
  "https://github.com/<owner>/<repo>/releases/download/v0.1.0/aurigin_protos-0.1.0-py3-none-any.whl"
```

Or in `pyproject.toml`:

```toml
[project]
dependencies = [
    "aurigin-protos @ https://github.com/<owner>/<repo>/releases/download/v0.1.0/aurigin_protos-0.1.0-py3-none-any.whl",
]
```

For private repos, set up `GH_TOKEN` and use the GitHub API URL pattern documented at [docs.github.com/en/rest/releases/assets](https://docs.github.com/en/rest/releases/assets), or fetch the asset with `gh release download` first.

```python
from aurigin.deepfake_detection.v1 import deepfake_detection_pb2, deepfake_detection_pb2_grpc
from twilio.tme.extensions.common.v1 import audio_buffer_pb2
```

Full server + client snippets: [examples/python/](examples/python/).

## Adding a new service

1. Create `proto/<package-path>/<service>.proto` (file path must mirror the proto `package`)
2. `make lint && make generate` — verify it builds
3. Bump versions in `gen/ts/package.json` and `gen/py/pyproject.toml`
4. `make publish`
