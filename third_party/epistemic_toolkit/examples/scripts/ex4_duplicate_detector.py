"""Component 6 applied to near-duplicate document detection.

Use case: attributing a submitted document to one of several source templates
(contract clause libraries, plagiarism checks, phishing-kit attribution).

The templates share boilerplate, as real templates do. The obvious statistic,
overall shingle distance, detects copying perfectly well — and cannot tell
WHICH template was copied, because the shared boilerplate dominates. This is the
same failure the kamea calibration exposed: a statistic that beats its null on
a known true positive for several alternatives at once. Calibration catches it
before the tool attributes anything; a distinctive-shingle statistic fixes it.
"""
import random
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from epistemic_toolkit import Instrument

rng0 = random.Random(1)
VOCAB = [f"w{i}" for i in range(600)]
BOILER = [rng0.choice(VOCAB) for _ in range(160)]          # shared by all templates

TEMPLATES = {}
for name in ("T_ALPHA", "T_BETA", "T_GAMMA", "T_DELTA"):
    own = [rng0.choice(VOCAB) for _ in range(40)]           # distinctive section
    TEMPLATES[name] = BOILER[:80] + own + BOILER[80:]
TRUE = "T_BETA"


def shingles(tokens, k=4):
    return {tuple(tokens[i:i + k]) for i in range(len(tokens) - k + 1)}


SH = {n: shingles(t) for n, t in TEMPLATES.items()}
COMMON = set.intersection(*SH.values())
DISTINCT = {n: s - set().union(*(SH[m] for m in SH if m != n)) for n, s in SH.items()}


def mutate(tokens, rate, rng):
    return [rng.choice(VOCAB) if rng.random() < rate else t for t in tokens]


def positive(rng, noise):
    return mutate(TEMPLATES[TRUE], 0.02 + noise, rng)


def negative(rng):
    return [rng.choice(VOCAB) for _ in range(len(TEMPLATES[TRUE]))]


def null(sample, rng):
    s = sample[:]
    rng.shuffle(s)          # same length and vocabulary, order destroyed
    return s


def naive_stat(sample, alt=TRUE):
    """1 - Jaccard(sample, template). Smaller = more similar."""
    a, b = shingles(sample), SH[alt]
    return 1 - len(a & b) / len(a | b)


def distinctive_stat(sample, alt=TRUE):
    """1 - fraction of the template's DISTINCTIVE shingles present."""
    d = DISTINCT[alt]
    return 1 - len(shingles(sample) & d) / len(d) if d else 1.0


if __name__ == "__main__":
    print(f"templates share {len(COMMON)} shingles; distinctive per template: "
          f"{ {k: len(v) for k, v in DISTINCT.items()} }\n")
    alts = list(TEMPLATES)

    print("=== naive statistic: overall shingle distance ===")
    naive = Instrument(naive_stat, positive, negative, null,
                       alternatives=alts, true_alternative=TRUE)
    c1 = naive.calibrate(trials=80, null_runs=100)
    print(c1.text())

    print("\n=== distinctive-shingle statistic ===")
    better = Instrument(distinctive_stat, positive, negative, null,
                        alternatives=alts, true_alternative=TRUE)
    c2 = better.calibrate(trials=80, null_runs=100)
    print(c2.text())

    print("\n=== same good statistic, but only 30 trials (toolkit B's default) ===")
    c3 = better.calibrate(trials=30, null_runs=100, noise_levels=(), discrimination_samples=3)
    print(f"  FPR {c3.false_positive_rate:.3f}, Wilson upper {c3.false_positive_rate_wilson_95[1]:.3f} "
          f"-> FPR gate {'PASS' if c3.checks['false_positive_rate_upper_bound_within_limit'] else 'FAIL'}; "
          f"clean trials needed for <=5%: {c3.trials_needed_for_fpr_claim}")

    print("\n=== fingerprint lock ===")
    sample = positive(random.Random(99), 0.0)
    print("  measure with calibrated instrument:", better.measure(sample, c2)["detected"])
    tweaked = Instrument(distinctive_stat, positive, negative, null, spec={"k": 5},
                         alternatives=alts, true_alternative=TRUE)
    try:
        tweaked.measure(sample, c2)
    except ValueError as e:
        print("  changed instrument ->", e)
