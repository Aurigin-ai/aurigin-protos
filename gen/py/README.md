# aurigin-protos

Generated gRPC Python stubs for Aurigin services. Built from [`aurigin-protos`](https://github.com/Aurigin-ai/aurigin-protos) using buf's `protocolbuffers/python` and `grpc/python` remote plugins (standard `protoc-gen-python` + `grpc_python_plugin`).

## Install (with `uv`)

```bash
uv pip install aurigin-protos
```

Ships with PEP 740 sigstore attestations — `pip 24.2+` verifies them automatically against the tagged commit on the repository's `publish-pypi.yml` workflow.

Aurigin services that need a pre-promotion (release-candidate) version can install from the internal AWS CodeArtifact mirror under the same name; see the [`infra/aws/`](https://github.com/Aurigin-ai/aurigin-protos/tree/main/infra/aws) runbook in the repo for the connection details.

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

This package is generated. To add or change a service, edit the `.proto` files in [aurigin-protos](https://github.com/Aurigin-ai/aurigin-protos), then cut a release via `gh workflow run release.yml -f version=<x.y.z>`. The orchestrator tags `main`, creates a GitHub Release, and dispatches `publish-codeartifact.yml` (internal) + `publish-pypi.yml` (public pypi.org, runs in the `public-release` env for the OIDC claim — no reviewer gate). For local dry-runs, `make publish-py-codeartifact`.
