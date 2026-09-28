from datetime import UTC, datetime

from ctd.models import (
    Evidence,
    FailureReason,
    RelationConstraint,
    ResolutionResult,
    ResolutionState,
    ResolutionUncertainty,
)

NOW = datetime(2026, 9, 12, tzinfo=UTC)


def test_correlated_evidence_and_constraint_policy_fields_are_modelled():
    evidence = Evidence(
        id="ev:1",
        source_id="src:1",
        source_type="registry",
        observed_at=NOW,
        confidence=0.95,
        trust=0.9,
        independence_group="registry-primary",
        extraction_method="direct",
        direct=True,
    )
    relation = RelationConstraint(
        id="r:1",
        subject_var="a",
        relation="REL",
        object_var="b",
        required_evidence_strength=0.9,
        required_source_classes={"registry"},
        max_inference_depth=1,
    )

    assert evidence.independence_group == "registry-primary"
    assert relation.required_evidence_strength == 0.9
    assert relation.required_source_classes == {"registry"}


def test_resolution_result_supports_v2_failure_and_uncertainty():
    result = ResolutionResult(
        state=ResolutionState.BUDGET_EXHAUSTED,
        constraint_coverage=0.5,
        uncertainty=ResolutionUncertainty(structural_coverage=0.5),
        failure_reasons=[FailureReason.EXPANSION_BUDGET_EXHAUSTED],
    )

    assert result.failure_reasons == [FailureReason.EXPANSION_BUDGET_EXHAUSTED]
    assert result.uncertainty.structural_coverage == 0.5


def test_v1_result_shape_still_deserializes_without_v2_fields():
    result = ResolutionResult(state="PARTIAL", constraint_coverage=0.25)

    assert result.state == ResolutionState.PARTIAL
    assert result.failure_reasons == []
    assert result.uncertainty.structural_coverage == 0.25
