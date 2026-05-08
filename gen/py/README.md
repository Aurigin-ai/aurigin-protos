# aurigin-protos

Generated gRPC Python stubs for Aurigin services. Built from [`aurigin-protos`](https://github.com/Aurigin-ai/aurigin-protos) using buf's `protocolbuffers/python` and `grpc/python` remote plugins (standard `protoc-gen-python` + `grpc_python_plugin`).

## Install (with `uv`)

This package is published in two places. Pick whichever your team authenticates against.

### Option A — AWS CodeArtifact

Configure CodeArtifact as an extra index in your `pyproject.toml`:

```toml
[project]
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

Refresh the auth token (CodeArtifact tokens expire after 12h) and sync:

```bash
export CODEARTIFACT_AUTH_TOKEN=$(aws codeartifact get-authorization-token \
  --domain $AURIGIN_CA_DOMAIN \
  --domain-owner $AURIGIN_CA_DOMAIN_OWNER \
  --query authorizationToken --output text)

uv sync
```

For ad-hoc use, after `aws codeartifact login --tool pip ...`:

```bash
uv pip install aurigin-protos
```

### Option B — GitHub Release asset

GitHub Packages doesn't host a Python registry, so the wheel + sdist are attached to a GitHub Release. Install directly via PEP 508 direct-URL:

```bash
uv pip install \
  "https://github.com/Aurigin-ai/aurigin-protos/releases/download/v0.1.0/aurigin_protos-0.1.0-py3-none-any.whl"
```

Or in `pyproject.toml`:

```toml
[project]
dependencies = [
    "aurigin-protos @ https://github.com/Aurigin-ai/aurigin-protos/releases/download/v0.1.0/aurigin_protos-0.1.0-py3-none-any.whl",
]
```

For private repos, fetch the asset first with `gh release download v0.1.0 -R Aurigin-ai/aurigin-protos -p '*.whl'` and `uv pip install ./aurigin_protos-*.whl`.

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

This package is generated. To add or change a service, edit the `.proto` files in [aurigin-protos](https://github.com/Aurigin-ai/aurigin-protos), then bump the version and republish via `make publish-py` (CodeArtifact) or `make publish-py-github` (GitHub Release).
