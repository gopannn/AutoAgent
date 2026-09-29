import json
import sqlite3
from datetime import datetime, timezone

from ctd.graph import EvidenceGraph
from ctd.models import AttributeConstraint, Node, QueryConstraintGraph, Variable
from ctd.providers import GraphProvider
from ctd.providers_ext import SQLProvider
from ctd.resolver import Resolver

NOW = datetime(2026, 9, 12, tzinfo=timezone.utc)


def constraints():
    return [AttributeConstraint(id="price", variable="s", attribute="price", op="lte", value=10)]


def test_graph_provider_prefilter_reduces_candidates_before_resolver_materialization():
    graph = EvidenceGraph()
    for idx, price in enumerate([5, 8, 11, 20, 7], start=1):
        graph.add_node(Node(id=f"s{idx}", type="Supplier", attributes={"price": price}))
    provider = GraphProvider(graph)

    result = provider.prefilter_nodes("Supplier", constraints(), limit=100)

    assert [node.id for node in result.items] == ["s1", "s2", "s5"]
    assert result.cost.items_examined == 5


def test_resolver_records_provider_prefilter_reduction_and_revalidates():
    graph = EvidenceGraph()
    for idx, price in enumerate([5, 8, 11, 20, 7], start=1):
        graph.add_node(Node(id=f"s{idx}", type="Supplier", attributes={"price": price}))
    query = QueryConstraintGraph(
        variables=[Variable(name="s", node_type="Supplier")],
        attributes=constraints(),
        as_of=NOW,
        top_k=3,
    )

    result = Resolver(graph).resolve(query)

    assert result.telemetry["prefilter_candidates_examined"] >= 5
    assert result.telemetry["prefilter_candidates_returned"] == 3
    assert result.telemetry["prefilter_candidates_pruned"] >= 2
    assert result.bindings["s"] in {"s1", "s2", "s5"}


def test_sql_provider_prefilter_uses_parameterized_sqlite_json_predicates():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE ctd_nodes(id TEXT PRIMARY KEY, type TEXT, payload_json TEXT)")
    for idx, price in enumerate([5, 15, 7], start=1):
        node = Node(id=f"s{idx}", type="Supplier", attributes={"price": price})
        conn.execute("INSERT INTO ctd_nodes VALUES(?,?,?)", (node.id, node.type, node.model_dump_json()))
    conn.commit()
    provider = SQLProvider(conn)

    result = provider.prefilter_nodes("Supplier", constraints(), limit=10)

    assert [node.id for node in result.items] == ["s1", "s3"]
    assert result.cost.items_examined == 2


def test_sql_provider_falls_back_safely_for_unsupported_in_operator():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE ctd_nodes(id TEXT PRIMARY KEY, type TEXT, payload_json TEXT)")
    for idx, category in enumerate(["A", "B", "C"], start=1):
        node = Node(id=f"s{idx}", type="Supplier", attributes={"category": category})
        conn.execute("INSERT INTO ctd_nodes VALUES(?,?,?)", (node.id, node.type, node.model_dump_json()))
    conn.commit()
    provider = SQLProvider(conn)
    cs = [AttributeConstraint(id="cat", variable="s", attribute="category", op="in", value=["A", "C"])]

    result = provider.prefilter_nodes("Supplier", cs, limit=10)

    assert [node.id for node in result.items] == ["s1", "s3"]
