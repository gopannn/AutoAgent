# CTD v4 — Resolution + Hypothesis Intelligence Engine Design

## Goal

Upgrade CTD v3 into a dual-plane intelligence engine that preserves deterministic, evidence-backed resolution while adding safe structural hypothesis generation, pre-mortem analysis, encoding-quality validation, explicit three-valued constraint semantics, actionable resolution gaps, and provider-level prefiltering.

## Non-negotiable invariant

A structural analogy is never evidence of truth by itself.

`TRANSFER -> HYPOTHESIS -> CHECK/EVIDENCE ACQUISITION -> CTD RESOLUTION -> SUPPORTED|REJECTED|UNKNOWN`

Only the Resolution Plane can produce `RESOLVED`.

## Architecture

### 1. Resolution Plane

Existing CTD v3 functionality remains authoritative: QCG compilation, authorization, adaptive planning, evidence search, fusion, contradiction handling, replay, persistence, telemetry, async decomposition, and bounded deterministic validation.

Enhancements:
- explicit `SATISFIED | VIOLATED | UNKNOWN` constraint truth;
- first-class `ResolutionGap` records for unresolved/violated constraints;
- provider-level hard-attribute prefilter capability;
- write-time enrichment hooks and prefilter telemetry;
- benchmark separation of resolution precision, closure rate, and correct abstention.

### 2. Encoding Quality Plane

New structural records are validated before entering the structural-case library.

Checks:
- `UNTYPED`: relation entities without declared semantic role;
- `SHALLOW`: no higher-order structure above configured minimum;
- `OFF_VOCAB`: predicates not in the registered shared vocabulary;
- `ORPHAN`: entities participating in a single relation only;
- `BARREN`: projected structure depends on entities that cannot be anchored through shared relations;
- `ROUNDTRIP_REQUIRED`: generated structural gloss requires human/source confirmation.

Validation is policy-driven. Errors block structural-transfer ingestion. Warnings remain visible and auditable.

### 3. Structural Representation

Introduce recursive `StructuralRelation` and immutable `StructuralCase` models. Relations may contain entities or nested relations, allowing higher-order causal/functional structure. `StructuralTarget` is an intentionally incomplete target pattern.

Every case contains:
- id and domain;
- relations;
- entity types;
- metadata/provenance;
- source references;
- optional severity/check templates for incident cases.

### 4. Structural Transfer Plane

A safe three-stage analogical pipeline:

1. **MAC retrieval**: cheap predicate/family overlap and domain-distance gating.
2. **FAC alignment**: bounded beam-search global mappings, not a single greedy alignment. Type compatibility is mandatory when both roles are known. Exact predicate matches outrank family matches.
3. **Projection**: only unmapped source relations whose arguments are fully licensed by the mapping are projected. No new entities are invented.

The aligner supports:
- configurable predicate families;
- beam width;
- minimum structural depth;
- maximum candidate cases;
- maximum mappings per case;
- deterministic tie-breaking;
- explicit kinship matches and looseness penalties.

### 5. Hypothesis Model

Each projected hypothesis includes:
- projected relation;
- target/case identifiers;
- source domain;
- structural systematicity;
- mapping depth;
- exact vs family match counts;
- mapping lineage;
- verification procedure/check;
- convergence count across independent domains;
- source-case count;
- confidence-like *priority* score explicitly labelled as hypothesis priority, never truth probability;
- state: `HYPOTHESIS`, `UNDER_TEST`, `SUPPORTED`, `REJECTED`, `UNKNOWN`.

### 6. Convergence

Equivalent projected relations are grouped canonically. Convergence counts independent source domains, not duplicate cases. Correlated domains/cases may be grouped with an `independence_group` and count once.

`priority = systematicity * depth_factor * convergence_factor * source_independence * checkability * looseness_penalty`

This score ranks investigation order only.

### 7. Pre-mortem Engine

A pre-mortem is a specialized transfer run against an incident library. It projects historical failure structures into a proposed target design and requires a concrete verification procedure for every output. Hypotheses without a check are excluded from deliverable results.

Outputs include severity, convergence, source incidents, projected failure relation, and test/check instruction.

### 8. Resolution Gaps

`ResolutionResult` gains `gaps: list[ResolutionGap]`.

A gap includes:
- constraint id and kind;
- truth state;
- variables involved;
- reason;
- satisfied dependencies;
- candidate count before failure;
- required evidence description;
- suggested provider/data source;
- suggested acquisition action;
- priority.

This converts abstention from a dead end into an executable acquisition plan.

### 9. Three-valued constraints

Attribute/relation evaluation distinguishes:
- `SATISFIED`: evidence/data proves the predicate;
- `VIOLATED`: evidence/data disproves the predicate;
- `UNKNOWN`: required data is absent, inaccessible, incompatible, or insufficient.

`UNKNOWN` must never be treated as success. Resolver terminal semantics remain fail-closed.

### 10. Provider prefiltering

Providers may implement optional `prefilter_nodes(node_type, constraints, limit)`.

Graph/document providers use in-memory indexed-style filtering; SQL provider emits parameterized predicates for supported scalar operators where practical, falling back safely for unsupported expressions. Resolver uses the capability only when present and retains local verification of every returned node.

Metrics distinguish:
- physical candidates;
- provider-prefiltered candidates;
- locally verified candidates;
- prefilter reductions.

### 11. Structural library and persistence

Introduce repository protocols for structural cases and hypothesis runs. In-memory and SQLite implementations are required. Structural case ingestion stores only encoding-valid cases unless `allow_warnings` policy permits warnings. Errors are never silently bypassed.

### 12. Intelligence Service

`ProductionEngine` receives:
- `validate_structural_case`;
- `ingest_structural_case`;
- `transfer`;
- `premortem`;
- `validate_hypothesis` helper that maps a hypothesis/check into normal CTD evidence workflow metadata without auto-resolving it.

### 13. API

Add authenticated v4 endpoints without changing v1-v3:
- `POST /v4/encoding/validate`
- `POST /v4/structural/cases`
- `GET /v4/structural/cases`
- `POST /v4/transfer`
- `POST /v4/premortem`
- `GET /v4/intelligence/metrics`

All v4 endpoints enforce the same tenant/scopes/security labels as v3. Structural cases carry tenant/security metadata and are filtered before transfer retrieval.

### 14. Metrics

New metrics:
- encoding accepted/rejected/warned;
- MAC cases examined/retained;
- FAC alignments attempted/accepted;
- hypotheses projected/deduplicated;
- domain convergence distribution;
- hypotheses with checks;
- pre-mortem findings by severity;
- provider prefilter reduction ratio;
- resolution gaps emitted;
- gap-to-resolution conversion (when later evidence closes a gap).

### 15. Benchmark acceptance

Tests and deterministic benchmarks must demonstrate:
- invalid encodings are blocked;
- typed higher-order analogies transfer while incompatible types do not;
- beam alignment can recover a valid mapping missed by greedy ordering fixture;
- projection never invents unmapped entities;
- convergence counts independent domains only;
- pre-mortem outputs always contain checks;
- hypotheses never change a CTD result to `RESOLVED` without evidence;
- UNKNOWN attribute data is unresolved, not satisfied;
- resolution gaps identify missing evidence precisely;
- provider prefiltering reduces candidate materialization on a deterministic fixture;
- authorization filters structural cases before MAC/FAC;
- v1-v3 regression tests remain green.

## File boundaries

New modules:
- `src/ctd/structural.py`
- `src/ctd/encoding_quality.py`
- `src/ctd/constraint_truth.py`
- `src/ctd/transfer.py`
- `src/ctd/premortem.py`
- `src/ctd/structural_repository.py`

Modified:
- `models.py`, `providers.py`, `providers_ext.py`, `resolver.py`, `service.py`, `api.py`, `telemetry.py`, `persistence.py`, `config.py`, `__init__.py`, `README.md`, `pyproject.toml`.

## Release criterion

The v4 release is complete only after the exact committed tree passes the entire v1-v4 test suite, compilation, editable installation, CLI regression, authenticated v4 API smoke tests, persistent SQLite structural-case restart test, transfer/premortem smoke tests, wheel build/import, source archive integrity, and checksum verification.
