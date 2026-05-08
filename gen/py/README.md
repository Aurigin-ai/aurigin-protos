# aurigin-protos

Generated gRPC Python stubs for Aurigin services. Built from [`aurigin-protos`](https://github.com/aurigin/aurigin-protos) using [`grpcio-tools`](https://pypi.org/project/grpcio-tools/) (standard `protoc-gen-python` + `grpc_python_plugin`).

## Install (with `uv`)

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

This package is generated. To add or change a service, edit the `.proto` files in [aurigin-protos](https://github.com/aurigin/aurigin-protos), then bump the version and republish via `make publish-py`.
