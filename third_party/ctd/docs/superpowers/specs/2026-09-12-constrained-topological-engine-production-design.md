# Constrained Topological Engine v3 — Production Intelligence Tier

## Status

Approved for implementation by the user on 2026-09-12. This specification extends CTD v2 on branch `feature/ctd-mvp` into a production-oriented, durable, multi-provider intelligence service while preserving the deterministic v2 resolver as the trusted decision kernel.

## 1. Objective

Build a complete production tier around the v2 evidence-resolution kernel with:

- durable SQLite persistence as the zero-dependency reference store,
- production PostgreSQL repository adapters with lazy optional dependency loading,
- a Neo4j evidence-provider adapter with lazy optional dependency loading,
- functional SQL, document, vector, and remote-API providers,
- deterministic provider federation and provider health reporting,
- enterprise identity, tenant isolation, RBAC/ABAC Focus derivation, API-key verification, and HS256 JWT verification,
- natural-language-to-QCG compilation against an explicit ontology,
- entity and ontology resolution with exact, alias, fuzzy, and optional vector-assisted matching,
- asynchronous independent-subproblem execution and deterministic result assembly,
- learned planner recommendations that remain advisory and cannot weaken hard constraints, budgets, or authorization,
- structured tracing with JSONL and optional OpenTelemetry export,
- readiness/liveness checks, durable execution replay, and production metrics,
- backward-compatible v1/v2 APIs plus a new v3 API surface.

The tier must run and be fully testable without external services. Optional PostgreSQL, Neo4j, and OpenTelemetry adapters must load only when explicitly configured.

## 2. Non-negotiable safety invariants

1. Hard constraints remain non-negotiable.
2. Authorization is applied before candidate visibility. Unauthorized data behaves as nonexistent.
3. Tenant isolation is provider-side, not a response post-filter.
4. Learned recommendations may only affect advisory ordering/provider preference; they cannot change hard/soft classification, evidence thresholds, authorization, or runtime ceilings.
5. Natural-language compilation never executes arbitrary code or expressions.
6. Compiler ambiguity is explicit. Unresolved required entities/terms are returned as diagnostics instead of guessed silently.
7. Provider federation de-duplicates deterministically and accounts for every provider's cost.
8. Remote API access has explicit timeouts and bounded response size.
9. Async decomposition may parallelize only independent query components and must merge deterministically.
10. Persistent replay stores the exact query, policy, principal-derived Focus, planner advisory prior, provider snapshot metadata, and normalized result fingerprint.
11. Optional production integrations fail closed when configured but unavailable.
12. v1/v2 behavior remains regression-tested.

## 3. Version and compatibility

- Package version becomes `0.3.0`.
- `create_app()` remains supported.
- Existing `/resolve`, `/v2/*`, QCG models, terminal states, and v2 result model remain accepted.
- v3 adds capabilities without requiring v1/v2 clients to change.
- Python floor remains `>=3.13`.
- Core runtime remains dependent only on FastAPI, Pydantic, Uvicorn, and the Python standard library.
- PostgreSQL, Neo4j, and OpenTelemetry dependencies are optional extras and imported lazily.

## 4. Production architecture

```text
Client / Agent / Application
          |
          v
Identity Gateway
 API key / HS256 JWT / disabled
          |
          v
Principal + Tenant + Roles + Scopes
          |
          v
Natural Language Compiler ---- Structured QCG
          |                         |
          v                         v
Ontology + Entity Resolver --> Query Compiler
                                  |
                                  v
                         Safe Advisory Planner
                      deterministic base + learned prior
                                  |
                                  v
                       Four-Barrier Runtime Kernel
                                  |
                      +-----------+-----------+
                      |                       |
                      v                       v
             Federated Providers      Async Subproblems
          graph/sql/doc/api/neo4j      independent QCGs
                      |                       |
                      +-----------+-----------+
                                  v
                     Evidence Fusion / Truth
                                  |
                                  v
                         Resolve / Abstain
                                  |
              +-------------------+-------------------+
              |                   |                   |
              v                   v                   v
       Durable Repositories   Tracing / OTel      Runtime Learning
       SQLite/PostgreSQL      JSONL/spans         Advisor statistics
```

## 5. Configuration

Create `ctd.config.EngineSettings`.

Fields:

- `environment: str = "development"`
- `persistence_backend: Literal["memory", "sqlite", "postgres"] = "memory"`
- `sqlite_path: str = ":memory:"`
- `postgres_dsn: str | None = None`
- `neo4j_uri: str | None = None`
- `neo4j_username: str | None = None`
- `neo4j_password: str | None = None`
- `auth_mode: Literal["disabled", "api_key", "hmac_jwt"] = "disabled"`
- `api_key_hashes: dict[str, str] = {}` mapping key ID to SHA-256 digest
- `jwt_hs256_secret: str | None = None`
- `jwt_issuer: str | None = None`
- `jwt_audience: str | None = None`
- `trace_backend: Literal["memory", "jsonl", "otel"] = "memory"`
- `trace_jsonl_path: str | None = None`
- `otel_service_name: str = "ctd-engine"`
- `remote_api_timeout_s: float = 2.0`
- `remote_api_max_bytes: int = 2_000_000`
- `federation_parallelism: int = 4`
- `compiler_fuzzy_threshold: float = 0.82`

`EngineSettings.from_env()` reads `CTD_*` variables. Secrets are never returned from capabilities endpoints.

Validation rules:

- configured postgres requires `postgres_dsn`,
- configured hmac_jwt requires secret,
- configured jsonl tracing requires path,
- configured Neo4j provider requires URI and credentials when constructed.

## 6. Identity and authorization

### 6.1 Principal

Create `ctd.auth.Principal`:

- `subject: str`
- `tenant_id: str | None`
- `roles: set[str]`
- `scopes: set[str]`
- `security_labels: set[str]`
- `auth_method: Literal["disabled", "api_key", "hmac_jwt", "internal"]`

### 6.2 Role policy

Create `RolePolicy`:

- role -> scopes,
- role -> allowed security labels,
- optional node-type and edge-type allowlists.

Default roles:

- `viewer`: `resolve:read`, labels `PUBLIC`, `INTERNAL`.
- `analyst`: viewer + `resolve:advanced`, labels `CONFIDENTIAL`.
- `admin`: wildcard scope and all labels.

### 6.3 PolicyEnforcer

`PolicyEnforcer.apply(principal, requested_policy) -> RuntimePolicy` intersects requested Focus with the principal's effective authorization.

It never broadens a caller-supplied restrictive policy.

The principal tenant ID becomes `RuntimePolicy.tenant_id` and is enforced inside providers using `node.attributes["tenant_id"]` / `edge.attributes["tenant_id"]`.

### 6.4 API keys

Header: `X-CTD-API-Key: <key-id>.<secret>`.

Verification:

- split key ID and secret,
- SHA-256 secret,
- constant-time compare against configured digest,
- principal metadata is supplied by an `ApiKeyIdentityStore` keyed by key ID.

No raw API key is persisted or logged.

### 6.5 HS256 JWT

Implement standard-library verification for compact JWT:

- exact `alg=HS256`,
- HMAC-SHA256 signature,
- `exp`, optional `nbf`, optional issuer/audience,
- claims `sub`, `tenant_id`, `roles`, `scopes`, `security_labels`.

Reject algorithm substitution and malformed base64/JSON.

## 7. Persistence

### 7.1 SQLite reference store

Create `ctd.persistence.SQLiteStore` using `sqlite3` with WAL mode where supported and explicit transactions.

Schema:

- `ctd_nodes(id PRIMARY KEY, type, tenant_id, payload_json, updated_at)`
- `ctd_edges(id PRIMARY KEY, source, target, type, tenant_id, payload_json, updated_at)`
- `ctd_evidence(id PRIMARY KEY, payload_json, updated_at)`
- `ctd_claims(claim_key, sequence, payload_json, PRIMARY KEY(claim_key, sequence))`
- `ctd_executions(execution_id PRIMARY KEY, payload_json, created_at)`
- `ctd_profiles(query_class PRIMARY KEY, payload_json, updated_at)`
- `ctd_advisor(query_class, operation_id, successes, failures, reward_sum, updated_at, PRIMARY KEY(query_class, operation_id))`
- `ctd_meta(key PRIMARY KEY, value)`

SQLiteStore implements all existing repository protocols and adds:

- `save_node`, `save_edge`, `load_graph`,
- `ping()`,
- `transaction()` context manager.

Writes use UPSERT where idempotence is expected. Execution records remain immutable by ID: duplicate IDs fail.

### 7.2 PostgreSQL store

Create `PostgresStore` with the same logical repository contract.

- lazy import `psycopg`,
- JSONB payloads,
- `TIMESTAMPTZ` metadata,
- transactional schema initialization,
- `%s` parameterized queries only,
- no SQL generated from user-controlled identifiers.

Tests use an injected DB-API-compatible fake connection for query/parameter verification. A live database is not required for the core test suite.

### 7.3 Persistent execution replay

v3 execution records include:

- query/input text,
- compiled QCG,
- compiler diagnostics,
- effective RuntimePolicy,
- sanitized principal identity,
- query class,
- planner advice snapshot,
- provider manifest and provider snapshot metadata,
- normalized result fingerprint,
- full result,
- trace ID.

## 8. Provider architecture

### 8.1 Extended provider metadata

Add `ProviderHealth`:

- `name`
- `status: healthy/degraded/unavailable`
- `latency_ms`
- `details` safe for exposure

Providers may implement `health()`.

### 8.2 SQLProvider

Create `ctd.providers_ext.SQLProvider` over a DB-API connection and fixed schema mapping.

Reference schema:

- nodes table: `id`, `type`, `tenant_id`, `attributes_json`
- edges table: `id`, `source`, `target`, `type`, `tenant_id`, `attributes_json`, `evidence_json`

Queries are parameterized. Tenant and security-label filtering is applied in SQL when possible and rechecked in Python.

### 8.3 DocumentProvider

Loads one or more JSON/JSONL documents with canonical records:

```json
{"kind":"node","id":"supplier:a","type":"Supplier","attributes":{...}}
{"kind":"edge","id":"e:1","source":"supplier:a","target":"component:x","type":"PRODUCES","evidence":[...]}
```

It builds a sparse in-process index and implements `EvidenceProvider`.

### 8.4 VectorProvider

Create `VectorRecord` and `VectorProvider` with exact cosine search for deterministic reference behavior.

- validates equal vector dimensionality,
- normalizes zero-vector behavior,
- optional type filter,
- deterministic tie-break by record ID,
- used by entity resolution as an advisory candidate source,
- vector similarity never proves a relation or satisfies a hard evidence constraint by itself.

### 8.5 RemoteAPIProvider

Configurable fixed endpoint base URL. Uses `urllib.request` by default and accepts an injected opener for tests.

Expected endpoints:

- `/nodes/{id}`
- `/nodes?type=...&limit=...`
- `/nodes/{id}/outgoing?type=...&limit=...`
- `/nodes/{id}/incoming?type=...&limit=...`
- `/stats/node-count?type=...`
- `/stats/distinct?type=...&attribute=...`

Safety:

- timeout from settings,
- max response bytes,
- JSON validation through Pydantic,
- non-2xx becomes provider error,
- no URL templates from query text.

### 8.6 Neo4jProvider

Create a lazy-driver adapter accepting an injected Neo4j driver.

Cypher uses parameters for values and validates relationship/type identifiers against a conservative identifier regex before interpolation.

It implements node lookup, type listing, outgoing/incoming edges, counts, and distinct-attribute statistics.

### 8.7 FederatedProvider

Combines ordered providers.

Rules:

- calls providers in stable configured order,
- optional bounded parallel fan-out for safe read operations,
- de-duplicates nodes by ID and edges by ID,
- first provider owns identical node/edge payload conflicts; conflict is emitted to trace,
- costs are summed,
- provider errors can be fail-fast or best-effort based on configuration,
- statistics are aggregated conservatively,
- tenant/security authorization remains enforced in every underlying provider.

## 9. Ontology and entity intelligence

### 9.1 OntologyRegistry

Create `ctd.ontology.OntologyRegistry`.

Stores:

- canonical entity types and aliases,
- canonical relations and aliases,
- per-type attribute aliases,
- known entities and aliases,
- type hierarchy (`child -> parent`),
- unit parsers for integer/float/currency/day values.

It exposes only normalized, allowlisted schema terms to the compiler.

### 9.2 EntityResolver

Resolution stages:

1. exact canonical ID,
2. exact normalized label,
3. alias lookup,
4. deterministic `difflib.SequenceMatcher` fuzzy ranking above threshold,
5. optional VectorProvider advisory candidates.

Output `EntityResolution`:

- input term,
- canonical ID or None,
- canonical type or None,
- method,
- score,
- alternatives.

Ambiguous fuzzy matches within 0.02 score are returned as ambiguous unless one is an exact/alias match.

## 10. Natural-language compiler

Create `ctd.compiler.NaturalLanguageCompiler`.

It is a deterministic controlled-language compiler, not a general LLM.

### 10.1 Supported input patterns

Core grammar supports:

- `find <entity-type>` / `find a <entity-type>`
- relation clauses: `<relation> <known-entity>`
- attribute clauses: `<attribute> <op> <value>`
- conjunctions `and`, commas, `with`, `that`, `which`
- comparison phrases: `under`, `below`, `at most`, `<=`, `over`, `above`, `at least`, `>=`, `equals`, `is`
- source requirements: `supported by <source-class>`
- evidence requirement: `evidence >= <0..1>`
- `top <n>`

Example:

`Find a supplier that produces component X, certification is ISO9001, lead time at most 30 days, and price under 500; top 3.`

### 10.2 Compiler behavior

- determine target type via ontology alias,
- create target variable,
- resolve relation object entities through EntityResolver,
- convert relation phrases into relation constraints,
- convert known attributes into attribute constraints,
- produce deterministic constraint IDs from sequence,
- reject unknown operators/schema terms as diagnostics,
- return `CompileResult` with QCG, confidence, diagnostics, unresolved terms, and extracted spans.

Compiler confidence is descriptive only and does not relax execution policy.

## 11. Learned advisory planner

### 11.1 Advisor contract

Create `PlannerAdvisor` protocol:

- `advise(query_class, operation_ids) -> PlannerAdvice`
- `observe(query_class, plan, result) -> None`

`PlannerAdvice` contains:

- operation preference order,
- provider preference map,
- per-operation advisory scores,
- model version.

### 11.2 AdaptivePlannerAdvisor

Deterministic online reward model stored in `AdvisorRepository`.

For each operation:

- successes,
- failures,
- cumulative reward,
- mean reward.

Reward after run:

- resolved: +1.0,
- partial: +0.25,
- contradicted: -0.25,
- provider error/budget exhausted/unresolvable: -0.5.

Operation order from the successful plan receives a discounted positional reward.

### 11.3 Safety envelope

Create `AdvisedQueryPlanner` wrapping `QueryPlanner`.

Rules:

- hard operations always remain before soft operations,
- base priority score remains authoritative,
- advisor can reorder only operations in the same hard/soft class whose base scores differ by at most configurable `advisory_priority_window` (default 0.05),
- advisor cannot alter required bindings, constraint semantics, or RuntimePolicy,
- plan records the advice snapshot and any rejected advisory movement.

## 12. Async subproblem execution

Create `ctd.async_exec.QueryDecomposer`.

It builds a variable-dependency graph from relation constraints and returns connected QCG components. Attribute-only constraints remain attached to their variable component.

Create `AsyncSubproblemExecutor`:

- one component -> normal resolver path,
- multiple independent components -> resolve concurrently with `asyncio.to_thread`,
- split global budgets deterministically by component weight (hard constraint count + 1), never increasing totals,
- merge only when variable sets are disjoint,
- union bindings/evidence/constraints,
- terminal merge precedence: PROVIDER_ERROR > CONTRADICTED > BUDGET_EXHAUSTED > UNRESOLVABLE > PARTIAL > RESOLVED,
- aggregate telemetry and retain per-component result summaries,
- deterministic component ordering by sorted variable names.

## 13. Tracing and observability

### 13.1 Trace model

Create:

- `TraceSpan(trace_id, span_id, parent_span_id, name, started_at, ended_at, attributes, status)`
- `TraceSink` protocol.

Implement:

- `InMemoryTraceSink`,
- `JsonLineTraceSink` with one JSON object per completed span and thread lock,
- `OpenTelemetryTraceSink` lazy importing OTel APIs and mapping spans/attributes.

### 13.2 Instrumented execution

v3 service emits spans:

- `ctd.request`
- `ctd.authenticate`
- `ctd.compile`
- `ctd.plan`
- `ctd.resolve`
- `ctd.resolve.component`
- `ctd.persistence`

Resolver telemetry remains the detailed algorithmic trace. The span trace provides service-level correlation.

## 14. Production service

Create `ctd.service.ProductionEngine` owning:

- settings,
- ontology,
- entity resolver,
- compiler,
- provider,
- resolver factory,
- repositories/store,
- profile manager,
- planner advisor,
- trace sink,
- auth enforcer.

Methods:

- `compile(text, as_of, principal) -> CompileResult`
- `plan(query, policy, principal) -> ExecutionPlan`
- `resolve(query|text, policy, principal) -> ResolutionResult`
- `resolve_async(query|text, policy, principal) -> ResolutionResult`
- `execution(id) -> dict | None`
- `verify_replay(id) -> dict`
- `metrics() -> dict`
- `readiness() -> dict`

Every resolve path persists execution and updates profile/advisor after completion.

## 15. v3 HTTP API

Add without removing existing routes.

### Public operational endpoints

- `GET /health/live`
- `GET /health/ready`
- `GET /v3/capabilities`

### Authenticated v3 endpoints when auth is enabled

- `POST /v3/compile`
- `POST /v3/plan`
- `POST /v3/resolve`
- `POST /v3/resolve/async`
- `GET /v3/executions/{execution_id}`
- `POST /v3/executions/{execution_id}/verify-replay`
- `GET /v3/metrics`
- `GET /v3/ontology`
- `GET /v3/providers/health`

`V3ResolveRequest` accepts exactly one of:

- `query: QueryConstraintGraph`, or
- `text: str` plus `as_of`.

It also accepts optional `RuntimePolicy`.

The effective policy returned in execution records is principal-constrained.

## 16. Production metrics

In addition to v2 metrics:

- compile success/ambiguity counts,
- auth success/failure counts by method (never credential values),
- executions by tenant,
- provider health and errors,
- provider p50/p95 latency where sample count exists,
- async component counts,
- advisor observations and accepted/rejected recommendations,
- persistent store health,
- replay verification success rate,
- terminal states/failure reasons.

## 17. Error handling

HTTP mappings:

- invalid request/schema: 422,
- authentication missing/invalid: 401,
- authenticated but missing scope: 403,
- execution missing: 404,
- configured production dependency unavailable at startup/readiness: readiness 503, not silent fallback,
- provider failures during resolution remain structured `PROVIDER_ERROR` results where possible.

No stack trace or secret is returned to API clients.

## 18. Testing strategy

All production code follows red-green-refactor.

Required test groups:

1. configuration validation/env parsing,
2. API-key and JWT verification including tampering/expiry/alg substitution,
3. role/tenant/security Focus intersection,
4. SQLite durability across store instances and transaction rollback,
5. PostgreSQL parameterized query contract using fake DB-API connection,
6. SQLProvider tenant filtering,
7. DocumentProvider JSONL loading,
8. VectorProvider cosine ranking/ties/zero vectors,
9. RemoteAPIProvider timeout/size/error/validation behavior with injected opener,
10. Neo4j adapter parameterization with fake driver,
11. federation deterministic de-duplication/cost/error policy,
12. ontology aliases/type hierarchy/entity ambiguity,
13. natural-language compiler supplier scenario and unknown-term diagnostics,
14. advisor learning and safety envelope,
15. query decomposition and deterministic async merge,
16. JSONL tracing and optional OTel adapter failure mode,
17. production service persistence/replay/advisor/profile update,
18. v3 API authentication/compile/resolve/async/readiness,
19. backward-compatible v1/v2 regression suite,
20. full package compile and CLI/API smoke tests.

## 19. Acceptance criteria

The production tier is complete when:

- all pre-existing 57 tests remain green,
- all new production tests pass,
- package compiles with `python -m compileall`,
- editable install succeeds without optional extras,
- SQLite execution persists and can be replayed from a new service instance,
- natural-language supplier request compiles and resolves end to end,
- tenant isolation prevents cross-tenant candidate visibility,
- API-key and JWT modes reject invalid credentials and accept valid principals,
- independent QCG components execute through async path and merge deterministically,
- advisor learns from observations but cannot move soft constraints ahead of hard constraints,
- optional PostgreSQL/Neo4j/OTel adapters fail with explicit dependency/configuration errors rather than silent fallback,
- v3 readiness reports provider/store status,
- v1/v2 routes still pass their regression tests,
- committed worktree is clean,
- source archive passes ZIP integrity validation.

## 20. Deferred beyond this tier

The following remain explicit future work, not hidden gaps in v3:

- distributed cluster scheduler and remote worker protocol,
- Kafka/Pulsar stream ingestion,
- full OAuth2/OIDC/JWKS discovery and key rotation,
- approximate-nearest-neighbor vector indexes,
- learned neural natural-language compiler,
- automatic ontology induction,
- RL policy training,
- multi-region active-active consistency,
- Kubernetes manifests/Helm/Terraform.

The v3 interfaces are designed so these can be added without weakening the resolver kernel.
