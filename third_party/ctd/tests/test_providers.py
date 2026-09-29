from datetime import UTC, datetime

from ctd.graph import EvidenceGraph
from ctd.models import Edge, Evidence, Node
from ctd.providers import GraphProvider, MemoryProvider
from ctd.repositories import InMemoryExecutionRepository, InMemoryQueryProfileRepository

NOW = datetime(2026, 9, 12, tzinfo=UTC)


def graph_fixture() -> EvidenceGraph:
    graph = EvidenceGraph()
    for node in [
        Node(id="supplier:a", type="Supplier"),
        Node(id="supplier:b", type="Supplier"),
        Node(id="component:x", type="Component"),
    ]:
        graph.add_node(node)
    evidence = Evidence(
        id="ev:1", source_id="src:1", source_type="erp", observed_at=NOW, confidence=1, trust=1
    )
    graph.add_edge(Edge(id="e:a", source="supplier:a", target="component:x", type="PRODUCES", evidence=[evidence]))
    graph.add_edge(Edge(id="e:b", source="supplier:b", target="component:x", type="PRODUCES", evidence=[evidence]))
    return graph


def test_graph_provider_filters_authorization_and_reports_cost():
    provider = GraphProvider(graph_fixture(), authorization_scope={"supplier:a", "component:x"})

    result = provider.outgoing("supplier:a", "PRODUCES", limit=10)
    hidden = provider.outgoing("supplier:b", "PRODUCES", limit=10)

    assert [edge.id for edge in result.items] == ["e:a"]
    assert result.cost.provider_calls == 1
    assert result.cost.items_examined == 1
    assert hidden.items == []


def test_graph_provider_exposes_deterministic_statistics_for_planning():
    provider = GraphProvider(graph_fixture())

    assert provider.node_count("Supplier") == 2
    assert [node.id for node in provider.nodes_of_type("Supplier", limit=1).items] == ["supplier:a"]


def test_memory_provider_is_graph_compatible_for_tests():
    provider = MemoryProvider.from_graph(graph_fixture())

    assert provider.get_node("component:x").items[0].type == "Component"
    assert provider.incoming("component:x", "PRODUCES", limit=5).cost.provider_calls == 1


def test_execution_and_profile_repositories_round_trip_records():
    executions = InMemoryExecutionRepository()
    profiles = InMemoryQueryProfileRepository()

    executions.save("run:1", {"state": "RESOLVED"})
    profiles.save("supplier", {"successful_runs": 2})

    assert executions.get("run:1")["state"] == "RESOLVED"
    assert profiles.get("supplier")["successful_runs"] == 2
