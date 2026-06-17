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
from pathlib import Path

import grpc

from aurigin.deepfake_detection.v1 import deepfake_detection_pb2_grpc as pb_grpc

from sim import Scenario, load_scenarios, run_session


DEFAULT_SCENARIOS_DIR = Path(__file__).resolve().parent.parent / "scenarios"


def _pick_scenario(
    metadata: tuple[tuple[str, str], ...],
    scenarios: dict[str, Scenario],
    default_id: str,
) -> Scenario:
    requested = next((v for k, v in metadata if k.lower() == "x-scenario-id"), None)
    if requested and requested in scenarios:
        return scenarios[requested]
    return scenarios[default_id]


class DeepfakeDetectionImpl(pb_grpc.DeepfakeDetectionServicer):
    def __init__(self, scenarios: dict[str, Scenario], default_id: str) -> None:
        self._scenarios = scenarios
        self._default_id = default_id

    async def DetectDeepfake(self, request_iterator, context):  # noqa: N802 - gRPC RPC name
        scenario = _pick_scenario(context.invocation_metadata(), self._scenarios, self._default_id)
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
    server.add_insecure_port(f"[::]:{port}")
    await server.start()
    print(
        f"DeepfakeDetection simulator listening on :{port} | "
        f"{len(scenarios)} scenarios loaded from {scenarios_dir} | "
        f"default='{default_id}'"
    )
    await server.wait_for_termination()


def serve(port: int = 50051) -> None:
    scenarios_dir = Path(os.environ.get("SCENARIOS_DIR", str(DEFAULT_SCENARIOS_DIR)))
    default_id = os.environ.get("SCENARIO_DEFAULT", "default")
    asyncio.run(_serve_async(port, scenarios_dir, default_id))


if __name__ == "__main__":
    serve(int(os.environ.get("PORT", "50051")))
