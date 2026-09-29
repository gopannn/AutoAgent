"""Component 6 — Instrument self-test: calibrate before you measure.

Two decision modes share one gate:
  null-referenced   decide by comparing the statistic to a matched null
                    (permutation / resampling) at level alpha           (B)
  threshold         decide by a fixed threshold on the statistic        (A)

Acceptance gate — interval bounds, not point estimates (A):
  Wilson-95 UPPER bound of the false-positive rate <= max_false_positive_rate
  Wilson-95 LOWER bound of the true-positive rate  >= min_true_positive_rate

Diagnostics that can fail the gate (B):
  null not anti-conservative (KS + mean p), noise degrades the measurement,
  true alternative ranked first, non-zero margin, exclusivity of attribution.
  NEW: discrimination is measured over many positive samples, not one.

Fingerprint gating (A): calibration records a SHA-256 of the instrument spec
and, where retrievable, the statistic's source code. `measure()` refuses to run
if calibration failed or the instrument changed.

Why the gate changed from B: B accepted a false-positive rate up to
alpha + 3*SE + 1/trials. With the defaults of its own example (alpha 0.01,
30 trials) that tolerance is 0.098 — ten times alpha — while zero false
positives in 30 trials only bounds the true rate below 0.114. Certifying
FPR <= 1% requires ~381 clean trials. The report now states the trials needed.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import math
import random
from dataclasses import dataclass, field
from typing import Any, Callable

Z95 = 1.959963984540054


def wilson(successes: int, total: int, z: float = Z95) -> tuple[float, float]:
    if total <= 0:
        return 0.0, 1.0
    p = successes / total
    den = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / den
    radius = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / den
    return max(0.0, centre - radius), min(1.0, centre + radius)


def trials_needed(max_rate: float, observed_rate: float = 0.0, cap: int = 1_000_000) -> int | None:
    """Smallest n whose Wilson upper bound, at the observed rate, is <= max_rate."""
    if max_rate <= observed_rate:
        return None
    lo, hi = 1, 1
    while wilson(round(observed_rate * hi), hi)[1] > max_rate:
        hi *= 2
        if hi > cap:
            return None
    while lo < hi:
        mid = (lo + hi) // 2
        if wilson(round(observed_rate * mid), mid)[1] <= max_rate:
            hi = mid
        else:
            lo = mid + 1
    return lo


def fingerprint(spec: dict, *functions: Callable | None) -> str:
    parts = [json.dumps(spec, sort_keys=True, separators=(",", ":"), default=str)]
    for fn in functions:
        if fn is None:
            continue
        try:
            parts.append(inspect.getsource(fn))
        except (OSError, TypeError):
            parts.append(getattr(fn, "__qualname__", repr(fn)))
    return hashlib.sha256("\n\x00".join(parts).encode("utf-8")).hexdigest()


@dataclass
class Decision:
    statistic: float
    threshold: float
    p_value: float | None
    detected: bool


@dataclass
class Calibration:
    instrument_sha256: str
    spec: dict
    mode: str
    alpha: float | None
    trials: int
    seed: int
    confusion_matrix: dict
    sensitivity: float
    sensitivity_wilson_95: tuple[float, float]
    false_positive_rate: float
    false_positive_rate_wilson_95: tuple[float, float]
    acceptance: dict
    trials_needed_for_fpr_claim: int | None
    p_uniformity_ks: float | None
    mean_negative_p: float | None
    noise_ladder: list[dict]
    discrimination: dict | None
    checks: dict[str, bool]
    limitations: list[str] = field(default_factory=list)
    sensitivity_curve: list[dict] = field(default_factory=list)

    @property
    def validated(self) -> bool:
        return all(self.checks.values())

    calibrated = validated

    def text(self) -> str:
        L = [f"Instrument calibration [{self.mode}]  sha256 {self.instrument_sha256[:12]}…",
             f"  trials per class     {self.trials}",
             f"  sensitivity          {self.sensitivity:.3f}  Wilson95 "
             f"[{self.sensitivity_wilson_95[0]:.3f}, {self.sensitivity_wilson_95[1]:.3f}]  "
             f"(need lower >= {self.acceptance['min_true_positive_rate']})",
             f"  false positive rate  {self.false_positive_rate:.3f}  Wilson95 "
             f"[{self.false_positive_rate_wilson_95[0]:.3f}, {self.false_positive_rate_wilson_95[1]:.3f}]  "
             f"(need upper <= {self.acceptance['max_false_positive_rate']}; "
             f"clean trials needed: {self.trials_needed_for_fpr_claim})"]
        if self.p_uniformity_ks is not None:
            L.append(f"  null p uniformity KS {self.p_uniformity_ks:.3f}  mean negative p {self.mean_negative_p:.3f}")
        if self.noise_ladder:
            L.append("  noise ladder:")
            for r in self.noise_ladder:
                L.append(f"    noise {r['noise']:<6} detection {r['detection_rate']:.2f}  "
                         f"median stat {r['median_statistic']:.4f}")
        if self.discrimination:
            d = self.discrimination
            L.append(f"  discrimination over {d['samples']} positives: true alt first in "
                     f"{d['true_first_fraction']:.0%}; median margin {d['median_margin']:.4f}; "
                     f"max alternatives beating null {d['max_alternatives_beating_null']}")
        L.append("  checks:")
        L += [f"    {'PASS' if v else 'FAIL'}  {k}" for k, v in self.checks.items()]
        L += [f"  LIMITATION: {x}" for x in self.limitations]
        L.append(f"  VALIDATED: {self.validated}")
        return "\n".join(L)

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["validated"] = self.validated
        d["calibrated"] = self.validated
        d["schema"] = "epistemic-toolkit/instrument-calibration/2.0.0"
        d["publication_rule"] = ("Publish this calibration, especially the false-positive bound, "
                                 "before applying the fingerprinted instrument to real data.")
        return d


class Instrument:
    """statistic(sample[, alt]) -> float; positive(rng, noise) -> sample;
    negative(rng) -> sample; null(sample, rng) -> sample (optional)."""

    def __init__(self, statistic: Callable[..., float], positive: Callable[[random.Random, float], Any],
                 negative: Callable[[random.Random], Any], null: Callable[[Any, random.Random], Any] | None = None,
                 *, spec: dict | None = None, smaller_is_stronger: bool = True, threshold: float | None = None,
                 alternatives: list | None = None, true_alternative: Any = None):
        if null is None and threshold is None:
            raise ValueError("give a matched null (null-referenced mode) or a threshold (threshold mode)")
        self.statistic, self.positive, self.negative, self.null = statistic, positive, negative, null
        self.smaller = smaller_is_stronger
        self.threshold = threshold
        self.alternatives, self.true_alt = alternatives, true_alternative
        self.spec = dict(spec or {})
        self.spec.setdefault("smaller_is_stronger", smaller_is_stronger)
        self.spec.setdefault("threshold", threshold)
        self.spec.setdefault("mode", "null" if null else "threshold")
        self.mode = self.spec["mode"]

    @property
    def sha256(self) -> str:
        return fingerprint(self.spec, self.statistic, self.null)

    def _stat(self, sample, alt=None):
        return self.statistic(sample, alt) if alt is not None else self.statistic(sample)

    def _stronger(self, a, b):
        return a < b if self.smaller else a > b

    def decide(self, sample, rng, *, null_runs: int = 200, alpha: float = 0.01, alt=None) -> Decision:
        obs = self._stat(sample, alt)
        if self.null is None:
            thr = self.threshold
            return Decision(obs, thr, None, obs <= thr if self.smaller else obs >= thr)
        if 1 / (1 + null_runs) > alpha:
            # A detector that cannot reach p <= alpha can never fire: every
            # downstream check (sensitivity, exclusivity, noise) would be vacuous.
            raise ValueError(f"null_runs={null_runs} cannot resolve p <= {alpha}; "
                             f"need >= {math.ceil(1 / alpha) - 1}")
        nulls = [self._stat(self.null(sample, rng), alt) for _ in range(null_runs)]
        at_least = sum(1 for v in nulls if (v <= obs if self.smaller else v >= obs))
        p = (1 + at_least) / (1 + null_runs)   # never exactly 0
        return Decision(obs, alpha, p, p <= alpha)

    def calibrate(self, *, alpha: float = 0.01, trials: int = 200, null_runs: int = 100,
                  noise_levels: tuple = (0.0, 0.05, 0.15, 0.3, 0.5), seed: int = 20260921,
                  max_false_positive_rate: float = 0.05, min_true_positive_rate: float = 0.8,
                  discrimination_samples: int = 10, sensitivity_curve: list | None = None) -> Calibration:
        if trials < 30:
            raise ValueError("at least 30 trials per class are required")
        rng = random.Random(seed)
        dec = lambda s, alt=self.true_alt, r=null_runs: self.decide(s, rng, null_runs=r, alpha=alpha, alt=alt)
        pos = [dec(self.positive(rng, 0.0)) for _ in range(trials)]
        neg = [dec(self.negative(rng)) for _ in range(trials)]
        tp = sum(d.detected for d in pos)
        fp = sum(d.detected for d in neg)
        sens_ci, fpr_ci = wilson(tp, trials), wilson(fp, trials)
        limitations, checks = [], {
            "true_positive_rate_lower_bound_meets_minimum": sens_ci[0] >= min_true_positive_rate,
            "false_positive_rate_upper_bound_within_limit": fpr_ci[1] <= max_false_positive_rate,
        }
        need = trials_needed(max_false_positive_rate, fp / trials)
        if not checks["false_positive_rate_upper_bound_within_limit"]:
            limitations.append(f"FPR upper bound {fpr_ci[1]:.3f} exceeds {max_false_positive_rate}; at the "
                               f"observed rate this needs {need} trials" if need else
                               f"observed FPR {fp / trials:.3f} is itself above the limit")

        ks = mean_p = None
        if self.null is not None:
            ps = sorted(d.p_value for d in neg)
            ks = max(max(abs((i + 1) / len(ps) - p), abs(i / len(ps) - p)) for i, p in enumerate(ps))
            mean_p = sum(ps) / len(ps)
            checks["null_not_anti_conservative"] = not (ks > 0.3 and mean_p < 0.4)
            if ks > 0.3 and mean_p >= 0.4:
                limitations.append(f"Null is CONSERVATIVE on negatives (KS {ks:.2f}, mean p {mean_p:.2f}): "
                                   f"false positives are controlled but p-values are not interpretable. "
                                   f"Report detections, not p-values.")

        ladder = []
        if noise_levels:
            for nz in noise_levels:
                ds = [dec(self.positive(rng, nz))
                      for _ in range(max(20, trials // 5))]
                st = sorted(d.statistic for d in ds)
                ladder.append({"noise": nz, "detection_rate": round(sum(d.detected for d in ds) / len(ds), 4),
                               "median_statistic": st[len(st) // 2]})
            meds = [r["median_statistic"] for r in ladder]
            dets = [r["detection_rate"] for r in ladder]
            stat_weaker = (meds[-1] > meds[0]) if self.smaller else (meds[-1] < meds[0])
            monotone = sum((b >= a) if self.smaller else (b <= a) for a, b in zip(meds, meds[1:])) >= len(meds) - 2
            checks["noise_degrades_measurement"] = ((stat_weaker and monotone) or dets[-1] < dets[0]) \
                and not dets[-1] > dets[0] + 0.15
            if len(set(dets)) == 1:
                limitations.append(f"Detection is flat at {dets[0]:.2f} across the noise ladder: the "
                                   f"breaking point was never reached. Extend noise_levels.")

        disc = None
        if self.alternatives:
            firsts, margins, beats = 0, [], []
            for _ in range(discrimination_samples):
                sample = self.positive(rng, 0.0)
                rows = []
                for alt in self.alternatives:
                    d = dec(sample, alt=alt)
                    rows.append((d.statistic, alt, d.detected))
                rows.sort(key=lambda r: r[0], reverse=not self.smaller)
                firsts += rows[0][1] == self.true_alt
                margins.append(abs(rows[1][0] - rows[0][0]) if len(rows) > 1 else 0.0)
                beats.append(sum(r[2] for r in rows))
            margins.sort()
            disc = {"samples": discrimination_samples, "true_alternative": self.true_alt,
                    "true_first_fraction": firsts / discrimination_samples,
                    "median_margin": margins[len(margins) // 2], "min_margin": margins[0],
                    "max_alternatives_beating_null": max(beats),
                    "mean_alternatives_beating_null": sum(beats) / len(beats)}
            checks["true_alternative_ranked_first"] = firsts == discrimination_samples
            checks["discriminates_between_alternatives"] = margins[0] > 1e-9
            checks["only_true_alternative_beats_null"] = max(beats) <= 1
            if max(beats) > 1:
                limitations.append(f"Up to {max(beats)} of {len(self.alternatives)} alternatives beat their "
                                   f"null on a KNOWN positive: single-alternative testing will misattribute.")

        curve = []
        for i, level in enumerate(sensitivity_curve or []):
            local = random.Random(seed + 10_000 + i)
            ds = [self.decide(self.positive(local, level), local, null_runs=null_runs, alpha=alpha, alt=self.true_alt)
                  for _ in range(trials)]
            curve.append({"level": level, "detection_rate": round(sum(d.detected for d in ds) / trials, 6)})

        return Calibration(self.sha256, self.spec, self.mode, alpha if self.null else None, trials, seed,
                           {"true_positive": tp, "false_negative": trials - tp, "false_positive": fp,
                            "true_negative": trials - fp},
                           round(tp / trials, 6), tuple(round(x, 6) for x in sens_ci),
                           round(fp / trials, 6), tuple(round(x, 6) for x in fpr_ci),
                           {"max_false_positive_rate": max_false_positive_rate,
                            "min_true_positive_rate": min_true_positive_rate},
                           need, None if ks is None else round(ks, 4),
                           None if mean_p is None else round(mean_p, 4), ladder, disc, checks, limitations, curve)

    def measure(self, sample, calibration: "Calibration | dict", *, seed: int = 0, null_runs: int = 1000,
                alpha: float | None = None) -> dict:
        cal = calibration.to_dict() if isinstance(calibration, Calibration) else calibration
        if not cal.get("validated", cal.get("calibrated")):
            raise ValueError("real-data measurement blocked: calibration gate did not pass")
        if cal.get("instrument_sha256") != self.sha256:
            raise ValueError("real-data measurement blocked: instrument differs from the calibrated version")
        d = self.decide(sample, random.Random(seed), null_runs=null_runs,
                        alpha=alpha if alpha is not None else (cal.get("alpha") or 0.01), alt=self.true_alt)
        return {"schema": "epistemic-toolkit/instrument-measurement/2.0.0",
                "instrument_sha256": self.sha256, "statistic": d.statistic, "p_value": d.p_value,
                "detected": d.detected,
                "false_positive_rate_wilson_95_from_calibration": cal["false_positive_rate_wilson_95"]}


# ------------------------------------------------ built-in: mean_shift (A)
def mean_shift_instrument(config: dict) -> Instrument:
    """Threshold-mode instrument defined entirely by JSON (toolkit-A config format)."""
    inst, syn = config["instrument"], config["synthetic_ground_truth"]
    if inst.get("kind") != "mean_shift":
        raise ValueError(f"unknown built-in instrument {inst.get('kind')!r}")
    base = float(inst.get("baseline_mean", 0.0))
    n, sigma = int(syn["sample_size"]), float(syn.get("sigma", 1.0))
    neg_mean, pos_mean = float(syn.get("negative_mean", 0.0)), float(syn["positive_mean"])
    higher = inst.get("orientation", "higher_is_positive") == "higher_is_positive"

    def statistic(sample):
        return sum(sample) / len(sample) - base

    def positive(rng, noise):
        # noise shrinks the planted effect towards the negative mean
        mean = pos_mean - noise * (pos_mean - neg_mean)
        return [rng.gauss(mean, sigma) for _ in range(n)]

    def negative(rng):
        return [rng.gauss(neg_mean, sigma) for _ in range(n)]

    spec = {"kind": "mean_shift", **inst}
    return Instrument(statistic, positive, negative, None, spec=spec, smaller_is_stronger=not higher,
                      threshold=float(inst["threshold"]))


def run_builtin_calibration(config: dict) -> dict:
    inst = mean_shift_instrument(config)
    acc = config["acceptance"]
    syn = config["synthetic_ground_truth"]
    pos_mean, neg_mean = float(syn["positive_mean"]), float(syn.get("negative_mean", 0.0))
    # sensitivity curve over absolute means, expressed as noise fractions
    levels = [(pos_mean - m) / (pos_mean - neg_mean) for m in syn.get("sensitivity_means", [])] \
        if pos_mean != neg_mean else []
    cal = inst.calibrate(trials=int(config.get("trials_per_class", 1000)), seed=int(config.get("seed", 20260921)),
                         max_false_positive_rate=float(acc["max_false_positive_rate"]),
                         min_true_positive_rate=float(acc["min_true_positive_rate"]),
                         noise_levels=(0.0, 0.25, 0.5, 0.75, 1.0), sensitivity_curve=levels)
    d = cal.to_dict()
    for row, m in zip(d["sensitivity_curve"], syn.get("sensitivity_means", [])):
        row["synthetic_mean"] = m
    d["calibration_id"] = config.get("calibration_id")
    d["synthetic_ground_truth"] = syn
    return d


def measure_real(sample: list[float], instrument_config: dict, calibration: dict) -> dict:
    """CLI helper: rebuild the built-in instrument from the calibration's own
    synthetic spec plus the (possibly changed) instrument block, then gate."""
    cfg = {"instrument": instrument_config, "synthetic_ground_truth": calibration["synthetic_ground_truth"]}
    inst = mean_shift_instrument(cfg)
    out = inst.measure(sample, calibration)
    out["calibration_id"] = calibration.get("calibration_id")
    return out
