from __future__ import annotations

from datetime import UTC, datetime

from ctd.compiler import NaturalLanguageCompiler
from ctd.ontology import EntityResolver, OntologyRegistry
from ctd.providers_ext import VectorProvider, VectorRecord

NOW = datetime(2026, 9, 12, tzinfo=UTC)


def ontology() -> OntologyRegistry:
    registry = OntologyRegistry()
    registry.register_type("Supplier", aliases={"supplier", "vendor"})
    registry.register_type("Component", aliases={"component", "part"})
    registry.register_type("Certification", aliases={"certification", "certificate"})
    registry.register_relation("PRODUCES", aliases={"produces", "makes"}, subject_type="Supplier", object_type="Component")
    registry.register_relation("CERTIFIED_WITH", aliases={"certification", "certified with"}, subject_type="Supplier", object_type="Certification")
    registry.register_attribute("Supplier", "lead_time_days", aliases={"lead time", "lead time days"}, value_type="int")
    registry.register_attribute("Supplier", "unit_price", aliases={"price", "unit price"}, value_type="float")
    registry.register_attribute("Certification", "code", aliases={"code"}, value_type="str")
    registry.register_attribute("Component", "code", aliases={"code"}, value_type="str")
    registry.register_entity("component:x", "Component", "Component X", aliases={"component x", "part x"}, identifying_attributes={"code": "X"})
    registry.register_entity("cert:y", "Certification", "ISO9001", aliases={"iso9001", "certification y"}, identifying_attributes={"code": "Y"})
    return registry


def test_ontology_normalizes_aliases_hierarchy_and_exact_entity_resolution():
    registry = ontology()
    registry.register_type("PreferredSupplier", aliases={"preferred vendor"}, parent="Supplier")
    resolver = EntityResolver(registry)

    assert registry.canonical_type("VENDOR") == "Supplier"
    assert registry.canonical_relation("makes") == "PRODUCES"
    assert registry.canonical_attribute("Supplier", "Lead Time") == "lead_time_days"
    assert registry.is_a("PreferredSupplier", "Supplier") is True
    assert resolver.resolve("component X", expected_type="Component").canonical_id == "component:x"
    assert resolver.resolve("part x", expected_type="Component").method == "alias"


def test_entity_resolver_reports_ambiguous_fuzzy_matches_instead_of_guessing():
    registry = ontology()
    registry.register_entity("supplier:alpha", "Supplier", "Alpha Systems", aliases={"alpha systems"})
    registry.register_entity("supplier:alfa", "Supplier", "Alfa Systems", aliases={"alfa systems"})
    resolver = EntityResolver(registry, fuzzy_threshold=0.70, ambiguity_margin=0.05)

    result = resolver.resolve("alpa systems", expected_type="Supplier")

    assert result.canonical_id is None
    assert result.method == "ambiguous"
    assert len(result.alternatives) >= 2


def test_entity_resolver_can_use_vector_candidates_only_as_advisory_fallback():
    registry = ontology()
    vector = VectorProvider([VectorRecord(id="component:x", node_type="Component", vector=[1.0, 0.0], metadata={})])
    resolver = EntityResolver(registry, vector_provider=vector, vectorizer=lambda text: [1.0, 0.0])

    result = resolver.resolve("mystery component", expected_type="Component")

    assert result.canonical_id == "component:x"
    assert result.method == "vector"
    assert 0 <= result.score <= 1


def test_natural_language_compiler_builds_supplier_qcg_deterministically():
    compiler = NaturalLanguageCompiler(ontology())
    text = (
        "Find a supplier that produces component X, certification is ISO9001, "
        "lead time at most 30 days, price under 500; top 3."
    )

    result = compiler.compile(text, as_of=NOW)

    assert result.query is not None
    query = result.query
    assert query.variables[0].name == "supplier"
    assert query.variables[0].node_type == "Supplier"
    assert query.top_k == 3
    assert [relation.relation for relation in query.relations] == ["PRODUCES", "CERTIFIED_WITH"]
    attrs = {(item.variable, item.attribute, item.op, item.value) for item in query.attributes}
    assert ("component", "code", "eq", "X") in attrs
    assert ("certification", "code", "eq", "Y") in attrs
    assert ("supplier", "lead_time_days", "lte", 30) in attrs
    assert ("supplier", "unit_price", "lt", 500.0) in attrs
    assert [item.id for item in [*query.relations, *query.attributes]] == [
        "r:001", "r:002", "a:001", "a:002", "a:003", "a:004"
    ]
    assert result.unresolved_terms == []
    assert result.confidence > 0.9


def test_compiler_surfaces_unknown_clauses_and_entities_without_schema_guessing():
    compiler = NaturalLanguageCompiler(ontology())

    result = compiler.compile(
        "Find a supplier that teleports component Q, risk aura under 4",
        as_of=NOW,
    )

    assert result.query is not None
    assert result.unresolved_terms
    messages = " ".join(item.message for item in result.diagnostics)
    assert "teleports component q" in messages.lower()
    assert "risk aura" in messages.lower()
    assert all(relation.relation != "PRODUCES" for relation in result.query.relations)
