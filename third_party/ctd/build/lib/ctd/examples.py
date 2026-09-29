from __future__ import annotations

from datetime import UTC, datetime

from .controller import RuntimePolicy
from .graph import EvidenceGraph
from .models import (
    AttributeConstraint,
    Edge,
    Evidence,
    Node,
    QueryConstraintGraph,
    RelationConstraint,
    Variable,
)


DEMO_AS_OF = datetime(2026, 9, 12, tzinfo=UTC)


def _evidence(evidence_id: str, source_type: str = "erp") -> Evidence:
    return Evidence(
        id=evidence_id,
        source_id=evidence_id.replace("ev:", "src:"),
        source_type=source_type,
        observed_at=DEMO_AS_OF,
        confidence=0.98,
        trust=0.95,
        independence_group=evidence_id.replace("ev:", "source-family:"),
        extraction_method="direct",
        direct=True,
    )


def build_supplier_demo() -> tuple[EvidenceGraph, QueryConstraintGraph, RuntimePolicy]:
    """Build a supplier fixture demonstrating planned constraint resolution."""
    graph = EvidenceGraph()
    nodes = [
        Node(
            id="supplier:a",
            type="Supplier",
            attributes={"name": "Alpha Components", "lead_time_days": 22, "unit_price": 430},
        ),
        Node(
            id="supplier:b",
            type="Supplier",
            attributes={"name": "Beta Industrial", "lead_time_days": 45, "unit_price": 390},
        ),
        Node(id="component:x", type="Component", attributes={"code": "X"}),
        Node(id="cert:z", type="Certification", attributes={"code": "Z", "status": "active"}),
        Node(id="cert:y", type="Certification", attributes={"code": "Y", "status": "active"}),
    ]
    for node in nodes:
        graph.add_node(node)

    graph.add_edge(
        Edge(
            id="edge:alpha-produces-x",
            source="supplier:a",
            target="component:x",
            type="PRODUCES",
            evidence=[_evidence("ev:alpha-produces-x")],
        )
    )
    graph.add_edge(
        Edge(
            id="edge:beta-produces-x",
            source="supplier:b",
            target="component:x",
            type="PRODUCES",
            evidence=[_evidence("ev:beta-produces-x")],
        )
    )
    # Deliberately inserted first. The v2 planner can avoid this irrelevant branch by
    # applying selective certification attributes before relation traversal.
    graph.add_edge(
        Edge(
            id="edge:alpha-cert-z",
            source="supplier:a",
            target="cert:z",
            type="CERTIFIED_WITH",
            evidence=[_evidence("ev:alpha-cert-z", "cert-registry")],
        )
    )
    graph.add_edge(
        Edge(
            id="edge:alpha-cert-y",
            source="supplier:a",
            target="cert:y",
            type="CERTIFIED_WITH",
            attributes={"claim_key": "cert:Y:status", "claim_value": "active"},
            evidence=[_evidence("ev:alpha-cert-y", "cert-registry")],
        )
    )
    graph.add_edge(
        Edge(
            id="edge:beta-cert-y",
            source="supplier:b",
            target="cert:y",
            type="CERTIFIED_WITH",
            attributes={"claim_key": "cert:Y:status", "claim_value": "active"},
            evidence=[_evidence("ev:beta-cert-y", "cert-registry")],
        )
    )

    query = QueryConstraintGraph(
        as_of=DEMO_AS_OF,
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
            AttributeConstraint(
                id="a:component-code", variable="component", attribute="code", op="eq", value="X"
            ),
            AttributeConstraint(id="a:cert-code", variable="cert", attribute="code", op="eq", value="Y"),
            AttributeConstraint(
                id="a:cert-status", variable="cert", attribute="status", op="eq", value="active"
            ),
            AttributeConstraint(
                id="a:lead-time", variable="supplier", attribute="lead_time_days", op="lte", value=30
            ),
            AttributeConstraint(
                id="a:unit-price", variable="supplier", attribute="unit_price", op="lte", value=500
            ),
        ],
        objective="find compliant supplier for component X",
    )
    policy = RuntimePolicy(
        initial_arousal=1,
        max_arousal=2,
        max_expansions=50,
        max_depth=4,
        deadline_ms=1000,
        min_confidence=0.90,
        min_trust=0.90,
    )
    return graph, query, policy
