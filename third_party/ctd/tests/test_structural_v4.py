from ctd.structural import PredicateVocabulary, StructuralCase, StructuralRelation, StructuralTarget


def test_recursive_relation_order_walk_and_canonical_key():
    delayed = StructuralRelation(pred="DELAYED", args=("signal",))
    causes = StructuralRelation(pred="CAUSES", args=(delayed, "oscillation"))
    case = StructuralCase(
        id="incident:1",
        domain="control",
        relations=(causes,),
        types={"signal": "signal", "oscillation": "failure"},
    )

    assert delayed.order == 1
    assert causes.order == 2
    assert [r.pred for r in case.all_relations()] == ["CAUSES", "DELAYED"]
    assert case.entities() == {"signal", "oscillation"}
    assert causes.canonical_key().startswith("CAUSES(")


def test_structural_target_can_be_extended_without_mutating_original():
    target = StructuralTarget(
        name="gateway",
        domain="software",
        relations=(StructuralRelation(pred="INCREASES", args=("latency", "retries")),),
        types={"latency": "signal", "retries": "action"},
    )
    added = StructuralRelation(pred="CAUSES", args=("retries", "load"))

    extended = target.with_relation(added)

    assert len(target.relations) == 1
    assert len(extended.relations) == 2


def test_predicate_vocabulary_knows_exact_and_family_matches():
    vocab = PredicateVocabulary.core()

    assert vocab.compatibility("RETRIES", "RETRIES") == 1.0
    assert 0.0 < vocab.compatibility("RETRIES", "REDUCES") < 1.0
    assert vocab.compatibility("RETRIES", "FLOWS") == 0.0
