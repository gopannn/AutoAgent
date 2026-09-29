from ctd.premortem import PreMortemEngine, PreMortemPolicy
from ctd.structural import PredicateVocabulary, StructuralCase, StructuralRelation, StructuralTarget
from ctd.transfer import HypothesisState, TransferPolicy


def R(pred, *args):
    return StructuralRelation(pred=pred, args=args)


def design():
    return StructuralTarget(
        name="gateway",
        domain="software",
        relations=(R("CAUSES", R("REDUCES", "controller", "queue"), R("INCREASES", "load", "pressure")),),
        types={"controller": "actor", "queue": "resource", "load": "load", "pressure": "state"},
    )


def incident(case_id, domain, independence, severity):
    return StructuralCase(
        id=case_id,
        domain=domain,
        independence_group=independence,
        relations=(
            R("CAUSES", R("RETRIES", "client", "service"), R("INCREASES", "demand", "saturation")),
            R("SATURATES", "service", "saturation"),
        ),
        types={"client": "actor", "service": "resource", "demand": "load", "saturation": "state"},
        severity=severity,
        metadata={"incident": True},
        check_templates={"SATURATES": "Load-test {0} and measure headroom against {1}."},
    )


def test_premortem_emits_only_checkable_hypotheses_with_lineage_and_severity():
    engine = PreMortemEngine(PredicateVocabulary.core())
    cases = [incident("i1", "distributed", "g1", 5), incident("i2", "ecology", "g2", 3)]

    result = engine.analyze(cases, design(), PreMortemPolicy(transfer=TransferPolicy(min_depth=2, mac_keep=10)))

    assert result.findings
    finding = result.findings[0]
    assert finding.state == HypothesisState.HYPOTHESIS
    assert finding.check
    assert finding.severity == 5
    assert finding.convergence == 2
    assert set(finding.source_cases) == {"i1", "i2"}
    assert finding.priority_score > finding.structural_priority


def test_premortem_filters_predictions_without_verification_check():
    no_check = incident("i1", "distributed", "g1", 4).model_copy(update={"check_templates": {}})
    engine = PreMortemEngine(PredicateVocabulary.core())

    result = engine.analyze([no_check], design())

    assert result.findings == []
    assert result.uncheckable_hypotheses >= 1
