from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any, Callable

from .controller import RuntimePolicy
from .models import QueryConstraintGraph, ResolutionResult, ResolutionState


class QueryDecomposer:
    def decompose(self, query: QueryConstraintGraph) -> list[QueryConstraintGraph]:
        names = [variable.name for variable in query.variables]
        parent = {name: name for name in names}

        def find(name: str) -> str:
            while parent[name] != name:
                parent[name] = parent[parent[name]]
                name = parent[name]
            return name

        def union(left: str, right: str) -> None:
            root_left = find(left)
            root_right = find(right)
            if root_left == root_right:
                return
            low, high = sorted([root_left, root_right])
            parent[high] = low

        for relation in query.relations:
            union(relation.subject_var, relation.object_var)

        groups: dict[str, set[str]] = defaultdict(set)
        for name in names:
            groups[find(name)].add(name)

        components: list[QueryConstraintGraph] = []
        for member_names in groups.values():
            variables = sorted(
                [variable for variable in query.variables if variable.name in member_names],
                key=lambda item: item.name,
            )
            relations = [
                relation
                for relation in query.relations
                if relation.subject_var in member_names and relation.object_var in member_names
            ]
            attributes = [attribute for attribute in query.attributes if attribute.variable in member_names]
            components.append(
                QueryConstraintGraph(
                    variables=variables,
                    relations=relations,
                    attributes=attributes,
                    as_of=query.as_of,
                    objective=query.objective,
                    top_k=query.top_k,
                    query_class=query.query_class,
                )
            )
        components.sort(key=lambda item: tuple(variable.name for variable in item.variables))
        return components


_STATE_PRECEDENCE = {
    ResolutionState.RESOLVED: 0,
    ResolutionState.PARTIAL: 1,
    ResolutionState.UNRESOLVABLE: 2,
    ResolutionState.BUDGET_EXHAUSTED: 3,
    ResolutionState.CONTRADICTED: 4,
    ResolutionState.PROVIDER_ERROR: 5,
}


class AsyncSubproblemExecutor:
    def __init__(
        self,
        resolver_factory: Callable[[], Any],
        *,
        decomposer: QueryDecomposer | None = None,
    ) -> None:
        self.resolver_factory = resolver_factory
        self.decomposer = decomposer or QueryDecomposer()

    async def resolve(
        self,
        query: QueryConstraintGraph,
        policy: RuntimePolicy,
        *,
        profile: dict[str, Any] | None = None,
    ) -> ResolutionResult:
        components = self.decomposer.decompose(query)
        if len(components) <= 1 or not self._can_split(policy, len(components)):
            return await asyncio.to_thread(
                self.resolver_factory().resolve, query, policy, profile=profile
            )

        weights = [
            1 + sum(1 for item in [*component.relations, *component.attributes] if item.hard)
            for component in components
        ]
        allocations = {
            "max_expansions": self._allocate(policy.max_expansions, weights),
            "max_provider_calls": self._allocate(policy.max_provider_calls, weights),
            "max_bytes_read": self._allocate(policy.max_bytes_read, weights),
            "max_candidates": self._allocate(policy.max_candidates, weights),
        }

        async def run(index: int, component: QueryConstraintGraph) -> ResolutionResult:
            updates = {field: values[index] for field, values in allocations.items()}
            component_policy = policy.model_copy(update=updates)
            return await asyncio.to_thread(
                self.resolver_factory().resolve,
                component,
                component_policy,
                profile=profile,
            )

        results = await asyncio.gather(
            *(run(index, component) for index, component in enumerate(components))
        )
        return self._merge(query, components, results)

    @staticmethod
    def _can_split(policy: RuntimePolicy, count: int) -> bool:
        return all(
            value >= count
            for value in (
                policy.max_expansions,
                policy.max_provider_calls,
                policy.max_bytes_read,
                policy.max_candidates,
            )
        )

    @staticmethod
    def _allocate(total: int, weights: list[int]) -> list[int]:
        count = len(weights)
        if count == 0:
            return []
        if total < count:
            raise ValueError("budget is too small to allocate safely")
        base = [1] * count
        remaining = total - count
        if remaining == 0:
            return base
        weight_sum = sum(weights)
        raw = [remaining * weight / weight_sum for weight in weights]
        floors = [int(value) for value in raw]
        result = [base[index] + floors[index] for index in range(count)]
        remainder = total - sum(result)
        fractional_order = sorted(
            range(count),
            key=lambda index: (-(raw[index] - floors[index]), index),
        )
        for index in fractional_order[:remainder]:
            result[index] += 1
        return result

    @staticmethod
    def _merge(
        original: QueryConstraintGraph,
        components: list[QueryConstraintGraph],
        results: list[ResolutionResult],
    ) -> ResolutionResult:
        state = max((result.state for result in results), key=lambda item: _STATE_PRECEDENCE[item])
        bindings: dict[str, str] = {}
        resolved: list[str] = []
        unresolved: list[str] = []
        violated: list[str] = []
        contradictions: list[str] = []
        evidence_ids: list[str] = []
        failure_reasons = []
        next_actions: list[str] = []

        total_constraints = len(original.relations) + len(original.attributes)
        weighted_resolved = 0
        component_summaries: list[dict[str, Any]] = []
        aggregate_telemetry = {
            "provider_calls": 0,
            "bytes_read": 0,
            "edges_traversed": 0,
            "nodes_considered": 0,
            "candidates_created": 0,
        }

        for component, result in zip(components, results, strict=True):
            for key, value in result.bindings.items():
                if key in bindings and bindings[key] != value:
                    raise ValueError(f"async component binding collision for {key}")
                bindings[key] = value
            resolved.extend(item for item in result.resolved_constraints if item not in resolved)
            unresolved.extend(item for item in result.unresolved_constraints if item not in unresolved)
            violated.extend(item for item in result.violated_constraints if item not in violated)
            contradictions.extend(item for item in result.contradictions if item not in contradictions)
            evidence_ids.extend(item for item in result.evidence_ids if item not in evidence_ids)
            failure_reasons.extend(item for item in result.failure_reasons if item not in failure_reasons)
            next_actions.extend(item for item in result.next_actions if item not in next_actions)
            component_constraint_count = len(component.relations) + len(component.attributes)
            weighted_resolved += result.constraint_coverage * component_constraint_count
            telemetry = result.telemetry or {}
            for field in aggregate_telemetry:
                aggregate_telemetry[field] += telemetry.get(field, 0) or 0
            component_summaries.append(
                {
                    "variables": [variable.name for variable in component.variables],
                    "state": result.state.value,
                    "constraint_coverage": result.constraint_coverage,
                    "telemetry": telemetry,
                }
            )

        coverage = weighted_resolved / total_constraints if total_constraints else 1.0
        telemetry_payload: dict[str, Any] = {
            **aggregate_telemetry,
            "async_components": len(components),
            "components": component_summaries,
        }
        return ResolutionResult(
            state=state,
            bindings=dict(sorted(bindings.items())),
            constraint_coverage=round(coverage, 12),
            resolved_constraints=resolved,
            unresolved_constraints=unresolved,
            violated_constraints=violated,
            contradictions=contradictions,
            evidence_ids=evidence_ids,
            failure_reasons=failure_reasons,
            next_actions=next_actions,
            telemetry=telemetry_payload,
        )
