"""YAML scenario loader with JSON-Schema validation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

SCHEMA_PATH = Path(__file__).resolve().parent.parent.parent / "scenarios" / "scenario.schema.json"


@dataclass(frozen=True)
class StreamConfig:
    duration_ms: int
    chunk_interval_ms: int = 100
    sample_rate_hz: int = 16000
    channels: int = 1
    codec: str = "pcm_s16le"


@dataclass(frozen=True)
class NetworkConfig:
    base_latency_ms: int = 0
    jitter_ms: int = 0
    drop_event_probability: float = 0.0
    duplicate_event_probability: float = 0.0
    out_of_order_probability: float = 0.0


@dataclass(frozen=True)
class GrpcConfig:
    initial_metadata: dict[str, str] = field(default_factory=dict)
    trailing_metadata: dict[str, str] = field(default_factory=dict)
    terminate_at_ms: int | None = None
    status_code: str | None = None
    status_message: str | None = None


@dataclass(frozen=True)
class ScenarioEvent:
    at_ms: int
    type: str
    name: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BackendSimulation:
    """Declarative deepfake-service mirror — mirrors the dfs config that
    drives analysis emission timing, instead of hand-coding emissions in the
    scenario. When this block is set on a Scenario, the simulator computes
    the timeline from (stream.duration_ms, analysis_interval_ms,
    min_chunk_duration_ms, tail_strategy)."""
    analysis_interval_ms: int
    min_chunk_duration_ms: int = 1000
    tail_strategy: str = "drop"  # "drop" | "extend" | "recompute"
    silent_windows: tuple[int, ...] = ()
    silence_confidence: float = 0.95


@dataclass(frozen=True)
class Scenario:
    id: str
    description: str
    stream: StreamConfig
    network: NetworkConfig
    grpc: GrpcConfig
    random_seed: int | None
    confidence_curve: dict[str, Any] | None
    events: tuple[ScenarioEvent, ...]
    backend_simulation: BackendSimulation | None = None


def _load_schema() -> dict[str, Any]:
    with SCHEMA_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


_VALIDATOR = Draft202012Validator(_load_schema())


def _validate(doc: dict[str, Any], source: Path) -> None:
    errors = sorted(_VALIDATOR.iter_errors(doc), key=lambda e: e.path)
    if not errors:
        return
    lines = [f"Scenario validation failed for {source}:"]
    for err in errors:
        loc = "/".join(str(p) for p in err.absolute_path) or "<root>"
        lines.append(f"  - {loc}: {err.message}")
    raise ValueError("\n".join(lines))


def _scenario_from_doc(doc: dict[str, Any], source: Path) -> Scenario:
    sc = doc["scenario"]
    st = doc["stream"]
    net = doc.get("network", {}) or {}
    grpc_cfg = doc.get("grpc", {}) or {}
    rnd = doc.get("random", {}) or {}
    events = tuple(
        ScenarioEvent(
            at_ms=e["at_ms"],
            type=e["type"],
            name=e.get("name"),
            payload=dict(e.get("payload") or {}),
        )
        for e in doc.get("events", []) or []
    )
    backend_sim_doc = doc.get("backend_simulation")
    backend_simulation = None
    if backend_sim_doc:
        backend_simulation = BackendSimulation(
            analysis_interval_ms=int(backend_sim_doc["analysis_interval_ms"]),
            min_chunk_duration_ms=int(backend_sim_doc.get("min_chunk_duration_ms", 1000)),
            tail_strategy=str(backend_sim_doc.get("tail_strategy", "drop")),
            silent_windows=tuple(int(w) for w in backend_sim_doc.get("silent_windows", []) or []),
            silence_confidence=float(backend_sim_doc.get("silence_confidence", 0.95)),
        )
    return Scenario(
        id=sc["id"],
        description=sc.get("description", ""),
        stream=StreamConfig(
            duration_ms=st["duration_ms"],
            chunk_interval_ms=st.get("chunk_interval_ms", 100),
            sample_rate_hz=st.get("sample_rate_hz", 16000),
            channels=st.get("channels", 1),
            codec=st.get("codec", "pcm_s16le"),
        ),
        network=NetworkConfig(
            base_latency_ms=net.get("base_latency_ms", 0),
            jitter_ms=net.get("jitter_ms", 0),
            drop_event_probability=net.get("drop_event_probability", 0.0),
            duplicate_event_probability=net.get("duplicate_event_probability", 0.0),
            out_of_order_probability=net.get("out_of_order_probability", 0.0),
        ),
        grpc=GrpcConfig(
            initial_metadata=dict(grpc_cfg.get("initial_metadata") or {}),
            trailing_metadata=dict(grpc_cfg.get("trailing_metadata") or {}),
            terminate_at_ms=grpc_cfg.get("terminate_at_ms"),
            status_code=grpc_cfg.get("status_code"),
            status_message=grpc_cfg.get("status_message"),
        ),
        random_seed=rnd.get("seed"),
        confidence_curve=doc.get("confidence_curve"),
        events=events,
        backend_simulation=backend_simulation,
    )


def load_scenarios(scenarios_dir: Path | str) -> dict[str, Scenario]:
    """Walk a directory tree of *.yaml scenario files. Return id -> Scenario."""
    scenarios_dir = Path(scenarios_dir)
    if not scenarios_dir.is_dir():
        raise FileNotFoundError(f"Scenarios directory not found: {scenarios_dir}")

    by_id: dict[str, Scenario] = {}
    seen_source: dict[str, Path] = {}
    for yaml_path in sorted(scenarios_dir.rglob("*.yaml")):
        with yaml_path.open("r", encoding="utf-8") as f:
            doc = yaml.safe_load(f)
        if doc is None:
            continue
        _validate(doc, yaml_path)
        scenario = _scenario_from_doc(doc, yaml_path)
        if scenario.id in seen_source:
            raise ValueError(
                f"Duplicate scenario id '{scenario.id}' in {yaml_path} (already defined in {seen_source[scenario.id]})"
            )
        seen_source[scenario.id] = yaml_path
        by_id[scenario.id] = scenario
    return by_id
