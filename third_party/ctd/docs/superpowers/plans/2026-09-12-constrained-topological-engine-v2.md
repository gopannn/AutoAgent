# Constrained Topological Engine v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade the CTD MVP into a deterministic adaptive evidence-resolution engine with cost-based planning, evidence fusion, bounded best-first resolution, explicit uncertainty/failure semantics, provider/repository boundaries, runtime profiles, and backward-compatible API behavior.

**Architecture:** Preserve the existing graph and `/resolve` surface while moving search intelligence into focused modules. The resolver consumes a compiled QCG, a deterministic execution plan, evidence providers, a four-barrier runtime controller, and truth/evidence services; all safety rules remain hard invariants and learned policy remains advisory-only.

**Tech Stack:** Python 3.13, Pydantic 2.x, FastAPI, pytest, standard-library `heapq`, dataclasses, protocols/ABCs, in-memory repositories.

**Spec:** `docs/superpowers/specs/2026-09-12-constrained-topological-engine-v2-design.md`

## Global Constraints

- Hard constraints are never traded away for score.
- Every accepted relation requires valid evidence or traceable inference lineage.
- Search widening never exceeds policy bounds.
- Validation has a protected budget reserve.
- Authorization is part of Focus and unauthorized graph elements behave as nonexistent.
- Correlated evidence is discounted by independence group.
- Temporal validity is evaluated against query `as_of`.
- Terminal states are `RESOLVED`, `PARTIAL`, `CONTRADICTED`, `UNRESOLVABLE`, `BUDGET_EXHAUSTED`, or `PROVIDER_ERROR`.
- Existing v1 request payloads remain valid where practical.
- The v2 controller is deterministic; no reinforcement-learning dependency is introduced.

---

### Task 1: Rich v2 Domain Model and Backward Compatibility

**Files:**
- Modify: `src/ctd/models.py`
- Create: `tests/test_models_v2.py`

**Interfaces:**
- Consumes: existing `Node`, `Edge`, `Evidence`, QCG and result models.
- Produces: `ClaimStatus`, `FailureReason`, `ResolutionUncertainty`, `Claim`, extended `Evidence`, extended constraint metadata, extended `ResolutionState`, and richer `ResolutionResult` while keeping all v1 fields valid.

- [ ] **Step 1: Write failing model tests**

```python
def test_correlated_evidence_and_constraint_policy_fields_are_modelled():
    ev = Evidence(..., independence_group="vendor-feed", extraction_method="direct", direct=True)
    rel = RelationConstraint(..., required_evidence_strength=0.9, required_source_classes={"registry"})
    assert ev.independence_group == "vendor-feed"
    assert rel.required_evidence_strength == 0.9


def test_resolution_result_supports_v2_failure_and_uncertainty():
    result = ResolutionResult(
        state=ResolutionState.BUDGET_EXHAUSTED,
        constraint_coverage=0.5,
        uncertainty=ResolutionUncertainty(structural_coverage=0.5),
        failure_reasons=[FailureReason.EXPANSION_BUDGET_EXHAUSTED],
    )
    assert result.failure_reasons == [FailureReason.EXPANSION_BUDGET_EXHAUSTED]
```

- [ ] **Step 2: Run tests and verify RED**

Run: `pytest tests/test_models_v2.py -q`
Expected: import/validation failures because the v2 types and fields do not yet exist.

- [ ] **Step 3: Implement the v2 domain extensions**

Implement Pydantic models and enums with conservative defaults so old payloads deserialize unchanged. Add `independence_group`, `extraction_method`, `direct`, `lineage`, and claim metadata to `Evidence`; evidence-policy and soft-ranking fields to constraints; `BUDGET_EXHAUSTED` and `PROVIDER_ERROR` to `ResolutionState`; and `uncertainty`, `failure_reasons`, `candidate_rank`, and optional `alternatives` to results.

- [ ] **Step 4: Run model and existing tests**

Run: `pytest tests/test_models_v2.py tests/test_graph.py tests/test_resolver.py -q`
Expected: PASS with no v1 regressions.

- [ ] **Step 5: Commit**

```bash
git add src/ctd/models.py tests/test_models_v2.py
git commit -m "feat: extend CTD domain model for v2"
```

---

### Task 2: Provider and Repository Boundaries

**Files:**
- Create: `src/ctd/providers.py`
- Create: `src/ctd/repositories.py`
- Create: `tests/test_providers.py`

**Interfaces:**
- Consumes: `EvidenceGraph`, `Node`, `Edge`.
- Produces: `EvidenceProvider`, `ProviderResult`, `GraphProvider`, `MemoryProvider`, `ExecutionRepository`, `QueryProfileRepository`, and in-memory implementations.

- [ ] **Step 1: Write failing provider/repository tests**

```python
def test_graph_provider_filters_authorization_and_reports_cost():
    provider = GraphProvider(graph, authorization_scope={"supplier:a", "component:x"})
    result = provider.outgoing("supplier:a", "PRODUCES", limit=10)
    assert [edge.id for edge in result.items] == ["e:prod:a"]
    assert result.cost.provider_calls == 1


def test_execution_repository_round_trips_execution_records():
    repo = InMemoryExecutionRepository()
    repo.save("run:1", {"state": "RESOLVED"})
    assert repo.get("run:1")["state"] == "RESOLVED"
```

- [ ] **Step 2: Verify RED**

Run: `pytest tests/test_providers.py -q`
Expected: module/type imports fail.

- [ ] **Step 3: Implement provider and repository abstractions**

Use `Protocol`-style interfaces. `GraphProvider` wraps graph access and returns provider cost metadata (`provider_calls`, `bytes_read`, `items_examined`). Authorization filtering occurs before returned candidates are visible to the resolver. Repositories remain deterministic in-memory maps.

- [ ] **Step 4: Verify GREEN**

Run: `pytest tests/test_providers.py tests/test_graph.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/ctd/providers.py src/ctd/repositories.py tests/test_providers.py
git commit -m "feat: add evidence provider and repository boundaries"
```

---

### Task 3: Evidence Fusion and Temporal Truth Maintenance

**Files:**
- Create: `src/ctd/truth.py`
- Create: `tests/test_truth.py`

**Interfaces:**
- Consumes: `Evidence`, `Claim`, query `as_of`, source/evidence requirements.
- Produces: `EvidenceAssessment`, `EvidenceFusion.fuse(...)`, `TruthMaintainer.evaluate_claims(...)`.

- [ ] **Step 1: Write failing truth tests**

```python
def test_fusion_discounts_duplicate_independence_groups():
    fused = EvidenceFusion().fuse([ev("a", group="feed"), ev("b", group="feed")], as_of=AS_OF)
    independent = EvidenceFusion().fuse([ev("a", group="feed1"), ev("b", group="feed2")], as_of=AS_OF)
    assert independent.strength > fused.strength


def test_newer_temporal_claim_supersedes_old_claim_without_contradiction():
    assessment = TruthMaintainer().evaluate_claims([active_old, revoked_new], as_of=AS_OF)
    assert assessment.status == ClaimStatus.SUPERSEDED
    assert assessment.contradiction_risk < 1.0
```

- [ ] **Step 2: Verify RED**

Run: `pytest tests/test_truth.py -q`
Expected: module/type imports fail.

- [ ] **Step 3: Implement deterministic fusion and truth maintenance**

Calculate evidence weight from `confidence * trust * freshness_factor`; cap contributions within one independence group by taking the strongest evidence in the group; combine independent groups with `1 - product(1 - weight)`. Track source diversity, freshness, accepted/rejected evidence IDs, and reasons. Resolve temporal supersession using validity/observation time when claims have the same key but different values.

- [ ] **Step 4: Verify GREEN**

Run: `pytest tests/test_truth.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/ctd/truth.py tests/test_truth.py
git commit -m "feat: add evidence fusion and temporal truth maintenance"
```

---

### Task 4: Deterministic Cost-Based Query Planner

**Files:**
- Create: `src/ctd/planner.py`
- Create: `tests/test_planner.py`

**Interfaces:**
- Consumes: `QueryConstraintGraph`, provider statistics and optional runtime profile priors.
- Produces: `PlanOperation`, `ExecutionPlan`, `QueryPlanner.plan(query, provider, profile=None)`.

- [ ] **Step 1: Write failing planner tests**

```python
def test_planner_prioritizes_selective_hard_attribute_before_broad_relation():
    plan = QueryPlanner().plan(query, provider)
    assert plan.operations[0].constraint_id == "a:component"
    assert plan.operations[0].hard is True


def test_plan_is_deterministic_for_same_snapshot_and_query():
    assert QueryPlanner().plan(query, provider).model_dump() == QueryPlanner().plan(query, provider).model_dump()
```

- [ ] **Step 2: Verify RED**

Run: `pytest tests/test_planner.py -q`
Expected: imports fail.

- [ ] **Step 3: Implement planner**

Estimate selectivity from provider node counts and observed attribute distributions when available. Use deterministic priority tuple `(hard-first, -information_gain*selectivity*reliability/cost, constraint_id)` so ties are stable. Record planner rationale for telemetry.

- [ ] **Step 4: Verify GREEN**

Run: `pytest tests/test_planner.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/ctd/planner.py tests/test_planner.py
git commit -m "feat: add deterministic cost-based query planner"
```

---

### Task 5: Runtime Budgeting, Validation Reserve, and Structured Telemetry

**Files:**
- Modify: `src/ctd/controller.py`
- Modify: `src/ctd/telemetry.py`
- Create: `tests/test_runtime_v2.py`

**Interfaces:**
- Consumes: existing `RuntimePolicy`, `BarrierController`, `ResolutionTelemetry`.
- Produces: provider/byte/candidate ceilings, validation reserve enforcement, richer runtime snapshots, structured trace events and failure reason derivation.

- [ ] **Step 1: Write failing runtime tests**

```python
def test_search_stops_before_using_validation_reserve():
    policy = RuntimePolicy(max_expansions=10, validation_reserve_ratio=0.2)
    state = controller.initialize(policy)
    for _ in range(8): controller.record_expansion(state, telemetry, depth=1)
    assert controller.can_search(state, policy, telemetry, depth=1) is False
    assert controller.can_validate(state, policy, telemetry) is True


def test_provider_call_budget_is_hard_bound():
    policy = RuntimePolicy(max_provider_calls=1)
    controller.record_provider_call(state, telemetry)
    assert controller.can_call_provider(state, policy, telemetry) is False
```

- [ ] **Step 2: Verify RED**

Run: `pytest tests/test_runtime_v2.py -q`
Expected: missing policy fields/controller methods.

- [ ] **Step 3: Implement expanded controller/telemetry**

Add policy fields from the spec. Runtime state tracks provider calls, bytes read, candidates, progress history, Focus/Arousal trajectory and whether validation reserve has been entered. Preserve existing `can_expand` as a compatibility alias over search permission.

- [ ] **Step 4: Verify GREEN and v1 compatibility**

Run: `pytest tests/test_runtime_v2.py tests/test_controller.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/ctd/controller.py src/ctd/telemetry.py tests/test_runtime_v2.py
git commit -m "feat: enforce v2 runtime budgets and validation reserve"
```

---

### Task 6: Bounded Best-First Adaptive Resolver

**Files:**
- Modify: `src/ctd/resolver.py`
- Create: `tests/test_resolver_v2.py`

**Interfaces:**
- Consumes: provider, planner, controller, truth services, v2 models.
- Produces: best-first `Resolver.resolve(...)` with hard pruning, progressive widening, Pareto-aware ranking, explicit failure taxonomy, uncertainty metrics, and top-k alternatives while preserving v1 constructor use (`Resolver(graph)`).

- [ ] **Step 1: Write failing resolver-v2 tests**

```python
def test_resolver_prefers_stronger_independent_evidence_when_candidates_both_fit():
    result = Resolver(graph).resolve(query, policy)
    assert result.bindings["supplier"] == "supplier:strong"
    assert result.uncertainty.source_diversity >= 2


def test_resolver_returns_budget_exhausted_with_precise_reason():
    result = Resolver(graph).resolve(query, RuntimePolicy(max_expansions=1))
    assert result.state == ResolutionState.BUDGET_EXHAUSTED
    assert FailureReason.EXPANSION_BUDGET_EXHAUSTED in result.failure_reasons


def test_resolver_temporally_supersedes_stale_conflict():
    result = Resolver(graph_with_old_active_new_revoked).resolve(query, policy)
    assert result.state != ResolutionState.CONTRADICTED
```

- [ ] **Step 2: Verify RED**

Run: `pytest tests/test_resolver_v2.py -q`
Expected: failures because resolver still uses v1 recursion/scoring.

- [ ] **Step 3: Implement best-first resolver**

Use a heap ordered by hard-constraint viability, expected remaining uncertainty, evidence strength and accumulated cost. Execute planner operations, call providers through the budget controller, fuse evidence on relation satisfaction, prune hard violations immediately, widen Arousal only after deterministic stagnation, reserve validation budget, validate top candidates, and map controller/provider outcomes into terminal states/failure reasons.

- [ ] **Step 4: Verify v2 and full v1 regression suite**

Run: `pytest tests/test_resolver_v2.py tests/test_resolver.py -q`
Expected: PASS; v1 success/partial/contradiction semantics remain compatible except budget exhaustion is now represented by the explicit v2 terminal state.

- [ ] **Step 5: Commit**

```bash
git add src/ctd/resolver.py tests/test_resolver_v2.py
git commit -m "feat: implement adaptive best-first CTD v2 resolver"
```

---

### Task 7: Runtime Profiles, API v2 Surface, Replay, and Benchmarks

**Files:**
- Create: `src/ctd/profiles.py`
- Modify: `src/ctd/api.py`
- Modify: `src/ctd/examples.py`
- Modify: `README.md`
- Create: `tests/test_profiles.py`
- Create: `tests/test_api_v2.py`
- Create: `tests/test_benchmarks.py`

**Interfaces:**
- Consumes: resolver telemetry/results and in-memory repositories.
- Produces: advisory query-class profiles, `/v2/resolve`, `/v2/plan`, `/v2/executions/{id}`, replayable execution metadata, richer health/capabilities payload, and deterministic benchmark fixtures.

- [ ] **Step 1: Write failing profile/API/benchmark tests**

```python
def test_profile_records_successful_plan_as_advisory_prior():
    profiles.observe("supplier", result)
    assert profiles.get("supplier").successful_runs == 1


def test_v2_plan_and_resolve_endpoints_expose_plan_and_uncertainty():
    plan = client.post("/v2/plan", json=payload).json()
    result = client.post("/v2/resolve", json=payload).json()
    assert plan["operations"]
    assert "uncertainty" in result


def test_planner_reduces_or_matches_candidate_work_on_selective_fixture():
    assert v2_result.telemetry["nodes_considered"] <= baseline_result.telemetry["nodes_considered"]
```

- [ ] **Step 2: Verify RED**

Run: `pytest tests/test_profiles.py tests/test_api_v2.py tests/test_benchmarks.py -q`
Expected: missing modules/endpoints.

- [ ] **Step 3: Implement profiles, v2 API, replay metadata and fixtures**

Profiles store aggregate counts and useful planner orders but never mutate hard policy. `/v2/plan` returns deterministic planner operations. `/v2/resolve` stores execution snapshot metadata with generated execution ID; execution lookup returns the stored record. `/resolve` remains supported. Benchmarks are deterministic pytest fixtures, not wall-clock microbenchmarks.

- [ ] **Step 4: Run complete test suite and compile check**

Run: `pytest -q && python -m compileall -q src tests`
Expected: all tests pass and compilation exits 0.

- [ ] **Step 5: Update docs and commit**

```bash
git add src/ctd/profiles.py src/ctd/api.py src/ctd/examples.py README.md tests/test_profiles.py tests/test_api_v2.py tests/test_benchmarks.py
git commit -m "feat: expose CTD v2 API profiles replay and benchmarks"
```

---

### Task 8: Final Verification and Packaged Artifact

**Files:**
- Verify all tracked files; no production changes unless a verification defect is found.

**Interfaces:**
- Produces: clean committed feature branch and reproducible source archive.

- [ ] **Step 1: Run full verification**

```bash
pytest -q
python -m compileall -q src tests
python -m ctd.cli demo
```

- [ ] **Step 2: Run API smoke verification**

Use FastAPI `TestClient` to verify `/health`, `/resolve`, `/v2/plan`, `/v2/resolve`, and execution lookup all return successful schema-valid responses.

- [ ] **Step 3: Inspect repository state**

```bash
git status --short
git log --oneline -8
```

Expected: no uncommitted source/test changes and all task commits present.

- [ ] **Step 4: Package committed tree**

```bash
git archive --format=zip --output=/mnt/data/constrained-topological-engine-v2.zip HEAD
```

Expected: archive exists and lists the v2 modules and tests.

---

### Task 9: Spec-Coverage Hardening

**Files:**
- Modify: `src/ctd/models.py`
- Modify: `src/ctd/controller.py`
- Modify: `src/ctd/providers.py`
- Modify: `src/ctd/repositories.py`
- Modify: `src/ctd/planner.py`
- Modify: `src/ctd/resolver.py`
- Modify: `src/ctd/telemetry.py`
- Modify: `src/ctd/api.py`
- Create: `src/ctd/inference.py`
- Create: `tests/test_inference.py`
- Create: `tests/test_hardening_v2.py`

**Interfaces:**
- Produces: deterministic acyclic inference rules, complete repository protocol set, optional security-label Focus, richer plan metadata/result summaries, aggregate metrics/profile listing, provider-failure behavior, and top-k/soft-ranking verification.

- [ ] **Step 1: Write failing hardening tests** covering inference lineage/depth, security labels, provider failure, repository interfaces, top-k soft ranking, profile/metrics endpoints, and correlated-source diversity policy.
- [ ] **Step 2: Run the new tests and confirm RED for the missing capabilities.**
- [ ] **Step 3: Implement the minimal deterministic capabilities required by the approved v2 spec.** Inference must never recurse cyclically, security filtering must happen before resolver scoring, and learned/profile data must remain advisory.
- [ ] **Step 4: Run `pytest -q` and `python -m compileall -q src tests`.**
- [ ] **Step 5: Commit the hardening slice independently.**
