# syntax=docker/dockerfile:1.7

# Build context: `examples/` (one directory up from this Dockerfile). The
# larger context is what lets the runtime stage bake in `examples/scenarios/`
# without copying-into-place gymnastics.
#
#   docker build -f examples/simulator/deepfake/Dockerfile -t aurigin/deepfake-simulator-service examples/
#
# docker-compose.yml in this directory sets the context automatically.

ARG PYTHON_VERSION=3.12
ARG UV_VERSION=0.5.11

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM python:${PYTHON_VERSION}-slim-bookworm AS builder
COPY --from=uv /uv /uvx /usr/local/bin/
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app
COPY simulator/deepfake/pyproject.toml simulator/deepfake/README.md ./
COPY simulator/deepfake/src ./src
# `uv sync --no-dev` grabs runtime deps (aurigin-protos, grpcio, pyyaml,
# jsonschema) from PyPI. No lockfile is committed for the simulator — it's a
# thin reference tool, and floating latest patch of each dep is fine.
#
# --no-editable forces the package to be COPIED into /opt/venv/lib/
# pythonX.Y/site-packages/ rather than installed as an editable pointer
# to /app/src (which only exists in the builder stage — the runtime
# stage copies /opt/venv but not /app/src, so an editable install
# dangles into a ModuleNotFoundError at runtime).
RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked \
    uv sync --no-dev --no-editable

FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=50051 \
    SCENARIOS_DIR=/scenarios

RUN useradd --create-home --uid 10001 app

COPY --from=builder /opt/venv /opt/venv

# Scenarios bake into the image at /scenarios so the container is
# self-sufficient out of the box. Bind-mount a host directory over
# /scenarios (or set SCENARIOS_DIR to another path) to override with
# custom scenarios at runtime.
COPY --chown=app:app scenarios /scenarios

# sim/loader.py:SCHEMA_PATH is computed as `Path(__file__).resolve().parents[5]
# / "scenarios" / "scenario.schema.json"` — a walk that assumes the DEV layout
# (examples/simulator/deepfake/src/deepfake_simulator_service/sim/loader.py).
# After `uv sync --no-editable` the file lives under /opt/venv/lib/pythonX.Y/
# site-packages/… so parents[5] resolves to /opt/venv, and SCHEMA_PATH ends
# up at /opt/venv/scenarios/scenario.schema.json.
#
# Symlink /opt/venv/scenarios → /scenarios so that hardcoded path resolves
# to the baked-in schema. Follow-up: fix SCHEMA_PATH to be env-driven
# (relative to SCENARIOS_DIR) and drop this workaround.
RUN ln -s /scenarios /opt/venv/scenarios

USER app
WORKDIR /app
EXPOSE 50051
ENTRYPOINT ["deepfake-simulator-service"]
