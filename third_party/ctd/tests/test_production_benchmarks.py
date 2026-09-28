from __future__ import annotations

from datetime import UTC, datetime

from ctd.advisor import AdaptivePlannerAdvisor, AdvisedQueryPlanner
from ctd.compiler import NaturalLanguageCompiler
from ctd.examples import DEMO_AS_OF, build_supplier_demo
from ctd.graph import EvidenceGraph
from ctd.models import Edge, Evidence, Node, ResolutionState
from ctd.ontology import EntityResolver
from ctd.planner import QueryPlanner
from ctd.providers import GraphProvider
from ctd.providers_ext import FederatedProvider
from ctd.repositories import InMemoryAdvisorRepository
from ctd.resolver import Resolver
from ctd.service import build_default_ontology

NOW = datetime(2026, 9, 12, tzinfo=UTC)
SUPPLIER_TEXT = (
    "Find a supplier that produces component X, certification is ISO9001, "
    "lead time at most 30 days, price under 500"
)


def test_natural_language_supplier_compile_stays_bounded_to_eight_constraints():
    ontology = build_default_ontology()
    compiler = NaturalLanguageCompiler(ontology, entity_resolver=EntityResolver(ontology))

    result = compiler.compile(SUPPLIER_TEXT, as_of=DEMO_AS_OF)

    assert result.query is not None
    constraint_count = len(result.query.relations) + len(result.query.attributes)
    assert constraint_count <= 8
    assert result.confidence >= 0.8


def test_selective_supplier_resolution_considers_less_than_full_physical_graph():
    graph, query, policy = build_supplier_demo()
    # The physical store contains unrelated domains that Focus should never inspect.
    for index in range(20):
        graph.add_node(Node(id=f"unrelated:{index}", type="Telemetry", attributes={"value": index}))

    result = Resolver(graph).resolve(query, policy)

    assert result.state == ResolutionState.RESOLVED
    assert result.telemetry["nodes_considered"] < len(graph.snapshot()["nodes"])


def test_tenant_isolation_exposes_zero_cross_tenant_candidates():
    graph = EvidenceGraph()
    for tenant in ("acme", "other"):
        graph.add_node(
            Node(
                id=f"supplier:{tenant}",
                type="Supplier",
                attributes={"tenant_id": tenant, "unit_price": 100},
            )
        )

    provider = GraphProvider(graph, tenant_id="acme")

    visible = provider.nodes_of_type("Supplier").items
    assert [node.id for node in visible] == ["supplier:acme"]
    assert provider.get_node("supplier:other").items == []


def test_federated_duplicate_provider_does_not_duplicate_candidate_bindings():
    graph, query, policy = build_supplier_demo()
    first = GraphProvider(graph)
    first.name = "primary"
    duplicate = GraphProvider(graph)
    duplicate.name = "duplicate"
    provider = FederatedProvider([first, duplicate])

    result = Resolver(provider=provider).resolve(query, policy)

    assert result.state == ResolutionState.RESOLVED
    assert result.bindings == {
        "supplier": "supplier:a",
        "component": "component:x",
        "cert": "cert:y",
    }
    assert result.telemetry["candidates_created"] <= 3


def test_learned_advisor_cannot_change_terminal_correctness_against_base_planner():
    graph, query, policy = build_supplier_demo()
    provider = GraphProvider(graph)
    base_result = Resolver(provider=provider, planner=QueryPlanner()).resolve(query, policy)

    base_plan = QueryPlanner().plan(query, provider)
    repository = InMemoryAdvisorRepository()
    # Deliberately teach an adversarial ordering preference. The safety envelope
    # must preserve hard/soft class and materially better deterministic priorities.
    for operation in base_plan.operations:
        reward = 100.0 if operation == base_plan.operations[-1] else -100.0
        repository.save_stat(
            query.query_class or "supplier-benchmark",
            operation.constraint_id,
            {"successes": 100 if reward > 0 else 0, "failures": 0 if reward > 0 else 100, "reward_sum": reward},
        )
    planner = AdvisedQueryPlanner(
        QueryPlanner(),
        AdaptivePlannerAdvisor(repository),
        advisory_priority_window=0.05,
    )
    advised_result = Resolver(provider=provider, planner=planner).resolve(query, policy)

    assert base_result.state == ResolutionState.RESOLVED
    assert advised_result.state == base_result.state
    assert advised_result.bindings == base_result.bindings
    assert advised_result.constraint_coverage == base_result.constraint_coverage
