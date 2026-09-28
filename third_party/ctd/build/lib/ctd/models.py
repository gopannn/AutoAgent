from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


ConstraintOperator = Literal["eq", "ne", "lt", "lte", "gt", "gte", "in"]
TemporalMode = Literal["as_of", "current", "any"]
Cardinality = Literal["one", "zero_or_one", "one_or_more", "many"]


class Node(BaseModel):
    id: str
    type: str
    attributes: dict[str, Any] = Field(default_factory=dict)


class Evidence(BaseModel):
    id: str
    source_id: str
    source_type: str
    observed_at: datetime
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    trust: float = Field(ge=0.0, le=1.0)
    independence_group: str | None = None
    extraction_method: str = "direct"
    direct: bool = True
    lineage: list[str] = Field(default_factory=list)
    claim_key: str | None = None
    claim_value: Any = None

    def is_valid_at(self, at: datetime) -> bool:
        if self.valid_from is not None and at < self.valid_from:
            return False
        if self.valid_until is not None and at > self.valid_until:
            return False
        return True


class Edge(BaseModel):
    id: str
    source: str
    target: str
    type: str
    attributes: dict[str, Any] = Field(default_factory=dict)
    evidence: list[Evidence] = Field(default_factory=list)


class Variable(BaseModel):
    name: str
    node_type: str


class _ConstraintPolicy(BaseModel):
    hard: bool = True
    required_evidence_strength: float | None = Field(default=None, ge=0.0, le=1.0)
    required_source_classes: set[str] = Field(default_factory=set)
    max_inference_depth: int | None = Field(default=None, ge=0)
    soft_weight: float = Field(default=1.0, ge=0.0)
    temporal_mode: TemporalMode = "as_of"
    cardinality: Cardinality = "one"
    exclusion: bool = False


class RelationConstraint(_ConstraintPolicy):
    id: str
    subject_var: str
    relation: str
    object_var: str


class AttributeConstraint(_ConstraintPolicy):
    id: str
    variable: str
    attribute: str
    op: ConstraintOperator
    value: Any


class QueryConstraintGraph(BaseModel):
    variables: list[Variable]
    relations: list[RelationConstraint] = Field(default_factory=list)
    attributes: list[AttributeConstraint] = Field(default_factory=list)
    as_of: datetime
    objective: str = "satisfy_constraints"
    top_k: int = Field(default=1, ge=1, le=100)
    query_class: str | None = None

    @model_validator(mode="after")
    def validate_references(self) -> "QueryConstraintGraph":
        names = {variable.name for variable in self.variables}
        if len(names) != len(self.variables):
            raise ValueError("variable names must be unique")
        referenced = {
            variable
            for relation in self.relations
            for variable in (relation.subject_var, relation.object_var)
        }
        referenced.update(attribute.variable for attribute in self.attributes)
        missing = referenced - names
        if missing:
            raise ValueError(f"constraints reference undeclared variables: {sorted(missing)}")
        constraint_ids = [item.id for item in [*self.relations, *self.attributes]]
        if len(set(constraint_ids)) != len(constraint_ids):
            raise ValueError("constraint ids must be unique")
        return self


class ClaimStatus(StrEnum):
    SUPPORTED = "SUPPORTED"
    DISPUTED = "DISPUTED"
    SUPERSEDED = "SUPERSEDED"
    RETRACTED = "RETRACTED"
    UNKNOWN = "UNKNOWN"
    INVALID = "INVALID"


class Claim(BaseModel):
    key: str
    value: Any
    status: ClaimStatus = ClaimStatus.UNKNOWN
    evidence_ids: list[str] = Field(default_factory=list)
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    lineage: list[str] = Field(default_factory=list)


class FailureReason(StrEnum):
    MISSING_DATA = "MISSING_DATA"
    UNSATISFIABLE_CONSTRAINT = "UNSATISFIABLE_CONSTRAINT"
    EVIDENCE_TOO_WEAK = "EVIDENCE_TOO_WEAK"
    CONTRADICTORY_EVIDENCE = "CONTRADICTORY_EVIDENCE"
    AUTHORIZATION_EXCLUDED = "AUTHORIZATION_EXCLUDED"
    DEADLINE_EXHAUSTED = "DEADLINE_EXHAUSTED"
    EXPANSION_BUDGET_EXHAUSTED = "EXPANSION_BUDGET_EXHAUSTED"
    DEPTH_BUDGET_EXHAUSTED = "DEPTH_BUDGET_EXHAUSTED"
    PROVIDER_CALL_BUDGET_EXHAUSTED = "PROVIDER_CALL_BUDGET_EXHAUSTED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    SCHEMA_MISMATCH = "SCHEMA_MISMATCH"
    SEARCH_SPACE_EXHAUSTED = "SEARCH_SPACE_EXHAUSTED"
    VALIDATION_RESERVE_EXHAUSTED = "VALIDATION_RESERVE_EXHAUSTED"


class ResolutionState(StrEnum):
    RESOLVED = "RESOLVED"
    PARTIAL = "PARTIAL"
    CONTRADICTED = "CONTRADICTED"
    UNRESOLVABLE = "UNRESOLVABLE"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    PROVIDER_ERROR = "PROVIDER_ERROR"




class ResolutionGap(BaseModel):
    constraint_id: str
    constraint_kind: Literal["attribute", "relation", "domain", "evidence", "authorization", "budget"]
    truth: Literal["SATISFIED", "VIOLATED", "UNKNOWN"] = "UNKNOWN"
    variables: list[str] = Field(default_factory=list)
    reason: str
    satisfied_dependencies: list[str] = Field(default_factory=list)
    candidate_count_before_failure: int = Field(default=0, ge=0)
    required_evidence: str
    suggested_provider: str | None = None
    suggested_action: str
    priority: float = Field(default=1.0, ge=0.0)


class ResolutionUncertainty(BaseModel):
    structural_coverage: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence_coverage: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence_strength: float = Field(default=0.0, ge=0.0, le=1.0)
    temporal_freshness: float = Field(default=0.0, ge=0.0, le=1.0)
    source_diversity: int = Field(default=0, ge=0)
    contradiction_risk: float = Field(default=0.0, ge=0.0, le=1.0)
    inference_depth_penalty: float = Field(default=0.0, ge=0.0, le=1.0)
    resolution_confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class ResolutionResult(BaseModel):
    state: ResolutionState
    bindings: dict[str, str] = Field(default_factory=dict)
    constraint_coverage: float = Field(ge=0.0, le=1.0)
    resolved_constraints: list[str] = Field(default_factory=list)
    unresolved_constraints: list[str] = Field(default_factory=list)
    violated_constraints: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    telemetry: dict[str, Any] = Field(default_factory=dict)
    next_actions: list[str] = Field(default_factory=list)
    uncertainty: ResolutionUncertainty | None = None
    failure_reasons: list[FailureReason] = Field(default_factory=list)
    candidate_rank: int | None = None
    alternatives: list[dict[str, Any]] = Field(default_factory=list)
    execution_id: str | None = None
    evidence_summary: dict[str, Any] = Field(default_factory=dict)
    planner_summary: dict[str, Any] = Field(default_factory=dict)
    budget_summary: dict[str, Any] = Field(default_factory=dict)
    trace_summary: dict[str, Any] = Field(default_factory=dict)
    gaps: list[ResolutionGap] = Field(default_factory=list)

    @model_validator(mode="after")
    def default_uncertainty_from_coverage(self) -> "ResolutionResult":
        if self.uncertainty is None:
            self.uncertainty = ResolutionUncertainty(
                structural_coverage=self.constraint_coverage,
                resolution_confidence=self.constraint_coverage,
            )
        return self
