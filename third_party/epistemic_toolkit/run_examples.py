#!/usr/bin/env python3
"""Rebuild every worked artifact in dist/ and fail if any gate misbehaves.

Expected "failures" (a derived field proven, a weight-driven hypothesis, a
vacuous check, an unbounded FSM) are positive demonstrations of the controls.
The build fails only if a control does NOT behave as specified.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "examples" / "scripts"))

from epistemic_toolkit import (GateConfig, Ledger, Machine, lint, load_records, measure_real, report,
                               run_builtin_calibration, run_manifest, run_robustness_gate)

DATA = ROOT / "examples" / "data"
DIST = ROOT / "dist"


def load(name):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def write(name, payload):
    p = DIST / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")


def main() -> int:
    led = Ledger.from_json(DATA / "evidence-ledger.json")
    write("ledger-audit.json", {"issues": led.validate(), "audit": led.audit(),
                                "breakdowns": [led.breakdown(h.id).to_dict() for h in led]})
    rob = run_robustness_gate(led, load("robustness-config.json"))
    write("robustness-report.json", rob)
    pub = [r.to_dict() for r in report(Ledger.from_json(DATA / "evidence-ledger.json"),
                                      GateConfig.from_dict(load("robustness-config.json")))]
    write("published-scores.json", pub)

    rows = load_records(DATA / "derived-fields.csv")
    der = lint(rows, key="record_id", config=load("derived-fields-config.json"))
    write("derived-fields-report.json", der.to_dict())

    from ex2_orders_schema import make_orders
    orders = lint(make_orders(), key="order_id")
    write("derived-orders-report.json", orders.to_dict())

    review = Machine.from_json(DATA / "fsm-review.json").generate(DIST / "fsm-review", "review")
    from ex3_payment_lifecycle import V2, V3
    v2 = Machine.from_dict(V2).prove()
    write("fsm-payment-v2-proof.json", v2.to_dict())
    v3 = Machine.from_dict(V3).generate(DIST / "fsm-payment", "payment")

    cal = run_builtin_calibration(load("instrument-calibration.json"))
    write("instrument-calibration.json", cal)
    meas = measure_real(load("real-sample.json"), load("instrument.json"), cal)
    write("real-measurement.json", meas)
    changed = dict(load("instrument.json"), threshold=0.1)
    try:
        measure_real(load("real-sample.json"), changed, cal)
        drift_blocked = False
    except ValueError:
        drift_blocked = True

    neg = run_manifest(load("negative-suite.json"), DATA)
    write("negative-test-report.json", neg)

    required = {
        "ledger valid and group-out applied to the UX study": not led.validate()
            and any(h["gate"] and h["gate"]["tests"]["group_out"]["applicable"] for h in rob["hypotheses"]),
        "robustness: >=1 ROBUST and >=1 WEIGHT_DRIVEN": rob["summary"]["ROBUST"] >= 1 and rob["summary"]["WEIGHT_DRIVEN"] >= 1,
        "policy: suppressed and unresolved publish null": all(p["confidence"] is None for p in pub if p["policy"] != "computed"),
        "derived: declared rules proven as errors": not der.passes() and set(der.removable) == {"max_users", "support_level"},
        "derived: orders lint finds all planted defects": set(orders.removable) == {"risk_score", "schema_version", "source_system", "subtotal_cents"}
            and set(orders.candidates) == {"tier_discount_rate", "vat_rate"},
        "fsm: review protocol proven and generated": review["proof"].well_formed,
        "fsm: B's payment v2 fails universal termination": not v2.well_formed and v2.checks["existential_termination"],
        "fsm: bounded payment v3 proven": v3["proof"].well_formed,
        "instrument: calibration passes Wilson gate": cal["validated"],
        "instrument: real measurement detects": meas["detected"],
        "instrument: changed instrument blocked": drift_blocked,
        "negative suite passes (strict)": neg["summary"]["passes"],
    }
    for label, ok in required.items():
        print(f"{'PASS' if ok else 'FAIL'}  {label}")
    return 0 if all(required.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
