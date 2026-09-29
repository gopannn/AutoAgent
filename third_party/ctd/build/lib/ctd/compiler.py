from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from .models import AttributeConstraint, QueryConstraintGraph, RelationConstraint, Variable
from .ontology import EntityResolver, OntologyRegistry, normalize_term


class CompileDiagnostic(BaseModel):
    code: str
    severity: str = "warning"
    message: str
    clause: str | None = None


class CompileResult(BaseModel):
    query: QueryConstraintGraph | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    diagnostics: list[CompileDiagnostic] = Field(default_factory=list)
    unresolved_terms: list[str] = Field(default_factory=list)
    extracted: list[dict[str, Any]] = Field(default_factory=list)


_COMPARATORS: list[tuple[str, str]] = [
    ("at most", "lte"),
    ("at least", "gte"),
    ("<=", "lte"),
    (">=", "gte"),
    ("under", "lt"),
    ("below", "lt"),
    ("over", "gt"),
    ("above", "gt"),
    ("equals", "eq"),
    ("is", "eq"),
    ("=", "eq"),
]


class NaturalLanguageCompiler:
    def __init__(
        self,
        ontology: OntologyRegistry,
        *,
        entity_resolver: EntityResolver | None = None,
    ) -> None:
        self.ontology = ontology
        self.entity_resolver = entity_resolver or EntityResolver(ontology)

    def compile(self, text: str, *, as_of: datetime, query_class: str | None = None) -> CompileResult:
        source = text.strip()
        normalized = normalize_term(source)
        diagnostics: list[CompileDiagnostic] = []
        unresolved: list[str] = []
        extracted: list[dict[str, Any]] = []

        top_k = 1
        top_match = re.search(r"(?:^|[;,.\s])top\s+(\d+)\s*\.?$", normalized)
        if top_match:
            top_k = max(1, min(100, int(top_match.group(1))))
            normalized = normalized[: top_match.start()].strip(" ;,.")

        find_match = re.match(r"^find\s+(?:(?:a|an|the)\s+)?(.+)$", normalized)
        if not find_match:
            return CompileResult(
                confidence=0.0,
                diagnostics=[CompileDiagnostic(code="missing_find", severity="error", message="Query must begin with 'find'.")],
                unresolved_terms=[source],
            )
        remainder = find_match.group(1)

        type_match: tuple[str, str] | None = None
        for alias, canonical in sorted(self.ontology.type_aliases().items(), key=lambda item: (-len(item[0]), item[0])):
            if remainder == alias or remainder.startswith(alias + " "):
                type_match = (alias, canonical)
                break
        if type_match is None:
            return CompileResult(
                confidence=0.0,
                diagnostics=[CompileDiagnostic(code="unknown_target_type", severity="error", message="Target entity type is not in the ontology.")],
                unresolved_terms=[remainder],
            )

        type_alias, target_type = type_match
        target_var = self._variable_name(target_type, set())
        variables = [Variable(name=target_var, node_type=target_type)]
        used_names = {target_var}
        remainder = remainder[len(type_alias) :].strip()
        remainder = re.sub(r"^(?:that|which|with)\s+", "", remainder)
        clauses = [item.strip() for item in re.split(r"\s*,\s*|\s+and\s+", remainder) if item.strip()]

        relations: list[RelationConstraint] = []
        attributes: list[AttributeConstraint] = []
        relation_counter = 0
        attribute_counter = 0
        resolved_units = 1
        total_units = 1 + len(clauses)

        for clause in clauses:
            relation_match = self._parse_relation(clause, target_type)
            if relation_match is not None:
                canonical_relation, object_text = relation_match
                definition = self.ontology.relation(canonical_relation)
                object_type = definition.object_type if definition else None
                if object_type is None:
                    diagnostics.append(CompileDiagnostic(code="relation_missing_object_type", message=f"Relation {canonical_relation} has no object type.", clause=clause))
                    unresolved.append(clause)
                    continue
                resolution = self.entity_resolver.resolve(object_text, expected_type=object_type)
                if resolution.canonical_id is None:
                    diagnostics.append(CompileDiagnostic(code="unresolved_entity", message=f"Could not resolve entity in clause: {clause}", clause=clause))
                    unresolved.append(object_text)
                    continue
                object_var = self._variable_name(object_type, used_names)
                used_names.add(object_var)
                variables.append(Variable(name=object_var, node_type=object_type))
                relation_counter += 1
                relations.append(
                    RelationConstraint(
                        id=f"r:{relation_counter:03d}",
                        subject_var=target_var,
                        relation=canonical_relation,
                        object_var=object_var,
                    )
                )
                entity = self.ontology.entity(resolution.canonical_id)
                if entity is not None:
                    for attr_name, value in entity.identifying_attributes.items():
                        attribute_counter += 1
                        attributes.append(
                            AttributeConstraint(
                                id=f"a:{attribute_counter:03d}",
                                variable=object_var,
                                attribute=attr_name,
                                op="eq",
                                value=value,
                            )
                        )
                extracted.append({"kind": "relation", "clause": clause, "relation": canonical_relation, "entity_id": resolution.canonical_id, "method": resolution.method})
                resolved_units += 1
                continue

            attribute_match = self._parse_attribute(clause, target_type)
            if attribute_match is not None:
                attr_name, operator, raw_value = attribute_match
                definition = self.ontology.attribute(target_type, attr_name)
                try:
                    value = self._parse_value(raw_value, definition.value_type if definition else "str")
                except ValueError:
                    diagnostics.append(CompileDiagnostic(code="invalid_value", message=f"Could not parse value in clause: {clause}", clause=clause))
                    unresolved.append(clause)
                    continue
                attribute_counter += 1
                attributes.append(
                    AttributeConstraint(
                        id=f"a:{attribute_counter:03d}",
                        variable=target_var,
                        attribute=attr_name,
                        op=operator,  # type: ignore[arg-type]
                        value=value,
                    )
                )
                extracted.append({"kind": "attribute", "clause": clause, "attribute": attr_name, "operator": operator, "value": value})
                resolved_units += 1
                continue

            diagnostics.append(CompileDiagnostic(code="unknown_clause", message=f"Unknown clause: {clause}", clause=clause))
            unresolved.append(clause)

        query = QueryConstraintGraph(
            variables=variables,
            relations=relations,
            attributes=attributes,
            as_of=as_of,
            objective=source,
            top_k=top_k,
            query_class=query_class,
        )
        confidence = round(resolved_units / max(1, total_units), 6)
        return CompileResult(
            query=query,
            confidence=confidence,
            diagnostics=diagnostics,
            unresolved_terms=unresolved,
            extracted=extracted,
        )

    def _parse_relation(self, clause: str, subject_type: str) -> tuple[str, str] | None:
        normalized = normalize_term(clause)
        aliases = self.ontology.relation_aliases(subject_type=subject_type)
        for alias, canonical in sorted(aliases.items(), key=lambda item: (-len(item[0]), item[0])):
            if normalized == alias:
                continue
            if normalized.startswith(alias + " "):
                remainder = normalized[len(alias) :].strip()
                remainder = re.sub(r"^(?:is|equals)\s+", "", remainder)
                if remainder:
                    return canonical, remainder
        return None

    def _parse_attribute(self, clause: str, entity_type: str) -> tuple[str, str, str] | None:
        normalized = normalize_term(clause)
        aliases = self.ontology.attribute_aliases(entity_type)
        for alias, canonical in sorted(aliases.items(), key=lambda item: (-len(item[0]), item[0])):
            if not normalized.startswith(alias + " "):
                continue
            remainder = normalized[len(alias) :].strip()
            for phrase, operator in _COMPARATORS:
                if remainder.startswith(phrase + " ") or remainder == phrase:
                    value = remainder[len(phrase) :].strip()
                    if value:
                        return canonical, operator, value
        return None

    @staticmethod
    def _parse_value(raw: str, value_type: str) -> Any:
        value = raw.strip()
        if value_type in {"int", "float", "currency", "days"}:
            match = re.search(r"[-+]?\d+(?:\.\d+)?", value.replace(",", ""))
            if not match:
                raise ValueError("numeric value required")
            number = float(match.group(0))
            if value_type in {"int", "days"} and number.is_integer():
                return int(number)
            return number
        if value_type == "bool":
            normalized = normalize_term(value)
            if normalized in {"true", "yes", "active"}:
                return True
            if normalized in {"false", "no", "inactive"}:
                return False
            raise ValueError("boolean value required")
        return value.strip('"\'')

    @staticmethod
    def _variable_name(entity_type: str, used: set[str]) -> str:
        base = re.sub(r"[^a-z0-9]+", "_", entity_type.casefold()).strip("_") or "entity"
        if base not in used:
            return base
        index = 2
        while f"{base}_{index}" in used:
            index += 1
        return f"{base}_{index}"
