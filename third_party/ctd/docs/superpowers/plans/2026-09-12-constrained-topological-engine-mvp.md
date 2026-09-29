# Constrained Topological Engine MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a runnable bounded graph-resolution engine that resolves typed constraint queries from evidence, validates provenance/contradictions, abstains when unsupported, and exposes API/CLI/UI entry points.

**Architecture:** A pure-Python core separates data models, graph storage, runtime control, resolution, and telemetry. FastAPI is a thin transport layer over the deterministic resolver, while a bundled HTML page provides inspection without a frontend build chain.

**Tech Stack:** Python 3.13, Pydantic 2, FastAPI, Uvicorn, pytest, standard-library `time`/`json`/`argparse`.

**Spec:** `docs/superpowers/specs/2026-09-12-constrained-topological-engine-design.md`

## Global Constraints
- Single-process MVP with replaceable storage/controller interfaces.
- No arbitrary executable predicates; only allowlisted constraint operators.
- `RESOLVED` requires every hard constraint plus evidence policy to pass.
- Budget exhaustion must return partial/abstention states, never fabricated assertions.
- Every accepted relation must expose provenance evidence IDs.
- Deterministic runtime controller; no reinforcement-learning policy in MVP.

---

### Task 1: Domain models and evidence graph

**Files:**
- Create: `pyproject.toml`
- Create: `src/ctd/__init__.py`
- Create: `src/ctd/models.py`
- Create: `src/ctd/graph.py`
- Test: `tests/test_graph.py`

**Interfaces:**
- Produces `Node`, `Evidence`, `Edge`, `Variable`, `RelationConstraint`, `AttributeConstraint`, `QueryConstraintGraph`, `ResolutionState`, `ResolutionResult`.
- Produces `EvidenceGraph.add_node`, `add_edge`, `nodes_of_type`, `outgoing`, `incoming`, `snapshot`.

- [ ] Write failing tests proving typed node lookup, edge traversal, and evidence preservation.
- [ ] Run `pytest tests/test_graph.py -q` and confirm failures are from missing implementation.
- [ ] Implement minimal Pydantic models and an in-memory adjacency-list graph.
- [ ] Re-run the graph tests and the full test suite.
- [ ] Commit `feat: add evidence graph domain model`.

### Task 2: Four-barrier controller and telemetry

**Files:**
- Create: `src/ctd/controller.py`
- Create: `src/ctd/telemetry.py`
- Test: `tests/test_controller.py`

**Interfaces:**
- Produces `RuntimePolicy`, `RuntimeState`, `BarrierController.initialize`, `can_expand`, `record_progress`, `should_widen`, `widen`.
- Produces `ResolutionTelemetry` counters and event trace.

- [ ] Write failing tests for expansion budget, depth/deadline limits, and bounded widening.
- [ ] Verify the tests fail for missing controller behavior.
- [ ] Implement deterministic budget/focus/arousal state transitions.
- [ ] Verify controller tests and full suite pass.
- [ ] Commit `feat: add bounded runtime controller`.

### Task 3: Constraint resolver and terminal-state semantics

**Files:**
- Create: `src/ctd/resolver.py`
- Test: `tests/test_resolver.py`

**Interfaces:**
- Consumes `EvidenceGraph`, `QueryConstraintGraph`, `RuntimePolicy`.
- Produces `Resolver.resolve(query, policy) -> ResolutionResult`.

- [ ] Write failing tests for a fully resolved supplier query, hard-constraint rejection, partial result under missing data, contradiction detection, and budget exhaustion.
- [ ] Verify failures demonstrate missing resolver behavior.
- [ ] Implement candidate binding, relation/attribute evaluation, evidence validation, contradiction detection, progressive widening, and explicit terminal-state selection.
- [ ] Verify resolver tests and full suite pass.
- [ ] Commit `feat: implement constrained resolution engine`.

### Task 4: Example fixture and external interfaces

**Files:**
- Create: `src/ctd/examples.py`
- Create: `src/ctd/api.py`
- Create: `src/ctd/cli.py`
- Create: `src/ctd/static/index.html`
- Test: `tests/test_api.py`

**Interfaces:**
- Produces `build_supplier_demo()` returning `(graph, query, policy)`.
- FastAPI endpoints: `GET /health`, `GET /graph`, `POST /resolve`, `POST /demo/supplier`, `GET /`.
- CLI: `python -m ctd.cli demo`.

- [ ] Write failing API tests for health, supplier demo, and generic resolve.
- [ ] Verify failures before implementation.
- [ ] Implement fixture, API adapters, CLI, and inspection page.
- [ ] Run API tests, CLI smoke test, and full suite.
- [ ] Commit `feat: expose resolution api cli and inspector`.

### Task 5: Documentation and end-to-end verification

**Files:**
- Create: `README.md`
- Create: `examples/supplier_query.json`
- Modify tests only if a discovered behavior defect requires a regression test first.

**Interfaces:**
- Documents package installation, architecture, API payloads, CLI usage, guarantees/non-guarantees, and extension points.

- [ ] Run `pytest -q` and confirm zero failures.
- [ ] Run `python -m ctd.cli demo` and verify a `RESOLVED` result with evidence IDs.
- [ ] Start the API locally and issue a health/demo request.
- [ ] Add README and example request payload matching the tested interfaces.
- [ ] Run final test/static syntax checks and commit `docs: add runnable ctd mvp guide`.
