from __future__ import annotations

from datetime import UTC, datetime

from ctd.advisor import AdaptivePlannerAdvisor, AdvisedQueryPlanner
from ctd.graph import EvidenceGraph
from ctd.models import AttributeConstraint, Node, QueryConstraintGraph, ResolutionResult, ResolutionState, Variable
from ctd.planner import QueryPlanner
from ctd.providers import GraphProvider
from ctd.repositories import InMemoryAdvisorRepository

NOW = datetime(2026, 9, 12, tzinfo=UTC)


def _provider() -> GraphProvider:
    graph = EvidenceGraph()
    for index in range(5):
        graph.add_node(Node(id=f"s:{index}", type="Supplier", attributes={"code": f"C{index}", "price": index * 10}))
    return GraphProvider(graph)


def _query() -> QueryConstraintGraph:
    return QueryConstraintGraph(
        as_of=NOW,
        variables=[Variable(name="supplier", node_type="Supplier")],
        attributes=[
            AttributeConstraint(id="hard-code", variable="supplier", attribute="code", op="eq", value="C1", hard=True),
            AttributeConstraint(id="hard-price", variable="supplier", attribute="price", op="ne", value=999, hard=True),
            AttributeConstraint(id="soft-price", variable="supplier", attribute="price", op="eq", value=10, hard=False),
        ],
    )


def test_adaptive_advisor_learns_deterministic_operation_preferences():
    repository = InMemoryAdvisorRepository()
    advisor = AdaptivePlannerAdvisor(repository)
    plan = QueryPlanner().plan(_query(), _provider())
    resolved = ResolutionResult(state=ResolutionState.RESOLVED, constraint_coverage=1.0)
    failed = ResolutionResult(state=ResolutionState.UNRESOLVABLE, constraint_coverage=0.0)

    advisor.observe("supplier", plan, resolved)
    advisor.observe("supplier", plan, resolved)
    advisor.observe("supplier", plan, failed)
    advice = advisor.advise("supplier", [item.constraint_id for item in plan.operations])

    assert advice.model_version.startswith("ctd-v3-advisor")
    assert set(advice.operation_scores) == {"hard-code", "hard-price", "soft-price"}
    assert advice.operation_order == sorted(
        advice.operation_order,
        key=lambda op: (-advice.operation_scores[op], op),
    )
    stats = repository.list_stats("supplier")
    assert all(item["successes"] == 2 for item in stats)
    assert all(item["failures"] == 1 for item in stats)


def test_advised_planner_never_moves_soft_operation_before_hard_operation():
    repository = InMemoryAdvisorRepository()
    advisor = AdaptivePlannerAdvisor(repository)
    for _ in range(20):
        repository.save_stat("supplier", "soft-price", {"successes": 20, "failures": 0, "reward_sum": 20.0})
        repository.save_stat("supplier", "hard-code", {"successes": 0, "failures": 20, "reward_sum": -10.0})
        repository.save_stat("supplier", "hard-price", {"successes": 0, "failures": 20, "reward_sum": -10.0})

    planner = AdvisedQueryPlanner(QueryPlanner(), advisor, advisory_priority_window=1.0)
    plan = planner.plan(_query(), _provider(), query_class="supplier")

    hard_ids = [item.constraint_id for item in plan.operations if item.hard]
    soft_ids = [item.constraint_id for item in plan.operations if not item.hard]
    assert [item.constraint_id for item in plan.operations] == hard_ids + soft_ids
    assert soft_ids == ["soft-price"]


def test_advisor_cannot_reverse_materially_better_base_priority():
    repository = InMemoryAdvisorRepository()
    repository.save_stat("supplier", "hard-price", {"successes": 100, "failures": 0, "reward_sum": 100.0})
    repository.save_stat("supplier", "hard-code", {"successes": 0, "failures": 100, "reward_sum": -50.0})
    advisor = AdaptivePlannerAdvisor(repository)
    base = QueryPlanner().plan(_query(), _provider())
    base_hard = [item.constraint_id for item in base.operations if item.hard]

    plan = AdvisedQueryPlanner(QueryPlanner(), advisor, advisory_priority_window=0.05).plan(
        _query(), _provider(), query_class="supplier"
    )
    advised_hard = [item.constraint_id for item in plan.operations if item.hard]

    assert advised_hard == base_hard
    assert plan.advice_rejected
