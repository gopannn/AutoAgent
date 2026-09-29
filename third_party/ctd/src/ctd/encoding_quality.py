from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, computed_field

from .structural import PredicateVocabulary, StructuralCase, StructuralRelation


FindingLevel = Literal["error", "warning", "info"]


class EncodingFinding(BaseModel):
    level: FindingLevel
    code: str
    detail: str


class EncodingQualityPolicy(BaseModel):
    min_order: int = Field(default=2, ge=1)
    off_vocab_level: FindingLevel = "warning"
    orphan_level: FindingLevel = "warning"
    require_types: bool = True
    require_roundtrip: bool = True


class EncodingValidation(BaseModel):
    record_id: str
    findings: list[EncodingFinding] = Field(default_factory=list)
    roundtrip: list[str] = Field(default_factory=list)
    max_order: int = 0

    @property
    def errors(self) -> list[EncodingFinding]:
        return [finding for finding in self.findings if finding.level == "error"]

    @property
    def warnings(self) -> list[EncodingFinding]:
        return [finding for finding in self.findings if finding.level == "warning"]

    @computed_field
    @property
    def ok(self) -> bool:
        return not self.errors


def _entity_appearances(case: StructuralCase) -> dict[str, int]:
    appearances: dict[str, int] = {}
    for relation in case.all_relations():
        for arg in relation.args:
            if isinstance(arg, str):
                appearances[arg] = appearances.get(arg, 0) + 1
    return appearances


def _barren_relations(case: StructuralCase, vocabulary: PredicateVocabulary) -> list[StructuralRelation]:
    """Find top-level projected structure whose entities have no independent anchor.

    An entity is anchored when it occurs in at least two structural positions or in
    a primitive (order-1) shared-vocabulary relation. This is stricter and more
    useful than merely asking whether it occurs somewhere in the same relation.
    """
    appearances = _entity_appearances(case)
    primitive_anchors = {
        arg
        for relation in case.all_relations()
        if relation.order == 1 and relation.pred.upper() in vocabulary.predicates
        for arg in relation.args
        if isinstance(arg, str)
    }
    barren: list[StructuralRelation] = []
    for relation in case.relations:
        entities = [arg for arg in relation.args if isinstance(arg, str)]
        if entities and any(appearances.get(entity, 0) <= 1 and entity not in primitive_anchors for entity in entities):
            barren.append(relation)
    return barren


def validate_structural_case(
    case: StructuralCase,
    *,
    vocabulary: PredicateVocabulary | None = None,
    policy: EncodingQualityPolicy | None = None,
) -> EncodingValidation:
    vocabulary = vocabulary or PredicateVocabulary.core()
    policy = policy or EncodingQualityPolicy()
    relations = case.all_relations()
    max_order = max((relation.order for relation in relations), default=0)
    findings: list[EncodingFinding] = []

    if not relations:
        findings.append(EncodingFinding(level="info", code="NO_RELATIONS", detail="No relational structure is available for transfer."))
        return EncodingValidation(record_id=case.id, findings=findings, roundtrip=[], max_order=0)

    entities = case.entities()
    if policy.require_types:
        untyped = sorted(entity for entity in entities if entity not in case.types)
        if untyped:
            findings.append(EncodingFinding(level="error", code="UNTYPED", detail=f"Entities lack declared roles: {', '.join(untyped)}"))

    if max_order < policy.min_order:
        findings.append(EncodingFinding(level="error", code="SHALLOW", detail=f"Maximum relational order {max_order} is below required {policy.min_order}."))

    off_vocab = sorted({relation.pred.upper() for relation in relations} - vocabulary.predicates)
    if off_vocab:
        findings.append(EncodingFinding(level=policy.off_vocab_level, code="OFF_VOCAB", detail=f"Predicates are outside the shared vocabulary: {', '.join(off_vocab)}"))

    appearances = _entity_appearances(case)
    orphans = sorted(entity for entity, count in appearances.items() if count == 1)
    if orphans:
        findings.append(EncodingFinding(level=policy.orphan_level, code="ORPHAN", detail=f"Entities appear in one structural position only: {', '.join(orphans)}"))

    barren = _barren_relations(case, vocabulary)
    if barren:
        findings.append(EncodingFinding(level="warning", code="BARREN", detail=f"{len(barren)} top-level relation(s) have weakly anchored entities and may not project reliably."))

    roundtrip = [vocabulary.gloss(relation) for relation in case.relations]
    if policy.require_roundtrip:
        findings.append(EncodingFinding(level="info", code="ROUNDTRIP_REQUIRED", detail="Confirm the generated gloss preserves the source meaning before production ingestion."))

    return EncodingValidation(record_id=case.id, findings=findings, roundtrip=roundtrip, max_order=max_order)


def _kernel_case_payload(case: StructuralCase) -> dict:
    """Normalize CTD role labels at the kernel seam without changing stored data."""
    role_aliases = {
        "ACTOR": "AGENT",
        "PERSON": "HUMAN",
        "COMPONENT": "RESOURCE",
        "STATE": "SIGNAL",
    }
    payload = case.model_dump(mode="json")
    payload["types"] = {
        entity: role_aliases.get(str(role).upper(), str(role).upper())
        for entity, role in case.types.items()
    }
    return payload


def validate_structural_case_strict(case: StructuralCase) -> EncodingValidation:
    """Run the hardened canonical structural schema gate over a CTD case.

    This is additive to the v4 quality gate. It checks predicate arity,
    argument kind, role signatures, contradictions, self-causation and causal
    cycles while returning the existing CTD validation model.
    """
    from .kernel.encoding import validate as kernel_validate
    from .kernel.interop import to_record

    raw = kernel_validate(to_record(_kernel_case_payload(case)))
    level_map = {"warn": "warning", "warning": "warning", "error": "error", "info": "info"}
    findings = [
        EncodingFinding(
            level=level_map.get(item.level, "warning"),
            code=item.code.replace("-", "_"),
            detail=item.detail,
        )
        for item in raw.findings
    ]
    max_order = max((relation.order for relation in case.all_relations()), default=0)
    return EncodingValidation(
        record_id=case.id,
        findings=findings,
        roundtrip=list(raw.roundtrip),
        max_order=max_order,
    )
