from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from ctd.async_exec import AsyncSubproblemExecutor, QueryDecomposer
from ctd.controller import RuntimePolicy
from ctd.models import (
    AttributeConstraint,
    QueryConstraintGraph,
    RelationConstraint,
    ResolutionResult,
    ResolutionState,
    Variable,
)

NOW = datetime(2026, 9, 12, tzinfo=UTC)


def disconnected_query() -> QueryConstraintGraph:
    return QueryConstraintGraph(
        as_of=NOW,
        variables=[
            Variable(name="supplier", node_type="Supplier"),
            Variable(name="component", node_type="Component"),
            Variable(name="person", node_type="Person"),
            Variable(name="company", node_type="Company"),
        ],
        relations=[
            RelationConstraint(id="r1", subject_var="supplier", relation="PRODUCES", object_var="component"),
            RelationConstraint(id="r2", subject_var="person", relation="WORKS_FOR", object_var="company"),
        ],
        attributes=[
            AttributeConstraint(id="a1", variable="supplier", attribute="price", op="lte", value=500),
            AttributeConstraint(id="a2", variable="person", attribute="active", op="eq", value=True),
        ],
        objective="resolve two independent domains",
    )


def test_query_decomposer_preserves_connected_constraints_and_deterministic_order():
    components = QueryDecomposer().decompose(disconnected_query())

    assert len(components) == 2
    assert [[v.name for v in item.variables] for item in components] == [
        ["company", "person"],
        ["component", "supplier"],
    ]
    assert [item.relations[0].id for item in components] == ["r2", "r1"]
    assert [item.attributes[0].id for item in components] == ["a2", "a1"]


class RecordingResolver:
    def __init__(self, calls, state_by_first_var=None):
        self.calls = calls
        self.state_by_first_var = state_by_first_var or {}

    def resolve(self, query, policy, *, profile=None):
        self.calls.append((query, policy))
        variable_names = sorted(variable.name for variable in query.variables)
        first = variable_names[0]
        state = self.state_by_first_var.get(first, ResolutionState.RESOLVED)
        bindings = {name: f"node:{name}" for name in variable_names}
        constraints = [item.id for item in [*query.relations, *query.attributes]]
        return ResolutionResult(
            state=state,
            bindings=bindings,
            constraint_coverage=1.0 if state == ResolutionState.RESOLVED else 0.5,
            resolved_constraints=constraints if state == ResolutionState.RESOLVED else constraints[:1],
            unresolved_constraints=[] if state == ResolutionState.RESOLVED else constraints[1:],
            telemetry={"provider_calls": 1, "bytes_read": 10, "edges_traversed": 2},
        )


def test_async_executor_splits_global_budgets_and_merges_deterministically():
    calls = []
    executor = AsyncSubproblemExecutor(lambda: RecordingResolver(calls))
    policy = RuntimePolicy(
        max_expansions=100,
        max_provider_calls=20,
        max_bytes_read=1000,
        max_candidates=50,
        deadline_ms=1000,
    )

    result = asyncio.run(executor.resolve(disconnected_query(), policy))

    assert result.state == ResolutionState.RESOLVED
    assert result.bindings == {
        "company": "node:company",
        "component": "node:component",
        "person": "node:person",
        "supplier": "node:supplier",
    }
    assert len(calls) == 2
    assert sum(call_policy.max_expansions for _, call_policy in calls) <= policy.max_expansions
    assert sum(call_policy.max_provider_calls for _, call_policy in calls) <= policy.max_provider_calls
    assert sum(call_policy.max_bytes_read for _, call_policy in calls) <= policy.max_bytes_read
    assert sum(call_policy.max_candidates for _, call_policy in calls) <= policy.max_candidates
    assert len(result.telemetry["components"]) == 2


def test_async_executor_uses_terminal_precedence_for_component_failure():
    calls = []
    executor = AsyncSubproblemExecutor(
        lambda: RecordingResolver(calls, state_by_first_var={"company": ResolutionState.PROVIDER_ERROR})
    )

    result = asyncio.run(executor.resolve(disconnected_query(), RuntimePolicy()))

    assert result.state == ResolutionState.PROVIDER_ERROR
    assert result.constraint_coverage < 1.0


def test_async_executor_does_not_duplicate_tiny_global_budgets():
    calls = []
    executor = AsyncSubproblemExecutor(lambda: RecordingResolver(calls))
    policy = RuntimePolicy(max_provider_calls=1, max_candidates=1, max_expansions=2, max_bytes_read=1)

    result = asyncio.run(executor.resolve(disconnected_query(), policy))

    # Unsafe split falls back to one whole-query execution rather than granting
    # each component a budget the caller did not authorize.
    assert len(calls) == 1
    assert len(calls[0][0].variables) == 4
    assert result.state == ResolutionState.RESOLVED
