"""End-to-end demo.

    1. Ingestion with the schema gate
    2. CLOSE      -- retrieve the incident matching a specification
    3. PREMORTEM  -- project failure modes onto a proposed design
    4. EVALUATION -- held-out-tail study with ablations and controls
    5. GUARDS     -- what the safety guards actually refuse
    6. INTEROP    -- CTD v4 exchange, and the invariant it preserves
    7. Telemetry

    python3 demo.py
"""

from __future__ import annotations

from library import DESIGN, INCIDENTS, LIBRARY
from topo import (
    Budget, Knobs, Outcome, RelaxationPolicy, SlotSpec, TopologicalEngine,
    Verdict, ask, format_ablations, has, report, run_ablations,
    score_closures, validate, where,
)
from topo.evaluate import format_guard_probe, guard_probe
from topo.interop import export_findings


class SimOracle:
    """Stand-in for a model call. Honest about what it proves: nothing about
    accuracy (it is handed ground truth), everything about how many times the
    expensive tier is invoked, which is set by filtering, not by the judge."""

    def __init__(self, truth):
        self.truth = truth
        self.calls = 0
        self.tokens = 0

    def judge(self, rec, question):
        self.calls += 1
        self.tokens += 60 + len(rec.text) // 4
        fn = self.truth.get(question)
        return Verdict.UNKNOWN if fn is None else (
            Verdict.PASS if fn(rec) else Verdict.FAIL)

    def reset(self):
        self.calls = self.tokens = 0


def rule(t: str) -> None:
    print("\n" + "=" * 74 + f"\n{t}\n" + "=" * 74)


def main() -> None:
    eng = TopologicalEngine(strict=True)

    # ---- write-time enrichment ---------------------------------------
    # The cheap tier can only filter on what was materialised at ingest.
    eng.enricher("depth", lambda r: r.depth())
    eng.enricher("predicates", lambda r: sorted(r.predicates()))
    eng.index("domain", "severity", "predicates", "incident")

    rule("1. INGESTION — the schema gate")
    print("Four malformed records, each tripping a different check. Three of "
          "the four\nwere accepted without complaint by v4.\n")
    for rec in LIBRARY:
        v = eng.ingest(rec)
        if not v.ok:
            print(f"REJECTED {v.explain()}\n")
        else:
            flags = sorted(v.codes - {"NO-RELS"})
            print(f"accepted {rec.id:<22} depth={rec.attrs['depth']} "
                  f"severity={rec.attrs['severity']}"
                  + (f"  warnings: {', '.join(flags)}" if flags else ""))

    print(f"\n{eng.health()}")
    print(f"\n{eng.validation_summary()}")
    print("\nround-trip of one accepted record (the check that catches "
          "'encoded correctly,\nencodes the wrong thing' — nothing automates "
          "this):")
    for line in validate(INCIDENTS[0]).roundtrip:
        print(f"   - {line}")

    # ---- 2. CLOSE -----------------------------------------------------
    rule("2. CLOSE — return the match")
    Q = "Did this incident involve a feedback loop that amplified its own load?"
    oracle = SimOracle({Q: lambda r: "AMPLIFIES" in r.attrs["predicates"]})

    spec = SlotSpec(
        query=Q,
        constraints=[
            has("is-incident", "incident", True),
            has("high-severity", "severity", 4, 5),
            has("has-retry-structure", "predicates", "RETRIES"),
            where("deep-enough", lambda r: r.attrs["depth"] >= 2),
            ask("self-amplifying", Q),
        ],
        expect_unique=False,
    )
    print(eng.explain_plan(spec))
    print()
    c = eng.close(spec, oracle, Budget(max_model_calls=10))
    print(c.explain())
    if c.provenance:
        print("provenance:")
        for rid, trail in c.provenance.items():
            print(f"   {rid}: {trail}")

    print("\n-- unsatisfiable spec: a data request, at zero model spend")
    oracle.reset()
    impossible = [
        has("is-incident", "incident", True),
        has("power-domain", "domain", "power-systems"),
        has("needs-oscillation", "predicates", "OSCILLATES"),
    ]
    c2 = eng.close(SlotSpec("impossible", list(impossible)), oracle)
    print(c2.explain())

    print("\n-- same spec, one drop permitted: PARTIAL, with the drop named")
    oracle.reset()
    c3 = eng.close(SlotSpec("impossible", list(impossible)), oracle,
                   relax=RelaxationPolicy(droppable=("needs-oscillation",),
                                          max_drops=1))
    print(c3.explain())

    print("\n-- scorecard over the three queries above")
    print(score_closures([(c, "retry-storm"), (c2, None), (c3, None)]))
    print("\nAbstention correctness is 50% and that is the metric working, not "
          "failing.\nc2 correctly refused. c3 is the SAME unanswerable query "
          "with relaxation\nenabled, and it produced an answer — so it scores "
          "as an answer, not as a\nrefusal. Turning relaxation on buys "
          "coverage and is charged for it here.\nA system that counted "
          "PARTIAL as a refusal could enable relaxation everywhere\nand keep "
          "a perfect abstention score while answering everything.")

    # ---- 3. PREMORTEM -------------------------------------------------
    rule("3. PREMORTEM — return the leftover")
    print(f"design under review: {DESIGN.name}  [{DESIGN.domain}]")
    for r in DESIGN.rels:
        print(f"   {r}")
    print("\nNo incident in the library is about AI infrastructure.\n")

    rounds = eng.premortem(DESIGN, Knobs(reach=1.0, mac_keep=6, min_depth=2,
                                         rounds=2, beam=8), enrich_top=3)
    print(report(rounds, limit=3))
    print("Round 2 predictions are marked CONDITIONAL and name the round-1")
    print("guesses they rest on. Check the premises first: if one fails, "
          "everything\nbelow it collapses and there is no point measuring it.")

    # ---- 4. EVALUATION ------------------------------------------------
    rule("4. EVALUATION — held-out tail, with controls")
    print("For each incident: withhold its causal tail, restate the premise as "
          "a design\nin an unseen domain, delete the incident from the "
          "library, and ask whether\nthe tail comes back. The tail is ground "
          "truth — it is what actually happened.\n")
    print(format_ablations(run_ablations(INCIDENTS)))
    print("\nTwo readings.")
    print("\n  deep@10. The frequency control -- no alignment, no mapping, "
          "just 'guess\n  the commonest predicates' -- ties the engine on "
          "hit@10 and recovers ZERO\n  order-2 causal relations. "
          "Reconstructing causal structure is the only thing\n  here that "
          "structural transfer does and frequency cannot.")
    print("\n  flat ranking vs full. Demoting a prediction that is already "
          "contained in a\n  higher one doubles hit@1 and lifts deep@10, "
          "while rec@10 appears to halve.\n  It does not: cov@10, which "
          "credits containment, is IDENTICAL for both arms.\n  Confirming "
          "CAUSES(AMPLIFIES(x,y), SATURATES(y)) confirms SATURATES(y); plain\n"
          "  recall was counting the same finding twice and rewarding the "
          "ranking for\n  restating itself.")

    # ---- 5. GUARDS ----------------------------------------------------
    rule("5. GUARDS — what they refuse, measured without a gold label")
    print("The held-out study cannot see the guards: every incident in the "
          "library uses\nthe same four roles in the same arrangement, so no "
          "incompatible binding is\never offered. This probe supplies one.\n")
    print(format_guard_probe(guard_probe(INCIDENTS)))
    print("Each guard alone suffices here; with both off, five nonsense "
          "relations reach\nthe output. That is the documented failure mode "
          "— cars sensing their own\njourney time — reproducing on demand.")

    # ---- 6. INTEROP ---------------------------------------------------
    rule("6. INTEROP — CTD v4 exchange")
    payloads = export_findings(rounds[0].predictions[:2], DESIGN)
    for p in payloads:
        print(f"   {p['canonical_key']}")
        print(f"      state={p['state']}  convergence={p['convergence']}  "
              f"priority={p['priority_score']}  checkable={p['checkable']}")
    print("\nEvery payload leaves as state=HYPOTHESIS. There is no code path "
          "in this\npackage that emits a resolved claim: promotion to truth "
          "belongs to the\nevidence resolver, which is the only component "
          "holding evidence to do it with.")

    # ---- 7. telemetry -------------------------------------------------
    rule("7. TELEMETRY")
    print(eng.telemetry)
    print("\nshard plan by domain (partition on the queried attribute, not on "
          "graph density):")
    for k, v in sorted(eng.shard_plan("domain").items()):
        print(f"   {k:<24} {', '.join(v)}")

    rule("WHAT THESE NUMBERS DO AND DO NOT SHOW")
    print("""
  real        model calls, records materialised, index probes, latency,
              abstention behaviour. These follow from filtering and are
              independent of how good the model or the retriever is.

  real        severity on each prediction — observed data from the incident
              record, not a generated score.

  real        the held-out-tail numbers, on this corpus. The tail is withheld
              ground truth and the controls run the identical protocol.

  real        the guard probe. An emitted relation either violates its own
              predicate signature or it does not; no label is involved.

  proxy       systematicity and convergence measure whether a transfer is
              structurally well-founded. They do not measure whether the
              predicted failure will occur.

  not shown   real-world predictive accuracy. Eleven hand-encoded incidents
              sharing a vocabulary by construction is a mechanism test, not
              an incident-prediction benchmark. The next experiment is a real
              postmortem corpus with a real model, and the claim to falsify is
              stated in the README.
""")


if __name__ == "__main__":
    main()
