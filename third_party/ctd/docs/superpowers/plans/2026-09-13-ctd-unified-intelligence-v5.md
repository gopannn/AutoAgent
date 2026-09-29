# CTD Unified Intelligence Engine v5 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Merge CTD v4 and Intelligence Engine v5 into one production package with one canonical structural kernel, preserved legacy surfaces, strict v5 intelligence APIs, and zero regression.

**Architecture:** CTD remains the production/auth/persistence/evidence shell. The v5 structural algorithms live once under `ctd.kernel`, while `topo` is a compatibility facade. Truth resolution and structural hypothesis generation remain separate planes connected only through evidence acquisition and validation.

**Tech Stack:** Python 3.13, Pydantic 2, FastAPI, pytest, SQLite/PostgreSQL/Neo4j optional adapters, standard-library bitmap structural kernel.

**Spec:** `docs/superpowers/specs/2026-09-13-ctd-unified-intelligence-v5-design.md`

## Global Constraints

- All 141 original CTD v4 tests must remain green.
- All 57 original Intelligence Engine v5 tests must run against the installed `topo` facade and remain green.
- Structural hypotheses may not produce `RESOLVED` directly.
- Authorization filtering precedes local CLOSE and structural reasoning.
- v1-v4 APIs remain additive and backward compatible.
- Relaxed closure must return `PARTIAL` with named dropped constraints.
- Strict v5 ingestion rejects schema-invalid structural cases.
- Package version is `0.5.0`.

---

### Task 1: Canonical structural kernel and `topo` compatibility

**Files:**
- Create: `src/ctd/kernel/*.py`
- Create: `src/topo/*.py`
- Test: `tests/test_topo_legacy_v5.py`
- Test: `tests/test_topo_robustness_v5.py`

**Interfaces:**
- Consumes: original Intelligence Engine v5 module interfaces.
- Produces: one canonical kernel under `ctd.kernel`; `topo` facade preserving original imports.

- [x] Copy validated v5 algorithms into `ctd.kernel` without semantic edits.
- [x] Create compatibility facade modules exposing public and test-required private names.
- [x] Run all 57 legacy v5 tests through the facade.
- [x] Confirm all CTD tests remain green.

### Task 2: Strict schema adapter and kernel-backed CTD transfer

**Files:**
- Modify: `src/ctd/encoding_quality.py`
- Modify: `src/ctd/transfer.py`
- Test: `tests/test_unified_kernel_v5.py`
- Test: `tests/test_transfer_v4.py`
- Test: `tests/test_premortem_v4.py`

**Interfaces:**
- Produces: `validate_structural_case_strict`, kernel-backed `StructuralTransferEngine`.

- [x] Add CTD-to-kernel role normalization at the boundary.
- [x] Translate hardened schema findings into CTD validation results.
- [x] Delegate MAC/FAC and projection to the kernel.
- [x] Preserve v4 mapping-depth/family/convergence result semantics.
- [x] Add strict-v5 blocked projection/conflict diagnostics.
- [x] Verify legacy transfer and pre-mortem tests.

### Task 3: Authorization-aware indexed CLOSE

**Files:**
- Create: `src/ctd/unified_close.py`
- Test: `tests/test_unified_close_v5.py`

**Interfaces:**
- Produces: `UnifiedCloseOperator.run(UnifiedCloseRequest) -> UnifiedCloseResult`.

- [x] Define JSON-serializable record and constraint contracts.
- [x] Apply tenant/security/scope filtering before store materialization.
- [x] Map runtime deadlines and compute ceilings into kernel budgets.
- [x] Map `DataRequest` to `ResolutionGap`.
- [x] Preserve explicit `PARTIAL` on relaxation.

### Task 4: Intelligence evaluation as a release gate

**Files:**
- Create: `src/ctd/evaluation.py`
- Test: `tests/test_evaluation_unified_v5.py`
- Test: `tests/test_intelligence_release_gate_v5.py`

**Interfaces:**
- Produces: `evaluate_structural_cases(...) -> IntelligenceEvaluationReport`.

- [x] Reject schema-invalid cases before scoring.
- [x] Run full ablations and frequency/random controls.
- [x] Run guard probe.
- [x] Require zero full-arm violations and MRR above random control.

### Task 5: Unified production service and additive v5 API

**Files:**
- Modify: `src/ctd/service.py`
- Modify: `src/ctd/api.py`
- Test: `tests/test_api_v5.py`

**Interfaces:**
- Produces: strict validation/ingest, CLOSE, strict transfer/premortem, evaluation and v5 aliases.

- [x] Add strict structural ingestion using existing durable repositories.
- [x] Add principal-scoped local CLOSE.
- [x] Add strict transfer and pre-mortem service methods.
- [x] Add `/v5` endpoints while preserving v1-v4.
- [x] Verify transfer findings remain `HYPOTHESIS` and premortem checks are present.

### Task 6: Remove duplicate executable code and preserve provenance

**Files:**
- Keep: `legacy/intelligence_engine_v5_reference/{README.md,demo.py,library.py,bench/*}`
- Release bundle: include original supplied v4 and v5 ZIP archives.

**Interfaces:**
- Produces: one executable structural implementation plus lossless original-source provenance.

- [x] Remove second vendor `topo` implementation from executable tree.
- [x] Keep reference corpus/demo/bench for evaluation and provenance.
- [x] Include both original user-supplied archives in final release package.

### Task 7: Documentation, exact-commit verification and package

**Files:**
- Modify: `README.md`
- Modify: `src/ctd/__init__.py`
- Modify: `pyproject.toml`
- Create release verification/manifest/checksum artifacts.

**Interfaces:**
- Produces: installable `constrained-topological-engine==0.5.0`, final unified source ZIP and wheel.

- [x] Set package/API version to 0.5.0.
- [x] Update README architecture/endpoints/compatibility/evaluation semantics.
- [x] Run full combined pytest suite.
- [x] Run compileall and CLI demo.
- [x] Run retained v5 demo/evaluation through canonical `topo` facade.
- [x] Build wheel and verify isolated import of `ctd` and `topo`.
- [x] Commit exact release tree and rerun all acceptance checks.
- [x] Build single final bundle with source, wheel, docs, original archives, verification report, manifest and SHA-256 checksums.
