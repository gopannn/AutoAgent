from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any, Callable

from pydantic import BaseModel, Field

from .providers_ext import VectorProvider


def normalize_term(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    normalized = re.sub(r"[_-]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip(" \t\r\n.,;:")


class TypeDefinition(BaseModel):
    name: str
    aliases: set[str] = Field(default_factory=set)
    parent: str | None = None


class RelationDefinition(BaseModel):
    name: str
    aliases: set[str] = Field(default_factory=set)
    subject_type: str | None = None
    object_type: str | None = None


class AttributeDefinition(BaseModel):
    entity_type: str
    name: str
    aliases: set[str] = Field(default_factory=set)
    value_type: str = "str"


class EntityDefinition(BaseModel):
    id: str
    entity_type: str
    label: str
    aliases: set[str] = Field(default_factory=set)
    identifying_attributes: dict[str, Any] = Field(default_factory=dict)


class EntityAlternative(BaseModel):
    id: str
    entity_type: str
    score: float
    label: str


class EntityResolution(BaseModel):
    input_term: str
    canonical_id: str | None = None
    canonical_type: str | None = None
    method: str
    score: float = Field(ge=0.0, le=1.0)
    alternatives: list[EntityAlternative] = Field(default_factory=list)


class OntologyRegistry:
    def __init__(self) -> None:
        self._types: dict[str, TypeDefinition] = {}
        self._type_aliases: dict[str, str] = {}
        self._relations: dict[str, RelationDefinition] = {}
        self._relation_aliases: dict[str, str] = {}
        self._attributes: dict[tuple[str, str], AttributeDefinition] = {}
        self._attribute_aliases: dict[str, dict[str, str]] = {}
        self._entities: dict[str, EntityDefinition] = {}
        self._entity_aliases: dict[str, set[str]] = {}

    def register_type(self, name: str, *, aliases: set[str] | None = None, parent: str | None = None) -> None:
        values = set(aliases or set()) | {name}
        definition = TypeDefinition(name=name, aliases=values, parent=parent)
        self._types[name] = definition
        for alias in values:
            self._type_aliases[normalize_term(alias)] = name

    def register_relation(
        self,
        name: str,
        *,
        aliases: set[str] | None = None,
        subject_type: str | None = None,
        object_type: str | None = None,
    ) -> None:
        values = set(aliases or set()) | {name}
        definition = RelationDefinition(
            name=name,
            aliases=values,
            subject_type=subject_type,
            object_type=object_type,
        )
        self._relations[name] = definition
        for alias in values:
            self._relation_aliases[normalize_term(alias)] = name

    def register_attribute(
        self,
        entity_type: str,
        name: str,
        *,
        aliases: set[str] | None = None,
        value_type: str = "str",
    ) -> None:
        values = set(aliases or set()) | {name}
        definition = AttributeDefinition(
            entity_type=entity_type,
            name=name,
            aliases=values,
            value_type=value_type,
        )
        self._attributes[(entity_type, name)] = definition
        mapping = self._attribute_aliases.setdefault(entity_type, {})
        for alias in values:
            mapping[normalize_term(alias)] = name

    def register_entity(
        self,
        entity_id: str,
        entity_type: str,
        label: str,
        *,
        aliases: set[str] | None = None,
        identifying_attributes: dict[str, Any] | None = None,
    ) -> None:
        values = set(aliases or set())
        definition = EntityDefinition(
            id=entity_id,
            entity_type=entity_type,
            label=label,
            aliases=values,
            identifying_attributes=dict(identifying_attributes or {}),
        )
        self._entities[entity_id] = definition
        for alias in {label, *values}:
            self._entity_aliases.setdefault(normalize_term(alias), set()).add(entity_id)

    def canonical_type(self, value: str) -> str | None:
        return self._type_aliases.get(normalize_term(value))

    def canonical_relation(self, value: str) -> str | None:
        return self._relation_aliases.get(normalize_term(value))

    def canonical_attribute(self, entity_type: str, value: str) -> str | None:
        return self._attribute_aliases.get(entity_type, {}).get(normalize_term(value))

    def relation(self, name: str) -> RelationDefinition | None:
        return self._relations.get(name)

    def attribute(self, entity_type: str, name: str) -> AttributeDefinition | None:
        return self._attributes.get((entity_type, name))

    def entity(self, entity_id: str) -> EntityDefinition | None:
        return self._entities.get(entity_id)

    def entities(self, entity_type: str | None = None) -> list[EntityDefinition]:
        return [
            item.model_copy(deep=True)
            for item in sorted(self._entities.values(), key=lambda value: value.id)
            if entity_type is None or self.is_a(item.entity_type, entity_type)
        ]

    def type_aliases(self) -> dict[str, str]:
        return dict(self._type_aliases)

    def relation_aliases(self, *, subject_type: str | None = None) -> dict[str, str]:
        result: dict[str, str] = {}
        for alias, canonical in self._relation_aliases.items():
            definition = self._relations[canonical]
            if subject_type is None or definition.subject_type in (None, subject_type):
                result[alias] = canonical
        return result

    def attribute_aliases(self, entity_type: str) -> dict[str, str]:
        return dict(self._attribute_aliases.get(entity_type, {}))

    def is_a(self, child: str, parent: str) -> bool:
        current: str | None = child
        visited: set[str] = set()
        while current is not None and current not in visited:
            if current == parent:
                return True
            visited.add(current)
            definition = self._types.get(current)
            current = definition.parent if definition else None
        return False

    def snapshot(self) -> dict[str, Any]:
        return {
            "types": [item.model_dump(mode="json") for item in sorted(self._types.values(), key=lambda value: value.name)],
            "relations": [item.model_dump(mode="json") for item in sorted(self._relations.values(), key=lambda value: value.name)],
            "attributes": [item.model_dump(mode="json") for item in sorted(self._attributes.values(), key=lambda value: (value.entity_type, value.name))],
            "entities": [item.model_dump(mode="json") for item in sorted(self._entities.values(), key=lambda value: value.id)],
        }


class EntityResolver:
    def __init__(
        self,
        ontology: OntologyRegistry,
        *,
        fuzzy_threshold: float = 0.82,
        ambiguity_margin: float = 0.02,
        vector_provider: VectorProvider | None = None,
        vectorizer: Callable[[str], list[float]] | None = None,
    ) -> None:
        self.ontology = ontology
        self.fuzzy_threshold = fuzzy_threshold
        self.ambiguity_margin = ambiguity_margin
        self.vector_provider = vector_provider
        self.vectorizer = vectorizer

    def resolve(self, term: str, *, expected_type: str | None = None) -> EntityResolution:
        normalized = normalize_term(term)
        direct = self.ontology.entity(term)
        if direct and (expected_type is None or self.ontology.is_a(direct.entity_type, expected_type)):
            return EntityResolution(input_term=term, canonical_id=direct.id, canonical_type=direct.entity_type, method="exact_id", score=1.0)

        entities = self.ontology.entities(expected_type)
        exact_matches: list[tuple[EntityDefinition, str]] = []
        for entity in entities:
            if normalize_term(entity.label) == normalized:
                exact_matches.append((entity, "label"))
            elif normalized in {normalize_term(alias) for alias in entity.aliases}:
                exact_matches.append((entity, "alias"))
        if len(exact_matches) == 1:
            entity, method = exact_matches[0]
            return EntityResolution(input_term=term, canonical_id=entity.id, canonical_type=entity.entity_type, method=method, score=1.0)
        if len(exact_matches) > 1:
            alternatives = [EntityAlternative(id=e.id, entity_type=e.entity_type, score=1.0, label=e.label) for e, _ in exact_matches]
            return EntityResolution(input_term=term, method="ambiguous", score=1.0, alternatives=alternatives)

        scored: list[EntityAlternative] = []
        for entity in entities:
            candidate_terms = [entity.label, *sorted(entity.aliases)]
            score = max(SequenceMatcher(None, normalized, normalize_term(value)).ratio() for value in candidate_terms)
            if score >= self.fuzzy_threshold:
                scored.append(EntityAlternative(id=entity.id, entity_type=entity.entity_type, score=round(score, 12), label=entity.label))
        scored.sort(key=lambda item: (-item.score, item.id))
        if scored:
            if len(scored) > 1 and scored[0].score - scored[1].score <= self.ambiguity_margin:
                return EntityResolution(input_term=term, method="ambiguous", score=scored[0].score, alternatives=scored[:5])
            top = scored[0]
            return EntityResolution(input_term=term, canonical_id=top.id, canonical_type=top.entity_type, method="fuzzy", score=top.score, alternatives=scored[1:5])

        if self.vector_provider is not None and self.vectorizer is not None:
            matches = self.vector_provider.search(self.vectorizer(term), node_type=expected_type, limit=5)
            if matches:
                top = matches[0]
                return EntityResolution(
                    input_term=term,
                    canonical_id=top.id,
                    canonical_type=top.node_type,
                    method="vector",
                    score=max(0.0, min(1.0, top.score)),
                    alternatives=[
                        EntityAlternative(id=item.id, entity_type=item.node_type, score=max(0.0, min(1.0, item.score)), label=item.id)
                        for item in matches[1:]
                    ],
                )

        return EntityResolution(input_term=term, method="unresolved", score=0.0)
