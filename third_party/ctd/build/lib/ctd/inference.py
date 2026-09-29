from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from hashlib import sha256

from pydantic import BaseModel, Field

from .models import Edge, Evidence


class InferenceRule(BaseModel):
    id: str
    premise_relation_types: list[str] = Field(min_length=1)
    output_relation_type: str
    confidence_multiplier: float = Field(default=1.0, ge=0.0, le=1.0)
    max_depth: int = Field(default=1, ge=1)




class RuleRegistry:
    """Deterministic registry for explicitly enabled inference rules."""

    def __init__(self, rules: list[InferenceRule] | None = None) -> None:
        self._rules: dict[str, InferenceRule] = {}
        for rule in rules or []:
            self.register(rule)

    def register(self, rule: InferenceRule) -> None:
        self._rules[rule.id] = rule

    def rules(self) -> list[InferenceRule]:
        return [self._rules[key] for key in sorted(self._rules)]


class InferenceEngine:
    """Deterministic acyclic relation-chain inference.

    Rules are intentionally small and declarative. Derived edges retain all
    premise evidence IDs in lineage and never revisit a node already in the
    current path, preventing cyclic expansion.
    """

    def __init__(self, registry: RuleRegistry | None = None) -> None:
        self.registry = registry

    @property
    def rules_enabled(self) -> bool:
        return self.registry is not None and bool(self.registry.rules())

    def derive(
        self,
        edges: list[Edge],
        rules: list[InferenceRule] | None = None,
        *,
        max_depth: int,
    ) -> list[Edge]:
        active_rules = rules if rules is not None else (self.registry.rules() if self.registry else [])
        if not active_rules:
            return []
        pool = list(edges)
        derived: dict[tuple[str, str, str, str], Edge] = {}

        # Iterative passes allow one derived relation to become a premise for a
        # later rule while a hard generation-depth ceiling prevents recursion.
        for _ in range(max_depth):
            added_this_round = 0
            by_source_type: dict[tuple[str, str], list[Edge]] = defaultdict(list)
            for edge in pool:
                by_source_type[(edge.source, edge.type)].append(edge)
            for bucket in by_source_type.values():
                bucket.sort(key=lambda item: item.id)

            for rule in sorted(active_rules, key=lambda item: item.id):
                for start_edge in sorted(pool, key=lambda item: item.id):
                    if start_edge.type != rule.premise_relation_types[0]:
                        continue
                    paths = [([start_edge], {start_edge.source, start_edge.target})]
                    for relation_type in rule.premise_relation_types[1:]:
                        next_paths = []
                        for path, visited in paths:
                            tail = path[-1].target
                            for edge in by_source_type.get((tail, relation_type), []):
                                if edge.target in visited:
                                    continue
                                next_paths.append(([*path, edge], {*visited, edge.target}))
                        paths = next_paths
                        if not paths:
                            break

                    for path, _ in paths:
                        generation_depth = max(
                            int(edge.attributes.get("inference_depth", 0)) for edge in path
                        ) + 1
                        if generation_depth > max_depth or generation_depth > rule.max_depth:
                            continue
                        source = path[0].source
                        target = path[-1].target
                        signature = (rule.id, source, target, rule.output_relation_type)
                        if signature in derived:
                            continue
                        evidence_records = [ev for edge in path for ev in edge.evidence]
                        if not evidence_records:
                            continue
                        lineage = sorted({ev.id for ev in evidence_records} | {line for ev in evidence_records for line in ev.lineage})
                        observed_at = max(ev.observed_at for ev in evidence_records)
                        confidence = min(ev.confidence for ev in evidence_records) * rule.confidence_multiplier
                        trust = min(ev.trust for ev in evidence_records)
                        valid_from_values = [ev.valid_from for ev in evidence_records if ev.valid_from is not None]
                        valid_until_values = [ev.valid_until for ev in evidence_records if ev.valid_until is not None]
                        valid_from = max(valid_from_values) if valid_from_values else None
                        valid_until = min(valid_until_values) if valid_until_values else None
                        if valid_from is not None and valid_until is not None and valid_from > valid_until:
                            continue
                        raw_id = f"{rule.id}|{source}|{target}|{generation_depth}"
                        digest = sha256(raw_id.encode("utf-8")).hexdigest()[:16]
                        inferred_evidence = Evidence(
                            id=f"inferred-evidence:{digest}",
                            source_id=f"inference:{rule.id}",
                            source_type="inference",
                            observed_at=observed_at,
                            valid_from=valid_from,
                            valid_until=valid_until,
                            confidence=round(confidence, 12),
                            trust=trust,
                            independence_group=f"inference:{rule.id}:{source}:{target}",
                            extraction_method=f"rule:{rule.id}",
                            direct=False,
                            lineage=lineage,
                        )
                        inferred = Edge(
                            id=f"inferred:{digest}",
                            source=source,
                            target=target,
                            type=rule.output_relation_type,
                            attributes={
                                "inference_rule": rule.id,
                                "inference_depth": generation_depth,
                            },
                            evidence=[inferred_evidence],
                        )
                        derived[signature] = inferred
                        added_this_round += 1
            if added_this_round == 0:
                break
            pool = [*edges, *derived.values()]

        return sorted(derived.values(), key=lambda edge: edge.id)
