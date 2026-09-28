from __future__ import annotations

from hashlib import sha256
from typing import Any, Literal

from pydantic import BaseModel, Field

from .models import AttributeConstraint, QueryConstraintGraph, RelationConstraint
from .providers import EvidenceProvider


class PlanOperation(BaseModel):
    operation_id: str
    constraint_id: str
    kind: Literal["attribute", "relation"]
    preferred_providers: list[str] = Field(default_factory=list)
    fallback_providers: list[str] = Field(default_factory=list)
    required_bindings: list[str] = Field(default_factory=list)
    hard: bool
    priority_score: float = Field(ge=0.0)
    information_gain: float = Field(ge=0.0)
    selectivity: float = Field(ge=0.0)
    reliability: float = Field(ge=0.0, le=1.0)
    estimated_cost: float = Field(gt=0.0)
    rationale: str


class ExecutionPlan(BaseModel):
    query_fingerprint: str
    operations: list[PlanOperation]
    planner_version: str = "ctd-v2-deterministic-1"
    advisor_version: str | None = None
    advisory_scores: dict[str, float] = Field(default_factory=dict)
    advice_applied: list[str] = Field(default_factory=list)
    advice_rejected: list[str] = Field(default_factory=list)


class QueryPlanner:
    """Deterministic cost-based planner.

    The estimates are intentionally simple in v2: they are inspectable and safe
    to improve later without changing resolution semantics.
    """

    def plan(
        self,
        query: QueryConstraintGraph,
        provider: EvidenceProvider,
        *,
        profile: dict[str, Any] | None = None,
    ) -> ExecutionPlan:
        variable_types = {variable.name: variable.node_type for variable in query.variables}
        preferred = list((profile or {}).get("preferred_order", []))
        preferred_rank = {constraint_id: index for index, constraint_id in enumerate(preferred)}
        operations: list[PlanOperation] = []

        for constraint in query.attributes:
            node_type = variable_types[constraint.variable]
            count = max(1, provider.node_count(node_type))
            distinct = provider.distinct_attribute_count(node_type, constraint.attribute)
            if constraint.op == "eq":
                selectivity = min(1.0, max(1.0 / count, distinct / count if distinct else 1.0 / count))
                information_gain = 1.0 + selectivity
            elif constraint.op in {"lt", "lte", "gt", "gte", "in"}:
                selectivity = 0.5
                information_gain = 1.0
            else:
                selectivity = 0.35
                information_gain = 0.8
            reliability = 1.0
            estimated_cost = 1.0
            priority = information_gain * selectivity * reliability / estimated_cost
            operations.append(
                PlanOperation(
                    operation_id=f"op:{constraint.id}",
                    constraint_id=constraint.id,
                    kind="attribute",
                    preferred_providers=[provider.name],
                    required_bindings=[constraint.variable],
                    hard=constraint.hard,
                    priority_score=round(priority, 9),
                    information_gain=information_gain,
                    selectivity=selectivity,
                    reliability=reliability,
                    estimated_cost=estimated_cost,
                    rationale=(
                        f"attribute {constraint.attribute} on {node_type}; "
                        f"nodes={count}, distinct={distinct}, op={constraint.op}"
                    ),
                )
            )

        for constraint in query.relations:
            source_type = variable_types[constraint.subject_var]
            target_type = variable_types[constraint.object_var]
            source_count = max(1, provider.node_count(source_type))
            target_count = max(1, provider.node_count(target_type))
            selectivity = min(0.75, 1.0 / max(1.0, (source_count + target_count) / 2.0))
            information_gain = 0.75 + (0.25 if constraint.required_source_classes else 0.0)
            reliability = 0.95
            estimated_cost = 1.5
            priority = information_gain * selectivity * reliability / estimated_cost
            operations.append(
                PlanOperation(
                    operation_id=f"op:{constraint.id}",
                    constraint_id=constraint.id,
                    kind="relation",
                    preferred_providers=[provider.name],
                    required_bindings=[constraint.subject_var, constraint.object_var],
                    hard=constraint.hard,
                    priority_score=round(priority, 9),
                    information_gain=information_gain,
                    selectivity=selectivity,
                    reliability=reliability,
                    estimated_cost=estimated_cost,
                    rationale=(
                        f"relation {constraint.relation}; source_nodes={source_count}, "
                        f"target_nodes={target_count}"
                    ),
                )
            )

        def order_key(operation: PlanOperation) -> tuple[int, float, int, str]:
            # Hard constraints always dominate profile hints. Profile data is an
            # advisory tie-breaker only after the computed cost/benefit score.
            profile_order = preferred_rank.get(operation.constraint_id, 1_000_000)
            return (
                0 if operation.hard else 1,
                -operation.priority_score,
                profile_order,
                operation.constraint_id,
            )

        operations.sort(key=order_key)
        canonical = query.model_dump_json(exclude_none=True)
        fingerprint = sha256(canonical.encode("utf-8")).hexdigest()[:24]
        return ExecutionPlan(query_fingerprint=fingerprint, operations=operations)
