# CTD v3 Production Intelligence Tier Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the CTD v2 deterministic resolution kernel into a durable, authenticated, multi-provider production intelligence service with natural-language compilation, ontology/entity resolution, async decomposition, safe learned planner advice, structured tracing, and v3 APIs.

**Architecture:** Keep `Resolver` as the trusted kernel. Add production capabilities through new configuration, auth, persistence, provider, compiler, advisor, async, tracing, and service modules, then expose them through additive v3 API routes. Optional PostgreSQL, Neo4j, and OpenTelemetry integrations load lazily; the default testable production reference stack uses SQLite and standard-library integrations.

**Tech Stack:** Python 3.13, Pydantic 2, FastAPI, sqlite3, asyncio, urllib, hmac/hashlib, optional psycopg 3, optional neo4j 5, optional OpenTelemetry 1.x.

**Spec:** `docs/superpowers/specs/2026-09-12-constrained-topological-engine-production-design.md`

## Global Constraints

- Preserve all v1/v2 public routes and models.
- Hard constraints, evidence rules, runtime budgets, tenant isolation, and authorization Focus cannot be relaxed by learned/advisory components.
- No optional production dependency may be imported at module import time.
- All externally supplied SQL values are parameterized; dynamic database identifiers are fixed by configuration or validated against conservative identifier rules.
- Raw credentials, API keys, JWTs, and configured secrets must never appear in telemetry, execution records, or API capability responses.
- Natural-language compilation is deterministic controlled-language parsing only; it cannot execute arbitrary expressions.
- Async execution may only parallelize independent QCG components and cannot increase aggregate configured budgets.
- Every task follows red-green-refactor and ends with the full relevant regression suite passing.

---

### Task 1: Production configuration and enterprise authorization

**Files:**
- Create: `src/ctd/config.py`
- Create: `src/ctd/auth.py`
- Modify: `src/ctd/controller.py`
- Test: `tests/test_config_auth_v3.py`

**Interfaces:**
- Produces: `EngineSettings`, `Principal`, `RolePolicy`, `ApiKeyIdentityStore`, `Authenticator`, `PolicyEnforcer`.
- Extends: `RuntimePolicy` with `tenant_id: str | None`.

- [ ] **Step 1: Write failing configuration tests**

Test `EngineSettings.from_env()` for defaults, SQLite path, auth mode, hashed API-key mapping, JWT validation requirements, JSONL tracing validation, and no secret exposure through `safe_dict()`.

- [ ] **Step 2: Run the configuration tests and verify RED**

Run: `pytest tests/test_config_auth_v3.py -q`
Expected: import failure for `ctd.config`.

- [ ] **Step 3: Implement `EngineSettings`**

Implement a Pydantic model with the exact fields from the production spec, a deterministic `from_env(env: Mapping[str, str] | None = None)` parser, and `safe_dict()` that omits secret values and API-key hashes.

- [ ] **Step 4: Write failing authentication tests**

Cover:

```python
principal = Authenticator(settings, identity_store).authenticate_api_key("ops.s3cret")
assert principal.subject == "analyst@example"

with pytest.raises(AuthenticationError):
    authenticator.authenticate_api_key("ops.wrong")
```

Add HS256 helpers in the test to verify valid token, tampered signature, `alg=none`, expiry, issuer, and audience behavior.

- [ ] **Step 5: Implement identity/authentication**

Implement:

```python
class Principal(BaseModel): ...
class AuthenticationError(ValueError): ...
class AuthorizationError(PermissionError): ...
class ApiKeyIdentityStore: ...
class Authenticator:
    def authenticate_api_key(self, value: str) -> Principal: ...
    def authenticate_jwt(self, token: str, *, now: datetime | None = None) -> Principal: ...
```

Use `hmac.compare_digest`; decode URL-safe base64 with strict JSON/object checks; require `alg == "HS256"`.

- [ ] **Step 6: Write failing Focus-intersection tests**

Verify tenant ID, role labels, node/edge allowlists, and requested restrictive Focus are intersected rather than broadened.

- [ ] **Step 7: Implement `RolePolicy` and `PolicyEnforcer`**

`PolicyEnforcer.apply(principal, requested_policy) -> RuntimePolicy` must preserve runtime ceilings and only narrow authorization-related fields.

- [ ] **Step 8: Run regression suite**

Run: `pytest tests/test_config_auth_v3.py tests/test_controller.py tests/test_resolver_v2.py -q`
Expected: all pass.

- [ ] **Step 9: Commit**

```bash
git add src/ctd/config.py src/ctd/auth.py src/ctd/controller.py tests/test_config_auth_v3.py
git commit -m "feat: add production configuration and authorization"
```

---

### Task 2: Durable SQLite and PostgreSQL repository adapters

**Files:**
- Create: `src/ctd/persistence.py`
- Modify: `src/ctd/repositories.py`
- Test: `tests/test_persistence_v3.py`

**Interfaces:**
- Produces: `AdvisorRepository`, `SQLiteStore`, `PostgresStore`, `AdvisorStat`.
- `SQLiteStore` implements all existing repository protocol methods plus graph persistence, `ping()`, and `transaction()`.

- [ ] **Step 1: Write failing SQLite durability tests**

Use `tmp_path / "ctd.db"`; save nodes, edges/evidence, execution, profile, advisor stat, close store, construct a second store, and verify records and `load_graph()` survive.

- [ ] **Step 2: Verify RED**

Run: `pytest tests/test_persistence_v3.py::test_sqlite_store_is_durable -q`
Expected: import failure for `ctd.persistence`.

- [ ] **Step 3: Implement SQLite schema and repository contract**

Use `sqlite3.connect(..., check_same_thread=False)`, row factory, a reentrant lock, WAL for file databases, parameterized statements, JSON serialization with sorted keys, and explicit schema version `3` in `ctd_meta`.

- [ ] **Step 4: Add rollback and immutable execution tests**

Verify exceptions inside `transaction()` roll back, node/profile UPSERT is idempotent, and duplicate execution IDs raise `ValueError`.

- [ ] **Step 5: Implement transactional behavior**

Nested operations inside explicit transaction must not commit independently. Repository methods outside explicit transaction commit atomically.

- [ ] **Step 6: Write failing PostgreSQL contract tests with fake connection**

Verify `PostgresStore` lazily accepts injected connection/factory, uses `%s` placeholders, serializes JSON, initializes schema transactionally, and never interpolates user payload values into SQL strings.

- [ ] **Step 7: Implement `PostgresStore`**

Lazy import `psycopg` only when no connection factory is injected. Provide the same logical repository interface and `ping()`.

- [ ] **Step 8: Run persistence and v2 repository regressions**

Run: `pytest tests/test_persistence_v3.py tests/test_providers.py tests/test_profiles.py -q`
Expected: all pass.

- [ ] **Step 9: Commit**

```bash
git add src/ctd/persistence.py src/ctd/repositories.py tests/test_persistence_v3.py
git commit -m "feat: add durable production repositories"
```

---

### Task 3: Production provider suite and federation

**Files:**
- Create: `src/ctd/providers_ext.py`
- Modify: `src/ctd/providers.py`
- Test: `tests/test_providers_v3.py`

**Interfaces:**
- Produces: `ProviderHealth`, `SQLProvider`, `DocumentProvider`, `VectorRecord`, `VectorProvider`, `RemoteAPIProvider`, `Neo4jProvider`, `FederatedProvider`.
- Extends provider authorization with tenant filtering.

- [ ] **Step 1: Write failing tenant-isolation and SQLProvider tests**

Populate SQLite node/edge tables with two tenants; construct `SQLProvider(..., tenant_id="acme")`; assert only Acme records are visible and costs are reported.

- [ ] **Step 2: Implement provider tenant filtering and SQLProvider**

Add `tenant_id` to `GraphProvider` and implement parameterized SQL queries in `SQLProvider`.

- [ ] **Step 3: Write DocumentProvider tests**

Create JSONL fixture with node and edge records; assert deterministic load, evidence validation, and malformed record failure.

- [ ] **Step 4: Implement DocumentProvider**

Parse `.json` array/object or `.jsonl`; index into an `EvidenceGraph`; delegate EvidenceProvider operations through `GraphProvider`.

- [ ] **Step 5: Write VectorProvider tests**

Verify cosine ordering, type filtering, deterministic tie-break, dimension mismatch rejection, and zero-vector score `0.0`.

- [ ] **Step 6: Implement VectorProvider**

Expose:

```python
class VectorProvider:
    def search(self, vector: list[float], *, node_type: str | None = None, limit: int = 5) -> list[VectorMatch]: ...
```

- [ ] **Step 7: Write RemoteAPIProvider tests with injected opener**

Verify node/edge parsing, URL encoding, timeout propagation, max-byte enforcement, non-2xx failure, and malformed JSON failure.

- [ ] **Step 8: Implement RemoteAPIProvider**

Use fixed endpoint paths and `urllib.parse.urlencode`; read at most `max_bytes + 1` and reject oversized payloads.

- [ ] **Step 9: Write Neo4jProvider fake-driver tests**

Assert parameterized Cypher values and identifier validation reject malicious relation/type names.

- [ ] **Step 10: Implement Neo4jProvider**

Lazy import driver only when requested. Convert driver records into Pydantic `Node`/`Edge`.

- [ ] **Step 11: Write federation tests**

Verify stable provider order, de-duplication, first-provider conflict ownership, summed cost, best-effort provider failure, fail-fast mode, tenant isolation, and health aggregation.

- [ ] **Step 12: Implement FederatedProvider**

Support sequential and bounded `ThreadPoolExecutor` fan-out. Sort merged results deterministically by first-provider precedence then ID.

- [ ] **Step 13: Run provider regressions**

Run: `pytest tests/test_providers.py tests/test_providers_v3.py tests/test_resolver_v2.py -q`
Expected: all pass.

- [ ] **Step 14: Commit**

```bash
git add src/ctd/providers.py src/ctd/providers_ext.py tests/test_providers_v3.py
git commit -m "feat: add production evidence providers and federation"
```

---

### Task 4: Ontology, entity resolution, and natural-language compilation

**Files:**
- Create: `src/ctd/ontology.py`
- Create: `src/ctd/compiler.py`
- Test: `tests/test_compiler_v3.py`

**Interfaces:**
- Produces: `OntologyRegistry`, `EntityResolution`, `EntityResolver`, `CompileDiagnostic`, `CompileResult`, `NaturalLanguageCompiler`.

- [ ] **Step 1: Write failing ontology and entity-resolution tests**

Register Supplier/Component aliases, PRODUCES aliases, attributes, and known `component:x` aliases. Verify exact, alias, fuzzy, ambiguous, and vector-assisted paths.

- [ ] **Step 2: Implement ontology registry and resolver**

Normalize with Unicode NFKC + casefold + collapsed whitespace. Fuzzy matching uses `SequenceMatcher`; vector matches are advisory after lexical methods.

- [ ] **Step 3: Write failing natural-language supplier compiler test**

Input:

```text
Find a supplier that produces component X, certification is ISO9001,
lead time at most 30 days, price under 500; top 3.
```

Assert target variable, relation to `component:x`, three attribute constraints, `top_k == 3`, stable constraint IDs, and no unresolved required terms.

- [ ] **Step 4: Implement compiler grammar**

Build deterministic phrase scanning from ontology aliases, comparison phrase registry, conjunction splitting, typed value parsing, and entity resolution.

- [ ] **Step 5: Add ambiguity/unknown diagnostics tests**

Unknown relation/attribute/entity must appear in diagnostics/unresolved terms and must not be guessed into a different schema term.

- [ ] **Step 6: Implement diagnostics and confidence**

Confidence is a descriptive weighted ratio of exact/alias/fuzzy resolutions and parsed clauses; it cannot affect RuntimePolicy.

- [ ] **Step 7: Run compiler/model regressions**

Run: `pytest tests/test_compiler_v3.py tests/test_models_v2.py tests/test_planner.py -q`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add src/ctd/ontology.py src/ctd/compiler.py tests/test_compiler_v3.py
git commit -m "feat: add ontology and natural language query compiler"
```

---

### Task 5: Safe learned planner advisor

**Files:**
- Create: `src/ctd/advisor.py`
- Modify: `src/ctd/planner.py`
- Test: `tests/test_advisor_v3.py`

**Interfaces:**
- Produces: `PlannerAdvice`, `PlannerAdvisor`, `AdaptivePlannerAdvisor`, `AdvisedQueryPlanner`.

- [ ] **Step 1: Write failing advisor learning tests**

Observe resolved and failed executions; verify durable reward statistics and deterministic advice order.

- [ ] **Step 2: Implement AdaptivePlannerAdvisor**

Use repository stats and mean reward with stable operation-ID tie-break.

- [ ] **Step 3: Write safety-envelope tests**

Construct a high-reward soft constraint and low-reward hard constraint. Assert the advised planner still places hard first. Construct hard operations whose base priorities differ beyond `0.05`; assert advice cannot reverse them.

- [ ] **Step 4: Implement AdvisedQueryPlanner**

Wrap base plan, group by hard flag, permit advisory reorder only inside the configured base-priority window, preserve all operation semantics, and include `advisor_version`, `advice_applied`, `advice_rejected` metadata in `ExecutionPlan` optional fields.

- [ ] **Step 5: Run planner/profile regressions**

Run: `pytest tests/test_advisor_v3.py tests/test_planner.py tests/test_profiles.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/ctd/advisor.py src/ctd/planner.py tests/test_advisor_v3.py
git commit -m "feat: add safe learned planner advisor"
```

---

### Task 6: Async independent-subproblem execution

**Files:**
- Create: `src/ctd/async_exec.py`
- Test: `tests/test_async_v3.py`

**Interfaces:**
- Produces: `QueryDecomposer`, `AsyncSubproblemExecutor`.

- [ ] **Step 1: Write decomposition tests**

Verify connected variables remain one component; two relation-disconnected variable groups become two deterministic components; attribute constraints stay with their variables.

- [ ] **Step 2: Implement QueryDecomposer**

Use union-find over relation variable pairs. Preserve query metadata and order components by sorted variable names.

- [ ] **Step 3: Write async budget/merge tests**

Use a resolver factory recording policies. Assert sum of component `max_expansions`, provider calls, bytes, and candidates never exceeds global limits. Assert deterministic merged bindings and terminal precedence.

- [ ] **Step 4: Implement AsyncSubproblemExecutor**

Allocate integer budgets proportionally by hard-constraint weight with deterministic remainder distribution. Resolve components with `asyncio.to_thread`; merge results and telemetry summaries.

- [ ] **Step 5: Run resolver regressions**

Run: `pytest tests/test_async_v3.py tests/test_resolver.py tests/test_resolver_v2.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/ctd/async_exec.py tests/test_async_v3.py
git commit -m "feat: add bounded asynchronous subproblem execution"
```

---

### Task 7: Structured tracing and OpenTelemetry bridge

**Files:**
- Create: `src/ctd/tracing.py`
- Test: `tests/test_tracing_v3.py`

**Interfaces:**
- Produces: `TraceSpan`, `TraceSink`, `Tracer`, `InMemoryTraceSink`, `JsonLineTraceSink`, `OpenTelemetryTraceSink`.

- [ ] **Step 1: Write failing in-memory/JSONL trace tests**

Verify nested span parentage, stable trace ID, status on exception, thread-safe JSONL append, and no secret attribute values when attributes are passed through `sanitize_trace_attributes()`.

- [ ] **Step 2: Implement tracing core**

Use UUID hex IDs, timezone-aware UTC timestamps, context-manager spans, and JSON-safe sanitized attributes.

- [ ] **Step 3: Write OTel dependency/fake-tracer tests**

If OpenTelemetry is unavailable, constructing configured OTel sink raises `RuntimeError` with install-extra guidance. With injected tracer, verify span names and attributes are forwarded.

- [ ] **Step 4: Implement OpenTelemetryTraceSink**

Lazy import only in constructor when no tracer injected.

- [ ] **Step 5: Run tracing tests**

Run: `pytest tests/test_tracing_v3.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/ctd/tracing.py tests/test_tracing_v3.py
git commit -m "feat: add production tracing and OpenTelemetry bridge"
```

---

### Task 8: ProductionEngine orchestration and durable replay

**Files:**
- Create: `src/ctd/service.py`
- Modify: `src/ctd/resolver.py`
- Test: `tests/test_service_v3.py`

**Interfaces:**
- Produces: `ProductionEngine` and production execution records.
- Resolver change: accept a planner compatible with existing `QueryPlanner.plan()` contract; no change to resolution semantics.

- [ ] **Step 1: Write failing end-to-end service test**

Construct SQLite store + supplier graph/provider + ontology. Call `engine.resolve(text=...)`; assert `RESOLVED`, execution persisted, profile updated, advisor observed, trace recorded.

- [ ] **Step 2: Implement service construction and text/QCG resolve path**

Service compiles text when needed, applies principal policy, retrieves profile/advice, resolves, persists execution, updates profile/advisor, and returns result.

- [ ] **Step 3: Write restart/replay test**

Close service/store, create a new store/engine over same SQLite file, retrieve execution, call `verify_replay`, and assert fingerprint match.

- [ ] **Step 4: Implement durable replay snapshot**

Persist provider/graph snapshot when provider supports snapshot; for graph-backed reference service replay from stored graph snapshot. Record explicit `replay_supported=false` for non-snapshot remote-only configurations instead of pretending reproducibility.

- [ ] **Step 5: Write readiness/provider-health tests**

Verify healthy SQLite/federated providers yield ready; configured unavailable dependency/provider yields `ready=False` with component detail.

- [ ] **Step 6: Implement metrics/readiness**

Aggregate v3 execution/auth/compiler/advisor/provider metrics without exposing secrets.

- [ ] **Step 7: Run service and v2 regressions**

Run: `pytest tests/test_service_v3.py tests/test_runtime_v2.py tests/test_api_v2.py -q`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add src/ctd/service.py src/ctd/resolver.py tests/test_service_v3.py
git commit -m "feat: add production engine orchestration"
```

---

### Task 9: v3 API, authentication middleware, health, and capabilities

**Files:**
- Modify: `src/ctd/api.py`
- Test: `tests/test_api_v3.py`

**Interfaces:**
- Adds v3 routes listed in spec while preserving all existing routes.

- [ ] **Step 1: Write failing v3 API tests**

Cover `/health/live`, `/health/ready`, `/v3/capabilities`, `/v3/ontology`, `/v3/compile`, `/v3/plan`, `/v3/resolve`, `/v3/resolve/async`, execution retrieval, replay verification, metrics, and provider health.

- [ ] **Step 2: Implement v3 request models and routes**

`V3ResolveRequest` model validator requires exactly one of `query` or `text`. Text requests require `as_of`.

- [ ] **Step 3: Write auth HTTP tests**

API-key mode: missing/invalid -> 401; valid key lacking scope -> 403; valid analyst -> 200. JWT mode: valid -> 200; tampered -> 401. v1/v2 behavior remains unchanged unless production app explicitly opts into protecting legacy routes.

- [ ] **Step 4: Implement principal dependency**

Authenticate from configured mode and enforce route scopes before calling service. Never pass raw auth header into traces or records.

- [ ] **Step 5: Run all API tests**

Run: `pytest tests/test_api.py tests/test_api_v2.py tests/test_api_v3.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/ctd/api.py tests/test_api_v3.py
git commit -m "feat: expose authenticated CTD v3 production API"
```

---

### Task 10: Packaging, production docs, benchmark fixtures, and release verification

**Files:**
- Modify: `pyproject.toml`
- Modify: `README.md`
- Modify: `src/ctd/__init__.py`
- Create: `examples/supplier_query.txt`
- Create: `tests/test_production_benchmarks.py`

**Interfaces:**
- Package version `0.3.0`.
- Optional extras: `postgres`, `neo4j`, `otel`, `production` aggregate.

- [ ] **Step 1: Write production benchmark tests**

Benchmark fixture assertions are deterministic functional thresholds, not machine-speed promises:

- NL supplier compile produces <= 8 constraints,
- supplier resolution considers fewer nodes than physical graph total when hard filters are selective,
- tenant isolation returns zero cross-tenant candidates,
- federated duplicate provider does not duplicate candidate bindings,
- advisor cannot alter terminal correctness compared with base planner.

- [ ] **Step 2: Update packaging**

Set version `0.3.0`; declare optional extras without installing them in core verification.

- [ ] **Step 3: Rewrite README production sections**

Document architecture, environment variables, auth examples, SQLite use, optional PostgreSQL/Neo4j/OTel installation, NL compile example, v3 endpoints, durability/replay limitations, and security model.

- [ ] **Step 4: Run full suite**

Run: `pytest -q`
Expected: all tests pass, including original 57.

- [ ] **Step 5: Compile package**

Run: `python -m compileall -q src tests`
Expected: exit 0.

- [ ] **Step 6: Verify editable installation without optional extras**

Run: `python -m pip install -e . --no-build-isolation`
Expected: exit 0.

- [ ] **Step 7: Run CLI regression**

Run: `ctd demo`
Expected: supplier demo returns `RESOLVED`.

- [ ] **Step 8: Run production API smoke script**

Use FastAPI `TestClient` against a temporary SQLite-backed production app. Exercise liveness/readiness, NL compile, resolve, async resolve, execution retrieval, replay verification, metrics, and provider health. Assert every response is successful and replay match is true.

- [ ] **Step 9: Verify repository cleanliness and inspect diff**

Run:

```bash
git status --short
git diff --check
```

Expected: only intended pre-commit changes, no whitespace errors.

- [ ] **Step 10: Commit release slice**

```bash
git add pyproject.toml README.md src/ctd/__init__.py examples/supplier_query.txt tests/test_production_benchmarks.py
git commit -m "release: complete CTD v3 production intelligence tier"
```

- [ ] **Step 11: Re-run full verification on exact committed tree**

Run:

```bash
pytest -q
python -m compileall -q src tests
ctd demo
git status --short
```

Expected: all green and clean worktree.

- [ ] **Step 12: Package and integrity-check archive**

Create `/mnt/data/constrained-topological-engine-v3-production.zip` from the committed tree excluding `.git`, `.pytest_cache`, `__pycache__`, build metadata, and local SQLite runtime files. Run `python -m zipfile -t` and compute SHA-256.
