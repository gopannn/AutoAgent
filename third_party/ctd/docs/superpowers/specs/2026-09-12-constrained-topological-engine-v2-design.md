# Constrained Topological Dynamics v2 — Adaptive Evidence Resolution Engine

## Status

Approved architecture direction. This document is the implementation contract for the v2 upgrade of the existing CTD MVP on branch `feature/ctd-mvp`.

## 1. Objective

Evolve the current deterministic in-memory graph resolver into a robust adaptive evidence-resolution engine that can compile structured problems, choose efficient execution plans, search heterogeneous evidence providers, fuse and challenge evidence, rank multiple valid candidates, preserve provenance, expose uncertainty explicitly, and abstain with precise failure causes when resolution cannot be justified.

The v2 engine must retain deterministic safety bounds. Learned or heuristic components may recommend actions, but cannot bypass hard constraints, authorization filters, evidence policy, or execution budgets.

## 2. Non-goals

v2 does not attempt to implement distributed cluster scheduling, production authentication, enterprise multi-tenancy, a full graph database, general natural-language understanding, reinforcement-learning policy training, or unrestricted autonomous external browsing. It creates interfaces and state models that make those capabilities addable later without rewriting the core resolver.

## 3. Core invariants

1. Hard constraints are never traded away for score.
2. A claim is not accepted solely because a graph edge exists.
3. Every accepted relation must have valid evidence or a traceable inference path that meets policy.
4. Search may widen, but never beyond explicit policy bounds.
5. Validation receives a protected budget reserve so search cannot consume the entire execution allowance.
6. Authorization is part of Focus. Unauthorized nodes and edges behave as nonexistent.
7. Correlated evidence is discounted; duplicated copies of one source do not count as independent confirmation.
8. Temporal validity is evaluated against the query `as_of` time.
9. Derived conclusions retain complete dependency lineage.
10. Terminal output is one of: `RESOLVED`, `PARTIAL`, `CONTRADICTED`, `UNRESOLVABLE`, `BUDGET_EXHAUSTED`, or `PROVIDER_ERROR`.
11. Every execution is deterministically replayable when given the same graph/provider snapshot, policy, planner seed, and query.
12. Existing v1 `/resolve` requests remain accepted where practical; v2-only fields are optional.

## 4. System decomposition

The engine is split into seven logical planes.

### 4.1 Query Intelligence Plane

Responsibilities:
- normalize input into a typed Query Constraint Graph (QCG),
- validate variable and constraint references,
- derive dependency relationships,
- classify hard versus soft constraints,
- assign evidence requirements,
- calculate initial uncertainty and selectivity estimates,
- produce a deterministic execution plan.

v2 accepts a structured QCG as its canonical API. A natural-language compiler may be added later through the same compiler interface.

### 4.2 Planning Plane

The planner produces an ordered set of resolution operations instead of traversing constraints in declaration order.

For each operation `a`, estimate:

`priority(a) = expected_information_gain(a) * selectivity(a) * reliability(a) / max(estimated_cost(a), epsilon)`

The planner prioritizes:
1. hard constraints before soft constraints,
2. highly selective predicates before broad predicates,
3. already-bound relations before unbound relations,
4. authoritative low-cost providers before expensive weak providers,
5. contradiction checks before declaring closure.

Planner output is inspectable and stored in telemetry.

### 4.3 Adaptive Runtime Control Plane

The existing Drive/Urgency/Arousal/Focus model becomes explicit runtime state.

- **Drive**: budget allocation across search, provider access, candidate evaluation, and validation.
- **Urgency**: wall-clock, expansion, depth, provider-call, and byte-read ceilings.
- **Arousal**: exploration width, candidate count, provider breadth, and relaxation level.
- **Focus**: admissible node types, relation types, sources, domains, authorization scope, temporal range, and constraint subset.

The controller updates state from progress and uncertainty, but all decisions remain deterministic in v2.

Controller behavior:
- convergence -> narrow Focus and reduce Arousal,
- stagnation with budget -> widen Arousal progressively,
- low remaining validation reserve -> stop search and validate best candidates,
- impossible hard constraint -> terminate branch immediately,
- deadline risk -> stop expansion before deadline rather than after it.

### 4.4 Provider Plane

The graph is generalized behind an `EvidenceProvider` interface.

Initial providers:
- `GraphProvider`: wraps the existing `EvidenceGraph`,
- `MemoryProvider`: deterministic provider used by tests and synthetic fixtures.

Future adapters can implement SQL, document, vector, search, stream, or remote API access.

Every provider returns normalized candidates/evidence plus cost metadata. The resolver must not depend directly on graph implementation details.

### 4.5 Truth and Evidence Plane

A relation is represented as a Claim with one or more Evidence records.

Claim status:
- `SUPPORTED`
- `DISPUTED`
- `SUPERSEDED`
- `RETRACTED`
- `UNKNOWN`
- `INVALID`

Evidence includes:
- source identity and source class,
- observed time,
- validity interval,
- confidence,
- source trust,
- independence group,
- extraction method,
- direct versus inferred flag,
- lineage references,
- optional claim key/value.

Evidence fusion initially uses deterministic weighted support:

`support = 1 - product(1 - effective_weight_i)`

where effective weight is derived from confidence, trust, freshness, and independence discount. Multiple pieces from the same independence group are capped so copies of one source cannot dominate.

Contradiction logic distinguishes:
- simultaneous incompatible assertions,
- temporal supersession,
- retraction,
- source disagreement,
- inference conflict.

A newer temporally valid fact can supersede an older one without marking the final answer contradictory when policy permits temporal resolution.

### 4.6 Resolution Plane

Resolution is a bounded best-first search over planned operations.

Candidate state contains:
- variable bindings,
- satisfied/violated/unresolved constraints,
- evidence bundle,
- claim states,
- accumulated cost,
- inference depth,
- uncertainty vector,
- candidate score.

Hard violations prune immediately.

Soft constraints contribute to ranking only after hard validity is established.

The resolver returns a Pareto-ranked set internally and exposes the best candidate by default. An optional `top_k` parameter may expose multiple valid candidates.

### 4.7 Telemetry and Runtime Memory Plane

Every transition emits a structured event.

Events include:
- query compiled,
- plan generated,
- operation selected,
- provider called,
- candidate created,
- branch pruned with reason,
- evidence accepted/rejected,
- contradiction detected/resolved,
- Focus narrowed/widened,
- Arousal changed,
- budget reserve entered,
- candidate validated,
- terminal state.

Query-class runtime profiles record aggregate execution statistics without modifying core policy directly.

Profiles contain:
- successful planner order,
- mean/percentile expansions,
- mean time-to-closure,
- useful providers,
- common failure causes,
- effective Arousal range,
- evidence source success rates.

Profiles are advisory priors only.

## 5. Domain model

### 5.1 Query constraints

Existing constraints remain supported.

New constraint concepts:
- `required_evidence_strength`
- `required_source_classes`
- `max_inference_depth`
- `soft_weight`
- `temporal_mode`
- `cardinality`
- `exclusion`

Operators remain simple in v2: `eq`, `ne`, `lt`, `lte`, `gt`, `gte`, `in`; extensibility is through a registry rather than adding arbitrary executable expressions.

### 5.2 Resolution uncertainty

Replace the single conceptual confidence number with explicit dimensions:
- `structural_coverage`
- `evidence_coverage`
- `evidence_strength`
- `temporal_freshness`
- `source_diversity`
- `contradiction_risk`
- `inference_depth_penalty`
- `resolution_confidence`

`resolution_confidence` is derived for ranking and UX, not used to override hard rules.

### 5.3 Failure taxonomy

`FailureReason` values:
- `MISSING_DATA`
- `UNSATISFIABLE_CONSTRAINT`
- `EVIDENCE_TOO_WEAK`
- `CONTRADICTORY_EVIDENCE`
- `AUTHORIZATION_EXCLUDED`
- `DEADLINE_EXHAUSTED`
- `EXPANSION_BUDGET_EXHAUSTED`
- `DEPTH_BUDGET_EXHAUSTED`
- `PROVIDER_CALL_BUDGET_EXHAUSTED`
- `PROVIDER_UNAVAILABLE`
- `SCHEMA_MISMATCH`
- `SEARCH_SPACE_EXHAUSTED`
- `VALIDATION_RESERVE_EXHAUSTED`

Terminal result can include multiple reasons.

## 6. Runtime budget model

`RuntimePolicy` is expanded with:
- `max_expansions`
- `max_depth`
- `deadline_ms`
- `max_provider_calls`
- `max_bytes_read`
- `max_candidates`
- `validation_reserve_ratio`
- `initial_arousal`
- `max_arousal`
- `widen_step`
- `stagnation_before_widen`
- `min_evidence_strength`
- `min_source_diversity`
- allowed node/edge/source classes
- authorization scope

The controller computes a search budget and protected validation budget from total budget. Search must stop once the protected reserve would be violated.

## 7. Planning model

Planner stages:
1. derive variable dependency graph,
2. estimate type cardinalities from provider statistics,
3. estimate attribute selectivity,
4. estimate relation branching factor,
5. calculate evidence requirements,
6. produce ordered `PlanStep` objects,
7. calculate fallback/widening alternatives.

`PlanStep` contains:
- operation id,
- constraint id,
- operation kind,
- provider preference order,
- estimated selectivity,
- estimated cost,
- expected information gain,
- fallback providers,
- required bindings.

Planner behavior is deterministic for equal scores through stable IDs.

## 8. Search model

The resolver uses a priority queue rather than depth-first recursion.

Candidate priority combines:
- hard constraints satisfied,
- hard constraints remaining,
- information gain achieved,
- evidence strength,
- source diversity,
- soft score,
- accumulated cost,
- contradiction risk.

Search algorithm:
1. seed from the most selective root operation,
2. evaluate/prune hard violations immediately,
3. enqueue viable candidates,
4. expand highest-priority candidate,
5. progressively widen provider/candidate breadth on stagnation,
6. stop search when validation reserve is reached,
7. validate top candidate set,
8. return resolution or explicit abstention.

## 9. Evidence validation model

An edge is accepted only when:
1. it is temporally valid,
2. at least one evidence record passes minimum confidence/trust,
3. aggregate evidence strength passes policy,
4. source diversity passes required threshold when configured,
5. no unresolved blocking contradiction remains,
6. authorization permits the source and linked entities.

Evidence rejection reasons are exposed in telemetry.

## 10. Inference model

v2 introduces deterministic rules only.

An inference rule declares:
- rule id,
- premise relation types,
- output relation type,
- maximum depth,
- rule confidence multiplier.

Derived evidence records reference their premise evidence IDs. Inference cannot exceed policy maximum depth. No cyclic rule expansion is permitted.

Inference is optional and disabled by default until a rule registry is supplied.

## 11. Persistence interfaces

Define repository protocols:
- `NodeRepository`
- `EdgeRepository`
- `ClaimRepository`
- `EvidenceRepository`
- `ExecutionRepository`
- `QueryProfileRepository`

v2 ships with in-memory implementations only. APIs are designed so SQLite/PostgreSQL/Neo4j implementations can be added later.

## 12. Backward compatibility

The existing public API remains usable:
- `QueryConstraintGraph`
- `RuntimePolicy`
- `Resolver.resolve()`
- `/resolve`

Compatibility rules:
- existing fields retain semantics,
- new fields have safe defaults,
- `ResolutionState` adds new states but keeps old values,
- legacy evidence without new metadata receives deterministic defaults,
- current supplier demo continues to resolve successfully.

## 13. API additions

New endpoints:
- `POST /v2/resolve` — v2 resolution with detailed diagnostics,
- `POST /v2/plan` — compile/inspect plan without executing,
- `GET /v2/executions/{id}` — retrieve execution trace from repository,
- `GET /v2/profiles` — inspect accumulated query-class runtime profiles,
- `GET /v2/metrics` — lightweight engine metrics.

Existing `/resolve` remains.

## 14. Detailed v2 output

`ResolutionResult` is extended with:
- execution id,
- ranked candidates,
- uncertainty metrics,
- evidence summary,
- failure reasons,
- planner summary,
- budget summary,
- trace summary,
- recommended next actions.

Detailed evidence can be requested through a verbose flag to avoid inflating default responses.

## 15. Observability metrics

Track at minimum:
- time-to-closure p50/p95/p99,
- nodes considered,
- edges traversed,
- candidates generated,
- candidates pruned,
- candidate reduction ratio,
- provider calls by provider,
- provider latency,
- evidence acceptance/rejection counts,
- contradiction detection/resolution counts,
- source diversity,
- inference depth,
- validation reserve usage,
- budget exhaustion category,
- structural coverage,
- evidence coverage,
- successful resolution rate,
- abstention rate,
- unsupported assertion rate in benchmark fixtures.

## 16. Security model

v2 implements authorization as a policy predicate over:
- node types,
- edge types,
- source classes,
- optional security labels on nodes/edges.

The provider filters unauthorized results before they reach candidate scoring. Telemetry records aggregate authorization pruning counts but does not leak hidden entity identifiers.

## 17. Testing strategy

The implementation follows test-driven development.

Required suites:
1. Domain validation tests.
2. Planner ordering/selectivity tests.
3. Controller budget reserve tests.
4. Evidence fusion tests.
5. Correlated-source discount tests.
6. Temporal supersession tests.
7. Contradiction tests.
8. Provider failure tests.
9. Authorization filtering tests.
10. Best-first search tests.
11. Pareto ranking tests.
12. Failure taxonomy tests.
13. Deterministic replay tests.
14. Backward compatibility tests.
15. API endpoint tests.
16. Benchmark comparison against v1 fixtures.

## 18. Benchmark acceptance criteria

Using deterministic synthetic fixtures:

- v2 must return the same valid supplier resolution as v1.
- v2 must visit no more nodes than v1 on a fixture where one constraint has substantially higher selectivity.
- v2 must reject duplicated correlated evidence as insufficient when independent-source policy requires two sources.
- v2 must resolve temporal supersession without reporting a false contradiction.
- v2 must preserve at least 20% of configured budget for validation whenever `validation_reserve_ratio=0.20`.
- v2 must return a specific `FailureReason` for every non-resolved benchmark case.
- replaying an execution with the same inputs must produce the same plan, candidate ordering, terminal state, and accepted evidence IDs.

Performance numbers are diagnostic rather than absolute in v2; no general O(k) guarantee is claimed.

## 19. Migration strategy

Phase 1: introduce v2 domain types and compatibility adapters.

Phase 2: introduce provider/repository abstractions around existing graph storage.

Phase 3: add planner and selectivity statistics.

Phase 4: replace recursive resolver internals with bounded best-first candidate search.

Phase 5: add evidence fusion, truth maintenance, uncertainty metrics, and failure taxonomy.

Phase 6: add execution traces, runtime profiles, and v2 API endpoints.

Phase 7: run compatibility and benchmark suites, update documentation, package release.

Each phase must leave the test suite green and preserve the v1 API contract.

## 20. File/module target layout

The existing flat modules will be migrated gradually to avoid a big-bang rewrite.

Target layout:

```text
src/ctd/
  domain/
    graph.py
    claims.py
    evidence.py
    query.py
    resolution.py
  planning/
    planner.py
    selectivity.py
    dependency.py
  runtime/
    controller.py
    budget.py
    policy.py
  resolution/
    engine.py
    search.py
    evaluator.py
    ranking.py
    validation.py
  truth/
    fusion.py
    contradictions.py
    temporal.py
    inference.py
  providers/
    base.py
    graph.py
    memory.py
  persistence/
    repositories.py
    memory.py
  intelligence/
    profiles.py
    heuristics.py
  telemetry/
    events.py
    metrics.py
    trace.py
  api/
    app.py
    schemas.py
```

Compatibility modules at current import paths may re-export new implementations during migration.

## 21. Release definition

CTD v2 is considered implemented when:
- all v1 tests remain green,
- new v2 tests and benchmarks pass,
- API compatibility is verified,
- deterministic replay is verified,
- evidence fusion and contradiction semantics are covered by tests,
- planner demonstrably reduces work on the selective benchmark fixture,
- runtime traces explain why the winning candidate was selected,
- README documents architecture, run instructions, API examples, limitations, and extension points,
- the committed tree is clean and packaged as a downloadable artifact.
