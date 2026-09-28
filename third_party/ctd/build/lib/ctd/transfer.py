from __future__ import annotations

from enum import StrEnum
from math import log1p
from time import perf_counter
from typing import Any

from pydantic import BaseModel, Field

from .structural import PredicateVocabulary, StructuralCase, StructuralRelation, StructuralTarget


class HypothesisState(StrEnum):
    HYPOTHESIS = "HYPOTHESIS"
    UNDER_TEST = "UNDER_TEST"
    SUPPORTED = "SUPPORTED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


class TransferPolicy(BaseModel):
    mac_keep: int = Field(default=8, ge=1, le=1000)
    beam_width: int = Field(default=24, ge=1, le=1000)
    mappings_per_case: int = Field(default=4, ge=1, le=100)
    min_depth: int = Field(default=2, ge=1)
    min_systematicity: float = Field(default=1.0, ge=0.0)
    cross_domain_only: bool = True
    max_hypotheses: int = Field(default=50, ge=1, le=1000)
    # Unified-v5 controls. Legacy defaults deliberately preserve v4 behavior.
    strict_schema: bool = False
    guard_projection: bool = False
    use_types: bool = True
    use_kinship: bool = True
    max_alignment_hypotheses: int = Field(default=600, ge=1)
    demote_subsumed: bool = True


class MappingLineage(BaseModel):
    source_case_id: str
    source_domain: str
    entity_map: dict[str, str]
    relation_map: dict[str, str]
    systematicity: float
    depth: int
    exact_matches: int
    family_matches: int


class StructuralHypothesis(BaseModel):
    relation: StructuralRelation
    state: HypothesisState = HypothesisState.HYPOTHESIS
    source_cases: list[str] = Field(default_factory=list)
    source_domains: list[str] = Field(default_factory=list)
    independence_groups: list[str] = Field(default_factory=list)
    convergence: int = Field(default=1, ge=1)
    source_case_count: int = Field(default=1, ge=1)
    systematicity: float = Field(default=0.0, ge=0.0)
    mapping_depth: int = Field(default=0, ge=0)
    exact_matches: int = Field(default=0, ge=0)
    family_matches: int = Field(default=0, ge=0)
    priority_score: float = Field(default=0.0, ge=0.0)
    verification_procedure: str | None = None
    lineage: list[MappingLineage] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    looseness: float = Field(default=0.0, ge=0.0, le=1.0)


class TransferResult(BaseModel):
    target_name: str
    cases_examined: int = 0
    mac_retained: int = 0
    fac_mappings_considered: int = 0
    hypotheses_projected: int = 0
    hypotheses_deduplicated: int = 0
    hypotheses: list[StructuralHypothesis] = Field(default_factory=list)
    elapsed_ms: float = 0.0
    rejected_cases: list[dict[str, Any]] = Field(default_factory=list)
    blocked_projections: list[dict[str, str]] = Field(default_factory=list)


def _relation_from_kernel(rel: Any) -> StructuralRelation:
    return StructuralRelation(
        pred=rel.pred,
        args=tuple(_relation_from_kernel(a) if hasattr(a, "pred") else a for a in rel.args),
    )


def _role_normalized_payload(obj: StructuralCase | StructuralTarget) -> dict[str, Any]:
    aliases = {
        "ACTOR": "AGENT",
        "PERSON": "HUMAN",
        "COMPONENT": "RESOURCE",
        "STATE": "SIGNAL",
    }
    payload = obj.model_dump(mode="json")
    payload["types"] = {
        entity: aliases.get(str(role).upper(), str(role).upper())
        for entity, role in obj.types.items()
    }
    return payload


class StructuralTransferEngine:
    """CTD public transfer contract backed by the canonical v5 kernel.

    v4 calls retain permissive schema/projection defaults for compatibility.
    Unified-v5 callers can enable ``strict_schema`` and ``guard_projection``.
    In both modes the actual MAC/FAC alignment and projection algorithm is the
    single implementation in ``ctd.kernel``.
    """

    def __init__(self, vocabulary: PredicateVocabulary | None = None) -> None:
        self.vocabulary = vocabulary or PredicateVocabulary.core()

    def mac_score(self, target: StructuralTarget, case: StructuralCase) -> float:
        from .kernel.interop import to_record, to_target
        from .kernel.transfer import mac_score
        return mac_score(
            to_target(_role_normalized_payload(target)),
            to_record(_role_normalized_payload(case)),
            use_kinship=True,
        )

    def transfer(
        self,
        cases: list[StructuralCase],
        target: StructuralTarget,
        policy: TransferPolicy | None = None,
    ) -> TransferResult:
        from .kernel.encoding import validate as kernel_validate
        from .kernel.interop import canonical_key, to_record, to_target
        from .kernel.schema import CORE_SCHEMA
        from .kernel.transfer import Knobs, align_k_best, link_conflicts, mac_score, project, subsumed_relations, Inference

        started = perf_counter()
        policy = policy or TransferPolicy()
        kernel_target = to_target(_role_normalized_payload(target))
        knobs = Knobs(
            mac_keep=policy.mac_keep,
            min_depth=policy.min_depth,
            rounds=1,
            beam=policy.beam_width,
            max_hypotheses=policy.max_alignment_hypotheses,
            mappings_per_case=policy.mappings_per_case,
            aligner="beam",
            use_types=policy.use_types,
            use_kinship=policy.use_kinship,
            guard_projection=policy.guard_projection,
            demote_subsumed=policy.demote_subsumed,
        )

        records = []
        case_by_id: dict[str, StructuralCase] = {}
        rejected: list[dict[str, Any]] = []
        for case in cases:
            if policy.cross_domain_only and case.domain == target.domain:
                continue
            rec = to_record(_role_normalized_payload(case))
            if policy.strict_schema:
                validation = kernel_validate(rec, CORE_SCHEMA)
                if not validation.ok:
                    rejected.append({
                        "case_id": case.id,
                        "findings": [
                            {"level": f.level, "code": f.code, "detail": f.detail}
                            for f in validation.findings
                        ],
                    })
                    continue
            records.append(rec)
            case_by_id[rec.id] = case

        scored = sorted(
            ((rec, mac_score(kernel_target, rec, CORE_SCHEMA, policy.use_kinship)) for rec in records),
            key=lambda pair: (-pair[1], pair[0].id),
        )
        retained = [rec for rec, score in scored[: policy.mac_keep] if score > 0]

        raw_pool: dict[Any, Inference] = {}
        lineage_by_rel: dict[Any, list[MappingLineage]] = {}
        stats_by_rel: dict[Any, tuple[int, int, int, float]] = {}
        blocked_output: list[dict[str, str]] = []
        mapping_count = 0

        for rec in retained:
            mappings = align_k_best(rec, kernel_target, k=policy.mappings_per_case, knobs=knobs, schema=CORE_SCHEMA)
            for mapping in mappings:
                if mapping.depth < policy.min_depth or mapping.systematicity < policy.min_systematicity:
                    continue
                mapping_count += 1
                admitted, blocked = project(mapping, kernel_target, knobs, CORE_SCHEMA)
                for item in blocked:
                    blocked_output.append({
                        "case_id": rec.id,
                        "relation": canonical_key(item.rel),
                        "reason": item.reason,
                    })
                for rel in admitted:
                    inf = raw_pool.setdefault(rel, Inference(rel))
                    if rec.id not in inf.sources:
                        inf.sources.append(rec.id)
                    inf.domains.add(rec.domain)
                    fingerprint = rec.structure_fingerprint()
                    inf.fingerprints.add(fingerprint)
                    group = str(rec.attrs.get("independence_group") or fingerprint)
                    inf.groups.add(group)
                    if mapping.systematicity > inf.systematicity:
                        inf.systematicity = mapping.systematicity
                        inf.looseness = mapping.looseness
                    relation_map = {
                        canonical_key(left): canonical_key(right)
                        for left, right in mapping.rel_map.items()
                    }
                    lineage = MappingLineage(
                        source_case_id=rec.id,
                        source_domain=rec.domain,
                        entity_map=dict(mapping.ent_map),
                        relation_map=relation_map,
                        systematicity=mapping.systematicity,
                        depth=mapping.depth,
                        exact_matches=mapping.exact_matches,
                        family_matches=mapping.family_matches,
                    )
                    existing = lineage_by_rel.setdefault(rel, [])
                    sig = (lineage.source_case_id, tuple(sorted(lineage.entity_map.items())), tuple(sorted(lineage.relation_map.items())))
                    if not any((x.source_case_id, tuple(sorted(x.entity_map.items())), tuple(sorted(x.relation_map.items()))) == sig for x in existing):
                        existing.append(lineage)
                    prev = stats_by_rel.get(rel, (0, 0, 0, 0.0))
                    if mapping.systematicity >= prev[3]:
                        stats_by_rel[rel] = (mapping.depth, mapping.exact_matches, mapping.family_matches, mapping.systematicity)

        pool = list(raw_pool.values())
        link_conflicts(pool, kernel_target, CORE_SCHEMA)
        subsumed = subsumed_relations(raw_pool) if policy.demote_subsumed else set()
        pool.sort(key=lambda inf: (inf.rel in subsumed, -inf.score, str(inf.rel)))

        hypotheses: list[StructuralHypothesis] = []
        for inf in pool[: policy.max_hypotheses]:
            depth, exact, family, systematicity = stats_by_rel.get(inf.rel, (inf.rel.order, 0, 0, inf.systematicity))
            if policy.strict_schema:
                convergence = max(1, inf.convergence)
            else:
                # Preserve v4 convergence semantics: independent domains and
                # declared independence groups, without penalizing two
                # structurally identical but independently observed cases.
                counts = [len(inf.domains)]
                if inf.groups:
                    counts.append(len(inf.groups))
                convergence = max(1, min(counts))
            # Preserve CTD's historical convergence-weighted score shape while
            # incorporating the kernel's looseness penalty.
            priority = systematicity * (1.0 + log1p(max(0, convergence - 1))) * (1.0 - 0.35 * inf.looseness)
            hypotheses.append(
                StructuralHypothesis(
                    relation=_relation_from_kernel(inf.rel),
                    source_cases=sorted(inf.sources),
                    source_domains=sorted(inf.domains),
                    independence_groups=sorted(inf.groups),
                    convergence=convergence,
                    source_case_count=len(inf.sources),
                    systematicity=systematicity,
                    mapping_depth=depth,
                    exact_matches=exact,
                    family_matches=family,
                    priority_score=round(max(priority, 0.0), 12),
                    lineage=lineage_by_rel.get(inf.rel, []),
                    conflicts=[canonical_key(item) for item in inf.conflicts],
                    looseness=inf.looseness,
                )
            )

        hypotheses.sort(key=lambda h: (-h.priority_score, -h.convergence, h.relation.canonical_key()))
        raw_count = sum(len(v) for v in lineage_by_rel.values())
        return TransferResult(
            target_name=target.name,
            cases_examined=len(cases),
            mac_retained=len(retained),
            fac_mappings_considered=mapping_count,
            hypotheses_projected=raw_count,
            hypotheses_deduplicated=max(0, raw_count - len(hypotheses)),
            hypotheses=hypotheses,
            elapsed_ms=round((perf_counter() - started) * 1000.0, 3),
            rejected_cases=rejected,
            blocked_projections=blocked_output,
        )
