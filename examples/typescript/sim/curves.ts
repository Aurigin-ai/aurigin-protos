// Confidence-curve evaluators.
//
// Each evaluator takes (curve_spec, rng, t_ms) and returns a fake_probability
// in [0, 1]. Curve specs are objects straight out of the YAML, already
// validated by the JSON Schema, so we don't re-check shape here.
//
// Mirrors examples/python/sim/curves.py.

export type Rng = () => number;

export type CurveSpec = Record<string, any>;

function clamp(x: number): number {
  return Math.max(0, Math.min(1, x));
}

function linear(spec: CurveSpec, _rng: Rng, tMs: number): number {
  const p0 = spec.from;
  const p1 = spec.to;
  const t0 = p0.at_ms as number;
  const t1 = p1.at_ms as number;
  if (tMs <= t0) return clamp(p0.fake_probability);
  if (tMs >= t1 || t1 === t0) return clamp(p1.fake_probability);
  const frac = (tMs - t0) / (t1 - t0);
  return clamp(p0.fake_probability + frac * (p1.fake_probability - p0.fake_probability));
}

function sigmoid(spec: CurveSpec, _rng: Rng, tMs: number): number {
  const p0 = spec.from;
  const p1 = spec.to;
  const t0 = p0.at_ms as number;
  const t1 = p1.at_ms as number;
  const steepness = (spec.steepness ?? 8.0) as number;
  const span = Math.max(t1 - t0, 1);
  // Centre the sigmoid at the midpoint of the interval.
  const x = (tMs - t0 - span / 2) / span;
  const s = 1.0 / (1.0 + Math.exp(-steepness * x));
  return clamp(p0.fake_probability + s * (p1.fake_probability - p0.fake_probability));
}

function step(spec: CurveSpec, _rng: Rng, tMs: number): number {
  // Piecewise constant: carry the most-recent point's value forward.
  let current = 0.0;
  for (const pt of spec.points as Array<{ at_ms: number; fake_probability: number }>) {
    if (pt.at_ms <= tMs) {
      current = pt.fake_probability;
    } else {
      break;
    }
  }
  return clamp(current);
}

function noise(spec: CurveSpec, rng: Rng, _tMs: number): number {
  const base = spec.base as number;
  const amp = spec.amplitude as number;
  // rng() ∈ [0, 1) → map to [-amp, +amp].
  return clamp(base + (rng() * 2 - 1) * amp);
}

function spike(spec: CurveSpec, _rng: Rng, tMs: number): number {
  // Each spike contributes a Gaussian-ish bump centred at its at_ms.
  const base = spec.base as number;
  let total = base;
  for (const sp of spec.spikes as Array<{ at_ms: number; fake_probability: number; width_ms?: number }>) {
    const centre = sp.at_ms;
    const width = Math.max(sp.width_ms ?? 500, 1);
    const peak = sp.fake_probability;
    // Normal-shaped bump, peak above base at centre, decays to ~0 within ±2*width.
    const contribution = (peak - base) * Math.exp(-0.5 * ((tMs - centre) / width) ** 2);
    total += contribution;
  }
  return clamp(total);
}

function decay(spec: CurveSpec, _rng: Rng, tMs: number): number {
  const p0 = spec.from;
  const elapsed = Math.max(tMs - p0.at_ms, 0);
  const halfLife = Math.max(spec.half_life_ms as number, 1);
  return clamp(p0.fake_probability * Math.pow(0.5, elapsed / halfLife));
}

const EVALUATORS: Record<string, (spec: CurveSpec, rng: Rng, tMs: number) => number> = {
  linear,
  sigmoid,
  step,
  noise,
  spike,
  decay,
};

export function evaluate(curveSpec: CurveSpec, rng: Rng, tMs: number): number {
  const fn = EVALUATORS[curveSpec.type as string];
  if (!fn) throw new Error(`Unknown curve type: ${curveSpec.type}`);
  return fn(curveSpec, rng, tMs);
}

export function labelFor(curveSpec: CurveSpec, fakeProbability: number): string {
  const threshold = (curveSpec.label_threshold ?? 0.5) as number;
  if (fakeProbability >= threshold) {
    return (curveSpec.label_above ?? "spoofed") as string;
  }
  return (curveSpec.label_below ?? "bonafide") as string;
}
