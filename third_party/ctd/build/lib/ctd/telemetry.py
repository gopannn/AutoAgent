from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ResolutionTelemetry:
    nodes_considered: int = 0
    edges_traversed: int = 0
    branches_pruned: int = 0
    widening_steps: int = 0
    constraints_evaluated: int = 0
    hard_satisfied: int = 0
    soft_satisfied: int = 0
    candidates_created: int = 0
    provider_calls: int = 0
    bytes_read: int = 0
    provider_latency_ms: float = 0.0
    validation_steps: int = 0
    authorization_pruned: int = 0
    prefilter_candidates_examined: int = 0
    prefilter_candidates_returned: int = 0
    prefilter_candidates_pruned: int = 0
    evidence_accepted: int = 0
    evidence_rejected: int = 0
    max_inference_depth_observed: int = 0
    contradictions_detected: int = 0
    contradictions_resolved: int = 0
    expansion_budget_exhausted: bool = False
    depth_budget_exhausted: bool = False
    deadline_exhausted: bool = False
    provider_call_budget_exhausted: bool = False
    byte_budget_exhausted: bool = False
    candidate_budget_exhausted: bool = False
    validation_reserve_entered: bool = False
    elapsed_ms: float = 0.0
    planner_operations: list[dict[str, Any]] = field(default_factory=list)
    arousal_history: list[int] = field(default_factory=list)
    focus_history: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)

    def event(self, name: str, **payload: Any) -> None:
        self.events.append({"sequence": len(self.events), "event": name, **payload})

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
