from datetime import UTC, datetime

from ctd.graph import EvidenceGraph
from ctd.models import Edge, Evidence, Node


def test_graph_preserves_typed_nodes_edges_and_evidence():
    graph = EvidenceGraph()
    supplier = Node(id="supplier:1", type="Supplier", attributes={"name": "Acme"})
    component = Node(id="component:x", type="Component", attributes={"code": "X"})
    evidence = Evidence(
        id="ev:contract-1",
        source_id="contract-1",
        source_type="erp",
        observed_at=datetime(2026, 9, 12, tzinfo=UTC),
        confidence=0.99,
        trust=0.95,
    )
    edge = Edge(
        id="edge:produces",
        source="supplier:1",
        target="component:x",
        type="PRODUCES",
        evidence=[evidence],
    )

    graph.add_node(supplier)
    graph.add_node(component)
    graph.add_edge(edge)

    assert [node.id for node in graph.nodes_of_type("Supplier")] == ["supplier:1"]
    assert graph.outgoing("supplier:1", "PRODUCES")[0].target == "component:x"
    assert graph.incoming("component:x", "PRODUCES")[0].source == "supplier:1"
    assert graph.outgoing("supplier:1")[0].evidence[0].id == "ev:contract-1"


def test_graph_snapshot_is_transport_safe():
    graph = EvidenceGraph()
    graph.add_node(Node(id="n1", type="Thing", attributes={"score": 3}))

    snapshot = graph.snapshot()

    assert snapshot["nodes"][0]["id"] == "n1"
    assert snapshot["edges"] == []
