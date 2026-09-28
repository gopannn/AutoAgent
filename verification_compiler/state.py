from __future__ import annotations

from typing import Literal

from typing_extensions import NotRequired, TypedDict

Status = Literal[
    "running",
    "validation_failed",   # repairable: routed to the builder while budget remains
    "execution_failed",    # infrastructure or compiler fault: terminal, never repaired
    "budget_exceeded",
    "released",
]

FeedbackSource = Literal["policy", "audit", "dependencies", "verification", "semantic_review"]


class SystemState(TypedDict):
    requirements: str
    requirement_contract: NotRequired[dict]
    verification_spec: NotRequired[dict]
    verification_spec_hash: NotRequired[str]
    codebase: NotRequired[dict]
    findings_ledger: NotRequired[dict[str, dict]]    # fingerprint -> finding record
    audit_round: NotRequired[int]
    audited_codebase_hash: NotRequired[str]
    lockfile: NotRequired[dict]
    verification_result: NotRequired[dict]
    verified_codebase_hash: NotRequired[str]
    semantic_validation: NotRequired[dict]
    reviewed_codebase_hash: NotRequired[str]
    release_manifest: NotRequired[dict]
    validation_feedback: NotRequired[str]
    feedback_source: NotRequired[FeedbackSource]
    iteration: NotRequired[int]                       # repair rounds consumed
    status: NotRequired[Status]
    error: NotRequired[str]
