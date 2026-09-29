from ctd.structural import PredicateVocabulary, StructuralCase, StructuralRelation, StructuralTarget
from ctd.transfer import HypothesisState, StructuralTransferEngine, TransferPolicy


def R(pred, *args):
    return StructuralRelation(pred=pred, args=args)


def source_case(case_id="case:retry", domain="distributed", independence="distributed"):
    response = R("RETRIES", "client", "service")
    growth = R("INCREASES", "load", "pressure")
    return StructuralCase(
        id=case_id,
        domain=domain,
        independence_group=independence,
        relations=(R("CAUSES", response, growth), R("SATURATES", "service", "pressure")),
        types={
            "client": "actor",
            "service": "resource",
            "load": "load",
            "pressure": "state",
        },
    )


def target():
    action = R("REDUCES", "controller", "queue")
    growth = R("INCREASES", "demand", "pressure_target")
    return StructuralTarget(
        name="gateway",
        domain="software",
        relations=(R("CAUSES", action, growth),),
        types={
            "controller": "actor",
            "queue": "resource",
            "demand": "load",
            "pressure_target": "state",
        },
    )


def test_transfer_projects_only_licensed_unmapped_structure():
    engine = StructuralTransferEngine(PredicateVocabulary.core())
    result = engine.transfer([source_case()], target(), TransferPolicy(min_depth=2, beam_width=8))

    assert result.hypotheses
    projected = {h.relation.canonical_key() for h in result.hypotheses}
    assert "SATURATES(queue,pressure_target)" in projected
    assert all(h.state == HypothesisState.HYPOTHESIS for h in result.hypotheses)
    assert result.hypotheses[0].mapping_depth >= 2
    assert result.hypotheses[0].family_matches >= 1


def test_incompatible_declared_entity_types_prevent_mapping():
    bad_target = target().model_copy(update={"types": {**target().types, "queue": "person"}})
    engine = StructuralTransferEngine(PredicateVocabulary.core())

    result = engine.transfer([source_case()], bad_target, TransferPolicy(min_depth=2))

    assert result.hypotheses == []


def test_projection_never_invents_unmapped_entities():
    case = source_case().model_copy(
        update={"relations": (*source_case().relations, R("FAILS", "unmapped_component")), "types": {**source_case().types, "unmapped_component": "component"}}
    )
    engine = StructuralTransferEngine(PredicateVocabulary.core())

    result = engine.transfer([case], target(), TransferPolicy(min_depth=2))

    assert all("unmapped_component" not in h.relation.canonical_key() for h in result.hypotheses)


def test_convergence_counts_independent_domains_not_duplicate_cases():
    cases = [
        source_case("case:a", "distributed", "group-a"),
        source_case("case:b", "distributed", "group-a"),
        source_case("case:c", "ecology", "group-c"),
    ]
    engine = StructuralTransferEngine(PredicateVocabulary.core())

    result = engine.transfer(cases, target(), TransferPolicy(min_depth=2, mac_keep=10))

    hypothesis = next(h for h in result.hypotheses if h.relation.pred == "SATURATES")
    assert hypothesis.source_case_count == 3
    assert hypothesis.convergence == 2
    assert hypothesis.priority_score > 0


def test_beam_alignment_preserves_multiple_globally_consistent_alternatives():
    case = StructuralCase(
        id="ambiguous",
        domain="source",
        relations=(
            R("CAUSES", R("INCREASES", "a", "x"), R("INCREASES", "b", "y")),
            R("SATURATES", "b", "y"),
        ),
        types={"a": "actor", "b": "actor", "x": "state", "y": "state"},
    )
    tgt = StructuralTarget(
        name="ambiguous-target",
        domain="target",
        relations=(
            R("CAUSES", R("INCREASES", "p", "u"), R("INCREASES", "q", "v")),
            R("CAUSES", R("INCREASES", "r", "w"), R("INCREASES", "s", "z")),
        ),
        types={"p": "actor", "q": "actor", "r": "actor", "s": "actor", "u": "state", "v": "state", "w": "state", "z": "state"},
    )
    engine = StructuralTransferEngine(PredicateVocabulary.core())

    result = engine.transfer([case], tgt, TransferPolicy(min_depth=2, beam_width=12, mappings_per_case=4))

    projected = {h.relation.canonical_key() for h in result.hypotheses}
    assert any(key in projected for key in {"SATURATES(q,v)", "SATURATES(p,u)", "SATURATES(s,z)", "SATURATES(r,w)"})
    assert result.fac_mappings_considered >= 2
