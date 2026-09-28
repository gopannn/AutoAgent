"""Component 2 — Robustness gate.

A score from hand-set weights is a hand-set number with extra steps unless its
conclusion survives its own inputs. A hypothesis is ROBUST only if its side of
the decision threshold survives every test:

  1. leave_one_out       dropping any single admissible item neither flips nor ties
  2. perturbation        >= same_side of lognormal weight-noise runs hold the side
  3. uniform_ablation    all admissible weights = 1.0; side holds and is not a tie
  4. adversarial_prior   prior moved to the opposite extreme; side holds
  5. group_out  (NEW)    dropping every item of one independence group (e.g. one
                         source cited three times) neither flips nor ties.
                         Applied only when some group has >= 2 bearing items.

Improvements over both parents:
  * sides are decided on UNCLAMPED log-odds (both parents decided on the clamped
    probability, so perturbation medians pinned at 0.99 hid information)
  * explicit tie state (A had it; B silently counted a tie as "not supported")
  * per-hypothesis stable seeds (A) with interpolated quantiles (A)
  * minimum_flip_weight (B) reported on every result
"""
from __future__ import annotations

import hashlib
import math
import random
from dataclasses import asdict, dataclass, field
from typing import Any

from .ledger import Ledger, load_ledger, logit, sigmoid
from .outcomes import COMPUTED, UNRESOLVED, public_result

EPS = 1e-12


@dataclass
class GateConfig:
    runs: int = 5000
    sigma: float = 0.5
    same_side: float = 0.95
    adversarial_low: float = 0.1
    adversarial_high: float = 0.9
    uniform_weight: float = 1.0
    seed: int = 20260921
    threshold: float = 0.5
    group_out: bool = True

    _ALIASES = {"perturbation_runs": "runs", "lognormal_sigma": "sigma",
                "minimum_same_side_fraction": "same_side", "adversarial_prior_low": "adversarial_low",
                "adversarial_prior_high": "adversarial_high", "decision_threshold": "threshold"}

    def __post_init__(self):
        if not 0.0 < self.threshold < 1.0:
            raise ValueError("threshold must be strictly between 0 and 1")
        if self.runs < 1 or self.sigma < 0:
            raise ValueError("runs must be positive and sigma non-negative")
        if not 0.0 <= self.same_side <= 1.0:
            raise ValueError("same_side must be in [0, 1]")
        for p in (self.adversarial_low, self.adversarial_high):
            if not 0.0 < p < 1.0:
                raise ValueError("adversarial priors must be strictly between 0 and 1")

    @classmethod
    def from_dict(cls, d: dict | None) -> "GateConfig":
        d = dict(d or {})
        d.pop("schema", None)
        kw = {cls._ALIASES.get(k, k): v for k, v in d.items()}
        return cls(**kw)

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items()}


def _side(x: float, t: float) -> str:
    if abs(x - t) <= EPS:
        return "tie"
    return "supported" if x > t else "not_supported"


def _quantile(values: list[float], q: float) -> float:
    pos = (len(values) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return values[lo]
    return values[lo] * (hi - pos) + values[hi] * (pos - lo)


@dataclass
class GateResult:
    hypothesis_id: str
    base_logit: float
    base_score: float
    side: str
    tests: dict[str, dict]
    verdict: str
    failures: list[str] = field(default_factory=list)
    minimum_flip_weight: float | None = None

    @property
    def robust(self) -> bool:
        return self.verdict == "ROBUST"

    @property
    def band(self) -> tuple[float, float]:
        p = self.tests["weight_perturbation"]
        return (p["p05"], p["p95"])

    def summary(self) -> str:
        p = self.tests["weight_perturbation"]
        s = (f"{self.hypothesis_id:34s} base {self.base_score:.3f} (logit {self.base_logit:+.2f})  "
             f"band {p['p05']:.2f}-{p['p95']:.2f}  same-side {p['fraction_same_side']:.0%}  "
             f"uniform {self.tests['uniform_weight_ablation']['score']:.3f}  "
             f"adv {self.tests['adversarial_prior']['score']:.3f}  -> {self.verdict}")
        if self.failures:
            s += "\n    " + "; ".join(self.failures)
        return s

    def to_dict(self) -> dict:
        d = asdict(self)
        d["robust"] = self.robust
        return d


def gate(ledger: Ledger, hid: str, cfg: GateConfig | None = None) -> GateResult:
    cfg = cfg or GateConfig()
    h = ledger[hid]
    t = logit(cfg.threshold)
    base_x = ledger.logit_score(hid)
    side = _side(base_x, t)
    bearing = h.bearing
    failures: list[str] = []
    if side == "tie":
        failures.append("base score sits exactly on the decision threshold")

    # 1. leave-one-out
    loo = []
    for ev in bearing:
        x = ledger.logit_score(hid, exclude={ev.id})
        s = _side(x, t)
        loo.append({"dropped": ev.id, "score": round(sigmoid(x), 6), "side": s,
                    "flips_side": s != side, "delta_logit": round(x - base_x, 6)})
    loo_flips = [r["dropped"] for r in loo if r["flips_side"]]
    loo_pass = side != "tie" and not loo_flips
    if loo_flips and side != "tie":
        failures.append(f"flipped or tied by dropping {loo_flips}")

    # 2. perturbation
    offset = int(hashlib.sha256(hid.encode()).hexdigest()[:8], 16)
    rng = random.Random(cfg.seed + offset)
    xs = []
    for _ in range(cfg.runs):
        noise = {e.id: math.exp(rng.gauss(0.0, cfg.sigma)) for e in bearing}
        xs.append(ledger.logit_score(hid, weight_fn=lambda e: e.weight * noise.get(e.id, 1.0)))
    xs.sort()
    same = sum(_side(x, t) == side for x in xs) / len(xs)
    lo, hi = ledger.clamp
    clamp = lambda p: max(lo, min(hi, p))
    pert = {"runs": cfg.runs, "sigma": cfg.sigma, "fraction_same_side": round(same, 6),
            "p05": round(clamp(sigmoid(_quantile(xs, 0.05))), 6),
            "median": round(clamp(sigmoid(_quantile(xs, 0.5))), 6),
            "p95": round(clamp(sigmoid(_quantile(xs, 0.95))), 6),
            "logit_p05": round(_quantile(xs, 0.05), 4), "logit_p95": round(_quantile(xs, 0.95), 4)}
    pert_pass = side != "tie" and same >= cfg.same_side
    if side != "tie" and not pert_pass:
        failures.append(f"only {same:.0%} of perturbed runs stay on side (need {cfg.same_side:.0%})")

    # 3. uniform ablation
    ux = ledger.logit_score(hid, weight_fn=lambda e: cfg.uniform_weight if e.bearing else e.weight)
    us = _side(ux, t)
    uni = {"score": round(sigmoid(ux), 6), "side": us, "balanced": us == "tie"}
    uni_pass = side != "tie" and us == side
    if us == "tie":
        failures.append("evidence is exactly balanced under uniform weights")
    elif not uni_pass and side != "tie":
        failures.append(f"uniform weights give {sigmoid(ux):.3f}, other side")

    # 4. adversarial prior
    adv_p = cfg.adversarial_low if side == "supported" else cfg.adversarial_high
    ax = ledger.logit_score(hid, prior_override=adv_p)
    adv = {"prior_used": adv_p, "score": round(sigmoid(ax), 6), "side": _side(ax, t)}
    adv_pass = side != "tie" and adv["side"] == side
    if side != "tie" and not adv_pass:
        failures.append(f"adversarial prior {adv_p} gives {sigmoid(ax):.3f}, other side")

    # 5. group-out (independence)
    groups: dict[str, list[str]] = {}
    for e in bearing:
        groups.setdefault(e.group, []).append(e.id)
    multi = {g: ids for g, ids in groups.items() if len(ids) > 1}
    grp_rows = []
    if cfg.group_out:
        for g, ids in sorted(multi.items()):
            x = ledger.logit_score(hid, exclude=set(ids))
            s = _side(x, t)
            grp_rows.append({"group": g, "dropped": ids, "score": round(sigmoid(x), 6),
                             "side": s, "flips_side": s != side})
    grp_flips = [r["group"] for r in grp_rows if r["flips_side"]]
    grp_pass = side != "tie" and not grp_flips
    if grp_flips and side != "tie":
        failures.append(f"conclusion rests on one correlated source group: {grp_flips}")

    tests = {
        "leave_one_out": {"passed": loo_pass, "detail": loo,
                          "most_influential": max(loo, key=lambda r: abs(r["delta_logit"]))["dropped"] if loo else None},
        "weight_perturbation": {"passed": pert_pass, **pert},
        "uniform_weight_ablation": {"passed": uni_pass, **uni},
        "adversarial_prior": {"passed": adv_pass, **adv},
        "group_out": {"passed": grp_pass, "applicable": bool(multi) and cfg.group_out, "detail": grp_rows},
    }
    robust = all(v["passed"] for v in tests.values())
    return GateResult(hid, round(base_x, 6), round(ledger.score(hid), 6), side, tests,
                      "ROBUST" if robust else "WEIGHT_DRIVEN", failures,
                      minimum_flip_weight(ledger, hid, cfg.threshold))


def gate_all(ledger: Ledger, cfg: GateConfig | None = None,
             skip_policies=("suppressed", "unresolved")) -> dict[str, GateResult]:
    return {h.id: gate(ledger, h.id, cfg) for h in ledger if h.score_policy not in skip_policies}


def minimum_flip_weight(ledger: Ledger, hid: str, threshold: float = 0.5) -> float | None:
    """Log-odds of NEW opposing evidence needed to reach the threshold. Small
    means fragile even when the gate passes. None when already on the boundary."""
    gap = ledger.logit_score(hid) - logit(threshold)
    return None if abs(gap) <= EPS else round(abs(gap), 4)


def run_robustness_gate(ledger_source: Any, config: dict | GateConfig | None = None) -> dict:
    """Report-level API (toolkit A's shape): gate every declared-computed hypothesis."""
    ledger = load_ledger(ledger_source)
    issues = ledger.validate()
    if issues:
        raise ValueError(f"ledger is invalid: {issues}")
    cfg = config if isinstance(config, GateConfig) else GateConfig.from_dict(config)
    results = []
    for h in ledger:
        if h.score_policy != COMPUTED:
            r = public_result(h.score_policy, None, h.policy_reason,
                              what_would_settle_it=h.what_would_settle_it)
            results.append({"id": h.id, "statement": h.statement, "declared_policy": h.score_policy,
                            "effective_policy": h.score_policy, "verdict": h.score_policy.upper(),
                            "quotable_as_finding": False, **r, "gate": None})
            continue
        if not h.bearing:
            r = public_result("suppressed", None, "declared computed but no admissible bearing evidence; "
                                                  "the score would equal the prior")
            results.append({"id": h.id, "statement": h.statement, "declared_policy": COMPUTED,
                            "effective_policy": "suppressed", "verdict": "SUPPRESSED",
                            "quotable_as_finding": False, **r, "gate": None})
            continue
        g = gate(ledger, h.id, cfg)
        r = (public_result(COMPUTED, g.base_score, band=g.band, what_would_settle_it=h.what_would_settle_it)
             if g.robust else
             public_result(UNRESOLVED, None, "Robustness gate failed: " + "; ".join(g.failures),
                           what_would_settle_it=h.what_would_settle_it))
        results.append({"id": h.id, "statement": h.statement, "declared_policy": COMPUTED,
                        "effective_policy": r["policy"], "verdict": g.verdict,
                        "quotable_as_finding": g.robust, **r, "gate": g.to_dict()})
    counts = {k: sum(x["verdict"] == k for x in results)
              for k in ("ROBUST", "WEIGHT_DRIVEN", "SUPPRESSED", "UNRESOLVED")}
    return {"schema": "epistemic-toolkit/robustness-report/2.0.0", "ledger_id": ledger.ledger_id,
            "configuration": cfg.to_dict(),
            "rule": "A computed result is published only when every applicable gate test passes.",
            "summary": counts, "hypotheses": results}
