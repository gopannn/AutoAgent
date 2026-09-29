from __future__ import annotations

from dataclasses import dataclass, field
from math import floor
from time import monotonic
from typing import Callable

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .models import FailureReason
from .telemetry import ResolutionTelemetry


class RuntimePolicy(BaseModel):
    max_expansions: int = Field(default=500, ge=1)
    max_depth: int = Field(default=4, ge=0)
    deadline_ms: int = Field(default=1000, ge=1)
    max_provider_calls: int = Field(default=100, ge=1)
    # "strict" hides untagged records from every tenant; "legacy" restores
    # pre-5.1 behaviour with a warning. See ctd.authz.TenantMode.
    tenant_mode: Literal["strict", "legacy"] = "strict"
    max_bytes_read: int = Field(default=5_000_000, ge=1)
    max_candidates: int = Field(default=1000, ge=1)
    validation_reserve_ratio: float = Field(default=0.15, ge=0.0, lt=1.0)
    initial_arousal: int = Field(default=1, ge=1)
    max_arousal: int = Field(default=8, ge=1)
    widen_step: int = Field(default=1, ge=1)
    stagnation_before_widen: int = Field(default=2, ge=1)
    min_confidence: float = Field(default=0.70, ge=0.0, le=1.0)
    min_trust: float = Field(default=0.70, ge=0.0, le=1.0)
    min_evidence_strength: float = Field(default=0.70, ge=0.0, le=1.0)
    min_source_diversity: int = Field(default=1, ge=1)
    allowed_node_types: set[str] | None = None
    allowed_edge_types: set[str] | None = None
    allowed_source_classes: set[str] | None = None
    authorization_scope: set[str] | None = None
    allowed_security_labels: set[str] | None = None
    tenant_id: str | None = None

    @model_validator(mode="after")
    def validate_arousal_bounds(self) -> "RuntimePolicy":
        if self.max_arousal < self.initial_arousal:
            raise ValueError("max_arousal must be >= initial_arousal")
        return self


@dataclass
class RuntimeState:
    started_at: float
    arousal: int
    expansions: int = 0
    max_depth_seen: int = 0
    provider_calls: int = 0
    bytes_read: int = 0
    candidates: int = 0
    validations: int = 0
    best_satisfied: int = -1
    stagnation_steps: int = 0
    in_validation: bool = False
    arousal_trajectory: list[int] = field(default_factory=list)


class BarrierController:
    """Deterministic implementation of Drive/Urgency/Arousal/Focus controls."""

    def __init__(self, clock: Callable[[], float] = monotonic) -> None:
        self._clock = clock

    def initialize(self, policy: RuntimePolicy) -> RuntimeState:
        state = RuntimeState(started_at=self._clock(), arousal=policy.initial_arousal)
        state.arousal_trajectory.append(policy.initial_arousal)
        return state

    def elapsed_ms(self, state: RuntimeState) -> float:
        return (self._clock() - state.started_at) * 1000.0

    @staticmethod
    def search_expansion_limit(policy: RuntimePolicy) -> int:
        return max(1, floor(policy.max_expansions * (1.0 - policy.validation_reserve_ratio)))

    def _deadline_ok(
        self, state: RuntimeState, policy: RuntimePolicy, telemetry: ResolutionTelemetry
    ) -> bool:
        if self.elapsed_ms(state) >= policy.deadline_ms:
            telemetry.deadline_exhausted = True
            return False
        return True

    def can_search(
        self,
        state: RuntimeState,
        policy: RuntimePolicy,
        telemetry: ResolutionTelemetry,
        *,
        depth: int,
    ) -> bool:
        if not self._deadline_ok(state, policy, telemetry):
            return False
        if depth > policy.max_depth:
            telemetry.depth_budget_exhausted = True
            return False
        if state.expansions >= self.search_expansion_limit(policy):
            telemetry.validation_reserve_entered = True
            telemetry.expansion_budget_exhausted = state.expansions >= policy.max_expansions
            state.in_validation = True
            return False
        return True

    def can_expand(
        self,
        state: RuntimeState,
        policy: RuntimePolicy,
        telemetry: ResolutionTelemetry,
        *,
        depth: int,
    ) -> bool:
        # Backward-compatible alias. Search now stops early enough to preserve a
        # validation reserve rather than consuming every allowed expansion.
        return self.can_search(state, policy, telemetry, depth=depth)

    def can_validate(
        self, state: RuntimeState, policy: RuntimePolicy, telemetry: ResolutionTelemetry
    ) -> bool:
        if not self._deadline_ok(state, policy, telemetry):
            return False
        if state.expansions >= policy.max_expansions:
            telemetry.expansion_budget_exhausted = True
            return False
        state.in_validation = True
        return True

    def record_expansion(
        self,
        state: RuntimeState,
        telemetry: ResolutionTelemetry,
        *,
        depth: int,
    ) -> None:
        state.expansions += 1
        state.max_depth_seen = max(state.max_depth_seen, depth)
        telemetry.edges_traversed += 1

    def record_validation(self, state: RuntimeState, telemetry: ResolutionTelemetry) -> None:
        state.validations += 1
        telemetry.validation_steps += 1

    def can_call_provider(
        self, state: RuntimeState, policy: RuntimePolicy, telemetry: ResolutionTelemetry
    ) -> bool:
        if not self._deadline_ok(state, policy, telemetry):
            return False
        if state.provider_calls >= policy.max_provider_calls:
            telemetry.provider_call_budget_exhausted = True
            return False
        if state.bytes_read >= policy.max_bytes_read:
            telemetry.byte_budget_exhausted = True
            return False
        return True

    def record_provider_call(
        self,
        state: RuntimeState,
        telemetry: ResolutionTelemetry,
        *,
        bytes_read: int = 0,
    ) -> None:
        state.provider_calls += 1
        state.bytes_read += bytes_read
        telemetry.provider_calls += 1
        telemetry.bytes_read += bytes_read

    def record_bytes(
        self, state: RuntimeState, telemetry: ResolutionTelemetry, bytes_read: int
    ) -> None:
        state.bytes_read += bytes_read
        telemetry.bytes_read += bytes_read

    def can_create_candidate(
        self, state: RuntimeState, policy: RuntimePolicy, telemetry: ResolutionTelemetry
    ) -> bool:
        if state.candidates >= policy.max_candidates:
            telemetry.candidate_budget_exhausted = True
            return False
        return True

    def record_candidate(self, state: RuntimeState, telemetry: ResolutionTelemetry) -> None:
        state.candidates += 1
        telemetry.candidates_created += 1

    def record_progress(self, state: RuntimeState, *, satisfied_constraints: int) -> None:
        if satisfied_constraints > state.best_satisfied:
            state.best_satisfied = satisfied_constraints
            state.stagnation_steps = 0
        else:
            state.stagnation_steps += 1

    def should_widen(self, state: RuntimeState, policy: RuntimePolicy) -> bool:
        return (
            state.stagnation_steps >= policy.stagnation_before_widen
            and state.arousal < policy.max_arousal
        )

    def widen(
        self,
        state: RuntimeState,
        policy: RuntimePolicy,
        telemetry: ResolutionTelemetry,
    ) -> int:
        next_value = min(policy.max_arousal, state.arousal + policy.widen_step)
        if next_value != state.arousal:
            state.arousal = next_value
            state.stagnation_steps = 0
            state.arousal_trajectory.append(state.arousal)
            telemetry.arousal_history.append(state.arousal)
            telemetry.widening_steps += 1
            telemetry.event("arousal_widened", arousal=state.arousal)
        return state.arousal

    def failure_reasons(self, telemetry: ResolutionTelemetry) -> list[FailureReason]:
        reasons: list[FailureReason] = []
        if telemetry.deadline_exhausted:
            reasons.append(FailureReason.DEADLINE_EXHAUSTED)
        if telemetry.expansion_budget_exhausted:
            reasons.append(FailureReason.EXPANSION_BUDGET_EXHAUSTED)
        if telemetry.depth_budget_exhausted:
            reasons.append(FailureReason.DEPTH_BUDGET_EXHAUSTED)
        if telemetry.provider_call_budget_exhausted:
            reasons.append(FailureReason.PROVIDER_CALL_BUDGET_EXHAUSTED)
        return reasons

    def snapshot(self, state: RuntimeState, policy: RuntimePolicy) -> dict[str, int | float | bool | list[int]]:
        return {
            "arousal": state.arousal,
            "expansions": state.expansions,
            "max_depth_seen": state.max_depth_seen,
            "provider_calls": state.provider_calls,
            "bytes_read": state.bytes_read,
            "candidates": state.candidates,
            "validations": state.validations,
            "in_validation": state.in_validation,
            "search_expansion_limit": self.search_expansion_limit(policy),
            "arousal_trajectory": list(state.arousal_trajectory),
        }
