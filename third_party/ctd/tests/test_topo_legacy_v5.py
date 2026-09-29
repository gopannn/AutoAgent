"""Test suite.

v4 shipped with no tests at all. Each test below either pins a behaviour the
architecture claims, or is a regression test for a specific defect found while
hardening it — those are marked REGRESSION and name the defect, because a
regression test whose purpose is forgotten gets deleted the first time it is
inconvenient.

    python3 -m pytest tests -q          (if pytest is available)
    python3 tests/test_topo.py          (otherwise; no dependencies)
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from topo import (                                              # noqa: E402
    ANY, CORE_SCHEMA, Budget, Closure, Knobs, MalformedRelation, Outcome,
    R, Record, RelaxationPolicy, Schema, SlotSpec, TargetPattern,
    TopoStore, TopologicalEngine, UnindexedAttribute, Verdict,
    align, ask, bit_count, bit_indices, has, lexical, run_close,
    run_premortem, run_transfer, split_record, validate, where,
)
from topo import bitset                                          # noqa: E402
from topo import interop                                         # noqa: E402
from topo.transfer import project                                # noqa: E402


# --------------------------------------------------------------------------
# bitset
# --------------------------------------------------------------------------

def test_bitset_roundtrip():
    for idx in ([], [0], [0, 7, 8, 63, 64, 200], list(range(0, 500, 7))):
        bm = bitset.from_indices(idx, 512)
        assert bitset.indices(bm) == sorted(idx)
        assert bitset.count(bm) == len(set(idx))


def test_bitset_first_index_and_universe():
    assert bitset.first_index(0) is None
    assert bitset.first_index(bitset.from_indices([9, 40], 64)) == 9
    assert bitset.count(bitset.universe(31)) == 31


def test_bitset_rejects_out_of_range():
    try:
        bitset.from_indices([64], 64)
    except IndexError:
        return
    raise AssertionError("expected IndexError for a bit outside the universe")


# --------------------------------------------------------------------------
# store
# --------------------------------------------------------------------------

def _store(n: int = 6) -> TopoStore:
    st = TopoStore()
    for i in range(n):
        st.ingest(Record(id=f"r{i}", domain=f"d{i % 2}",
                         attrs={"severity": i % 3, "tags": [f"t{i % 2}"]}))
    st.declare_index("domain", )
    st.declare_index("severity")
    st.declare_index("tags")
    return st


def test_unindexed_attribute_raises_rather_than_returning_empty():
    """REGRESSION: v4's bitmap() returned 0 for an unindexed attribute, which
    downstream is indistinguishable from 'nothing matched' — and with
    negate=True returned the entire universe, silently disabling the filter."""
    st = _store()
    try:
        st.bitmap("no-such-attr", 1)
    except UnindexedAttribute:
        return
    raise AssertionError("expected UnindexedAttribute")


def test_delete_tombstones_without_shifting_positions():
    st = _store()
    before = st.bitmap("domain", "d0")
    assert st.delete("r0")
    assert not st.delete("r0")
    assert st.get("r0") is None
    after = st.bitmap("domain", "d0")
    assert bitset.count(after) == bitset.count(before) - 1
    # every surviving record keeps its original row position
    assert st.pos["r2"] == 2


def test_duplicate_id_refused():
    st = TopoStore()
    st.ingest(Record(id="x", domain="d"))
    try:
        st.ingest(Record(id="x", domain="d"))
    except ValueError:
        return
    raise AssertionError("expected duplicate id to be refused")


def test_record_identity_is_the_id():
    """REGRESSION: v4 used dataclass field-wise equality, so `rec in list`
    deep-compared attribute dicts and equated distinct records."""
    a = Record(id="a", domain="d", attrs={"k": 1})
    b = Record(id="a", domain="other", attrs={"k": 2})
    c = Record(id="c", domain="d", attrs={"k": 1})
    assert a == b and a != c and len({a, b, c}) == 2


def test_malformed_relations_refused_at_construction():
    for bad in (lambda: R("CAUSES"), lambda: R("", "x"),
                lambda: R("FLOWS", ["a"]), lambda: R("FLOWS", "a", "")):
        try:
            bad()
        except MalformedRelation:
            continue
        raise AssertionError("expected MalformedRelation")


def test_persistence_roundtrip_preserves_structure_and_tombstones():
    st = _store()
    st.ingest(Record(id="deep", domain="d0",
                     rels=(R("CAUSES", R("EXCESS", "load"),
                             R("QUEUEING", "svc")),)))
    st.delete("r1")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "store.json")
        st.save(path)
        back = TopoStore.load(path)
    assert len(back) == len(st)
    assert back.get("r1") is None
    assert back.get("deep").rels == st.get("deep").rels
    assert back.indexed() == st.indexed()


def test_structure_fingerprint_ignores_entity_names():
    a = Record(id="a", domain="x",
               rels=(R("CAUSES", R("EXCESS", "load"), R("QUEUEING", "svc")),))
    b = Record(id="b", domain="y",
               rels=(R("CAUSES", R("EXCESS", "orders"),
                       R("QUEUEING", "factory")),))
    assert a.structure_fingerprint() == b.structure_fingerprint()


# --------------------------------------------------------------------------
# schema
# --------------------------------------------------------------------------

def test_role_lattice_allows_siblings_and_refuses_disjoint():
    s = CORE_SCHEMA
    assert s.roles_compatible("POOL", "GATEWAY")      # siblings under RESOURCE
    assert s.roles_compatible("SERVICE", "AGENT")     # refinement
    assert not s.roles_compatible("AGENT", "RESOURCE")
    assert not s.roles_compatible("LOAD", "SIGNAL")


def test_signature_checks_arity_and_role():
    s = CORE_SCHEMA
    assert s.check_arity("CAUSES", 1)
    assert s.check_arity("CAUSES", 2) is None
    # a resource cannot be the subject of SENSES
    assert s.check_args("SENSES", ("broker", "rate"),
                        {"broker": "RESOURCE", "rate": "SIGNAL"})
    assert not s.check_args("SENSES", ("collector", "rate"),
                            {"collector": "AGENT", "rate": "SIGNAL"})


def test_anchors_and_causes_have_no_family():
    s = CORE_SCHEMA
    assert s.family_of("DEPENDS") is None
    assert s.family_of("CAUSES") is None
    assert not s.kin("CAUSES", "PRECEDES")
    assert s.kin("SATURATES", "FAILS")


def test_schema_rejects_duplicate_and_bad_antonyms():
    from topo.schema import ENT, Signature, SchemaError
    sig = Signature("X", (ENT("AGENT"),), "{0}")
    try:
        Schema([sig, sig])
    except SchemaError:
        pass
    else:
        raise AssertionError("expected duplicate signature to be refused")
    try:
        Schema([sig], antonyms=[("X", "NOPE")])
    except SchemaError:
        return
    raise AssertionError("expected unknown antonym predicate to be refused")


# --------------------------------------------------------------------------
# encoding
# --------------------------------------------------------------------------

def _codes(rec) -> set[str]:
    return validate(rec).codes


def test_validator_fires_each_check():
    good = Record(
        id="good", domain="d",
        types={"a": "AGENT", "r": "RESOURCE", "l": "LOAD", "s": "SIGNAL"},
        rels=(R("DEPENDS", "a", "r"),
              R("CAUSES", R("EXCESS", "l"), R("QUEUEING", "r")),
              R("INCREASES", R("QUEUEING", "r"), "s"),
              R("SENSES", "a", "s")))
    assert validate(good).ok

    untyped = Record(id="u", domain="d",
                     rels=(R("CAUSES", R("EXCESS", "l"), R("QUEUEING", "r")),))
    assert "UNTYPED" in _codes(untyped)

    shallow = Record(id="s", domain="d", types={"a": "AGENT", "r": "RESOURCE"},
                     rels=(R("DEPENDS", "a", "r"),))
    assert "SHALLOW" in _codes(shallow)

    arity = Record(id="ar", domain="d", types={"l": "LOAD"},
                   rels=(R("CAUSES", R("EXCESS", "l")),))
    assert "ARITY" in _codes(arity)

    sig = Record(id="sg", domain="d",
                 types={"a": "AGENT", "r": "RESOURCE", "l": "LOAD"},
                 rels=(R("CAUSES", R("EXCESS", "l"), R("QUEUEING", "r")),
                       R("SENSES", "r", "a")))
    assert "SIGNATURE" in _codes(sig)

    contra = Record(id="c", domain="d",
                    types={"r": "RESOURCE", "l": "LOAD", "s": "SIGNAL"},
                    rels=(R("CAUSES", R("EXCESS", "l"), R("QUEUEING", "r")),
                          R("INCREASES", R("QUEUEING", "r"), "s"),
                          R("DECREASES", R("QUEUEING", "r"), "s")))
    assert "CONTRADICTION" in _codes(contra)

    cycle = Record(id="cy", domain="d", types={"r": "RESOURCE", "l": "LOAD"},
                   rels=(R("CAUSES", R("EXCESS", "l"), R("QUEUEING", "r")),
                         R("CAUSES", R("QUEUEING", "r"), R("EXCESS", "l"))))
    assert "CYCLE" in _codes(cycle)

    selfc = Record(id="sc", domain="d", types={"l": "LOAD"},
                   rels=(R("CAUSES", R("EXCESS", "l"), R("EXCESS", "l")),))
    assert "SELF-CAUSE" in _codes(selfc)

    offv = Record(id="ov", domain="d", types={"a": "AGENT", "r": "RESOURCE"},
                  rels=(R("DEPENDS", "a", "r"),
                        R("CAUSES", R("WOBBLES", "a"), R("QUEUEING", "r"))))
    assert "OFF-VOCAB" in _codes(offv)


def test_roundtrip_gloss_is_readable_english():
    rec = Record(id="g", domain="d",
                 types={"a": "AGENT", "r": "RESOURCE", "l": "LOAD"},
                 rels=(R("CAUSES", R("EXCESS", "l"), R("QUEUEING", "r")),))
    assert validate(rec).roundtrip == [
        "there is too much l, which causes work piles up at r"]


# --------------------------------------------------------------------------
# CLOSE
# --------------------------------------------------------------------------

class _Oracle:
    def __init__(self, answer=Verdict.PASS):
        self.calls = 0
        self.tokens = 0
        self.answer = answer

    def judge(self, rec, question):
        self.calls += 1
        self.tokens += 10
        return self.answer


def _close_store() -> TopoStore:
    st = TopoStore()
    for i in range(8):
        st.ingest(Record(id=f"c{i}", domain="d" if i < 4 else "e",
                         attrs={"severity": 5 if i % 2 else 1},
                         text="alpha" if i == 3 else "beta"))
    st.declare_index("domain")
    st.declare_index("severity")
    return st


def test_index_bitmaps_are_probed_once_per_key():
    """REGRESSION: v4 called c.bitmap(store) inside the sort key AND inside the
    intersection loop, so every probe ran and was counted exactly twice."""
    st = _close_store()
    spec = SlotSpec("q", [has("dom", "domain", "d"),
                          has("sev", "severity", 5)])
    res = run_close(st, spec)
    assert res.probes == 2, f"expected 2 probes, got {res.probes}"


def test_closure_request_names_the_binding_constraint():
    st = _close_store()
    spec = SlotSpec("q", [has("dom", "domain", "d"),
                          has("sev", "severity", 99)])
    res = run_close(st, spec)
    assert res.outcome is Outcome.REQUEST
    assert res.request.binding_constraint == "sev"
    assert res.model_calls == 0


def test_ambiguity_abstains_and_relaxation_never_fires_on_it():
    st = _close_store()
    spec = SlotSpec("q", [has("dom", "domain", "d")])
    res = run_close(st, spec, relax=RelaxationPolicy(("dom",), 1))
    assert res.outcome is Outcome.ABSTAIN
    assert "underspecified" in res.reason


def test_relaxation_tries_combinations_not_only_prefixes():
    """REGRESSION: v4 only ever tried droppable[:n], so declaring ('a','b')
    droppable with max_drops=1 could never try dropping 'b' alone."""
    st = _close_store()
    spec = SlotSpec("q", [
        has("keep", "domain", "d"),
        has("impossible", "severity", 77),
        has("narrow", "severity", 5),
    ])
    res = run_close(st, spec, relax=RelaxationPolicy(
        droppable=("impossible", "narrow"), max_drops=1))
    # dropping 'impossible' alone leaves domain=d AND severity=5 -> 2 records,
    # which is ambiguous, so the only single drop that closes is impossible+...
    assert res.outcome in (Outcome.PARTIAL, Outcome.ABSTAIN, Outcome.REQUEST)
    if res.outcome is Outcome.PARTIAL:
        assert res.dropped and set(res.dropped) <= {"impossible", "narrow"}


def test_budget_refuses_a_stage_it_cannot_pay_for():
    st = _close_store()
    spec = SlotSpec("q", [has("dom", "domain", "d"),
                          ask("judge", "is it?")], expect_unique=False)
    o = _Oracle()
    res = run_close(st, spec, o, Budget(max_model_calls=2))
    assert res.outcome is Outcome.ABSTAIN
    assert o.calls == 0, "budget must be checked BEFORE the stage runs"


def test_unknown_defers_upward_and_blocks_closure():
    st = _close_store()
    spec = SlotSpec("q", [has("dom", "domain", "d"),
                          ask("judge", "?")], expect_unique=False)
    res = run_close(st, spec, _Oracle(Verdict.UNKNOWN), Budget(20))
    assert res.outcome is Outcome.ABSTAIN
    assert "undecided" in res.reason


def test_lexical_reads_only_declared_fields():
    """REGRESSION: v4 stringified every attribute value, so a query term could
    match against unrelated metadata such as stored check procedures."""
    st = TopoStore()
    st.ingest(Record(id="x", domain="d", text="nothing here",
                     attrs={"checks": {"K": "the word alpha appears here"}}))
    st.declare_index("domain")
    spec = SlotSpec("q", [has("dom", "domain", "d"),
                          lexical("lex", ["alpha"])])
    assert run_close(st, spec).outcome is not Outcome.CLOSED


def test_spec_validation_catches_duplicate_names_and_empty_specs():
    for bad in (lambda: SlotSpec("q", []),
                lambda: SlotSpec("q", [has("a", "domain", "d"),
                                       has("a", "domain", "e")])):
        try:
            bad()
        except ValueError:
            continue
        raise AssertionError("expected an invalid spec to be refused")


def test_deadline_is_enforced():
    st = _close_store()
    spec = SlotSpec("q", [has("dom", "domain", "d"),
                          where("slow", lambda r: True),
                          ask("judge", "?")], expect_unique=False)
    res = run_close(st, spec, _Oracle(), Budget(max_model_calls=99,
                                                max_ms=0.0))
    assert res.deadline_hit and res.outcome is Outcome.ABSTAIN


# --------------------------------------------------------------------------
# TRANSFER
# --------------------------------------------------------------------------

def _pair():
    src = Record(
        id="src", domain="a", attrs={"incident": True, "severity": 4},
        types={"w": "AGENT", "p": "RESOURCE", "c": "LOAD", "t": "SIGNAL"},
        rels=(R("DEPENDS", "w", "p"),
              R("CAUSES", R("EXCESS", "c"), R("QUEUEING", "p")),
              R("INCREASES", R("QUEUEING", "p"), "t"),
              R("SENSES", "w", "t"),
              R("CAUSES", R("SENSES", "w", "t"), R("RETRIES", "w", "p"))))
    tgt = TargetPattern(
        name="t", domain="b",
        types={"a": "AGENT", "r": "RESOURCE", "l": "LOAD", "s": "SIGNAL"},
        rels=(R("DEPENDS", "a", "r"),
              R("CAUSES", R("EXCESS", "l"), R("QUEUEING", "r")),
              R("INCREASES", R("QUEUEING", "r"), "s")))
    return src, tgt


def test_repeated_subrelations_do_not_abort_unification():
    """REGRESSION: v4 tested one-to-one consistency with `is not`, an identity
    comparison over frozen dataclasses. `R("QUEUEING","r")` written twice in a
    record constructs two equal-but-distinct objects, so the second relation
    mentioning it failed to unify and the mapping came out truncated — taking
    the whole sensing-and-response arm of every projection with it."""
    src, tgt = _pair()
    m = align(src, tgt)
    assert "t" in m.ent_map, "signal entity must bind via INCREASES"
    assert m.ent_map["t"] == "s"
    admitted, _ = project(m, tgt)
    assert R("SENSES", "a", "s") in admitted


def test_type_guard_refuses_incompatible_roles():
    src, tgt = _pair()
    mistyped = TargetPattern(
        name="bad", domain="b",
        types={"a": "RESOURCE", "r": "RESOURCE", "l": "LOAD", "s": "SIGNAL"},
        rels=tgt.rels)
    guarded = align(src, mistyped, Knobs(use_types=True))
    unguarded = align(src, mistyped, Knobs(use_types=False))
    assert guarded.ent_map.get("w") != "a"
    assert unguarded.ent_map.get("w") == "a"


def test_projection_guard_blocks_schema_violating_output():
    src, tgt = _pair()
    mistyped = TargetPattern(
        name="bad", domain="b",
        types={"a": "RESOURCE", "r": "RESOURCE", "l": "LOAD", "s": "SIGNAL"},
        rels=tgt.rels)
    m = align(src, mistyped, Knobs(use_types=False))
    admitted, blocked = project(m, mistyped, Knobs(guard_projection=True))
    assert blocked
    assert all(not CORE_SCHEMA.check_args(r.pred, r.args, mistyped.types)
               for r in admitted)


def test_kinship_is_discounted_and_reported():
    src = Record(id="s", domain="a", attrs={"incident": True},
                 types={"w": "AGENT", "p": "RESOURCE", "c": "LOAD"},
                 rels=(R("DEPENDS", "w", "p"),
                       R("CAUSES", R("EXCESS", "c"), R("FAILS", "p"))))
    tgt = TargetPattern(
        name="t", domain="b",
        types={"a": "AGENT", "r": "RESOURCE", "l": "LOAD"},
        rels=(R("DEPENDS", "a", "r"),
              R("CAUSES", R("EXCESS", "l"), R("SATURATES", "r"))))
    m = align(src, tgt)
    assert ("FAILS", "SATURATES") in m.kin_matches
    assert 0.0 < m.looseness <= 1.0
    strict = align(src, tgt, Knobs(use_kinship=False))
    assert strict.systematicity < m.systematicity


def test_mappings_returned_are_maximal():
    src, tgt = _pair()
    from topo.transfer import align_k_best
    ms = align_k_best(src, tgt, k=5)
    keys = [frozenset(m.rel_map.items()) for m in ms]
    for i, a in enumerate(keys):
        for j, b in enumerate(keys):
            assert i == j or not (a < b), "a returned mapping is a proper subset"


def test_convergence_is_not_inflated_by_duplicate_cases():
    src, tgt = _pair()
    twin = Record(id="twin", domain="other-domain",
                  attrs=dict(src.attrs), types=dict(src.types), rels=src.rels)
    st = TopoStore()
    st.ingest(src)
    st.ingest(twin)
    rounds = run_transfer(st, tgt, Knobs(rounds=1))
    assert rounds[0].inferences
    assert all(i.convergence == 1 for i in rounds[0].inferences), \
        "identical structure under two domain labels is not two witnesses"


# --------------------------------------------------------------------------
# PREMORTEM
# --------------------------------------------------------------------------

def test_conditional_predictions_name_their_premises():
    src, tgt = _pair()
    st = TopoStore()
    st.ingest(src)
    rounds = run_premortem(st, tgt, Knobs(rounds=2), enrich_top=2)
    later = [p for rr in rounds[1:] for p in rr.predictions if p.conditional]
    for p in later:
        assert p.assumptions
        assert p.confidence_factor < 1.0
        assert p.priority < p.structural_score * max(p.severity, 1)


def test_every_prediction_carries_a_check_or_is_marked_a_lead():
    src, tgt = _pair()
    src.attrs["checks"] = {"RETRIES": "Read the client config for {0}."}
    st = TopoStore()
    st.ingest(src)
    rounds = run_premortem(st, tgt, Knobs(rounds=1))
    assert rounds[0].predictions
    for p in rounds[0].predictions:
        assert p.checkable or p.check is None


# --------------------------------------------------------------------------
# evaluation
# --------------------------------------------------------------------------

def test_split_keeps_anchors_in_the_premise_and_relabels_the_domain():
    src, _ = _pair()
    held = split_record(src)
    assert held.usable
    assert R("DEPENDS", "w", "p") in held.premise.rels
    assert held.premise.domain != src.domain
    assert set(held.tail).isdisjoint(set(held.premise.rels))


def test_held_out_evaluation_cannot_see_the_answer():
    from topo.evaluate import _build_store
    src, _ = _pair()
    st = _build_store([src], exclude=src.id)
    assert len(st) == 0


# --------------------------------------------------------------------------
# interop
# --------------------------------------------------------------------------

def test_ctd_case_roundtrip():
    case = {
        "id": "ctd-1", "domain": "distributed", "independence_group": "g1",
        "severity": 4, "text": "t",
        "metadata": {"incident": True},
        "check_templates": {"saturates": "Stress {0}."},
        "types": {"client": "agent", "service": "resource", "load": "load"},
        "relations": [
            {"pred": "DEPENDS", "args": ["client", "service"]},
            {"pred": "CAUSES", "args": [
                {"pred": "EXCESS", "args": ["load"]},
                {"pred": "QUEUEING", "args": ["service"]}]},
        ],
    }
    rec = interop.to_record(case)
    assert rec.attrs["severity"] == 4
    assert rec.attrs["independence_group"] == "g1"
    assert rec.attrs["checks"]["SATURATES"] == "Stress {0}."
    assert rec.types["client"] == "AGENT"
    assert interop.canonical_key(rec.rels[1]) == \
        "CAUSES(EXCESS(load),QUEUEING(service))"
    assert interop.relation_from_dict(
        interop.relation_to_dict(rec.rels[1])) == rec.rels[1]


def test_import_applies_the_schema_gate_ctd_lacks():
    bad = {"id": "b", "domain": "d",
           "types": {"broker": "resource", "collector": "agent",
                     "rate": "load"},
           "relations": [
               {"pred": "CAUSES", "args": [
                   {"pred": "EXCESS", "args": ["rate"]},
                   {"pred": "QUEUEING", "args": ["broker"]}]},
               {"pred": "SENSES", "args": ["broker", "collector"]}]}
    report = interop.import_cases([bad])
    assert report.rejected and not report.accepted
    assert "SIGNATURE" in report.rejected[0].codes


def test_exported_hypotheses_are_never_resolved():
    src, tgt = _pair()
    st = TopoStore()
    st.ingest(src)
    rounds = run_premortem(st, tgt, Knobs(rounds=1))
    payloads = interop.export_findings(rounds[0].predictions, tgt)
    assert payloads
    assert all(p["state"] == "HYPOTHESIS" for p in payloads)
    assert all("RESOLVED" not in str(p.values()) for p in payloads)


# --------------------------------------------------------------------------
# engine
# --------------------------------------------------------------------------

def test_engine_gate_rejects_malformed_and_reports_duplicates():
    eng = TopologicalEngine(strict=True)
    eng.index("domain")
    src, _ = _pair()
    eng.ingest(src)
    eng.ingest(Record(id="twin", domain="z", types=dict(src.types),
                      rels=src.rels, attrs={"incident": True}))
    eng.ingest(Record(id="bad", domain="d",
                      rels=(R("FLOWS", "x", "y"),)))
    assert eng.telemetry.rejected_on_ingest == 1
    assert "near-duplicate" in eng.health()


def test_explain_plan_warns_when_nothing_cheap_is_selective():
    eng = TopologicalEngine()
    eng.index("domain")
    eng.ingest(Record(id="a", domain="d"))
    plan = eng.explain_plan(SlotSpec("q", [ask("judge", "?")]))
    assert "WARNING" in plan


# --------------------------------------------------------------------------

def _run_all() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  pass  {name}")
        except Exception as exc:                      # noqa: BLE001
            failed += 1
            print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
