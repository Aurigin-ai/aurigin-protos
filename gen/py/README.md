# aurigin-protos

Generated gRPC Python stubs for Aurigin services. Built from [`aurigin-protos`](https://github.com/Aurigin-ai/aurigin-protos) using buf's `protocolbuffers/python` and `grpc/python` remote plugins (standard `protoc-gen-python` + `grpc_python_plugin`).

## Install (with `uv`)

This package is published in two places. Pick whichever your team authenticates against.

### Option A — AWS CodeArtifact

Aurigin's CodeArtifact setup:

| | |
|---|---|
| Domain | `aurigin-ai-domain` |
| Domain owner | `717279723333` |
| Repository | `aurigin-shared` |
| Region | `eu-west-1` |

Configure as an extra index in your `pyproject.toml`:

```toml
[project]
dependencies = [
    "aurigin-protos",
    "grpcio>=1.62",
]

[[tool.uv.index]]
name = "aurigin"
url = "https://aws:${CODEARTIFACT_AUTH_TOKEN}@aurigin-ai-domain-717279723333.d.codeartifact.eu-west-1.amazonaws.com/pypi/aurigin-shared/simple/"
explicit = true

[tool.uv.sources]
aurigin-protos = { index = "aurigin" }
```

Refresh the auth token (CodeArtifact tokens expire after 12h) and sync:

```bash
export CODEARTIFACT_AUTH_TOKEN=$(aws codeartifact get-authorization-token \
  --domain aurigin-ai-domain \
  --domain-owner 717279723333 \
  --region eu-west-1 \
  --query authorizationToken --output text)

uv sync
```

For ad-hoc use, after `aws codeartifact login --tool pip --domain aurigin-ai-domain --domain-owner 717279723333 --repository aurigin-shared --region eu-west-1`:

```bash
uv pip install aurigin-protos
```

### Option B — GitHub Release asset

GitHub Packages doesn't host a Python registry, so the wheel + sdist are attached to a GitHub Release. Pick a tag from [the Releases page](https://github.com/Aurigin-ai/aurigin-protos/releases/latest) and substitute for `<x.y.z>`:

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

For private repos (or to grab the latest without hardcoding), use `gh release download` — omit the tag to default to the latest release:

```bash
gh release download -R Aurigin-ai/aurigin-protos -p '*.whl' \
  && uv pip install ./aurigin_protos-*.whl
```

## Usage

```python
import grpc
from aurigin.deepfake_detection.v1 import deepfake_detection_pb2 as pb
from aurigin.deepfake_detection.v1 import deepfake_detection_pb2_grpc as pb_grpc

with grpc.insecure_channel("localhost:50051") as channel:
    stub = pb_grpc.DeepfakeDetectionStub(channel)
    # DetectDeepfake is bidi-streaming — see examples/python/client.py for a runnable demo.
```

Server-side: subclass `pb_grpc.DeepfakeDetectionServicer` and register with `add_DeepfakeDetectionServicer_to_server`.

## Layout

Proto packages map 1:1 to Python import paths. Each `.proto` file produces two modules:

- `<package_path>.<file>_pb2` — message classes
- `<package_path>.<file>_pb2_grpc` — service stub + servicer base class

Currently published modules:

- `aurigin.deepfake_detection.v1.deepfake_detection_pb2[_grpc]`
- `twilio.tme.extensions.common.v1.audio_buffer_pb2` (vendored Twilio Media Extensions message — no service)

## Source

This package is generated. To add or change a service, edit the `.proto` files in [aurigin-protos](https://github.com/Aurigin-ai/aurigin-protos), tag a release (`git tag v<x.y.z> && git push --tags`); both publish workflows fire in parallel and ship to CodeArtifact + GitHub Release. For local dry-runs, `make publish-py-codeartifact` (CodeArtifact) or `make publish-py-github` (GitHub Release).
