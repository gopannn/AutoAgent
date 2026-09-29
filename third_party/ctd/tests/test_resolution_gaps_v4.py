from datetime import datetime, timezone

from ctd.constraint_truth import ConstraintTruth, evaluate_attribute_constraint
from ctd.graph import EvidenceGraph
from ctd.models import AttributeConstraint, Node, QueryConstraintGraph, RelationConstraint, ResolutionState, Variable
from ctd.resolver import Resolver

NOW = datetime(2026, 9, 12, tzinfo=timezone.utc)


def test_missing_attribute_is_unknown_not_satisfied_or_violated():
    constraint = AttributeConstraint(id="a1", variable="s", attribute="risk", op="lte", value=3)
    evaluation = evaluate_attribute_constraint(constraint, Node(id="s1", type="Supplier", attributes={}))

    assert evaluation.truth == ConstraintTruth.UNKNOWN
    assert "missing" in evaluation.reason.lower()


def test_incompatible_attribute_values_are_violated_with_reason():
    constraint = AttributeConstraint(id="a1", variable="s", attribute="risk", op="lte", value=3)
    evaluation = evaluate_attribute_constraint(constraint, Node(id="s1", type="Supplier", attributes={"risk": "high"}))

    assert evaluation.truth == ConstraintTruth.VIOLATED
    assert "incompatible" in evaluation.reason.lower()


def test_resolver_keeps_unknown_candidate_partial_and_emits_actionable_gap():
    graph = EvidenceGraph()
    graph.add_node(Node(id="s1", type="Supplier", attributes={"name": "A"}))
    query = QueryConstraintGraph(
        variables=[Variable(name="s", node_type="Supplier")],
        attributes=[AttributeConstraint(id="risk", variable="s", attribute="risk", op="lte", value=3)],
        as_of=NOW,
    )

    result = Resolver(graph).resolve(query)

    assert result.state == ResolutionState.PARTIAL
    assert result.bindings == {"s": "s1"}
    gap = next(g for g in result.gaps if g.constraint_id == "risk")
    assert gap.truth == "UNKNOWN"
    assert gap.constraint_kind == "attribute"
    assert "risk" in gap.required_evidence
    assert "risk" in " ".join(result.next_actions)


def test_unresolved_relation_emits_relation_gap_without_promoting_hypothesis():
    graph = EvidenceGraph()
    graph.add_node(Node(id="s1", type="Supplier"))
    graph.add_node(Node(id="c1", type="Component"))
    query = QueryConstraintGraph(
        variables=[Variable(name="s", node_type="Supplier"), Variable(name="c", node_type="Component")],
        relations=[RelationConstraint(id="produces", subject_var="s", relation="PRODUCES", object_var="c")],
        as_of=NOW,
    )

    result = Resolver(graph).resolve(query)

    assert result.state != ResolutionState.RESOLVED
    gap = next(g for g in result.gaps if g.constraint_id == "produces")
    assert gap.truth == "UNKNOWN"
    assert gap.constraint_kind == "relation"
    assert "PRODUCES" in gap.required_evidence
