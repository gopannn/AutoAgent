from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, Field

from .models import QueryConstraintGraph, ResolutionResult, ResolutionState
from .planner import ExecutionPlan, PlanOperation, QueryPlanner
from .providers import EvidenceProvider
from .repositories import AdvisorRepository


class PlannerAdvice(BaseModel):
    operation_order: list[str] = Field(default_factory=list)
    provider_preferences: dict[str, list[str]] = Field(default_factory=dict)
    operation_scores: dict[str, float] = Field(default_factory=dict)
    model_version: str = "ctd-v3-advisor-1"


class PlannerAdvisor(Protocol):
    def advise(self, query_class: str, operation_ids: list[str]) -> PlannerAdvice: ...
    def observe(self, query_class: str, plan: ExecutionPlan, result: ResolutionResult) -> None: ...


class AdaptivePlannerAdvisor:
    """Small deterministic online reward model.

    The model only recommends order/provider preferences. Enforcement of the
    advisory safety envelope belongs to AdvisedQueryPlanner.
    """

    def __init__(self, repository: AdvisorRepository) -> None:
        self.repository = repository

    def advise(self, query_class: str, operation_ids: list[str]) -> PlannerAdvice:
        scores: dict[str, float] = {}
        for operation_id in operation_ids:
            record = self.repository.get_stat(query_class, operation_id) or {}
            observations = int(record.get("successes", 0)) + int(record.get("failures", 0))
            reward = float(record.get("reward_sum", 0.0))
            scores[operation_id] = round(reward / observations, 12) if observations else 0.0
        order = sorted(operation_ids, key=lambda item: (-scores[item], item))
        return PlannerAdvice(operation_order=order, operation_scores=scores)

    def observe(self, query_class: str, plan: ExecutionPlan, result: ResolutionResult) -> None:
        base_reward = {
            ResolutionState.RESOLVED: 1.0,
            ResolutionState.PARTIAL: 0.25,
            ResolutionState.CONTRADICTED: -0.25,
            ResolutionState.UNRESOLVABLE: -0.5,
            ResolutionState.BUDGET_EXHAUSTED: -0.5,
            ResolutionState.PROVIDER_ERROR: -0.5,
        }[result.state]
        success = result.state == ResolutionState.RESOLVED
        for index, operation in enumerate(plan.operations):
            existing = self.repository.get_stat(query_class, operation.constraint_id) or {}
            reward = base_reward / (1.0 + (index * 0.1))
            record = {
                "successes": int(existing.get("successes", 0)) + int(success),
                "failures": int(existing.get("failures", 0)) + int(not success),
                "reward_sum": float(existing.get("reward_sum", 0.0)) + reward,
            }
            self.repository.save_stat(query_class, operation.constraint_id, record)


class AdvisedQueryPlanner:
    def __init__(
        self,
        base_planner: QueryPlanner,
        advisor: PlannerAdvisor,
        *,
        advisory_priority_window: float = 0.05,
    ) -> None:
        self.base_planner = base_planner
        self.advisor = advisor
        self.advisory_priority_window = advisory_priority_window

    def plan(
        self,
        query: QueryConstraintGraph,
        provider: EvidenceProvider,
        *,
        profile: dict | None = None,
        query_class: str | None = None,
    ) -> ExecutionPlan:
        base = self.base_planner.plan(query, provider, profile=profile)
        resolved_query_class = query_class or query.query_class or "default"
        advice = self.advisor.advise(
            resolved_query_class, [operation.constraint_id for operation in base.operations]
        )
        rank = {operation_id: index for index, operation_id in enumerate(advice.operation_order)}
        final: list[PlanOperation] = []
        applied: list[str] = []
        rejected: list[str] = []

        for hard in (True, False):
            group = [operation for operation in base.operations if operation.hard is hard]
            cursor = 0
            while cursor < len(group):
                first = group[cursor]
                cluster = [first]
                cursor += 1
                while cursor < len(group):
                    candidate = group[cursor]
                    if first.priority_score - candidate.priority_score > self.advisory_priority_window:
                        break
                    cluster.append(candidate)
                    cursor += 1
                original_ids = [item.constraint_id for item in cluster]
                ordered = sorted(
                    cluster,
                    key=lambda item: (rank.get(item.constraint_id, 1_000_000), original_ids.index(item.constraint_id)),
                )
                ordered_ids = [item.constraint_id for item in ordered]
                if ordered_ids != original_ids:
                    applied.extend([item for item in ordered_ids if original_ids.index(item) != ordered_ids.index(item)])
                final.extend(ordered)

            desired = [item for item in advice.operation_order if item in {op.constraint_id for op in group}]
            actual = [item.constraint_id for item in final if item.hard is hard]
            if desired != actual:
                rejected.extend([item for item in desired if desired.index(item) != actual.index(item)])

        return base.model_copy(
            update={
                "operations": final,
                "planner_version": "ctd-v3-advised-1",
                "advisor_version": advice.model_version,
                "advisory_scores": advice.operation_scores,
                "advice_applied": sorted(set(applied)),
                "advice_rejected": sorted(set(rejected)),
            }
        )
