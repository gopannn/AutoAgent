from __future__ import annotations

from pathlib import Path

from ctd.auth import Principal
from ctd.config import EngineSettings
from ctd.examples import DEMO_AS_OF, build_supplier_demo
from ctd.models import ResolutionState
from ctd.service import ProductionEngine
from ctd.tracing import InMemoryTraceSink


def analyst() -> Principal:
    return Principal(
        subject="analyst:1",
        roles={"analyst"},
        scopes={"resolve:read", "resolve:advanced"},
        security_labels={"PUBLIC", "INTERNAL", "CONFIDENTIAL"},
        auth_method="internal",
    )


def test_production_engine_compiles_resolves_persists_profiles_advisor_and_trace(tmp_path):
    graph, _, _ = build_supplier_demo()
    trace_sink = InMemoryTraceSink()
    settings = EngineSettings(persistence_backend="sqlite", sqlite_path=str(tmp_path / "ctd.db"))
    engine = ProductionEngine(settings=settings, graph=graph, trace_sink=trace_sink)

    result = engine.resolve(
        text=(
            "Find a supplier that produces component X, certification is ISO9001, "
            "lead time at most 30 days, price under 500"
        ),
        as_of=DEMO_AS_OF,
        principal=analyst(),
    )

    assert result.state == ResolutionState.RESOLVED
    assert result.bindings["supplier"] == "supplier:a"
    assert result.execution_id
    record = engine.execution(result.execution_id)
    assert record["result"]["state"] == "RESOLVED"
    assert record["input"]["text"].startswith("Find a supplier")
    assert record["effective_policy"]["tenant_id"] is None
    assert record["planner_advice"]["advisor_version"]
    assert engine.profiles.get(record["query_class"])["total_runs"] == 1
    assert engine.store.list_advisor_stats(record["query_class"])
    assert any(span.name == "ctd.resolve" for span in trace_sink.spans())
    assert result.trace_summary["trace_id"]


def test_production_engine_restart_can_verify_durable_replay(tmp_path):
    graph, query, policy = build_supplier_demo()
    path = tmp_path / "ctd.db"
    settings = EngineSettings(persistence_backend="sqlite", sqlite_path=str(path))
    engine = ProductionEngine(settings=settings, graph=graph)
    original = engine.resolve(query=query, policy=policy, principal=analyst())
    execution_id = original.execution_id
    engine.close()

    restarted = ProductionEngine(settings=settings, graph=graph)
    verification = restarted.verify_replay(execution_id)

    assert verification["supported"] is True
    assert verification["match"] is True
    assert verification["expected_fingerprint"] == verification["replay_fingerprint"]


def test_production_engine_readiness_reports_store_and_provider_health(tmp_path):
    graph, _, _ = build_supplier_demo()
    engine = ProductionEngine(
        settings=EngineSettings(persistence_backend="sqlite", sqlite_path=str(tmp_path / "ctd.db")),
        graph=graph,
    )

    readiness = engine.readiness()

    assert readiness["ready"] is True
    assert readiness["store"]["status"] == "healthy"
    assert readiness["providers"][0]["status"] == "healthy"


def test_production_engine_async_path_resolves_structured_query(tmp_path):
    graph, query, policy = build_supplier_demo()
    engine = ProductionEngine(
        settings=EngineSettings(persistence_backend="sqlite", sqlite_path=str(tmp_path / "ctd.db")),
        graph=graph,
    )

    result = __import__("asyncio").run(
        engine.resolve_async(query=query, policy=policy, principal=analyst())
    )

    assert result.state == ResolutionState.RESOLVED
    assert result.execution_id
    assert engine.execution(result.execution_id)["mode"] == "async"


def test_production_metrics_cover_provider_latency_advisor_replay_auth_and_async(tmp_path):
    from ctd.models import AttributeConstraint, QueryConstraintGraph, Variable
    from ctd.controller import RuntimePolicy
    import asyncio

    graph, query, policy = build_supplier_demo()
    engine = ProductionEngine(
        settings=EngineSettings(persistence_backend="sqlite", sqlite_path=str(tmp_path / "metrics.db")),
        graph=graph,
    )
    sync = engine.resolve(query=query, policy=policy, principal=analyst())
    assert sync.execution_id
    assert engine.verify_replay(sync.execution_id)["match"] is True
    engine.observe_auth("api_key", success=True)
    engine.observe_auth("api_key", success=False)

    disconnected = QueryConstraintGraph(
        as_of=DEMO_AS_OF,
        variables=[
            Variable(name="fast", node_type="Supplier"),
            Variable(name="cheap", node_type="Supplier"),
        ],
        attributes=[
            AttributeConstraint(id="fast-limit", variable="fast", attribute="lead_time_days", op="lte", value=30),
            AttributeConstraint(id="cheap-limit", variable="cheap", attribute="unit_price", op="lte", value=500),
        ],
    )
    async_result = asyncio.run(
        engine.resolve_async(query=disconnected, policy=RuntimePolicy(), principal=analyst())
    )
    assert async_result.telemetry["async_components"] == 2

    metrics = engine.metrics()
    assert metrics["auth"]["success_by_method"]["api_key"] == 1
    assert metrics["auth"]["failure_by_method"]["api_key"] == 1
    assert metrics["replay_verification_rate"] == 1.0
    assert metrics["advisor"]["observations"] > 0
    assert metrics["async"]["components_total"] >= 2
    assert metrics["provider_latency_ms"]["graph"]["samples"] > 0
    assert metrics["store_health"]["status"] == "healthy"
    assert metrics["provider_health"][0]["status"] == "healthy"
    assert "failure_reasons" in metrics


def test_tenant_execution_snapshot_never_persists_cross_tenant_graph_data(tmp_path):
    from ctd.graph import EvidenceGraph
    from ctd.models import AttributeConstraint, Node, QueryConstraintGraph, Variable
    from ctd.controller import RuntimePolicy

    graph = EvidenceGraph()
    graph.add_node(Node(id="supplier:acme", type="Supplier", attributes={"tenant_id": "acme", "unit_price": 100}))
    graph.add_node(Node(id="supplier:other", type="Supplier", attributes={"tenant_id": "other", "unit_price": 90}))
    engine = ProductionEngine(
        settings=EngineSettings(persistence_backend="sqlite", sqlite_path=str(tmp_path / "tenant.db")),
        graph=graph,
    )
    principal = Principal(
        subject="acme-analyst",
        tenant_id="acme",
        roles={"analyst"},
        scopes={"resolve:read", "resolve:advanced"},
        security_labels={"PUBLIC", "INTERNAL", "CONFIDENTIAL"},
        auth_method="internal",
    )
    query = QueryConstraintGraph(
        as_of=DEMO_AS_OF,
        variables=[Variable(name="supplier", node_type="Supplier")],
        attributes=[AttributeConstraint(id="price", variable="supplier", attribute="unit_price", op="lte", value=500)],
    )

    result = engine.resolve(query=query, policy=RuntimePolicy(), principal=principal)
    record = engine.execution(result.execution_id)

    assert [node["id"] for node in record["graph_snapshot"]["nodes"]] == ["supplier:acme"]
    serialized = __import__("json").dumps(record["graph_snapshot"])
    assert "supplier:other" not in serialized
