from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .structural import StructuralCase
from .kernel.encoding import validate as kernel_validate
from .kernel.evaluate import guard_probe, run_ablations
from .kernel.interop import to_record


_ROLE_ALIASES = {
    "ACTOR": "AGENT",
    "PERSON": "HUMAN",
    "COMPONENT": "RESOURCE",
    "STATE": "SIGNAL",
}


class EvaluationArm(BaseModel):
    name: str
    note: str
    queries: int
    hit_at: dict[int, float]
    recall_at_max_k: float
    coverage_at_max_k: float
    deep_hit_at_max_k: float
    deep_recall_at_max_k: float
    mrr: float
    violation_rate: float
    mean_candidates: float


class GuardProbeResult(BaseModel):
    configuration: str
    emitted: int
    schema_violations: int
    refused_by_guard: int
    clean: bool


class IntelligenceEvaluationReport(BaseModel):
    accepted_cases: int
    rejected_cases: list[dict[str, Any]] = Field(default_factory=list)
    arms: list[EvaluationArm] = Field(default_factory=list)
    guards: list[GuardProbeResult] = Field(default_factory=list)
    controls_present: bool = False
    full_beats_random_on_mrr: bool | None = None
    full_violation_rate: float | None = None


def _payload(case: StructuralCase) -> dict[str, Any]:
    raw = case.model_dump(mode="json")
    raw["types"] = {
        entity: _ROLE_ALIASES.get(str(role).upper(), str(role).upper())
        for entity, role in case.types.items()
    }
    return raw


def evaluate_structural_cases(cases: list[StructuralCase]) -> IntelligenceEvaluationReport:
    records = []
    rejected: list[dict[str, Any]] = []
    for case in cases:
        rec = to_record(_payload(case))
        validation = kernel_validate(rec)
        if validation.ok:
            records.append(rec)
        else:
            rejected.append({
                "case_id": case.id,
                "findings": [
                    {"level": f.level, "code": f.code, "detail": f.detail}
                    for f in validation.findings
                ],
            })

    if not records:
        return IntelligenceEvaluationReport(accepted_cases=0, rejected_cases=rejected)

    raw_arms = run_ablations(records)
    arms: list[EvaluationArm] = []
    for arm in raw_arms:
        max_k = max(arm.ks)
        arms.append(
            EvaluationArm(
                name=arm.name,
                note=arm.note,
                queries=len(arm.per_query),
                hit_at={int(k): arm.hit_rate_at(k) for k in arm.ks},
                recall_at_max_k=arm.recall_at(max_k),
                coverage_at_max_k=arm.coverage_at(max_k),
                deep_hit_at_max_k=arm.deep_hit_rate_at(max_k),
                deep_recall_at_max_k=arm.deep_recall_at(max_k),
                mrr=arm.mrr,
                violation_rate=arm.violation_rate,
                mean_candidates=arm.mean_candidates,
            )
        )
    raw_guards = guard_probe(records)
    guards = [
        GuardProbeResult(
            configuration=row.arm,
            emitted=row.emitted,
            schema_violations=row.schema_violations,
            refused_by_guard=row.refused_by_guard,
            clean=row.clean,
        )
        for row in raw_guards
    ]
    by_name = {arm.name: arm for arm in arms}
    full = by_name.get("full")
    random = by_name.get("random control")
    return IntelligenceEvaluationReport(
        accepted_cases=len(records),
        rejected_cases=rejected,
        arms=arms,
        guards=guards,
        controls_present="frequency control" in by_name and "random control" in by_name,
        full_beats_random_on_mrr=(full.mrr >= random.mrr) if full and random else None,
        full_violation_rate=full.violation_rate if full else None,
    )
