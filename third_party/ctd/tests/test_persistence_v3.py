from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ctd.models import Edge, Evidence, Node
from ctd.persistence import AdvisorStat, PostgresStore, SQLiteStore

NOW = datetime(2026, 9, 12, tzinfo=UTC)


def _node(node_id: str = "supplier:a") -> Node:
    return Node(id=node_id, type="Supplier", attributes={"tenant_id": "acme", "price": 420})


def _edge() -> Edge:
    return Edge(
        id="edge:1",
        source="supplier:a",
        target="component:x",
        type="PRODUCES",
        attributes={"tenant_id": "acme"},
        evidence=[
            Evidence(
                id="ev:1",
                source_id="erp",
                source_type="erp",
                observed_at=NOW,
                confidence=0.95,
                trust=0.9,
            )
        ],
    )


def test_sqlite_store_is_durable_across_instances(tmp_path):
    path = tmp_path / "ctd.db"
    store = SQLiteStore(path)
    store.save_node(_node())
    store.save_node(Node(id="component:x", type="Component", attributes={"tenant_id": "acme"}))
    store.save_edge(_edge())
    store.save_evidence(_edge().evidence[0])
    store.save_claim("supplier:a:status", {"value": "active"})
    store.save_execution("run:1", {"state": "RESOLVED", "tenant_id": "acme"})
    store.save_profile("supplier", {"total_runs": 2})
    store.save_advisor_stat(AdvisorStat(query_class="supplier", operation_id="price", successes=2, reward_sum=1.5))
    store.close()

    reopened = SQLiteStore(path)
    assert reopened.get_node("supplier:a").attributes["price"] == 420
    assert reopened.get_edge("edge:1").evidence[0].id == "ev:1"
    assert reopened.get_evidence("ev:1").source_id == "erp"
    assert reopened.get_claims("supplier:a:status")[0]["value"] == "active"
    assert reopened.get_execution("run:1")["state"] == "RESOLVED"
    assert reopened.get_profile("supplier")["total_runs"] == 2
    assert reopened.get_advisor_stat("supplier", "price").successes == 2
    graph = reopened.load_graph()
    assert graph.get_node("component:x") is not None
    assert graph.outgoing("supplier:a", "PRODUCES")[0].id == "edge:1"
    assert reopened.ping() is True
    reopened.close()


def test_sqlite_transaction_rolls_back_and_execution_ids_are_immutable(tmp_path):
    store = SQLiteStore(tmp_path / "ctd.db")

    with pytest.raises(RuntimeError):
        with store.transaction():
            store.save_node(_node("supplier:rollback"))
            raise RuntimeError("abort")

    assert store.get_node("supplier:rollback") is None

    store.save_execution("run:1", {"state": "RESOLVED"})
    with pytest.raises(ValueError):
        store.save_execution("run:1", {"state": "PARTIAL"})
    assert store.get_execution("run:1")["state"] == "RESOLVED"


def test_sqlite_repository_adapters_satisfy_existing_manager_contracts(tmp_path):
    store = SQLiteStore(tmp_path / "ctd.db")
    store.executions.save("run:2", {"state": "PARTIAL"})
    store.profiles.save("q", {"total_runs": 1})

    assert store.executions.get("run:2")["state"] == "PARTIAL"
    assert store.executions.list()[0]["state"] == "PARTIAL"
    assert store.profiles.get("q")["total_runs"] == 1
    assert store.profiles.list()[0]["total_runs"] == 1


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = []

    def execute(self, sql, params=None):
        self.connection.calls.append((sql, params))
        return self

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeConnection:
    def __init__(self):
        self.calls = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


def test_postgres_store_uses_parameterized_values_and_injected_connection():
    connection = FakeConnection()
    store = PostgresStore("postgresql://unused", connection_factory=lambda: connection)
    malicious_id = "supplier:'; DROP TABLE ctd_nodes; --"
    store.save_node(_node(malicious_id))

    insert_calls = [(sql, params) for sql, params in connection.calls if "INSERT INTO ctd_nodes" in sql]
    assert insert_calls
    sql, params = insert_calls[-1]
    assert "%s" in sql
    assert malicious_id not in sql
    assert params[0] == malicious_id
    assert connection.commits >= 1


def test_postgres_store_exposes_same_manager_repository_contract_as_sqlite():
    connection = FakeConnection()
    store = PostgresStore("postgresql://unused", connection_factory=lambda: connection)

    assert hasattr(store, "executions")
    assert hasattr(store, "profiles")
    assert hasattr(store, "advisor")
    assert callable(store.get_execution)
    assert callable(store.list_executions)
    assert callable(store.get_profile)
    assert callable(store.list_profiles)
    assert callable(store.get_advisor_stat)
    assert callable(store.list_advisor_stats)
    assert callable(store.load_graph)


def test_postgres_optional_dependency_failure_is_explicit(monkeypatch):
    import builtins

    original_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "psycopg" or name.startswith("psycopg."):
            raise ImportError("blocked")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    with pytest.raises(RuntimeError, match="postgres"):
        PostgresStore("postgresql://unused")
