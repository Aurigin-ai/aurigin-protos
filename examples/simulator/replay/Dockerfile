# syntax=docker/dockerfile:1.7

# Build context: `examples/` (one directory up from this Dockerfile). The
# larger context matches the fingerprint + deepfake simulators so all three
# share the same build invocation shape:
#
#   docker build -f examples/simulator/replay/Dockerfile -t aurigin/replay-simulator-service examples/
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
COPY simulator/replay/pyproject.toml simulator/replay/README.md ./
COPY simulator/replay/src ./src
# `uv sync --no-dev` grabs runtime deps (aurigin-protos, grpcio) from
# PyPI. No lockfile is committed for the simulator — thin reference tool,
# floating latest patch of each dep is fine.
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
    PORT=50051

RUN useradd --create-home --uid 10001 app

COPY --from=builder /opt/venv /opt/venv

USER app
WORKDIR /app
EXPOSE 50051
ENTRYPOINT ["replay-simulator-service"]
