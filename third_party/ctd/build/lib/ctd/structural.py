from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Union

from pydantic import BaseModel, ConfigDict, Field


class StructuralRelation(BaseModel):
    model_config = ConfigDict(frozen=True)

    pred: str
    args: tuple[Union[str, "StructuralRelation"], ...]

    @property
    def order(self) -> int:
        nested = [arg.order for arg in self.args if isinstance(arg, StructuralRelation)]
        return 1 + (max(nested) if nested else 0)

    def canonical_key(self) -> str:
        def render(value: Union[str, StructuralRelation]) -> str:
            if isinstance(value, StructuralRelation):
                return value.canonical_key()
            return value

        return f"{self.pred.upper()}({','.join(render(arg) for arg in self.args)})"


StructuralRelation.model_rebuild()


def walk_relations(relations: Iterable[StructuralRelation]) -> list[StructuralRelation]:
    out: list[StructuralRelation] = []
    seen: set[StructuralRelation] = set()

    def visit(relation: StructuralRelation) -> None:
        if relation in seen:
            return
        seen.add(relation)
        out.append(relation)
        for arg in relation.args:
            if isinstance(arg, StructuralRelation):
                visit(arg)

    for relation in relations:
        visit(relation)
    return out


class StructuralCase(BaseModel):
    id: str
    domain: str
    relations: tuple[StructuralRelation, ...]
    types: dict[str, str] = Field(default_factory=dict)
    text: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    source_refs: list[str] = Field(default_factory=list)
    tenant_id: str | None = None
    security_label: str | None = None
    independence_group: str | None = None
    severity: int | None = Field(default=None, ge=1, le=5)
    check_templates: dict[str, str] = Field(default_factory=dict)

    def all_relations(self) -> list[StructuralRelation]:
        return walk_relations(self.relations)

    def predicates(self) -> set[str]:
        return {relation.pred.upper() for relation in self.all_relations()}

    def entities(self) -> set[str]:
        entities: set[str] = set()
        for relation in self.all_relations():
            for arg in relation.args:
                if isinstance(arg, str):
                    entities.add(arg)
        return entities


class StructuralTarget(BaseModel):
    name: str
    domain: str
    relations: tuple[StructuralRelation, ...]
    types: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    tenant_id: str | None = None
    security_label: str | None = None

    def all_relations(self) -> list[StructuralRelation]:
        return walk_relations(self.relations)

    def predicates(self) -> set[str]:
        return {relation.pred.upper() for relation in self.all_relations()}

    def entities(self) -> set[str]:
        entities: set[str] = set()
        for relation in self.all_relations():
            for arg in relation.args:
                if isinstance(arg, str):
                    entities.add(arg)
        return entities

    def with_relation(self, relation: StructuralRelation) -> "StructuralTarget":
        return self.model_copy(update={"relations": (*self.relations, relation)}, deep=True)


class PredicateVocabulary(BaseModel):
    predicates: set[str]
    families: dict[str, set[str]] = Field(default_factory=dict)
    kinship_weight: float = Field(default=0.5, gt=0.0, lt=1.0)
    glosses: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def core(cls) -> "PredicateVocabulary":
        predicates = {
            "FLOWS", "DEPENDS", "SHARED", "EXCESS", "SCARCE", "CAUSES",
            "INCREASES", "DECREASES", "AMPLIFIES", "SENSES", "REDUCES",
            "REDISTRIBUTES", "RETRIES", "DELAYED", "OSCILLATES", "QUEUEING",
            "SATURATES", "EXHAUSTS", "FAILS", "CASCADES", "BLOCKS", "LIMITS",
            "TRIGGERS", "CONSUMES", "CONTENDS", "RECOVERS", "DROPS", "THROTTLES",
        }
        families = {
            "ACTION": {"RETRIES", "REDUCES", "REDISTRIBUTES", "THROTTLES"},
            "OVERLOAD": {"SATURATES", "EXHAUSTS", "FAILS"},
            "GROWTH": {"INCREASES", "AMPLIFIES"},
            "DEPENDENCY": {"DEPENDS", "CONSUMES", "CONTENDS"},
        }
        glosses = {
            "FLOWS": "{0} moves through {1}",
            "DEPENDS": "{0} depends on {1}",
            "SHARED": "{0} is shared across {1}",
            "EXCESS": "there is too much {0}",
            "SCARCE": "{0} is scarce",
            "CAUSES": "{0}, which causes {1}",
            "INCREASES": "{0} drives up {1}",
            "DECREASES": "{0} brings down {1}",
            "AMPLIFIES": "{0} is amplified at {1}",
            "SENSES": "{0} observes {1}",
            "REDUCES": "{0} cuts back {1}",
            "REDISTRIBUTES": "{0} spreads across {1}",
            "RETRIES": "{0} retries against {1}",
            "DELAYED": "{0} arrives late",
            "OSCILLATES": "{0} oscillates",
            "QUEUEING": "work queues at {0}",
            "SATURATES": "{0} saturates",
            "EXHAUSTS": "{0} exhausts {1}",
            "FAILS": "{0} fails",
            "CASCADES": "{0} cascades through {1}",
            "BLOCKS": "{0} blocks {1}",
            "LIMITS": "{0} limits {1}",
            "TRIGGERS": "{0} triggers {1}",
            "CONSUMES": "{0} consumes {1}",
            "CONTENDS": "{0} contends for {1}",
            "RECOVERS": "{0} recovers {1}",
            "DROPS": "{0} drops {1}",
            "THROTTLES": "{0} throttles {1}",
        }
        return cls(predicates=predicates, families=families, glosses=glosses)

    def family(self, predicate: str) -> str | None:
        predicate = predicate.upper()
        for name, members in self.families.items():
            if predicate in members:
                return name
        return None

    def compatibility(self, left: str, right: str) -> float:
        left, right = left.upper(), right.upper()
        if left == right:
            return 1.0
        left_family = self.family(left)
        right_family = self.family(right)
        if left_family is not None and left_family == right_family:
            return self.kinship_weight
        return 0.0

    def gloss(self, relation: StructuralRelation) -> str:
        parts = [self.gloss(arg) if isinstance(arg, StructuralRelation) else str(arg) for arg in relation.args]
        template = self.glosses.get(relation.pred.upper())
        if template is None:
            return f"{relation.pred.upper()}({', '.join(parts)})"
        try:
            return template.format(*parts)
        except (IndexError, KeyError):
            return f"{relation.pred.upper()}({', '.join(parts)})"
