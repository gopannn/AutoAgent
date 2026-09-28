"""Components 1-3 applied to incident root-cause analysis (SRE / postmortems).

Scenario: a checkout service degraded for 40 minutes. Four candidate causes.
Postmortems routinely assert "root cause: X (high confidence)". Here every
confidence is computed from traceable evidence, gated for robustness, and
reported as null where a number would mislead.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from epistemic_toolkit import (Evidence as E, Hypothesis as H, Ledger,
                               gate, auto_classify, report, report_table,
                               minimum_flip_weight)

led = Ledger()

led.add(H("H_CONFIG_PUSH", "The 14:02 feature-flag config push caused the degradation.",
          prior=0.3)
        .add(E("E1", "Error rate rose 88 seconds after the push completed.",
               "temporal", "supports", 1.6, source="deploy-log#4411"))
        .add(E("E2", "Rolling back the flag restored latency within 3 minutes.",
               "experimental", "supports", 2.4, source="incident-channel 14:47"))
        .add(E("E3", "Canary cohort with the flag off showed no degradation.",
               "observational", "supports", 1.4, source="grafana/checkout-canary"))
        .add(E("E4", "The push touched only a UI flag with no backend code path.",
               "structural", "undermines", 0.8, source="PR#9123 diff")))

led.add(H("H_DB_FAILOVER", "A database primary failover caused the degradation.",
          prior=0.3)
        .add(E("E5", "A replica lag alert fired at 14:05.",
               "temporal", "supports", 0.7, source="pagerduty#77120"))
        .add(E("E6", "No failover event appears in the RDS event log for the window.",
               "observational", "undermines", 2.2, source="rds-events 14:00-15:00"))
        .add(E("E7", "Connection-pool saturation was not observed on any app node.",
               "observational", "undermines", 1.1, source="app-metrics/pool")))

# Balanced: this is the kind of hypothesis that gets a confident-sounding number.
led.add(H("H_THIRD_PARTY", "Degradation in the payment provider's API contributed.",
          prior=0.5)
        .add(E("E8", "Provider status page showed 'degraded' from 14:10 to 14:30.",
               "external", "supports", 1.2, source="status.provider.example"))
        .add(E("E9", "p99 latency to the provider was elevated during the incident.",
               "observational", "supports", 0.9, source="trace-sampler"))
        .add(E("E10", "Degradation began 8 minutes BEFORE the provider incident.",
               "temporal", "undermines", 1.3, source="timeline reconstruction"))
        .add(E("E11", "Non-payment endpoints degraded identically.",
               "observational", "undermines", 0.8, source="grafana/all-endpoints")))

# Looks robust item-by-item, but all three supporting rows cite ONE dashboard.
led.add(H("H_CACHE_EVICTION", "A cache eviction storm amplified the degradation.", prior=0.4)
        .add(E("E14", "Cache hit ratio fell from 97% to 61% at 14:03.",
               "observational", "supports", 1.1, source="grafana/cache#hit-ratio"))
        .add(E("E15", "Origin request rate tripled at 14:03.",
               "observational", "supports", 1.0, source="grafana/cache#origin-rps"))
        .add(E("E16", "Eviction counter spiked at 14:03.",
               "observational", "supports", 0.9, source="grafana/cache#evictions"))
        .add(E("E17", "Cache node memory was at 40% throughout; no pressure.",
               "observational", "undermines", 0.9, source="node-exporter/cache-01")))
for e in led["H_CACHE_EVICTION"].evidence[:3]:
    e.independence_group = "grafana/cache dashboard (one scrape pipeline)"

# Untestable: nobody captured the data.
led.add(H("H_MEMORY_LEAK", "A slow memory leak in the new build contributed.",
          prior=0.2)
        .add(E("E12", "Heap profiles were not retained for the incident window.",
               "methodological", "neutral", 0.0, source="profiler retention policy"))
        .add(E("E13", "The build was 6 hours old; the incident count is one, so "
               "'it happened after a deploy' is true by construction.",
               "definitional", "supports", 0.0, circular=True,
               computed_by="incident selection criteria")))
led["H_MEMORY_LEAK"].what_would_settle_it = (
    "Continuous heap profiling retained for >= 7 days, then compare RSS slope "
    "between old and new builds under matched load.")

if __name__ == "__main__":
    print("=== 1. Ledger breakdowns ===")
    print(led.breakdown("H_CONFIG_PUSH").explain())
    print()
    print("=== audit ===")
    for i in led.audit():
        print(f"  {i['hypothesis']} [{i['code']}]: {i['issue']}")

    print("\n=== 2. Robustness gate ===")
    for hid in led.hypotheses:
        if led[hid].admissible and any(e.weight > 0 for e in led[hid].admissible):
            print(gate(led, hid).summary())
    print(f"\n  minimum new counter-evidence to flip H_CONFIG_PUSH: "
          f"{minimum_flip_weight(led, 'H_CONFIG_PUSH')} log-odds units")

    print("\n=== 3. Policy: classify from data, then report ===")
    pol = auto_classify(led)
    for k, v in pol.items():
        print(f"  {k:16s} -> {v}")
    print()
    for r in report(led):
        print(" ", r)
    print()
    print(report_table(report(led)))
