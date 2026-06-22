"""Scenario-driven gRPC simulator for DeepfakeDetection.DetectDeepfake.

Loads YAML scenarios from a directory at startup, validates each against
the JSON Schema in `examples/scenarios/scenario.schema.json`, and serves
them per session.

Client selects a scenario via the `x-scenario-id` request metadata header.
If the header is missing or names an unknown scenario, the server falls
back to the scenario whose id matches `SCENARIO_DEFAULT` (default `default`).

Env vars:
    PORT             gRPC listen port              (default 50051)
    SCENARIOS_DIR    directory of *.yaml scenarios (default <repo>/examples/scenarios)
    SCENARIO_DEFAULT id of the fallback scenario   (default "default")
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

import grpc

from aurigin.deepfake_detection.v1 import deepfake_detection_pb2_grpc as pb_grpc

from sim import Scenario, load_scenarios, run_session


DEFAULT_SCENARIOS_DIR = Path(__file__).resolve().parent.parent / "scenarios"
DEFAULT_TLS_DIR = Path(__file__).resolve().parent.parent / "certs"


def _load_tls() -> tuple[bytes, bytes] | None:
    """Return (key_bytes, cert_bytes) if a usable keypair is present, else None.

    Auto-detect: defaults to examples/certs/server.{crt,key} (committed to the
    repo so the example is TLS-by-default). Override via TLS_CERT / TLS_KEY
    env vars; point them at non-existent paths to force insecure mode.
    """
    cert_path = Path(os.environ.get("TLS_CERT", DEFAULT_TLS_DIR / "server.crt"))
    key_path = Path(os.environ.get("TLS_KEY", DEFAULT_TLS_DIR / "server.key"))
    if cert_path.is_file() and key_path.is_file():
        return key_path.read_bytes(), cert_path.read_bytes()
    return None


def _mtls_client_ca() -> bytes | None:
    """Return the PEM bytes to verify client certs against when MTLS=1.

    Defaults to examples/certs/client.crt — the committed self-signed client
    cert acts as its own CA, which is the textbook 'two self-signed certs
    that mutually trust each other' example setup. Override via TLS_CLIENT_CA.
    Returns None if MTLS isn't requested or the file doesn't exist.
    """
    if os.environ.get("MTLS", "").lower() not in ("1", "true", "yes"):
        return None
    ca_path = Path(os.environ.get("TLS_CLIENT_CA", DEFAULT_TLS_DIR / "client.crt"))
    if ca_path.is_file():
        return ca_path.read_bytes()
    return None


def _pick_scenario(
    metadata: tuple[tuple[str, str], ...],
    scenarios: dict[str, Scenario],
    default_id: str,
) -> tuple[Scenario, str | None]:
    """Return (chosen_scenario, requested_id_if_present)."""
    requested = next((v for k, v in metadata if k.lower() == "x-scenario-id"), None)
    if requested and requested in scenarios:
        return scenarios[requested], requested
    return scenarios[default_id], requested


class DeepfakeDetectionImpl(pb_grpc.DeepfakeDetectionServicer):
    def __init__(self, scenarios: dict[str, Scenario], default_id: str) -> None:
        self._scenarios = scenarios
        self._default_id = default_id

    async def DetectDeepfake(self, request_iterator, context):  # noqa: N802 - gRPC RPC name
        scenario, requested = _pick_scenario(
            context.invocation_metadata(), self._scenarios, self._default_id,
        )
        # Log the inbound call before the runner starts so the operator sees
        # *which* scenario the client asked for (and whether we honored it or
        # fell back to the default). The runner's `start` line lands ~1 RTT
        # later with the generated session id once CreateSessionResponse is
        # written.
        peer = context.peer() if context else "?"
        if requested is None:
            note = f"requested=none → default={scenario.id}"
        elif requested == scenario.id:
            note = f"scenario={scenario.id}"
        else:
            note = f"requested={requested!r} unknown → fallback={scenario.id}"
        print(f"[incoming] peer={peer} | {note}", file=sys.stderr, flush=True)
        async for response in run_session(scenario, request_iterator, context):
            yield response


async def _serve_async(port: int, scenarios_dir: Path, default_id: str) -> None:
    scenarios = load_scenarios(scenarios_dir)
    if default_id not in scenarios:
        raise SystemExit(
            f"Default scenario id '{default_id}' not found in {scenarios_dir}. "
            f"Available: {sorted(scenarios)}"
        )
    server = grpc.aio.server()
    pb_grpc.add_DeepfakeDetectionServicer_to_server(
        DeepfakeDetectionImpl(scenarios, default_id), server
    )

    tls = _load_tls()
    if tls is not None:
        key_bytes, cert_bytes = tls
        client_ca = _mtls_client_ca()
        if client_ca is not None:
            # require_client_auth=True flips the handshake to demand a client
            # cert chaining to root_certificates. With no chain → handshake
            # fails before the RPC even starts (UNAVAILABLE on the client).
            creds = grpc.ssl_server_credentials(
                [(key_bytes, cert_bytes)],
                root_certificates=client_ca,
                require_client_auth=True,
            )
            tls_status = "mTLS (self-signed, examples/certs/)"
        else:
            creds = grpc.ssl_server_credentials([(key_bytes, cert_bytes)])
            tls_status = "TLS (self-signed, examples/certs/)"
            if os.environ.get("MTLS", "").lower() in ("1", "true", "yes"):
                tls_status += " — MTLS=1 but examples/certs/client.crt missing, falling back to plain TLS"
        server.add_secure_port(f"[::]:{port}", creds)
    else:
        server.add_insecure_port(f"[::]:{port}")
        tls_status = "insecure (no examples/certs/server.crt found)"
    await server.start()
    # Match runner.py's _log: write to stderr + explicit flush so process
    # supervisors (uv, hatch entry-point shim, just) don't swallow startup
    # output before the loop returns to handle requests.
    print(
        f"DeepfakeDetection simulator listening on :{port} | "
        f"{len(scenarios)} scenarios loaded from {scenarios_dir} | "
        f"default='{default_id}' | transport={tls_status}",
        file=sys.stderr, flush=True,
    )

    # Install SIGINT/SIGTERM handlers that flip a shutdown event. This lets
    # Ctrl-C unwind through the loop cleanly: we wait for either the server
    # to terminate on its own (it won't, normally) or for a signal, then call
    # server.stop() with a small grace window for in-flight RPCs to finish.
    # Without this, asyncio.run() cancels every coroutine mid-flight and the
    # gRPC handler tears down with a noisy AbortError + Event-loop-is-closed
    # warning during __del__.
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def _request_shutdown(signame: str) -> None:
        if not shutdown_event.is_set():
            print(f"\nReceived {signame}, shutting down...", file=sys.stderr, flush=True)
            shutdown_event.set()

    for sig, name in ((signal.SIGINT, "SIGINT"), (signal.SIGTERM, "SIGTERM")):
        loop.add_signal_handler(sig, _request_shutdown, name)

    await shutdown_event.wait()
    # 2 s drain window — long enough for the typical per-session timeline
    # to push its FinalResult, short enough that Ctrl-C feels snappy.
    await server.stop(grace=2.0)
    print("DeepfakeDetection simulator stopped.", file=sys.stderr, flush=True)


def serve(port: int = 50051) -> None:
    scenarios_dir = Path(os.environ.get("SCENARIOS_DIR", str(DEFAULT_SCENARIOS_DIR)))
    default_id = os.environ.get("SCENARIO_DEFAULT", "default")
    # We deliberately do NOT use asyncio.run() here. Its Runner installs its
    # own SIGINT handler at the Python signal.signal() level, which races our
    # loop.add_signal_handler in _serve_async — the result is a CancelledError
    # injected into server.stop() mid-shutdown and the noisy traceback that
    # this whole rework is meant to eliminate. Driving the loop manually keeps
    # add_signal_handler as the only SIGINT path.
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_serve_async(port, scenarios_dir, default_id))
    finally:
        loop.close()


if __name__ == "__main__":
    serve(int(os.environ.get("PORT", "50051")))
