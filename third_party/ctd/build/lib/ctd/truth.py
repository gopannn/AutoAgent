from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from math import prod
from typing import Any

from pydantic import BaseModel, Field

from .models import Claim, ClaimStatus, Evidence


class EvidenceAssessment(BaseModel):
    strength: float = Field(ge=0.0, le=1.0)
    freshness: float = Field(ge=0.0, le=1.0)
    source_diversity: int = Field(ge=0)
    accepted_evidence_ids: list[str] = Field(default_factory=list)
    rejected_evidence_ids: list[str] = Field(default_factory=list)
    rejection_reasons: dict[str, str] = Field(default_factory=dict)
    source_classes: set[str] = Field(default_factory=set)
    direct_count: int = Field(default=0, ge=0)
    max_inference_depth: int = Field(default=0, ge=0)


class ClaimAssessment(BaseModel):
    key: str | None = None
    value: Any = None
    status: ClaimStatus
    contradiction_risk: float = Field(ge=0.0, le=1.0)
    active_claims: list[Claim] = Field(default_factory=list)
    superseded_claims: list[Claim] = Field(default_factory=list)


class EvidenceFusion:
    """Deterministic evidence aggregation with correlation discounting."""

    @staticmethod
    def _freshness(evidence: Evidence, as_of: datetime) -> float:
        if not evidence.is_valid_at(as_of):
            return 0.0
        age_seconds = max(0.0, (as_of - evidence.observed_at).total_seconds())
        age_days = age_seconds / 86400.0
        # Evidence loses at most half its freshness over a ten-year horizon.
        return max(0.5, 1.0 - min(age_days, 3650.0) / 7300.0)

    def fuse(
        self,
        evidence: list[Evidence],
        *,
        as_of: datetime,
        required_source_classes: set[str] | None = None,
        max_inference_depth: int | None = None,
    ) -> EvidenceAssessment:
        required_source_classes = required_source_classes or set()
        accepted: list[tuple[Evidence, float, float]] = []
        rejected: list[str] = []
        rejection_reasons: dict[str, str] = {}

        for item in evidence:
            freshness = self._freshness(item, as_of)
            inferred_depth = len(item.lineage) if not item.direct else 0
            allowed_source = not required_source_classes or item.source_type in required_source_classes
            allowed_depth = max_inference_depth is None or inferred_depth <= max_inference_depth
            if freshness <= 0.0:
                rejected.append(item.id)
                rejection_reasons[item.id] = "temporally_invalid"
                continue
            if not allowed_source:
                rejected.append(item.id)
                rejection_reasons[item.id] = "source_class_not_allowed"
                continue
            if not allowed_depth:
                rejected.append(item.id)
                rejection_reasons[item.id] = "inference_depth_exceeded"
                continue
            weight = min(1.0, max(0.0, item.confidence * item.trust * freshness))
            accepted.append((item, weight, freshness))

        # Correlated copies are one evidence family. Keep only the strongest
        # member from each family instead of allowing duplicate multiplication.
        grouped: dict[str, list[tuple[Evidence, float, float]]] = defaultdict(list)
        for item, weight, freshness in accepted:
            group = item.independence_group or item.source_id
            grouped[group].append((item, weight, freshness))

        representatives = [
            max(group, key=lambda entry: (entry[1], entry[0].id))
            for _, group in sorted(grouped.items(), key=lambda pair: pair[0])
        ]
        weights = [entry[1] for entry in representatives]
        strength = 1.0 - prod(1.0 - weight for weight in weights) if weights else 0.0
        freshness = (
            sum(entry[2] for entry in representatives) / len(representatives)
            if representatives
            else 0.0
        )
        all_accepted_ids = sorted(item.id for item, _, _ in accepted)
        source_classes = {item.source_type for item, _, _ in accepted}
        direct_count = sum(1 for item, _, _ in accepted if item.direct)
        inference_depth = max(
            (len(item.lineage) if not item.direct else 0 for item, _, _ in accepted),
            default=0,
        )
        return EvidenceAssessment(
            strength=round(strength, 12),
            freshness=round(freshness, 12),
            source_diversity=len(grouped),
            accepted_evidence_ids=all_accepted_ids,
            rejected_evidence_ids=sorted(rejected),
            rejection_reasons=rejection_reasons,
            source_classes=source_classes,
            direct_count=direct_count,
            max_inference_depth=inference_depth,
        )


class TruthMaintainer:
    """Evaluates temporal claim compatibility without collapsing history."""

    @staticmethod
    def _valid(claim: Claim, as_of: datetime) -> bool:
        if claim.valid_from is not None and as_of < claim.valid_from:
            return False
        if claim.valid_until is not None and as_of > claim.valid_until:
            return False
        return True

    def evaluate_claims(self, claims: list[Claim], *, as_of: datetime) -> ClaimAssessment:
        if not claims:
            return ClaimAssessment(status=ClaimStatus.UNKNOWN, contradiction_risk=0.0)

        key = claims[0].key
        same_key = [claim for claim in claims if claim.key == key]
        active = [claim for claim in same_key if self._valid(claim, as_of)]
        historical = [claim for claim in same_key if claim not in active]

        if not active:
            return ClaimAssessment(
                key=key,
                status=ClaimStatus.UNKNOWN,
                contradiction_risk=0.0,
                superseded_claims=historical,
            )

        active_values = {repr(claim.value) for claim in active}
        if len(active_values) > 1:
            return ClaimAssessment(
                key=key,
                status=ClaimStatus.DISPUTED,
                contradiction_risk=1.0,
                active_claims=active,
                superseded_claims=historical,
            )

        chosen = max(
            active,
            key=lambda claim: (
                claim.valid_from or datetime.min.replace(tzinfo=as_of.tzinfo),
                repr(claim.value),
            ),
        )
        historical_values = {repr(claim.value) for claim in historical}
        status = (
            ClaimStatus.SUPERSEDED
            if historical_values and any(value != repr(chosen.value) for value in historical_values)
            else ClaimStatus.SUPPORTED
        )
        risk = 0.15 if status == ClaimStatus.SUPERSEDED else 0.0
        return ClaimAssessment(
            key=key,
            value=chosen.value,
            status=status,
            contradiction_risk=risk,
            active_claims=active,
            superseded_claims=historical,
        )
