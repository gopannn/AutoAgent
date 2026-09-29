# CTD v4 Intelligence Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate encoding-quality validation, safe structural transfer, pre-mortem intelligence, three-valued resolution gaps, and provider prefiltering into the verified CTD v3 production engine without weakening deterministic resolution or authorization.

**Architecture:** Preserve the existing CTD resolver as the only truth-producing plane. Add a separate structural hypothesis plane with validated recursive relational cases, bounded MAC/FAC alignment, convergence ranking, and mandatory checks for pre-mortem findings. Add actionable resolution gaps and optional provider prefiltering to the existing resolution plane.

**Tech Stack:** Python 3.13, Pydantic v2, FastAPI, SQLite/PostgreSQL adapters, pytest.

**Spec:** `docs/superpowers/specs/2026-09-12-constrained-topological-intelligence-v4-design.md`

## Global Constraints

- Structural hypotheses MUST NOT directly produce `ResolutionState.RESOLVED`.
- UNKNOWN constraints MUST NOT be treated as satisfied.
- Type compatibility is mandatory when both structural entities have declared roles.
- Structural retrieval MUST enforce tenant/security Focus before alignment.
- Projection MUST NOT invent unmapped entities.
- v1-v3 API and resolver behavior remains backward compatible except additive fields.
- All new APIs are additive under `/v4`.
- Exact committed-tree verification is required before packaging.

---

### Task 1: Structural Domain and Encoding Quality

**Files:**
- Create: `src/ctd/structural.py`
- Create: `src/ctd/encoding_quality.py`
- Create: `tests/test_structural_v4.py`
- Create: `tests/test_encoding_quality_v4.py`

**Interfaces:**
- Produces `StructuralRelation`, `StructuralCase`, `StructuralTarget`, `PredicateVocabulary`.
- Produces `EncodingFinding`, `EncodingValidation`, `EncodingQualityPolicy`, `validate_structural_case()`.

- [ ] Write tests for recursive relation order/walking, canonicalization, typed entities, invalid UNTYPED/SHALLOW/OFF_VOCAB/ORPHAN cases, and round-trip gloss.
- [ ] Run tests and verify RED due to missing modules.
- [ ] Implement minimal structural domain and quality validator.
- [ ] Run targeted tests and full regression suite.
- [ ] Commit.

### Task 2: Bounded Structural Transfer and Convergence

**Files:**
- Create: `src/ctd/transfer.py`
- Create: `tests/test_transfer_v4.py`

**Interfaces:**
- Produces `TransferPolicy`, `StructuralMapping`, `StructuralHypothesis`, `TransferResult`, `StructuralTransferEngine.transfer()`.

- [ ] Write failing tests for MAC filtering, type-safe FAC mapping, beam alternative recovery, exact-vs-family scoring, no-unmapped-entity projection, deduplication, and independent-domain convergence.
- [ ] Verify RED.
- [ ] Implement deterministic bounded beam alignment and projection.
- [ ] Verify targeted and regression tests.
- [ ] Commit.

### Task 3: Pre-mortem Intelligence

**Files:**
- Create: `src/ctd/premortem.py`
- Create: `tests/test_premortem_v4.py`

**Interfaces:**
- Produces `PreMortemPolicy`, `PreMortemFinding`, `PreMortemResult`, `PreMortemEngine.analyze()`.

- [ ] Write tests requiring every emitted finding to have a check, severity/convergence ranking, incident lineage, and hypothesis-only state.
- [ ] Verify RED.
- [ ] Implement pre-mortem specialization over transfer engine.
- [ ] Verify tests.
- [ ] Commit.

### Task 4: Three-Valued Constraint Semantics and Resolution Gaps

**Files:**
- Create: `src/ctd/constraint_truth.py`
- Modify: `src/ctd/models.py`
- Modify: `src/ctd/resolver.py`
- Create: `tests/test_resolution_gaps_v4.py`

**Interfaces:**
- Produces `ConstraintTruth`, `ConstraintEvaluation`, `ResolutionGap`.
- `ResolutionResult.gaps` remains additive/default-empty.

- [ ] Write tests proving missing attribute -> UNKNOWN, incompatible comparison -> VIOLATED with reason, unresolved relation -> actionable gap, and no UNKNOWN promotion to RESOLVED.
- [ ] Verify RED.
- [ ] Implement evaluator and integrate gap generation into terminal/evaluation paths.
- [ ] Run full suite.
- [ ] Commit.

### Task 5: Provider Prefilter Capability

**Files:**
- Modify: `src/ctd/providers.py`
- Modify: `src/ctd/providers_ext.py`
- Modify: `src/ctd/resolver.py`
- Modify: `src/ctd/telemetry.py`
- Create: `tests/test_prefilter_v4.py`

**Interfaces:**
- Optional `prefilter_nodes(node_type, constraints, limit)` capability returns normal `ProviderResult[Node]`.

- [ ] Write tests proving graph/document prefilter reduces materialized candidates while resolver locally re-verifies; SQL supported scalar predicates use parameterized filtering; unsupported constraints safely fall back.
- [ ] Verify RED.
- [ ] Implement capability and telemetry.
- [ ] Run provider/resolver/full tests.
- [ ] Commit.

### Task 6: Structural Persistence and Authorization

**Files:**
- Create: `src/ctd/structural_repository.py`
- Modify: `src/ctd/persistence.py`
- Create: `tests/test_structural_persistence_v4.py`

**Interfaces:**
- `StructuralCaseRepository.save/get/list` and `HypothesisRunRepository.save/get/list`.
- In-memory and SQLite-backed adapters.

- [ ] Write restart and tenant/security filtering tests.
- [ ] Verify RED.
- [ ] Implement repositories/tables/adapters.
- [ ] Run persistence/full tests.
- [ ] Commit.

### Task 7: Production Service and v4 API

**Files:**
- Modify: `src/ctd/service.py`
- Modify: `src/ctd/api.py`
- Modify: `src/ctd/config.py`
- Modify: `src/ctd/__init__.py`
- Create: `tests/test_service_v4.py`
- Create: `tests/test_api_v4.py`

**Interfaces:**
- `ProductionEngine.validate_structural_case`, `ingest_structural_case`, `list_structural_cases`, `transfer`, `premortem`, `intelligence_metrics`.
- Authenticated `/v4/encoding/validate`, `/v4/structural/cases`, `/v4/transfer`, `/v4/premortem`, `/v4/intelligence/metrics`.

- [ ] Write service/API tests for authorization, persistence, transfer, premortem, metrics, and v1-v3 compatibility.
- [ ] Verify RED.
- [ ] Implement service/API integration.
- [ ] Run full suite.
- [ ] Commit.

### Task 8: Benchmarks, Documentation, Version and Release

**Files:**
- Modify: `tests/test_production_benchmarks.py`
- Create: `tests/test_intelligence_benchmarks_v4.py`
- Modify: `README.md`
- Modify: `pyproject.toml`
- Create: `examples/v4_intelligence_demo.py`

**Interfaces:**
- Version becomes `0.4.0`.

- [ ] Add deterministic benchmarks for candidate reduction, correct abstention, transfer convergence, and pre-mortem checkability.
- [ ] Update documentation and example.
- [ ] Run full tests, `compileall`, editable install, CLI, authenticated v4 smoke, SQLite restart, transfer/premortem smoke, wheel build/import, `git diff --check`.
- [ ] Commit release slice.
- [ ] Re-run complete acceptance matrix against exact commit.
- [ ] Build consolidated source+wheel+release ZIP and verify checksums/integrity.
