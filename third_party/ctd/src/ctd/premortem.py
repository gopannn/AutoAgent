from __future__ import annotations

from pydantic import BaseModel, Field

from .structural import PredicateVocabulary, StructuralCase, StructuralRelation, StructuralTarget
from .transfer import HypothesisState, StructuralHypothesis, StructuralTransferEngine, TransferPolicy


class PreMortemPolicy(BaseModel):
    transfer: TransferPolicy = Field(default_factory=TransferPolicy)
    max_findings: int = Field(default=20, ge=1, le=500)
    require_check: bool = True


class PreMortemFinding(BaseModel):
    relation: StructuralRelation
    state: HypothesisState = HypothesisState.HYPOTHESIS
    check: str
    severity: int = Field(ge=1, le=5)
    convergence: int = Field(ge=1)
    source_cases: list[str]
    source_domains: list[str]
    structural_priority: float = Field(ge=0.0)
    priority_score: float = Field(ge=0.0)
    lineage: list[dict] = Field(default_factory=list)


class PreMortemResult(BaseModel):
    target_name: str
    findings: list[PreMortemFinding] = Field(default_factory=list)
    hypotheses_considered: int = 0
    uncheckable_hypotheses: int = 0
    transfer_metrics: dict = Field(default_factory=dict)


def _check_for(relation: StructuralRelation, templates: dict[str, str]) -> str | None:
    template = templates.get(relation.pred.upper())
    if template:
        args = [arg.canonical_key() if isinstance(arg, StructuralRelation) else str(arg) for arg in relation.args]
        try:
            return template.format(*args)
        except (IndexError, KeyError):
            return template
    for arg in reversed(relation.args):
        if isinstance(arg, StructuralRelation):
            nested = _check_for(arg, templates)
            if nested:
                return nested
    return None


class PreMortemEngine:
    def __init__(self, vocabulary: PredicateVocabulary | None = None) -> None:
        self.transfer_engine = StructuralTransferEngine(vocabulary or PredicateVocabulary.core())

    def analyze(
        self,
        incidents: list[StructuralCase],
        design: StructuralTarget,
        policy: PreMortemPolicy | None = None,
    ) -> PreMortemResult:
        policy = policy or PreMortemPolicy()
        incident_cases = [case for case in incidents if case.metadata.get("incident", True)]
        transfer = self.transfer_engine.transfer(incident_cases, design, policy.transfer)
        by_id = {case.id: case for case in incident_cases}
        findings: list[PreMortemFinding] = []
        uncheckable = 0

        for hypothesis in transfer.hypotheses:
            check = None
            severity = 1
            for case_id in hypothesis.source_cases:
                case = by_id.get(case_id)
                if case is None:
                    continue
                severity = max(severity, case.severity or 1)
                if check is None:
                    check = _check_for(hypothesis.relation, case.check_templates)
            if not check:
                uncheckable += 1
                if policy.require_check:
                    continue
                check = "No verification procedure is registered; treat this as a lead only."
            findings.append(
                PreMortemFinding(
                    relation=hypothesis.relation,
                    state=HypothesisState.HYPOTHESIS,
                    check=check,
                    severity=severity,
                    convergence=hypothesis.convergence,
                    source_cases=hypothesis.source_cases,
                    source_domains=hypothesis.source_domains,
                    structural_priority=hypothesis.priority_score,
                    priority_score=round(hypothesis.priority_score * severity, 12),
                    lineage=[item.model_dump(mode="json") for item in hypothesis.lineage],
                )
            )
        findings.sort(key=lambda item: (-item.priority_score, -item.severity, -item.convergence, item.relation.canonical_key()))
        findings = findings[: policy.max_findings]
        return PreMortemResult(
            target_name=design.name,
            findings=findings,
            hypotheses_considered=len(transfer.hypotheses),
            uncheckable_hypotheses=uncheckable,
            transfer_metrics={
                "cases_examined": transfer.cases_examined,
                "mac_retained": transfer.mac_retained,
                "fac_mappings_considered": transfer.fac_mappings_considered,
                "hypotheses_projected": transfer.hypotheses_projected,
                "hypotheses_deduplicated": transfer.hypotheses_deduplicated,
                "elapsed_ms": transfer.elapsed_ms,
            },
        )
