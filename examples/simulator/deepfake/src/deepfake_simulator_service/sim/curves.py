"""Confidence-curve evaluators.

Each evaluator takes (curve_spec, rng, t_ms) and returns a fake_probability
in [0, 1]. Curve specs are dicts straight out of the YAML, already validated
by the JSON Schema, so we don't re-check shape here.
"""

from __future__ import annotations

import math
import random
from typing import Any


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def _linear(spec: dict[str, Any], _rng: random.Random, t_ms: int) -> float:
    p0, p1 = spec["from"], spec["to"]
    t0, t1 = p0["at_ms"], p1["at_ms"]
    if t_ms <= t0:
        return _clamp(p0["fake_probability"])
    if t_ms >= t1 or t1 == t0:
        return _clamp(p1["fake_probability"])
    frac = (t_ms - t0) / (t1 - t0)
    return _clamp(p0["fake_probability"] + frac * (p1["fake_probability"] - p0["fake_probability"]))


def _sigmoid(spec: dict[str, Any], _rng: random.Random, t_ms: int) -> float:
    p0, p1 = spec["from"], spec["to"]
    t0, t1 = p0["at_ms"], p1["at_ms"]
    steepness = spec.get("steepness", 8.0)
    span = max(t1 - t0, 1)
    # Centre the sigmoid at the midpoint of the interval.
    x = (t_ms - t0 - span / 2) / span
    s = 1.0 / (1.0 + math.exp(-steepness * x))
    return _clamp(p0["fake_probability"] + s * (p1["fake_probability"] - p0["fake_probability"]))


def _step(spec: dict[str, Any], _rng: random.Random, t_ms: int) -> float:
    # Piecewise constant: carry the most-recent point's value forward.
    current = 0.0
    for pt in spec["points"]:
        if pt["at_ms"] <= t_ms:
            current = pt["fake_probability"]
        else:
            break
    return _clamp(current)


def _noise(spec: dict[str, Any], rng: random.Random, _t_ms: int) -> float:
    base = spec["base"]
    amp = spec["amplitude"]
    return _clamp(base + rng.uniform(-amp, amp))


def _spike(spec: dict[str, Any], _rng: random.Random, t_ms: int) -> float:
    # Each spike contributes a Gaussian-ish bump centred at its at_ms.
    base = spec["base"]
    total = base
    for sp in spec["spikes"]:
        centre = sp["at_ms"]
        width = max(sp.get("width_ms", 500), 1)
        peak = sp["fake_probability"]
        # Normal-shaped bump, peak above base at centre, decays to ~0 within ±2*width.
        contribution = (peak - base) * math.exp(-0.5 * ((t_ms - centre) / width) ** 2)
        total += contribution
    return _clamp(total)


def _decay(spec: dict[str, Any], _rng: random.Random, t_ms: int) -> float:
    p0 = spec["from"]
    elapsed = max(t_ms - p0["at_ms"], 0)
    half_life = max(spec["half_life_ms"], 1)
    return _clamp(p0["fake_probability"] * (0.5 ** (elapsed / half_life)))


_EVALUATORS = {
    "linear": _linear,
    "sigmoid": _sigmoid,
    "step": _step,
    "noise": _noise,
    "spike": _spike,
    "decay": _decay,
}


def evaluate(curve_spec: dict[str, Any], rng: random.Random, t_ms: int) -> float:
    return _EVALUATORS[curve_spec["type"]](curve_spec, rng, t_ms)


def label_for(curve_spec: dict[str, Any], fake_probability: float) -> str:
    threshold = curve_spec.get("label_threshold", 0.5)
    if fake_probability >= threshold:
        return curve_spec.get("label_above", "spoofed")
    return curve_spec.get("label_below", "bonafide")
