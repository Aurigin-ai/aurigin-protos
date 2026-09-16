# syntax=docker/dockerfile:1.7

# Build context: `examples/` (one directory up from this Dockerfile). The
# larger context matches the deepfake simulator so both share the same
# build invocation shape:
#
#   docker build -f examples/simulator/fingerprint/Dockerfile -t aurigin/fingerprint-simulator-service examples/
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
COPY simulator/fingerprint/pyproject.toml simulator/fingerprint/README.md ./
COPY simulator/fingerprint/src ./src
# `uv sync --no-dev` grabs runtime deps (aurigin-protos, grpcio) from
# PyPI. No lockfile is committed for the simulator — thin reference tool,
# floating latest patch of each dep is fine.
RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked \
    uv sync --no-dev

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
ENTRYPOINT ["fingerprint-simulator-service"]
