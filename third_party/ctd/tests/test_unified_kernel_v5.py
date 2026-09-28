from ctd.encoding_quality import validate_structural_case_strict
from ctd.structural import StructuralCase, StructuralRelation, StructuralTarget
from ctd.transfer import StructuralTransferEngine, TransferPolicy


def R(pred, *args):
    return StructuralRelation(pred=pred, args=args)


def test_strict_schema_gate_rejects_bad_predicate_arity_before_alignment():
    case = StructuralCase(
        id="bad",
        domain="ops",
        relations=(R("SATURATES", "pool", "extra"),),
        types={"pool": "resource", "extra": "signal"},
    )
    result = validate_structural_case_strict(case)
    assert not result.ok
    assert any("argument" in finding.detail.lower() or "takes" in finding.detail.lower() for finding in result.findings)


def test_kernel_backed_transfer_preserves_ctd_mapping_depth_and_family_match_diagnostics():
    case = StructuralCase(
        id="retry",
        domain="distributed",
        independence_group="g1",
        relations=(
            R("CAUSES", R("RETRIES", "client", "service"), R("INCREASES", "load", "pressure")),
            R("SATURATES", "service", "pressure"),
        ),
        types={"client": "actor", "service": "resource", "load": "load", "pressure": "state"},
    )
    target = StructuralTarget(
        name="gateway",
        domain="software",
        relations=(R("CAUSES", R("REDUCES", "controller", "queue"), R("INCREASES", "demand", "pressure_target")),),
        types={"controller": "actor", "queue": "resource", "demand": "load", "pressure_target": "state"},
    )
    result = StructuralTransferEngine().transfer([case], target, TransferPolicy(min_depth=2))
    hypothesis = next(h for h in result.hypotheses if h.relation.pred == "SATURATES")
    assert hypothesis.mapping_depth >= 2
    assert hypothesis.family_matches >= 1
    assert hypothesis.lineage and hypothesis.lineage[0].source_case_id == "retry"
