from datetime import UTC, datetime

from ctd.inference import InferenceEngine, InferenceRule
from ctd.models import Edge, Evidence

NOW = datetime(2026, 9, 12, tzinfo=UTC)


def ev(eid: str, confidence: float = 0.9) -> Evidence:
    return Evidence(
        id=eid,
        source_id=f"src:{eid}",
        source_type="registry",
        observed_at=NOW,
        confidence=confidence,
        trust=0.95,
    )


def test_deterministic_inference_preserves_lineage_and_confidence_multiplier():
    edges = [
        Edge(id="e1", source="a", target="b", type="OWNS", evidence=[ev("ev1", 0.9)]),
        Edge(id="e2", source="b", target="c", type="LOCATED_IN", evidence=[ev("ev2", 0.8)]),
    ]
    rule = InferenceRule(
        id="r:owner-location",
        premise_relation_types=["OWNS", "LOCATED_IN"],
        output_relation_type="OWNER_LOCATED_IN",
        confidence_multiplier=0.8,
        max_depth=2,
    )

    inferred = InferenceEngine().derive(edges, [rule], max_depth=2)

    assert len(inferred) == 1
    edge = inferred[0]
    assert (edge.source, edge.target, edge.type) == ("a", "c", "OWNER_LOCATED_IN")
    assert edge.evidence[0].direct is False
    assert set(edge.evidence[0].lineage) == {"ev1", "ev2"}
    assert edge.evidence[0].confidence == 0.64


def test_inference_prevents_cyclic_path_expansion():
    edges = [
        Edge(id="e1", source="a", target="b", type="LINK", evidence=[ev("ev1")]),
        Edge(id="e2", source="b", target="a", type="LINK", evidence=[ev("ev2")]),
    ]
    rule = InferenceRule(
        id="r:chain",
        premise_relation_types=["LINK", "LINK"],
        output_relation_type="LINK",
        max_depth=3,
    )

    inferred = InferenceEngine().derive(edges, [rule], max_depth=3)

    assert inferred == []


def test_rule_registry_enables_optional_inference_without_changing_default_behavior():
    from ctd.inference import RuleRegistry

    registry = RuleRegistry()
    assert registry.rules() == []

    rule = InferenceRule(
        id="r:chain",
        premise_relation_types=["A", "B"],
        output_relation_type="C",
        max_depth=1,
    )
    registry.register(rule)

    assert [item.id for item in registry.rules()] == ["r:chain"]
    assert InferenceEngine(registry=registry).rules_enabled is True
    assert InferenceEngine().rules_enabled is False
