from __future__ import annotations

from typing import Literal

from typing_extensions import NotRequired, TypedDict

Status = Literal[
    "running",
    "validation_failed",   # repairable: routed to the builder while budget remains
    "execution_failed",    # infrastructure or compiler fault: terminal, never repaired
    "budget_exceeded",
    "abstained",           # contradictory requirements, or evidence that does not justify release: terminal
    "released",
]

FeedbackSource = Literal["policy", "audit", "dependencies", "verification", "semantic_review"]


class SystemState(TypedDict):
    requirements: str
    requirement_contract: NotRequired[dict]
    requirement_encoding: NotRequired[dict]          # REASON: statements over the requirement schema
    requirement_check: NotRequired[dict]             # kernel validation + contradictions
    structural_risks: NotRequired[dict]              # DISCOVER: {"pre_code": [...], "code": [...]} (hypotheses)
    risk_verdicts: NotRequired[dict]                 # risk id -> auditor verdict
    spec_calibration: NotRequired[dict]              # VERIFY: test id -> {null service: passed}
    decision: NotRequired[dict]                      # DECIDE: per-requirement CTD state + justification
    abstain_reason: NotRequired[str]
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
