# Examples

Reference snippets showing how to consume the generated packages.

## Python (with `uv`)

[`uv`](https://github.com/astral-sh/uv) is the recommended Python package manager for this repo's consumers — it's a drop-in pip replacement that's ~10–100× faster and handles project venvs automatically.

Install once: `brew install uv`.

### Option A — quick, ad-hoc

After running `aws codeartifact login --tool pip ...` (writes the index URL into `~/.config/pip/pip.conf`), `uv pip` reuses that config:

```bash
uv venv                                  # create .venv/
uv pip install aurigin-protos grpcio

uv run python examples/python/server.py   # in one terminal
uv run python examples/python/client.py   # in another
```

### Option B — project-managed (`pyproject.toml`)

The `examples/python/` directory ships a `pyproject.toml` (with placeholders for the CodeArtifact URL) so you can run it as a self-contained uv project. `[project.scripts]` defines `server` and `client` entry points, mirroring the TypeScript example's `npm run server` / `npm run client`:

```bash
cd examples/python

export CODEARTIFACT_AUTH_TOKEN=$(aws codeartifact get-authorization-token \
  --domain $AURIGIN_CA_DOMAIN \
  --domain-owner $AURIGIN_CA_DOMAIN_OWNER \
  --query authorizationToken --output text)

uv sync                                   # creates .venv/, installs deps
uv run server                             # in one terminal
uv run client                             # in another
```

For a downstream service, copy the same registry config into your service's `pyproject.toml`:

```toml
[project]
name = "my-service"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = [
    "aurigin-protos",
    "grpcio>=1.62",
]

[[tool.uv.index]]
name = "aurigin"
url = "https://aws:${CODEARTIFACT_AUTH_TOKEN}@<domain>-<owner>.d.codeartifact.<region>.amazonaws.com/pypi/<repo>/simple/"
explicit = true

[tool.uv.sources]
aurigin-protos = { index = "aurigin" }
```

CodeArtifact tokens expire after 12h, so re-export `CODEARTIFACT_AUTH_TOKEN` before each `uv sync` (the same `aws codeartifact get-authorization-token` command shown above).

Expected client output:

```
Session: demo-session-0001
Analysis | offset=0ms     | score=0.050 | label=bonafide
Analysis | offset=500ms   | score=0.050 | label=bonafide
... (one Analysis line per audio buffer)
FINAL    | total=3000ms   | score=0.050 | label=bonafide
```

Files:
- `python/server.py` — `DeepfakeDetectionServicer` impl with stub analysis, listens on `[::]:50051`
- `python/client.py` — opens the bidi stream, sends a `CreateSessionRequest` plus 6×500 ms silent audio buffers, prints every response

## TypeScript

The `examples/typescript/` directory has its own `package.json` so you can install and run directly:

```bash
cd examples/typescript

# After authenticating to CodeArtifact (one-time per machine):
aws codeartifact login --tool npm \
  --domain $AURIGIN_CA_DOMAIN \
  --domain-owner $AURIGIN_CA_DOMAIN_OWNER \
  --repository $AURIGIN_CA_REPO \
  --region $AWS_REGION

npm install
```

Then:

```bash
npm run server    # in one terminal
npm run client    # in another
```

Files:
- `typescript/server.ts` — `Server` from `@grpc/grpc-js` + `addService(DeepfakeDetectionService, impl)` with bidi stream handling
- `typescript/client.ts` — `DeepfakeDetectionClient.detectDeepfake()` duplex stream demo

### Notes on ts-proto naming

`ts-proto` flattens nested types with underscores and suffixes service exports:

| Proto | Generated TypeScript |
|---|---|
| `service DeepfakeDetection` | `DeepfakeDetectionService` (definition), `DeepfakeDetectionServer` (server interface), `DeepfakeDetectionClient` (client class) |
| `oneof response { ... }` | discriminated optional fields on the message (e.g. `response.analysisResult`) |

Deep imports use the proto path: `@aurigin/protos/aurigin/deepfake_detection/v1/deepfake_detection`.
