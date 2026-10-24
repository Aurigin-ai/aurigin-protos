#!/usr/bin/env bash
# Regenerate HTML gRPC docs from the proto tree.
#
# Writes `docs/grpc/index.html` — one page covering every service +
# message + enum under `proto/`. Committed to the repo so consumers
# (notably the platform-backend-services local api-docs portal) can
# mount it without having to re-run protoc themselves.
#
# Runs the upstream `pseudomuto/protoc-gen-doc` toolchain in a
# container so there's no local protoc dependency. The image carries
# protoc + the plugin pre-installed; we just mount the proto tree +
# an output dir.
#
# Usage (from repo root):
#     ./scripts/generate-docs.sh
#
# CI (follow-up): run this script, then `git diff --exit-code docs/grpc/`
# to flag any proto change that forgot to refresh the committed HTML.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROTO_DIR="${REPO_ROOT}/proto"
OUT_DIR="${REPO_ROOT}/docs/grpc"

mkdir -p "${OUT_DIR}"

# Pin the plugin image — upstream has a `latest` tag but we want
# reproducible local output (and the CI drift check to be stable
# across runs). Bump by hand when a new release lands.
IMAGE="pseudomuto/protoc-gen-doc:1.5.1"

# Collect every .proto file as a relative path (protoc-gen-doc needs
# them as separate args, not a glob). `find -print0 | xargs -0` keeps
# whitespace-safe even though our paths don't currently have any.
mapfile -t PROTO_FILES < <(cd "${PROTO_DIR}" && find . -name "*.proto" -type f | sort)

if [[ ${#PROTO_FILES[@]} -eq 0 ]]; then
  echo "ERROR: no .proto files found under ${PROTO_DIR}" >&2
  exit 1
fi

echo "Regenerating ${OUT_DIR}/index.html from ${#PROTO_FILES[@]} proto files…"

docker run --rm \
  -v "${PROTO_DIR}:/protos:ro" \
  -v "${OUT_DIR}:/out" \
  "${IMAGE}" \
  --doc_opt=html,index.html \
  "${PROTO_FILES[@]}"

echo "Wrote ${OUT_DIR}/index.html ($(wc -c < "${OUT_DIR}/index.html" | tr -d ' ') bytes)."
