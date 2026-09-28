from __future__ import annotations

import io
import json
import sqlite3
from datetime import UTC, datetime
from urllib.error import HTTPError

import pytest

from ctd.graph import EvidenceGraph
from ctd.models import Edge, Evidence, Node
from ctd.persistence import SQLiteStore
from ctd.providers import GraphProvider, ProviderCost, ProviderResult
from ctd.providers_ext import (
    DocumentProvider,
    FederatedProvider,
    Neo4jProvider,
    RemoteAPIProvider,
    SQLProvider,
    VectorProvider,
    VectorRecord,
)

NOW = datetime(2026, 9, 12, tzinfo=UTC)


def _graph(tenant: str = "acme", price: int = 420) -> EvidenceGraph:
    graph = EvidenceGraph()
    graph.add_node(Node(id=f"supplier:{tenant}", type="Supplier", attributes={"tenant_id": tenant, "price": price}))
    graph.add_node(Node(id=f"component:{tenant}", type="Component", attributes={"tenant_id": tenant}))
    graph.add_edge(
        Edge(
            id=f"edge:{tenant}",
            source=f"supplier:{tenant}",
            target=f"component:{tenant}",
            type="PRODUCES",
            attributes={"tenant_id": tenant},
            evidence=[Evidence(id=f"ev:{tenant}", source_id="erp", source_type="erp", observed_at=NOW, confidence=1, trust=1)],
        )
    )
    return graph


def test_graph_provider_enforces_tenant_before_candidate_visibility():
    graph = EvidenceGraph()
    for tenant in ["acme", "other"]:
        source = _graph(tenant)
        for node in source.snapshot()["nodes"]:
            graph.add_node(Node.model_validate(node))
        for edge in source.snapshot()["edges"]:
            graph.add_edge(Edge.model_validate(edge))

    provider = GraphProvider(graph, tenant_id="acme")

    assert [node.id for node in provider.nodes_of_type("Supplier").items] == ["supplier:acme"]
    assert provider.get_node("supplier:other").items == []
    assert provider.outgoing("supplier:other").items == []


def test_sql_provider_reads_persistent_store_and_enforces_tenant(tmp_path):
    store = SQLiteStore(tmp_path / "ctd.db")
    for tenant in ["acme", "other"]:
        graph = _graph(tenant)
        for node in graph.snapshot()["nodes"]:
            store.save_node(Node.model_validate(node))
        for edge in graph.snapshot()["edges"]:
            store.save_edge(Edge.model_validate(edge))

    provider = SQLProvider(store._conn, tenant_id="acme")

    assert [node.id for node in provider.nodes_of_type("Supplier").items] == ["supplier:acme"]
    assert provider.node_count("Supplier") == 1
    assert provider.outgoing("supplier:acme", "PRODUCES").items[0].target == "component:acme"
    assert provider.outgoing("supplier:other", "PRODUCES").items == []
    assert provider.health().status == "healthy"


def test_document_provider_loads_jsonl_and_rejects_malformed_records(tmp_path):
    path = tmp_path / "evidence.jsonl"
    node_a = Node(id="supplier:a", type="Supplier").model_dump(mode="json")
    node_b = Node(id="component:x", type="Component").model_dump(mode="json")
    edge = Edge(
        id="edge:1",
        source="supplier:a",
        target="component:x",
        type="PRODUCES",
        evidence=[Evidence(id="ev:1", source_id="s", source_type="erp", observed_at=NOW, confidence=1, trust=1)],
    ).model_dump(mode="json")
    path.write_text("\n".join([json.dumps({"kind": "node", **node_a}), json.dumps({"kind": "node", **node_b}), json.dumps({"kind": "edge", **edge})]))

    provider = DocumentProvider([path])
    assert provider.get_node("supplier:a").items[0].type == "Supplier"
    assert provider.outgoing("supplier:a", "PRODUCES").items[0].id == "edge:1"

    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps({"kind": "unknown", "id": "x"}))
    with pytest.raises(ValueError):
        DocumentProvider([bad])


def test_vector_provider_cosine_search_is_deterministic_and_validates_dimensions():
    provider = VectorProvider(
        [
            VectorRecord(id="b", node_type="Supplier", vector=[1.0, 0.0], metadata={}),
            VectorRecord(id="a", node_type="Supplier", vector=[1.0, 0.0], metadata={}),
            VectorRecord(id="c", node_type="Component", vector=[0.0, 1.0], metadata={}),
            VectorRecord(id="z", node_type="Supplier", vector=[0.0, 0.0], metadata={}),
        ]
    )

    matches = provider.search([1.0, 0.0], node_type="Supplier", limit=4)
    assert [match.id for match in matches[:2]] == ["a", "b"]
    assert matches[-1].id == "z"
    assert matches[-1].score == 0.0
    with pytest.raises(ValueError):
        provider.search([1.0, 0.0, 0.0])


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200):
        self._body = io.BytesIO(body)
        self.status = status

    def read(self, size=-1):
        return self._body.read(size)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeOpener:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def open(self, request, timeout):
        url = request.full_url
        self.calls.append((url, timeout))
        value = self.routes.get(url)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise HTTPError(url, 404, "missing", {}, None)
        return FakeResponse(json.dumps(value).encode())


def test_remote_api_provider_validates_payload_timeout_and_size():
    node = Node(id="supplier:a", type="Supplier").model_dump(mode="json")
    opener = FakeOpener({"https://example.test/nodes/supplier%3Aa": {"items": [node]}})
    provider = RemoteAPIProvider("https://example.test", opener=opener, timeout_s=0.5, max_bytes=1024)

    result = provider.get_node("supplier:a")
    assert result.items[0].id == "supplier:a"
    assert opener.calls[0][1] == 0.5

    oversized = FakeOpener({"https://example.test/nodes/supplier%3Aa": {"items": [node], "padding": "x" * 500}})
    with pytest.raises(RuntimeError, match="response too large"):
        RemoteAPIProvider("https://example.test", opener=oversized, max_bytes=100).get_node("supplier:a")

    malformed = FakeOpener({"https://example.test/nodes/supplier%3Aa": {"items": [{"bad": True}]}})
    with pytest.raises(RuntimeError, match="invalid provider payload"):
        RemoteAPIProvider("https://example.test", opener=malformed).get_node("supplier:a")


class FakeNeo4jSession:
    def __init__(self, driver):
        self.driver = driver

    def run(self, query, params=None):
        self.driver.calls.append((query, params or {}))
        return self.driver.rows

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeNeo4jDriver:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.calls = []

    def session(self):
        return FakeNeo4jSession(self)


def test_neo4j_provider_parameterizes_values_and_validates_identifiers():
    driver = FakeNeo4jDriver([{"node": {"id": "supplier:a", "type": "Supplier", "attributes": {}}}])
    provider = Neo4jProvider(driver=driver)

    result = provider.get_node("supplier:a")
    assert result.items[0].id == "supplier:a"
    query, params = driver.calls[-1]
    assert "$node_id" in query
    assert params["node_id"] == "supplier:a"

    with pytest.raises(ValueError):
        provider.nodes_of_type("Supplier`) MATCH (x) RETURN x //")


def test_federated_provider_deduplicates_stably_sums_cost_and_can_tolerate_failure():
    first = GraphProvider(_graph("acme", 420))
    first.name = "first"
    second_graph = _graph("acme", 999)
    second = GraphProvider(second_graph)
    second.name = "second"

    class Broken:
        name = "broken"
        def nodes_of_type(self, *args, **kwargs): raise RuntimeError("down")
        def get_node(self, *args, **kwargs): raise RuntimeError("down")
        def outgoing(self, *args, **kwargs): raise RuntimeError("down")
        def incoming(self, *args, **kwargs): raise RuntimeError("down")
        def node_count(self, *args, **kwargs): raise RuntimeError("down")
        def distinct_attribute_count(self, *args, **kwargs): raise RuntimeError("down")

    provider = FederatedProvider([first, second, Broken()], fail_fast=False)
    result = provider.nodes_of_type("Supplier")

    assert [node.id for node in result.items] == ["supplier:acme"]
    assert result.items[0].attributes["price"] == 420
    assert result.cost.provider_calls >= 2
    assert provider.health().status == "degraded"

    strict = FederatedProvider([first, Broken()], fail_fast=True)
    with pytest.raises(RuntimeError, match="down"):
        strict.nodes_of_type("Supplier")


def test_remote_and_neo4j_providers_apply_tenant_filter_before_returning_candidates():
    remote_items = {
        "items": [
            Node(id="supplier:acme", type="Supplier", attributes={"tenant_id": "acme"}).model_dump(mode="json"),
            Node(id="supplier:other", type="Supplier", attributes={"tenant_id": "other"}).model_dump(mode="json"),
        ]
    }
    opener = FakeOpener({"https://example.test/nodes?type=Supplier": remote_items})
    remote = RemoteAPIProvider("https://example.test", opener=opener, tenant_id="acme")
    remote_result = remote.nodes_of_type("Supplier")
    assert [item.id for item in remote_result.items] == ["supplier:acme"]
    assert remote_result.cost.authorization_pruned == 1

    driver = FakeNeo4jDriver(
        [
            {"node": {"id": "supplier:acme", "type": "Supplier", "attributes": {"tenant_id": "acme"}}},
            {"node": {"id": "supplier:other", "type": "Supplier", "attributes": {"tenant_id": "other"}}},
        ]
    )
    neo = Neo4jProvider(driver=driver, tenant_id="acme")
    neo_result = neo.nodes_of_type("Supplier")
    assert [item.id for item in neo_result.items] == ["supplier:acme"]
    assert neo_result.cost.authorization_pruned == 1


def test_neo4j_optional_dependency_failure_is_explicit(monkeypatch):
    import builtins

    original_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "neo4j" or name.startswith("neo4j."):
            raise ImportError("blocked")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    with pytest.raises(RuntimeError, match="neo4j"):
        Neo4jProvider(uri="neo4j://localhost", username="neo4j", password="secret")
