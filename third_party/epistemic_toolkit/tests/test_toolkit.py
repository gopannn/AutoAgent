"""Test suite for epistemic_toolkit 2.0.

Sections tagged [A] port toolkit A's tests, [B] port toolkit B's (including its
BUG-n regressions), [MERGE] cover defects found while comparing the parents,
[NEW] cover capabilities neither parent had. Run: python -m unittest -v
"""
from __future__ import annotations

import copy
import io
import json
import math
import random
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "examples" / "scripts"))

from epistemic_toolkit import (COMPUTED, SUPPRESSED, UNRESOLVED, Evidence as E, GateConfig,
                               Hypothesis as H, Instrument, Ledger, LedgerError, Machine, MachineError, Suite,
                               auto_classify, confidence_vector_rank, gate, lint, load_records,
                               measure_real, minimum_flip_weight, public_result, report,
                               run_builtin_calibration, run_manifest, run_robustness_gate, trials_needed, wilson)
from epistemic_toolkit.cli import main as cli_main

DATA = ROOT / "examples" / "data"
FAST = GateConfig(runs=600)


def load(name):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def item(i, direction="supports", w=1.0, src=None):
    return E(f"e{i}", f"Evidence item number {i}.", "textual", direction, w, source=src or f"src{i}#p{i}")


# ============================================================ 1. ledger
class LedgerTests(unittest.TestCase):
    def test_B_untraceable_evidence_rejected(self):
        with self.assertRaises(LedgerError):
            E("x", "A real sentence here.", "textual", "supports", 1.0)

    def test_B_arithmetic_evidence_must_be_circular(self):
        with self.assertRaises(LedgerError):
            E("x", "Seventy-two times five is 360.", "arithmetic", "supports", 1.0, computed_by="math")
        E("x", "Seventy-two times five is 360.", "arithmetic", "supports", 0.0, computed_by="math", circular=True)

    def test_A_circular_cannot_carry_weight(self):
        with self.assertRaises(LedgerError):
            E("x", "Identity of the construction.", "textual", "supports", 1.0, source="s", circular=True)

    def test_B_log_odds_arithmetic_is_exact(self):
        h = H("h", "A hypothesis statement.", prior=0.3).add(item(1, w=1.2)).add(item(2, "undermines", 0.5))
        expected = 1 / (1 + math.exp(-(math.log(0.3 / 0.7) + 1.2 - 0.5)))
        self.assertAlmostEqual(Ledger([h]).score("h"), expected, places=12)

    def test_A_example_ledger_loads_validates_and_circular_contributes_zero(self):
        led = Ledger.from_json(DATA / "evidence-ledger.json")
        self.assertEqual(led.validate(), [])
        bd = led.breakdown("HYP_CONTROL_EFFECTIVE")
        c = next(x for x in bd.contributions if x["evidence_id"] == "EV_ARITHMETIC_IDENTITY")
        self.assertEqual((c["admissible"], c["log_odds_delta"]), (False, 0))

    def test_NEW_round_trip_both_parent_formats(self):
        led = Ledger.from_json(DATA / "evidence-ledger.json")           # A's v1 format
        again = Ledger.from_dict(json.loads(led.to_json()))
        for h in led:
            self.assertAlmostEqual(led.score(h.id), again.score(h.id), places=12)
        b_style = {"hypotheses": [{"id": "h", "statement": "A hypothesis statement.", "prior": 0.4,
                                   "evidence": [{"id": "a", "statement": "Supporting item one.", "kind": "textual",
                                                 "direction": "supports", "weight": 1.0, "source": "log#12"}]}]}
        self.assertEqual(Ledger.from_dict(b_style)["h"].evidence[0].source.locator, "12")

    def test_A_declared_policy_shapes_are_validated(self):
        led = Ledger.from_json(DATA / "evidence-ledger.json")
        led["HYP_UNKNOWN_VENDOR_LINEAGE"].evidence.append(item(99))
        led["HYP_MIGRATION_REDUCES_COST"].evidence.pop()
        codes = {i["code"] for i in led.validate()}
        self.assertTrue({"SUPPRESSED_HAS_EVIDENCE", "UNRESOLVED_NOT_CONTESTED"} <= codes)

    def test_NEW_correlated_source_is_audited(self):
        h = H("h", "Three rows cite one study.").add(item(1, src="study#t1")).add(item(2, src="study#t2"))
        codes = [a["code"] for a in Ledger([h]).audit()]
        self.assertIn("CORRELATED_SOURCE", codes)

    def test_NEW_evidence_ids_unique_across_ledger(self):
        h1 = H("h1", "First hypothesis here.").add(item(1))
        h2 = H("h2", "Second hypothesis here.").add(item(1))
        with self.assertRaises(LedgerError):
            Ledger([h1, h2])


# ======================================================== 2. robustness
def _balanced():
    h = H("h", "A balanced hypothesis.", prior=0.5)
    for i, (d, w) in enumerate([("supports", 1.0), ("supports", 1.4), ("undermines", 1.3), ("undermines", 1.2)]):
        h.add(item(i, d, w))
    return Ledger([h])


class RobustnessTests(unittest.TestCase):
    def test_B_balanced_ledger_is_weight_driven(self):
        self.assertEqual(gate(_balanced(), "h", FAST).verdict, "WEIGHT_DRIVEN")

    def test_B_strong_independent_evidence_is_robust(self):
        h = H("h", "A well-supported hypothesis.")
        for i in range(4):
            h.add(item(i, w=1.5))
        self.assertTrue(gate(Ledger([h]), "h", FAST).robust)

    def test_NEW_same_evidence_from_one_source_fails_group_out(self):
        h = H("h", "A well-supported hypothesis.")
        for i in range(4):
            h.add(item(i, w=1.5, src=f"one-dashboard#{i}"))
        g = gate(Ledger([h]), "h", FAST)
        self.assertFalse(g.tests["group_out"]["passed"])
        self.assertTrue(gate(Ledger([h]), "h", GateConfig(runs=600, group_out=False)).robust)

    def test_B_single_dominating_item_is_caught(self):
        h = H("h", "One item carries everything.", prior=0.3)
        h.add(item(0, w=3.0)).add(item(1, "undermines", 0.8)).add(item(2, "undermines", 0.8))
        g = gate(Ledger([h]), "h", FAST)
        self.assertIn("e0", [r["dropped"] for r in g.tests["leave_one_out"]["detail"] if r["flips_side"]])
        self.assertFalse(g.robust)

    def test_B_minimum_flip_weight(self):
        led = Ledger([H("h", "A hypothesis statement.").add(item(1, w=2.0))])
        self.assertAlmostEqual(minimum_flip_weight(led, "h"), 2.0)

    def test_A_gate_keeps_robust_and_demotes_weight_driven(self):
        r = run_robustness_gate(DATA / "evidence-ledger.json", {**load("robustness-config.json"), "perturbation_runs": 300})
        by = {x["id"]: x for x in r["hypotheses"]}
        self.assertEqual(by["HYP_CONTROL_EFFECTIVE"]["verdict"], "ROBUST")
        self.assertIsNotNone(by["HYP_CONTROL_EFFECTIVE"]["score"])
        self.assertEqual(by["HYP_INTERFACE_IMPROVES_JUDGMENT"]["effective_policy"], UNRESOLVED)
        self.assertIsNone(by["HYP_INTERFACE_IMPROVES_JUDGMENT"]["score"])

    def test_MERGE_exact_tie_is_a_state_not_a_side(self):
        h = H("h", "A hypothesis at the boundary.", prior=0.5)
        h.add(item(1, w=2.0)).add(item(2, "undermines", 1.2)).add(item(3, "undermines", 0.8))
        g = gate(Ledger([h]), "h", FAST)
        self.assertEqual(g.side, "tie")
        self.assertFalse(g.robust)
        self.assertIsNone(g.minimum_flip_weight)

    def test_MERGE_decisions_use_unclamped_log_odds(self):
        h = H("h", "Strong evidence for a claim.").add(item(1, w=5.0)).add(item(2, w=5.0))
        led = Ledger([h], clamp=(0.4, 0.6))
        bd = led.breakdown("h")
        self.assertEqual(bd.clamped_probability, 0.6)
        self.assertGreater(bd.raw_probability, 0.9999)
        g = gate(led, "h", FAST)
        self.assertAlmostEqual(g.base_logit, 10.0, places=6)

    def test_A_perturbation_seed_is_stable_per_hypothesis(self):
        led = _balanced()
        a = gate(led, "h", FAST).tests["weight_perturbation"]
        led.add(H("other", "An unrelated second hypothesis.").add(item(9)))
        b = gate(led, "h", FAST).tests["weight_perturbation"]
        self.assertEqual(a, b)


# ============================================================ 3. policy
class PolicyTests(unittest.TestCase):
    def test_A_null_states_distinct_and_reasoned(self):
        s, u = public_result(SUPPRESSED, 0.1, "no evidence"), public_result(UNRESOLVED, 0.5, "cancels")
        self.assertIsNone(s["score"]); self.assertIsNone(u["score"])
        self.assertNotEqual(s["label"], u["label"])
        with self.assertRaises(ValueError):
            public_result(UNRESOLVED, None, "")
        with self.assertRaises(ValueError):
            public_result(COMPUTED, None)

    def test_B_no_informative_evidence_is_suppressed(self):
        h = H("h", "Nobody collected the data.", prior=0.3)
        h.add(E("n", "The logs were not retained.", "methodological", "neutral", 0.0, source="retention"))
        led = Ledger([h])
        auto_classify(led, FAST)
        self.assertEqual(led["h"].score_policy, SUPPRESSED)
        self.assertIsNone(report(led, FAST)[0].confidence)

    def test_B_balanced_is_unresolved(self):
        led = _balanced()
        auto_classify(led, FAST)
        r = report(led, FAST)[0]
        self.assertEqual((r.policy, r.confidence), (UNRESOLVED, None))

    def test_B_manual_computed_failing_gate_is_demoted(self):
        r = report(_balanced(), FAST)[0]
        self.assertEqual(r.policy, UNRESOLVED)
        self.assertTrue(r.demoted)

    def test_MERGE_manual_computed_without_evidence_demoted_to_suppressed(self):
        h = H("h", "Marked computed but empty.", prior=0.3)
        h.add(E("n", "Nothing measurable was kept.", "methodological", "neutral", 0.0, source="policy"))
        r = report(Ledger([h]), FAST)[0]
        self.assertEqual((r.policy, r.demoted), (SUPPRESSED, True))

    def test_B_suppressed_without_reason_is_rejected(self):
        led = _balanced()
        led["h"].score_policy = SUPPRESSED
        with self.assertRaises(ValueError):
            report(led, FAST)

    def test_NEW_computed_scores_carry_band(self):
        h = H("h", "A well-supported hypothesis.")
        for i in range(3):
            h.add(item(i, w=1.5))
        r = report(Ledger([h]), FAST)[0]
        self.assertEqual(r.policy, COMPUTED)
        self.assertTrue(r.band[0] <= r.confidence <= r.band[1])


# =========================================================== 4. derived
def _rows(n=300, seed=1):
    rng = random.Random(seed)
    out = []
    for i in range(n):
        g = rng.choice("ABC")
        x = round(rng.uniform(1, 500), 2)
        out.append({"id": i, "grp": g, "grp_label": {"A": "a", "B": "b", "C": "c"}[g], "x": x,
                    "x100": round(x * 100), "const": 7, "y": rng.random(), "free": rng.randint(0, 9)})
        out[-1]["y_copy"] = out[-1]["y"]
    return out


class DerivedTests(unittest.TestCase):
    def test_B_finds_planted_defects_one_side_of_bijection(self):
        rep = lint(_rows(), key="id")
        self.assertTrue({"const", "y_copy", "x100"} <= set(rep.removable))
        self.assertTrue(("grp" in rep.candidates) != ("grp_label" in rep.candidates))

    def test_B_no_false_positives_on_clean_columns(self):
        rep = lint(_rows(), key="id")
        self.assertFalse({"free", "y", "x"} & set(rep.removable + rep.candidates))

    def test_B_BUG1_affine_reported_once(self):
        fd = [f for f in lint(_rows(), key="id").findings if f.kind == "arithmetic_derivation"]
        self.assertEqual([f.column for f in fd], ["x100"])

    def test_B_BUG2_high_cardinality_determinant_not_flagged(self):
        rep = lint(_rows(), key="id")
        self.assertFalse([f for f in rep.findings if f.determined_by == ["x"]
                          and f.kind in ("functional_dependency", "near_dependency")])

    def test_MERGE_A_key_like_test_had_BUG2_support_criterion_fixes_it(self):
        from ex2_orders_schema import make_orders
        rep = lint(make_orders(), key="order_id")
        spurious = [f for f in rep.findings if f.kind in ("functional_dependency", "near_dependency")
                    and f.determined_by and f.determined_by[0] in ("subtotal", "fraud_score", "subtotal_cents")]
        self.assertEqual(spurious, [])        # A's detector flagged 29 pairs here, mostly these

    def test_MERGE_linear_time_on_orders(self):
        from ex2_orders_schema import make_orders
        t = time.time()
        lint(make_orders(), key="order_id", max_determinant_size=2)
        self.assertLess(time.time() - t, 5.0)   # A's implementation took ~27 s single-column

    def test_A_rule_proven_derivations_are_errors_with_bits(self):
        rep = lint(load_records(DATA / "derived-fields.csv"), key="record_id", config=load("derived-fields-config.json"))
        self.assertEqual(set(rep.removable), {"max_users", "support_level"})
        self.assertFalse(rep.passes())
        region = next(o for o in rep.observations if o["determinants"] == ["region"])
        self.assertFalse(region["is_total_function"])

    def test_NEW_observed_fd_is_warning_until_rule_declared(self):
        rows = _rows()
        rep = lint(rows, key="id")
        f = next(f for f in rep.findings if f.kind == "functional_dependency")
        self.assertEqual((f.evidence_level, f.severity), ("observed", "WARNING"))
        rule = {"declared_rules": [{"id": "R", "determinants": [f.determined_by[0]], "dependent": f.column,
                                    "lookup": ({"A": "a", "B": "b", "C": "c"} if f.column == "grp_label"
                                               else {"a": "A", "b": "B", "c": "C"})}]}
        f2 = next(x for x in lint(rows, key="id", config=rule).findings if x.column == f.column)
        self.assertEqual((f2.evidence_level, f2.severity), ("rule_proven", "ERROR"))

    def test_MERGE_symmetric_drift_is_ambiguous_and_rows_listed(self):
        rows = _rows()
        for r in rows[:5]:
            r["grp_label"] = "zzz"
        near = [f for f in lint(rows, key="id").findings if f.kind == "near_dependency"]
        self.assertTrue(near)
        self.assertTrue(set(near[0].violating_keys) <= set(range(5)) or near[0].violating_keys)

    def test_NEW_rule_violation_lists_drifted_rows(self):
        rows = _rows()
        rows[3]["grp_label"] = "q"
        cfg = {"declared_rules": [{"id": "R", "determinants": ["grp"], "dependent": "grp_label",
                                   "lookup": {"A": "a", "B": "b", "C": "c"}}]}
        v = next(f for f in lint(rows, key="id", config=cfg).findings if f.kind == "rule_violated")
        self.assertEqual(v.violating_keys, [3])

    def test_NEW_affine_rule_and_allow_stored(self):
        cfg = {"declared_rules": [{"id": "C", "type": "affine", "determinants": ["x"], "dependent": "x100",
                                   "slope": 100, "tolerance": 1e-6}], "allow_stored": [["x", "x100"]]}
        f = next(f for f in lint(_rows(), key="id", config=cfg).findings if f.column == "x100")
        self.assertEqual((f.evidence_level, f.allowed_to_store, f.severity), ("rule_proven", True, "INFO"))

    def test_NEW_composite_determinant(self):
        rng = random.Random(3)
        rows = [{"id": i, "a": rng.choice("xy"), "b": rng.choice("pq"), "noise": rng.random()} for i in range(200)]
        for r in rows:
            r["ab"] = int(r["a"] == "x" and r["b"] == "p")   # lossy AND: only (a, b) determines it
        rep = lint(rows, key="id", max_determinant_size=2)
        self.assertTrue(any(f.column == "ab" and f.determined_by == ["a", "b"] for f in rep.findings))

    def test_NEW_csv_coercion_keeps_leading_zeros_and_sqlite_loads(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.csv"
            p.write_text("code,n,f\n007,5,0.25\n010,6,1.5\n", encoding="utf-8")
            rows = load_records(p)
            self.assertEqual((rows[0]["code"], rows[0]["n"], rows[0]["f"]), ("007", 5, 0.25))
            db = Path(d) / "t.db"
            with sqlite3.connect(db) as c:
                c.execute("create table t (a int, b text)"); c.execute("insert into t values (1, 'x')")
            self.assertEqual(load_records(db, table="t"), [{"a": 1, "b": "x"}])
            with self.assertRaises(ValueError):
                load_records(db, table='t"; drop table t; --')

    def test_NEW_confidence_vector_true_rank(self):
        rows = [{"a": v, "b": 0.9, "c": v, "d": w, "e": (v + w) / 2} for v, w in [(0.1, 0.3), (0.5, 0.2), (0.7, 0.9), (0.2, 0.6)]]
        r = confidence_vector_rank(rows, ["a", "b", "c", "d", "e"])
        self.assertEqual(r["constant"], ["b"])
        self.assertIn(("a", "c"), r["duplicates"])
        self.assertEqual(r["effective_rank"], 2)
        self.assertIn("e", r["linearly_dependent"])


# ====================================================== 5. state machine
def _spec(**over):
    d = {"name": "M", "events": ["go"],
         "states": [{"id": "S", "kind": "initial"}, {"id": "A"}, {"id": "DONE", "kind": "terminal"},
                    {"id": "ERR", "kind": "terminal"}],
         "transitions": [{"from": "S", "event": "go", "to": "A", "guard": "ok", "on_failure": "ERR"},
                         {"from": "A", "event": "go", "to": "DONE"}]}
    d.update(over)
    return d


class MachineTests(unittest.TestCase):
    def test_B_well_formed_machine_proves(self):
        p = Machine.from_dict(_spec()).prove()
        self.assertTrue(p.well_formed, p.reasons)

    def test_B_guard_without_failure_target_is_an_error(self):
        s = _spec()
        del s["transitions"][0]["on_failure"]
        p = Machine.from_dict(s).prove()
        self.assertFalse(p.well_formed)
        self.assertTrue(any("no on_failure" in e for e in p.errors))

    def test_B_failure_edge_is_what_makes_error_reachable(self):
        s = _spec()
        s["transitions"][0] = {"from": "S", "event": "go", "to": "A"}
        p = Machine.from_dict(s).prove()
        self.assertIn("ERR", p.unreachable_states)

    def test_B_unused_and_undeclared_events(self):
        self.assertIn("retry", Machine.from_dict(_spec(events=["go", "retry"])).prove().unused_events)
        self.assertIn("go", Machine.from_dict(_spec(events=["other"])).prove().undeclared_events)

    def test_B_nondeterminism_detected(self):
        s = _spec()
        s["transitions"].append({"from": "S", "event": "go", "to": "DONE"})
        self.assertFalse(Machine.from_dict(s).prove().well_formed)

    def test_A_review_protocol_proven_and_all_encodings_share_fingerprint(self):
        m = Machine.from_json(DATA / "fsm-review.json")
        with tempfile.TemporaryDirectory() as d:
            r = m.generate(d)
            self.assertTrue(r["proof"].well_formed)
            for name in r["manifest"]["files"]:
                self.assertIn(m.fingerprint(), (Path(d) / name).read_text())

    def test_A_unbounded_cycle_fails_universal_termination(self):
        spec = {"id": "CYCLE", "name": "cycle", "initial_state": "A",
                "states": [{"id": "A", "kind": "normal"}, {"id": "B", "kind": "normal"}, {"id": "Z", "kind": "terminal"}],
                "transitions": [{"from": "A", "event": "go", "to": "B", "on_failure": "Z"},
                                {"from": "B", "event": "back", "to": "A", "on_failure": "Z"}]}
        p = Machine.from_dict(spec).prove()
        self.assertFalse(p.checks["universal_termination"])
        self.assertTrue(p.cycle_witness)
        self.assertTrue(p.checks["existential_termination"])
        spec["require_universal_termination"] = False
        p2 = Machine.from_dict(spec).prove()
        self.assertTrue(p2.well_formed)
        self.assertTrue(any(r.startswith("ALLOWED unbounded cycle") for r in p2.reasons))

    def test_MERGE_B_payment_v2_has_unbounded_retry_loop_v3_is_bounded(self):
        from ex3_payment_lifecycle import V2, V3
        p2 = Machine.from_dict(V2).prove()
        self.assertFalse(p2.well_formed)
        self.assertTrue(p2.checks["existential_termination"])
        self.assertTrue(Machine.from_dict(V3).prove().well_formed)

    def test_MERGE_verify_encodings_detects_added_and_removed_edges(self):
        m = Machine.from_dict(_spec())
        enc = m.encodings()
        self.assertEqual(m.verify_encodings(enc), [])
        added = dict(enc, **{"state-machine.mmd": enc["state-machine.mmd"] + "    S --> DONE: sneak\n"})
        removed = dict(enc, **{"state-machine.mmd": enc["state-machine.mmd"].replace("    A --> DONE: go\n", "")})
        self.assertTrue(m.verify_encodings(added))
        self.assertTrue(m.verify_encodings(removed))

    def test_A_resource_bounds_violation_detected(self):
        s = _spec(resources=[{"id": "n", "min": 0, "initial": 0, "max": 1}])
        s["transitions"][1]["updates"] = {"n": -1}
        p = Machine.from_dict(s).prove()
        self.assertFalse(p.checks["resource_updates_stay_in_bounds"])

    def test_B_BUG3_unregistered_guard_raises(self):
        with self.assertRaises(MachineError):
            Machine.from_dict(_spec()).runtime({}).fire("go")

    def test_B_runtime_routes_guard_failure(self):
        rt = Machine.from_dict(_spec()).runtime({"ok": lambda c: c.get("ok")})
        self.assertEqual(rt.fire("go", {"ok": False}), "ERR")
        self.assertTrue(rt.terminal)

    def test_NEW_runtime_enforces_resource_guards(self):
        from ex3_payment_lifecycle import V3
        rt = Machine.from_dict(V3).runtime({"card_valid": lambda c: False})
        for _ in range(2):
            rt.fire("authorize"); rt.fire("retry")
        rt.fire("authorize")
        self.assertNotIn("retry", rt.allowed())
        with self.assertRaises(MachineError):
            rt.fire("retry")


# ========================================================= 6. instrument
def _inst(stat, alts=None):
    def pos(rng, noise):
        return [1.0 + rng.gauss(0, 0.05 + noise) for _ in range(20)]

    def neg(rng):
        return [rng.gauss(0, 1) for _ in range(20)]

    def null(s, rng):
        return [rng.gauss(0, 1) for _ in s]
    return Instrument(stat, pos, neg, null, smaller_is_stronger=False, alternatives=alts,
                      true_alternative=(alts[0] if alts else None), spec={"test": True})


def mean(s, alt=None):
    return sum(s) / len(s)


class InstrumentTests(unittest.TestCase):
    def test_B_real_signal_validates(self):
        # an exact null gives FPR ~= alpha (0.01); certifying <= 5% needs a few hundred trials
        c = _inst(mean).calibrate(trials=400, null_runs=100, noise_levels=(0.0, 0.5, 2.0, 5.0))
        self.assertTrue(c.validated, c.text())

    def test_B_no_signal_statistic_does_not_validate(self):
        rnd = random.Random(0)
        c = _inst(lambda s: rnd.random()).calibrate(trials=40, null_runs=100, noise_levels=())
        self.assertFalse(c.validated)

    def test_B_BUG4_flat_noise_ladder_is_reported(self):
        c = _inst(mean).calibrate(trials=40, null_runs=100, noise_levels=(0.0, 0.01, 0.02))
        self.assertTrue(any("flat" in x for x in c.limitations))

    def test_B_BUG5_non_exclusive_attribution_fails(self):
        c = _inst(mean, alts=["A", "B", "C"]).calibrate(trials=40, null_runs=100, noise_levels=(),
                                                         discrimination_samples=3)
        self.assertFalse(c.checks["only_true_alternative_beats_null"])
        self.assertFalse(c.validated)

    def test_MERGE_null_runs_that_cannot_reach_alpha_are_refused(self):
        with self.assertRaises(ValueError):
            _inst(mean).decide([1.0] * 20, random.Random(0), null_runs=50, alpha=0.01)

    def test_MERGE_thirty_clean_trials_do_not_certify_low_fpr(self):
        self.assertGreater(wilson(0, 30)[1], 0.098)       # B's tolerance accepted this
        self.assertEqual(trials_needed(0.01), 381)
        for seed in (1, 2, 3):
            c = _inst(mean).calibrate(trials=30, null_runs=100, noise_levels=(), seed=seed)
            self.assertFalse(c.checks["false_positive_rate_upper_bound_within_limit"])

    def test_MERGE_p_value_is_never_zero(self):
        d = _inst(mean).decide([5.0] * 20, random.Random(0), null_runs=199, alpha=0.01)
        self.assertEqual(d.p_value, 1 / 200)

    def test_A_builtin_calibration_and_fingerprint_gate(self):
        cfg = load("instrument-calibration.json")
        cal = run_builtin_calibration({**cfg, "trials_per_class": 600})
        self.assertTrue(cal["validated"], cal["checks"])
        self.assertTrue(measure_real(load("real-sample.json"), cfg["instrument"], cal)["detected"])
        with self.assertRaises(ValueError):
            measure_real(load("real-sample.json"), dict(cfg["instrument"], threshold=0.1), cal)
        failed = dict(cal, validated=False, calibrated=False)
        with self.assertRaises(ValueError):
            measure_real(load("real-sample.json"), cfg["instrument"], failed)


# ========================================================= 7. regression
def _suite(strict=True):
    s = Suite(strict=strict)
    s.check("positive", lambda a: all(v > 0 for v in a), catches=["has_negative"])
    s.check("nonempty", lambda a: len(a) > 0, catches=["has_negative"])
    s.check("sorted", lambda a: a == sorted(a))
    s.check("len3", lambda a: len(a) == 3)
    s.good("g", [1, 2, 3, 4])
    s.mutation_suite([1, 2, 3, 4], {"has_negative": lambda a: a.append(-1)})
    return s


class RegressionTests(unittest.TestCase):
    def test_B_all_statuses(self):
        st = {o.name: o.status for o in _suite().run().outcomes}
        self.assertEqual(st, {"positive": "MEANINGFUL", "nonempty": "VACUOUS", "sorted": "UNTARGETED", "len3": "BROKEN"})

    def test_B_BUG6_list_comprehension_mutation_is_in_place(self):
        s = Suite()
        s.check("no_flag", lambda rows: all("flag" not in r for r in rows), catches=["m"])
        s.good("g", [{"a": 1}, {"a": 2}])
        s.mutation_suite([{"a": 1}, {"a": 2}], {"m": lambda rows: [r.update(flag=1) for r in rows]})
        o = s.run().outcomes[0]
        self.assertEqual((o.status, o.errors), ("MEANINGFUL", {}))

    def test_B_BUG7_crash_is_not_a_catch(self):
        s = Suite()
        s.check("fragile", lambda a: a["key"] > 0, catches=["missing"])
        s.good("g", {"key": 1}); s.bad("missing", {})
        res = s.run()
        self.assertEqual(res.outcomes[0].status, "ERRORED")
        self.assertFalse(res.ok)
        self.assertEqual(res.coverage["missing"], [])

    def test_MERGE_untargeted_fails_strict_passes_lenient(self):
        def build(strict):
            s = Suite(strict=strict)
            s.check("real", lambda a: a > 0, catches=["neg"]); s.check("extra", lambda a: a < 100)
            return s.good("g", 5).bad("neg", -1).run()
        self.assertFalse(build(True).ok)
        self.assertTrue(build(False).ok)

    def test_NEW_uncovered_defect_fails_strict(self):
        s = Suite()
        s.check("real", lambda a: a > 0, catches=["neg"])
        res = s.good("g", 5).bad("neg", -1).bad("huge", 10 ** 9).run()
        self.assertEqual(res.uncovered, ["huge"])
        self.assertFalse(res.ok)

    def test_A_manifest_suite_passes_and_vacuity_detected(self):
        self.assertTrue(run_manifest(load("negative-suite.json"), DATA)["summary"]["passes"])
        m = load("negative-suite.json")
        m["broken_artifacts"][0]["artifact"] = "negative/good.json"
        r = run_manifest(m, DATA)
        self.assertFalse(r["summary"]["passes"])
        self.assertEqual(next(c for c in r["checks"] if c["name"] == "eligibility_is_derived")["status"], "VACUOUS")

    def test_A_manifest_unknown_target_is_an_error(self):
        m = load("negative-suite.json")
        m["broken_artifacts"][0]["must_fail"].append("no_such_check")
        self.assertFalse(run_manifest(m, DATA)["summary"]["passes"])


# ============================================================ packaging
class PackagingTests(unittest.TestCase):
    def test_A_schemas_parse_with_draft_marker(self):
        paths = sorted((ROOT / "schemas").glob("*.json"))
        self.assertGreaterEqual(len(paths), 5)          # guard against a vacuous loop
        for p in paths:
            self.assertEqual(json.loads(p.read_text())["$schema"], "https://json-schema.org/draft/2020-12/schema", p)

    def test_NEW_cli_exit_codes(self):
        with tempfile.TemporaryDirectory() as d, redirect_stdout(io.StringIO()):
            self.assertEqual(cli_main(["ledger", str(DATA / "evidence-ledger.json"), "--output", f"{d}/l.json"]), 0)
            self.assertEqual(cli_main(["negative", str(DATA / "negative-suite.json"), "--output", f"{d}/n.json"]), 0)
            self.assertEqual(cli_main(["derived", str(DATA / "derived-fields.csv"), "--config",
                                       str(DATA / "derived-fields-config.json"), "--fail-on", "error",
                                       "--output", f"{d}/d.json"]), 1)
            self.assertEqual(cli_main(["fsm", str(DATA / "fsm-review.json"), f"{d}/fsm"]), 0)
            self.assertEqual(cli_main(["verify-encodings", f"{d}/fsm"]), 0)
            (Path(d) / "fsm" / "seed.sql").write_text("-- tampered\n")
            self.assertEqual(cli_main(["verify-encodings", f"{d}/fsm"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
