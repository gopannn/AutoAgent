from datetime import UTC, datetime

from fastapi.testclient import TestClient

from ctd.api import create_app
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
from ctd.planner import QueryPlanner
from ctd.providers import GraphProvider
from ctd.repositories import (
    InMemoryClaimRepository,
    InMemoryEdgeRepository,
    InMemoryEvidenceRepository,
    InMemoryNodeRepository,
)
from ctd.resolver import Resolver

NOW = datetime(2026, 9, 12, tzinfo=UTC)


def ev(eid: str, group: str = "g") -> Evidence:
    return Evidence(
        id=eid,
        source_id=f"src:{eid}",
        source_type="registry",
        observed_at=NOW,
        confidence=0.95,
        trust=0.95,
        independence_group=group,
    )


def basic_query(*, top_k: int = 1) -> QueryConstraintGraph:
    return QueryConstraintGraph(
        as_of=NOW,
        top_k=top_k,
        query_class="hardening",
        variables=[Variable(name="s", node_type="Supplier"), Variable(name="c", node_type="Component")],
        relations=[RelationConstraint(id="r", subject_var="s", relation="PRODUCES", object_var="c")],
        attributes=[AttributeConstraint(id="a:c", variable="c", attribute="code", op="eq", value="X")],
    )


def test_security_labels_are_filtered_before_candidate_scoring():
    graph = EvidenceGraph()
    graph.add_node(Node(id="secret", type="Supplier", attributes={"security_label": "restricted"}))
    graph.add_node(Node(id="c", type="Component", attributes={"code": "X", "security_label": "public"}))
    graph.add_edge(
        Edge(
            id="e",
            source="secret",
            target="c",
            type="PRODUCES",
            attributes={"security_label": "restricted"},
            evidence=[ev("e")],
        )
    )

    result = Resolver(graph).resolve(
        basic_query(),
        RuntimePolicy(allowed_security_labels={"public"}, initial_arousal=2, max_arousal=2),
    )

    assert result.state == ResolutionState.UNRESOLVABLE
    assert FailureReason.AUTHORIZATION_EXCLUDED in result.failure_reasons
    assert result.telemetry["authorization_pruned"] > 0


def test_repository_protocol_implementations_preserve_domain_objects():
    node_repo = InMemoryNodeRepository()
    edge_repo = InMemoryEdgeRepository()
    evidence_repo = InMemoryEvidenceRepository()
    claim_repo = InMemoryClaimRepository()
    node = Node(id="n", type="Thing")
    evidence = ev("ev")
    edge = Edge(id="e", source="n", target="n", type="SELF", evidence=[evidence])

    node_repo.save(node)
    edge_repo.save(edge)
    evidence_repo.save(evidence)
    claim_repo.save("k", {"value": "v"})

    assert node_repo.get("n") == node
    assert edge_repo.get("e") == edge
    assert evidence_repo.get("ev") == evidence
    assert claim_repo.get_by_key("k") == [{"value": "v"}]


def test_provider_failure_becomes_explicit_provider_error():
    class BrokenProvider:
        name = "broken"
        def node_count(self, node_type): raise RuntimeError("offline")
        def distinct_attribute_count(self, node_type, attribute): raise RuntimeError("offline")
        def nodes_of_type(self, *args, **kwargs): raise RuntimeError("offline")
        def outgoing(self, *args, **kwargs): raise RuntimeError("offline")
        def incoming(self, *args, **kwargs): raise RuntimeError("offline")
        def get_node(self, *args, **kwargs): raise RuntimeError("offline")

    result = Resolver(provider=BrokenProvider()).resolve(basic_query())

    assert result.state == ResolutionState.PROVIDER_ERROR
    assert FailureReason.PROVIDER_UNAVAILABLE in result.failure_reasons


def test_top_k_ranking_uses_soft_preferences_after_hard_closure():
    graph = EvidenceGraph()
    graph.add_node(Node(id="preferred", type="Supplier", attributes={"tier": "preferred"}))
    graph.add_node(Node(id="standard", type="Supplier", attributes={"tier": "standard"}))
    graph.add_node(Node(id="c", type="Component", attributes={"code": "X"}))
    graph.add_edge(Edge(id="e1", source="standard", target="c", type="PRODUCES", evidence=[ev("same1", "one")]))
    graph.add_edge(Edge(id="e2", source="preferred", target="c", type="PRODUCES", evidence=[ev("same2", "two")]))
    query = basic_query(top_k=2)
    query.attributes.append(
        AttributeConstraint(
            id="a:tier",
            variable="s",
            attribute="tier",
            op="eq",
            value="preferred",
            hard=False,
            soft_weight=3.0,
        )
    )

    result = Resolver(graph).resolve(query, RuntimePolicy(initial_arousal=4, max_arousal=4))

    assert result.bindings["s"] == "preferred"
    assert len(result.alternatives) == 1
    assert result.alternatives[0]["bindings"]["s"] == "standard"


def test_two_independent_sources_can_be_required():
    graph = EvidenceGraph()
    graph.add_node(Node(id="s", type="Supplier"))
    graph.add_node(Node(id="c", type="Component", attributes={"code": "X"}))
    graph.add_edge(
        Edge(
            id="e",
            source="s",
            target="c",
            type="PRODUCES",
            evidence=[ev("a", "same"), ev("b", "same")],
        )
    )

    result = Resolver(graph).resolve(
        basic_query(),
        RuntimePolicy(min_source_diversity=2, initial_arousal=2, max_arousal=2),
    )

    assert result.state == ResolutionState.PARTIAL
    assert FailureReason.EVIDENCE_TOO_WEAK in result.failure_reasons


def test_plan_contains_execution_metadata_required_for_inspection():
    graph = EvidenceGraph()
    graph.add_node(Node(id="s", type="Supplier"))
    graph.add_node(Node(id="c", type="Component", attributes={"code": "X"}))

    plan = QueryPlanner().plan(basic_query(), GraphProvider(graph))

    assert plan.operations[0].operation_id
    assert plan.operations[0].preferred_providers
    assert isinstance(plan.operations[0].required_bindings, list)


def test_v2_metrics_and_profile_listing_endpoints_are_available():
    client = TestClient(create_app())
    from ctd.examples import build_supplier_demo
    _, query, policy = build_supplier_demo()
    payload = {"query": query.model_dump(mode="json"), "policy": policy.model_dump(mode="json")}
    client.post("/v2/resolve", json=payload)

    profiles = client.get("/v2/profiles")
    metrics = client.get("/v2/metrics")

    assert profiles.status_code == 200
    assert profiles.json()["profiles"]
    assert metrics.status_code == 200
    assert metrics.json()["executions"] == 1
    assert "time_to_closure_ms" in metrics.json()


def test_resolution_result_includes_compact_v2_summaries():
    graph = EvidenceGraph()
    graph.add_node(Node(id="s", type="Supplier"))
    graph.add_node(Node(id="c", type="Component", attributes={"code": "X"}))
    graph.add_edge(Edge(id="e", source="s", target="c", type="PRODUCES", evidence=[ev("e")]))

    result = Resolver(graph).resolve(basic_query(), RuntimePolicy(initial_arousal=2, max_arousal=2))

    assert result.planner_summary["operation_order"]
    assert result.evidence_summary["accepted_evidence"] == 1
    assert "expansions" in result.budget_summary
    assert result.trace_summary["event_count"] > 0


def test_policy_level_evidence_rejections_include_explicit_reasons():
    graph = EvidenceGraph()
    graph.add_node(Node(id="s", type="Supplier"))
    graph.add_node(Node(id="c", type="Component", attributes={"code": "X"}))
    weak = Evidence(
        id="weak",
        source_id="src:weak",
        source_type="registry",
        observed_at=NOW,
        confidence=0.2,
        trust=0.95,
        independence_group="weak",
    )
    graph.add_edge(Edge(id="e", source="s", target="c", type="PRODUCES", evidence=[weak]))

    result = Resolver(graph).resolve(
        basic_query(),
        RuntimePolicy(min_confidence=0.8, initial_arousal=2, max_arousal=2),
    )

    evidence_events = [
        event for event in result.telemetry["events"] if event["event"] == "evidence_assessed"
    ]
    assert evidence_events
    assert evidence_events[0]["rejection_reasons"]["weak"] == "confidence_below_policy"


def test_metrics_expose_operational_resolution_quality_dimensions():
    client = TestClient(create_app())
    from ctd.examples import build_supplier_demo
    _, query, policy = build_supplier_demo()
    payload = {"query": query.model_dump(mode="json"), "policy": policy.model_dump(mode="json")}
    client.post("/v2/resolve", json=payload)

    metrics = client.get("/v2/metrics").json()

    assert "search" in metrics
    assert "mean_nodes_considered" in metrics["search"]
    assert "provider_latency_ms" in metrics
    assert "uncertainty" in metrics
    assert "mean_structural_coverage" in metrics["uncertainty"]
    assert "mean_max_inference_depth" in metrics["uncertainty"]
    assert "validation_reserve_entries" in metrics
    assert "budget_exhaustion_categories" in metrics
