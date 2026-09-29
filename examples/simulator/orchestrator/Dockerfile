# syntax=docker/dockerfile:1.7

# Minimal image for the AudioVerification simulator. Build context is
# `examples/simulator/orchestrator/` (this directory).
#
#   docker build -t aurigin/orchestrator-simulator-service examples/simulator/orchestrator/
#
# docker-compose.yml in this directory wires it up automatically.

ARG PYTHON_VERSION=3.12
ARG UV_VERSION=0.5.11

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM python:${PYTHON_VERSION}-slim-bookworm AS builder
COPY --from=uv /uv /uvx /usr/local/bin/
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
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
    PORT=50053

RUN useradd --create-home --uid 10001 app

COPY --from=builder /opt/venv /opt/venv

USER app
WORKDIR /app
EXPOSE 50053
ENTRYPOINT ["orchestrator-simulator-service"]
