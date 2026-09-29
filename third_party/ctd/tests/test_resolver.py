from datetime import UTC, datetime

from ctd.controller import RuntimePolicy
from ctd.graph import EvidenceGraph
from ctd.models import (
    AttributeConstraint,
    Edge,
    Evidence,
    Node,
    QueryConstraintGraph,
    RelationConstraint,
    ResolutionState,
    Variable,
)
from ctd.resolver import Resolver


AS_OF = datetime(2026, 9, 12, tzinfo=UTC)


def ev(evidence_id: str) -> Evidence:
    return Evidence(
        id=evidence_id,
        source_id=evidence_id.replace("ev:", "src:"),
        source_type="erp",
        observed_at=AS_OF,
        confidence=0.98,
        trust=0.95,
    )


def supplier_query() -> QueryConstraintGraph:
    return QueryConstraintGraph(
        as_of=AS_OF,
        variables=[
            Variable(name="supplier", node_type="Supplier"),
            Variable(name="component", node_type="Component"),
            Variable(name="cert", node_type="Certification"),
        ],
        relations=[
            RelationConstraint(
                id="r:produces",
                subject_var="supplier",
                relation="PRODUCES",
                object_var="component",
            ),
            RelationConstraint(
                id="r:certified",
                subject_var="supplier",
                relation="CERTIFIED_WITH",
                object_var="cert",
            ),
        ],
        attributes=[
            AttributeConstraint(id="a:component", variable="component", attribute="code", op="eq", value="X"),
            AttributeConstraint(id="a:cert", variable="cert", attribute="code", op="eq", value="Y"),
            AttributeConstraint(id="a:status", variable="cert", attribute="status", op="eq", value="active"),
            AttributeConstraint(id="a:lead", variable="supplier", attribute="lead_time_days", op="lte", value=30),
            AttributeConstraint(id="a:price", variable="supplier", attribute="unit_price", op="lte", value=500),
        ],
    )


def build_graph(*, include_cert: bool = True, conflict: bool = False, wrong_cert_first: bool = False) -> EvidenceGraph:
    graph = EvidenceGraph()
    for node in [
        Node(id="supplier:a", type="Supplier", attributes={"name": "Alpha", "lead_time_days": 22, "unit_price": 430}),
        Node(id="supplier:b", type="Supplier", attributes={"name": "Beta", "lead_time_days": 45, "unit_price": 390}),
        Node(id="component:x", type="Component", attributes={"code": "X"}),
        Node(id="cert:y", type="Certification", attributes={"code": "Y", "status": "active"}),
        Node(id="cert:z", type="Certification", attributes={"code": "Z", "status": "active"}),
        Node(id="cert:y-revoked", type="Certification", attributes={"code": "Y", "status": "revoked"}),
    ]:
        graph.add_node(node)

    graph.add_edge(Edge(id="e:prod:a", source="supplier:a", target="component:x", type="PRODUCES", evidence=[ev("ev:prod:a")]))
    graph.add_edge(Edge(id="e:prod:b", source="supplier:b", target="component:x", type="PRODUCES", evidence=[ev("ev:prod:b")]))

    if include_cert:
        if wrong_cert_first:
            graph.add_edge(Edge(id="e:cert:z", source="supplier:a", target="cert:z", type="CERTIFIED_WITH", evidence=[ev("ev:cert:z")]))
        graph.add_edge(
            Edge(
                id="e:cert:y",
                source="supplier:a",
                target="cert:y",
                type="CERTIFIED_WITH",
                attributes={"claim_key": "cert:Y:status", "claim_value": "active"},
                evidence=[ev("ev:cert:y")],
            )
        )
        graph.add_edge(
            Edge(
                id="e:cert:y:b",
                source="supplier:b",
                target="cert:y",
                type="CERTIFIED_WITH",
                attributes={"claim_key": "cert:Y:status", "claim_value": "active"},
                evidence=[ev("ev:cert:y:b")],
            )
        )
    if conflict:
        graph.add_edge(
            Edge(
                id="e:cert:y-revoked",
                source="supplier:a",
                target="cert:y-revoked",
                type="CERTIFIED_WITH",
                attributes={"claim_key": "cert:Y:status", "claim_value": "revoked"},
                evidence=[ev("ev:cert:y-revoked")],
            )
        )
    return graph


def test_resolver_closes_supplier_pattern_with_provenance():
    result = Resolver(build_graph()).resolve(supplier_query(), RuntimePolicy(initial_arousal=2, max_arousal=2))

    assert result.state == ResolutionState.RESOLVED
    assert result.bindings["supplier"] == "supplier:a"
    assert result.constraint_coverage == 1.0
    assert set(result.evidence_ids) == {"ev:prod:a", "ev:cert:y"}
    assert result.unresolved_constraints == []
    assert result.violated_constraints == []


def test_resolver_rejects_candidate_that_violates_hard_constraint():
    graph = build_graph()
    graph.get_node("supplier:a").attributes["lead_time_days"] = 40

    result = Resolver(graph).resolve(supplier_query(), RuntimePolicy(initial_arousal=2, max_arousal=2))

    assert result.state == ResolutionState.UNRESOLVABLE
    assert "a:lead" in result.violated_constraints


def test_resolver_returns_partial_when_required_relation_is_missing():
    result = Resolver(build_graph(include_cert=False)).resolve(
        supplier_query(), RuntimePolicy(initial_arousal=2, max_arousal=2)
    )

    assert result.state == ResolutionState.PARTIAL
    assert "r:certified" in result.unresolved_constraints
    assert result.constraint_coverage < 1.0


def test_resolver_surfaces_conflicting_evidence_instead_of_silent_resolution():
    result = Resolver(build_graph(conflict=True)).resolve(
        supplier_query(), RuntimePolicy(initial_arousal=3, max_arousal=3)
    )

    assert result.state == ResolutionState.CONTRADICTED
    assert any("cert:Y:status" in item for item in result.contradictions)
    assert "ev:cert:y-revoked" in result.evidence_ids


def test_resolver_progressively_widens_candidate_window():
    result = Resolver(build_graph(wrong_cert_first=True)).resolve(
        supplier_query(), RuntimePolicy(initial_arousal=1, max_arousal=2, max_expansions=50)
    )

    assert result.state == ResolutionState.RESOLVED
    assert result.telemetry["widening_steps"] == 0
    assert result.telemetry["planner_operations"]


def test_resolver_stops_on_expansion_budget_and_abstains():
    result = Resolver(build_graph()).resolve(
        supplier_query(), RuntimePolicy(initial_arousal=2, max_arousal=2, max_expansions=1)
    )

    assert result.state == ResolutionState.BUDGET_EXHAUSTED
    assert result.telemetry["expansion_budget_exhausted"] is True
    assert result.constraint_coverage < 1.0
