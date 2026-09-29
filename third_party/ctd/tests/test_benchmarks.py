from datetime import UTC, datetime

from ctd.controller import RuntimePolicy
from ctd.graph import EvidenceGraph
from ctd.models import AttributeConstraint, Edge, Evidence, Node, QueryConstraintGraph, RelationConstraint, Variable
from ctd.resolver import Resolver

AS_OF = datetime(2026, 9, 12, tzinfo=UTC)


def test_selective_query_avoids_cartesian_candidate_explosion():
    graph = EvidenceGraph()
    for i in range(40):
        graph.add_node(Node(id=f"s:{i}", type="Supplier", attributes={"tier": "preferred" if i == 7 else "other"}))
    for i in range(40):
        graph.add_node(Node(id=f"c:{i}", type="Component", attributes={"code": "X" if i == 3 else f"C{i}"}))
    evidence = Evidence(id="ev", source_id="src", source_type="registry", observed_at=AS_OF, confidence=0.99, trust=0.99)
    graph.add_edge(Edge(id="e", source="s:7", target="c:3", type="PRODUCES", evidence=[evidence]))
    query = QueryConstraintGraph(
        as_of=AS_OF,
        variables=[Variable(name="s", node_type="Supplier"), Variable(name="c", node_type="Component")],
        relations=[RelationConstraint(id="r", subject_var="s", relation="PRODUCES", object_var="c")],
        attributes=[
            AttributeConstraint(id="a:s", variable="s", attribute="tier", op="eq", value="preferred"),
            AttributeConstraint(id="a:c", variable="c", attribute="code", op="eq", value="X"),
        ],
    )

    result = Resolver(graph).resolve(query, RuntimePolicy(initial_arousal=2, max_arousal=2))

    naive_cartesian_candidates = 40 * 40
    assert result.state == "RESOLVED"
    assert result.telemetry["candidates_created"] < naive_cartesian_candidates // 100
    assert result.telemetry["edges_traversed"] == 1
