from datetime import UTC, datetime, timedelta

from ctd.controller import RuntimePolicy
from ctd.graph import EvidenceGraph
from ctd.models import (
    AttributeConstraint,
    Edge,
    Evidence,
    FailureReason,
    Node,
    QueryConstraintGraph,
    RelationConstraint,
    ResolutionState,
    Variable,
)
from ctd.resolver import Resolver

AS_OF = datetime(2026, 9, 12, tzinfo=UTC)


def evidence(eid: str, *, group: str, valid_from=None, valid_until=None, confidence=0.95):
    return Evidence(
        id=eid,
        source_id=f"src:{eid}",
        source_type="registry",
        observed_at=AS_OF - timedelta(days=1),
        valid_from=valid_from,
        valid_until=valid_until,
        confidence=confidence,
        trust=0.95,
        independence_group=group,
    )


def simple_query(*, required_strength: float | None = None) -> QueryConstraintGraph:
    return QueryConstraintGraph(
        as_of=AS_OF,
        variables=[
            Variable(name="supplier", node_type="Supplier"),
            Variable(name="component", node_type="Component"),
        ],
        relations=[
            RelationConstraint(
                id="r:produces",
                subject_var="supplier",
                relation="PRODUCES",
                object_var="component",
                required_evidence_strength=required_strength,
            )
        ],
        attributes=[
            AttributeConstraint(id="a:code", variable="component", attribute="code", op="eq", value="X")
        ],
    )


def test_resolver_prefers_stronger_independent_evidence_when_candidates_both_fit():
    graph = EvidenceGraph()
    for node in [
        Node(id="supplier:weak", type="Supplier"),
        Node(id="supplier:strong", type="Supplier"),
        Node(id="component:x", type="Component", attributes={"code": "X"}),
    ]:
        graph.add_node(node)
    graph.add_edge(
        Edge(
            id="e:weak",
            source="supplier:weak",
            target="component:x",
            type="PRODUCES",
            evidence=[evidence("weak:1", group="same"), evidence("weak:2", group="same")],
        )
    )
    graph.add_edge(
        Edge(
            id="e:strong",
            source="supplier:strong",
            target="component:x",
            type="PRODUCES",
            evidence=[evidence("strong:1", group="one"), evidence("strong:2", group="two")],
        )
    )

    result = Resolver(graph).resolve(simple_query(), RuntimePolicy(initial_arousal=4, max_arousal=4))

    assert result.state == ResolutionState.RESOLVED
    assert result.bindings["supplier"] == "supplier:strong"
    assert result.uncertainty.source_diversity >= 2
    assert result.uncertainty.evidence_strength > 0.95


def test_resolver_returns_budget_exhausted_with_precise_reason():
    graph = EvidenceGraph()
    for node in [
        Node(id="s", type="Supplier"),
        Node(id="c", type="Component", attributes={"code": "X"}),
        Node(id="cert", type="Certification", attributes={"status": "active"}),
    ]:
        graph.add_node(node)
    graph.add_edge(Edge(id="e1", source="s", target="c", type="PRODUCES", evidence=[evidence("e1", group="g1")]))
    graph.add_edge(Edge(id="e2", source="s", target="cert", type="CERTIFIED", evidence=[evidence("e2", group="g2")]))
    query = QueryConstraintGraph(
        as_of=AS_OF,
        variables=[Variable(name="s", node_type="Supplier"), Variable(name="c", node_type="Component"), Variable(name="cert", node_type="Certification")],
        relations=[
            RelationConstraint(id="r1", subject_var="s", relation="PRODUCES", object_var="c"),
            RelationConstraint(id="r2", subject_var="s", relation="CERTIFIED", object_var="cert"),
        ],
        attributes=[AttributeConstraint(id="a1", variable="c", attribute="code", op="eq", value="X")],
    )

    result = Resolver(graph).resolve(query, RuntimePolicy(max_expansions=1, initial_arousal=2, max_arousal=2))

    assert result.state == ResolutionState.BUDGET_EXHAUSTED
    assert FailureReason.EXPANSION_BUDGET_EXHAUSTED in result.failure_reasons


def test_resolver_temporally_supersedes_stale_conflict():
    graph = EvidenceGraph()
    for node in [
        Node(id="s", type="Supplier"),
        Node(id="cert:old", type="Certification", attributes={"status": "active"}),
        Node(id="cert:new", type="Certification", attributes={"status": "revoked"}),
    ]:
        graph.add_node(node)
    graph.add_edge(
        Edge(
            id="old",
            source="s",
            target="cert:old",
            type="CERTIFIED",
            attributes={"claim_key": "cert:status", "claim_value": "active"},
            evidence=[evidence("old", group="registry", valid_from=AS_OF - timedelta(days=100), valid_until=AS_OF - timedelta(days=5))],
        )
    )
    graph.add_edge(
        Edge(
            id="new",
            source="s",
            target="cert:new",
            type="CERTIFIED",
            attributes={"claim_key": "cert:status", "claim_value": "revoked"},
            evidence=[evidence("new", group="registry", valid_from=AS_OF - timedelta(days=5))],
        )
    )
    query = QueryConstraintGraph(
        as_of=AS_OF,
        variables=[Variable(name="s", node_type="Supplier"), Variable(name="cert", node_type="Certification")],
        relations=[RelationConstraint(id="r", subject_var="s", relation="CERTIFIED", object_var="cert")],
        attributes=[AttributeConstraint(id="a", variable="cert", attribute="status", op="eq", value="revoked")],
    )

    result = Resolver(graph).resolve(query, RuntimePolicy(initial_arousal=4, max_arousal=4))

    assert result.state == ResolutionState.RESOLVED
    assert result.bindings["cert"] == "cert:new"
    assert result.contradictions == []
    assert result.uncertainty.contradiction_risk < 1.0


def test_resolver_marks_evidence_too_weak_instead_of_claiming_closure():
    graph = EvidenceGraph()
    graph.add_node(Node(id="s", type="Supplier"))
    graph.add_node(Node(id="c", type="Component", attributes={"code": "X"}))
    graph.add_edge(
        Edge(id="weak", source="s", target="c", type="PRODUCES", evidence=[evidence("weak", group="g", confidence=0.3)])
    )

    result = Resolver(graph).resolve(
        simple_query(required_strength=0.9), RuntimePolicy(initial_arousal=2, max_arousal=2)
    )

    assert result.state == ResolutionState.PARTIAL
    assert FailureReason.EVIDENCE_TOO_WEAK in result.failure_reasons
    assert "r:produces" in result.unresolved_constraints


def test_authorization_scope_is_enforced_as_focus_not_post_filtering():
    graph = EvidenceGraph()
    graph.add_node(Node(id="secret:s", type="Supplier"))
    graph.add_node(Node(id="c", type="Component", attributes={"code": "X"}))
    graph.add_edge(Edge(id="e", source="secret:s", target="c", type="PRODUCES", evidence=[evidence("e", group="g")]))

    result = Resolver(graph).resolve(
        simple_query(),
        RuntimePolicy(authorization_scope={"c"}, initial_arousal=2, max_arousal=2),
    )

    assert result.state == ResolutionState.UNRESOLVABLE
    assert FailureReason.AUTHORIZATION_EXCLUDED in result.failure_reasons
