from ctd.encoding_quality import EncodingQualityPolicy, validate_structural_case
from ctd.structural import PredicateVocabulary, StructuralCase, StructuralRelation


def _good_case() -> StructuralCase:
    response = StructuralRelation(pred="RETRIES", args=("client", "service"))
    causal = StructuralRelation(pred="CAUSES", args=(response, "load"))
    return StructuralCase(
        id="incident:retry",
        domain="distributed-systems",
        relations=(causal,),
        types={"client": "actor", "service": "resource", "load": "load"},
        text="Clients retry a delayed service and increase load.",
    )


def test_quality_gate_accepts_typed_higher_order_case_and_produces_roundtrip():
    validation = validate_structural_case(_good_case())

    assert validation.ok is True
    assert validation.max_order == 2
    assert validation.roundtrip
    assert any("causes" in line.lower() for line in validation.roundtrip)


def test_quality_gate_rejects_untyped_and_shallow_case():
    case = StructuralCase(
        id="bad",
        domain="x",
        relations=(StructuralRelation(pred="FLOWS", args=("a", "b")),),
        types={},
    )

    validation = validate_structural_case(case)

    codes = {f.code for f in validation.errors}
    assert "UNTYPED" in codes
    assert "SHALLOW" in codes
    assert validation.ok is False


def test_off_vocab_and_orphan_are_auditable_warnings_by_default():
    nested = StructuralRelation(pred="CUSTOM", args=("a", "b"))
    case = StructuralCase(
        id="warn",
        domain="x",
        relations=(StructuralRelation(pred="CAUSES", args=(nested, "c")),),
        types={"a": "actor", "b": "resource", "c": "outcome"},
    )

    validation = validate_structural_case(case)

    warning_codes = {f.code for f in validation.warnings}
    assert "OFF_VOCAB" in warning_codes
    assert "ORPHAN" in warning_codes


def test_policy_can_make_off_vocab_blocking():
    case = StructuralCase(
        id="strict",
        domain="x",
        relations=(StructuralRelation(pred="CAUSES", args=(StructuralRelation(pred="CUSTOM", args=("a", "b")), "c")),),
        types={"a": "actor", "b": "resource", "c": "outcome"},
    )

    validation = validate_structural_case(
        case,
        vocabulary=PredicateVocabulary.core(),
        policy=EncodingQualityPolicy(off_vocab_level="error"),
    )

    assert "OFF_VOCAB" in {f.code for f in validation.errors}
