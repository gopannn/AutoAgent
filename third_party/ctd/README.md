# Constrained Topological Intelligence Engine (CTD) Unified v5

CTD is a production-oriented constrained evidence-resolution engine. It is designed for problems where an answer must satisfy explicit structural constraints, authorization boundaries, evidence requirements, temporal validity, and bounded compute before the system is allowed to declare resolution.

CTD does not treat retrieval as truth. Candidate discovery can be graph-based, relational, document-based, vector-assisted, or remote. The deterministic resolver remains the decision kernel:

```text
input / natural language
        ↓
controlled compiler + ontology/entity resolution
        ↓
Query Constraint Graph (QCG)
        ↓
cost-based deterministic planner
        ↓
bounded adaptive search
        ↓
evidence fusion + truth maintenance
        ↓
contradiction challenge + validation reserve
        ↓
RESOLVED / PARTIAL / CONTRADICTED /
UNRESOLVABLE / BUDGET_EXHAUSTED / PROVIDER_ERROR
```

The production tier adds durable persistence, tenant-aware authorization, multi-provider adapters, deterministic federation, natural-language compilation, an advisory learned planner, asynchronous subproblem execution, tracing, replay verification, readiness/metrics, and additive authenticated v3/v4/v5 APIs while preserving v1-v4 contracts. Unified v5 retains the separate structural hypothesis plane and replaces its internal transfer implementation with the hardened canonical structural kernel.


## Unified v5 architecture

Unified v5 merges the production CTD platform with the hardened Intelligence Engine v5 structural core without creating a second truth engine. CTD remains authoritative for evidence-backed resolution, authorization, persistence, provider federation, replay and telemetry. The structural algorithms now have one canonical implementation under `ctd.kernel`; the installed `topo` package is a backward-compatible facade over that kernel.

```text
                           CTD Unified v5
                                 |
                 +---------------+---------------+
                 |                               |
                 v                               v
          Resolution / Truth Plane          Hypothesis Plane
          QCG + evidence resolver           strict schema gate
          cost planner/providers            bitmap CLOSE
          evidence fusion                    MAC retrieval
          truth maintenance                  beam FAC alignment
          contradiction challenge            guarded projection
                 |                            conflict linking
                 |                            convergence/premortem
                 +---------------+---------------+
                                 v
                         evidence acquisition
                                 |
                         truth-plane validation
```

The safety boundary is absolute: **structural hypotheses never directly produce `RESOLVED`**. Transfer output remains `HYPOTHESIS` until ordinary evidence is acquired and validated by the resolution plane.

### What v5 adds

- one canonical structural kernel instead of parallel CTD/prototype implementations;
- strict predicate arity, argument-kind and role-lattice validation;
- explicit contradiction, self-cause and causal-cycle encoding diagnostics;
- authorization-aware bitmap CLOSE with index/scan/semantic/model cost tiers;
- bounded, explicitly reported constraint relaxation;
- kernel `DataRequest` -> CTD `ResolutionGap` acquisition plans;
- beam FAC alignment with maximal-mapping filtering;
- guarded projection with visible refusal reasons;
- structural conflict linking and subsumption-aware ranking;
- held-out-tail intelligence evaluation, ablations, frequency/random controls and guard probes;
- full Python `topo` v5 compatibility over the canonical kernel;
- additive authenticated `/v5` HTTP surface.

### v5 endpoints

- `GET /v5/capabilities`
- `POST /v5/encoding/validate`
- `POST /v5/structural/cases`
- `GET /v5/structural/cases`
- `POST /v5/close`
- `POST /v5/resolve`
- `POST /v5/resolve/async`
- `POST /v5/transfer`
- `POST /v5/premortem`
- `GET /v5/evaluate`
- `GET /v5/executions/{execution_id}`
- `POST /v5/executions/{execution_id}/verify-replay`
- `GET /v5/metrics`
- `GET /v5/health`

Existing v1-v4 endpoints remain available.

### Intelligence-quality release gate

Software correctness and intelligence quality are checked separately. The retained v5 incident corpus runs the original held-out-tail protocol and controls through the installed compatibility facade. A release must keep full-guard schema violations at zero, beat the deterministic random control on MRR, retain frequency/random controls, and prove the guard probe can still reproduce invalid projections when both safety guards are deliberately disabled. These are mechanism tests on the included corpus, not claims of real-world predictive accuracy.

## Production guarantees and boundaries

CTD Unified v5 enforces these invariants in code and tests:

- Hard constraints cannot be relaxed by the learned advisor.
- Authorization and tenant filtering occur inside evidence providers before candidates reach the resolver.
- Replay snapshots from graph-backed providers contain only evidence visible through the effective authorization Focus.
- Runtime budgets are hard ceilings. Async decomposition partitions global budgets rather than duplicating them.
- A validation reserve prevents search from consuming the full compute allowance before the best candidate is challenged.
- Evidence from correlated copies is discounted by independence group.
- Simultaneously incompatible valid claims produce contradiction; temporally replaced claims produce supersession.
- Unsupported or insufficient evidence produces abstention rather than forced generation.
- Learned planner advice is advisory only. It cannot alter hard/soft classification, evidence policy, authorization, or caller ceilings.
- Credential material is never written to traces or execution records.
- Persistent execution IDs are immutable.
- PostgreSQL, Neo4j, and OpenTelemetry are optional and loaded only when configured.

This version is production-oriented infrastructure, not a claim of formally proven truth. Upstream data can still be wrong; CTD makes evidence, provenance, contradictions, uncertainty, and abstention explicit so those errors are inspectable rather than silently hidden.

## Requirements

- Python 3.13+
- Core install: FastAPI, Pydantic, Uvicorn
- Optional PostgreSQL: psycopg 3
- Optional Neo4j: Neo4j Python driver
- Optional OpenTelemetry: OpenTelemetry API + SDK

Install the core package:

```bash
python -m pip install -e . --no-build-isolation
```

For development:

```bash
python -m pip install -e '.[dev]' --no-build-isolation
```

Optional production integrations:

```bash
python -m pip install -e '.[postgres]' --no-build-isolation
python -m pip install -e '.[neo4j]' --no-build-isolation
python -m pip install -e '.[otel]' --no-build-isolation
# or all optional production integrations
python -m pip install -e '.[production]' --no-build-isolation
```

The core package remains runnable without any optional integration.

## Quick verification

```bash
pytest -q
python -m compileall -q src tests
ctd demo
```

The CLI demo must return a `RESOLVED` supplier result.

## Starting the API

For local/demo mode:

```bash
uvicorn ctd.api:app --host 127.0.0.1 --port 8000
```

The module-level ASGI app reads `CTD_*` environment variables through `EngineSettings.from_env()` at import/startup.

For a durable SQLite service:

```bash
export CTD_ENVIRONMENT=production
export CTD_PERSISTENCE_BACKEND=sqlite
export CTD_SQLITE_PATH=/var/lib/ctd/ctd.db
export CTD_AUTH_MODE=hmac_jwt
export CTD_JWT_HS256_SECRET='<load-from-secret-manager>'
export CTD_JWT_ISSUER='your-idp'
export CTD_JWT_AUDIENCE='ctd'
export CTD_TRACE_BACKEND=jsonl
export CTD_TRACE_JSONL_PATH=/var/log/ctd/traces.jsonl

uvicorn ctd.api:app --host 0.0.0.0 --port 8000
```

When a persistent backend is configured and no graph is explicitly supplied to `create_app`, v3 reads the persistent store rather than silently falling back to the supplier demo. Legacy `/demo/supplier` remains a separate deterministic demo endpoint.

## Environment configuration

All runtime environment variables are prefixed with `CTD_`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `CTD_ENVIRONMENT` | `development` | Environment label |
| `CTD_PERSISTENCE_BACKEND` | `memory` | `memory`, `sqlite`, or `postgres` |
| `CTD_SQLITE_PATH` | `:memory:` | SQLite database path |
| `CTD_POSTGRES_DSN` | unset | PostgreSQL DSN; required for postgres backend |
| `CTD_NEO4J_URI` | unset | Optional Neo4j evidence provider URI |
| `CTD_NEO4J_USERNAME` | unset | Neo4j username |
| `CTD_NEO4J_PASSWORD` | unset | Neo4j password |
| `CTD_AUTH_MODE` | `disabled` | `disabled`, `api_key`, or `hmac_jwt` |
| `CTD_API_KEY_HASHES` | `{}` | JSON map of key ID to SHA-256 secret hash |
| `CTD_JWT_HS256_SECRET` | unset | HS256 verification secret |
| `CTD_JWT_ISSUER` | unset | Optional required JWT issuer |
| `CTD_JWT_AUDIENCE` | unset | Optional required JWT audience |
| `CTD_TRACE_BACKEND` | `memory` | `memory`, `jsonl`, or `otel` |
| `CTD_TRACE_JSONL_PATH` | unset | Required for JSONL tracing |
| `CTD_OTEL_SERVICE_NAME` | `ctd-engine` | OpenTelemetry tracer service name |
| `CTD_REMOTE_API_TIMEOUT_S` | `2.0` | Default remote-provider timeout |
| `CTD_REMOTE_API_MAX_BYTES` | `2000000` | Remote response hard byte cap |
| `CTD_FEDERATION_PARALLELISM` | `4` | Maximum provider fan-out workers |
| `CTD_COMPILER_FUZZY_THRESHOLD` | `0.82` | Entity fuzzy-match threshold |
| `CTD_ADVISORY_PRIORITY_WINDOW` | `0.05` | Maximum deterministic-priority window the advisor may reorder |

`EngineSettings.safe_dict()` deliberately removes DSNs, passwords, JWT secrets, and API-key hashes before configuration is returned by the capabilities endpoint.

## Authentication and authorization

### Disabled mode

`CTD_AUTH_MODE=disabled` creates an internal anonymous admin principal for v3. This is intended for development/testing only.

### API-key mode

An API key is supplied as:

```text
key-id.secret
```

Only the SHA-256 hash of `secret` belongs in configuration. Example:

```bash
python - <<'PY'
import hashlib
print(hashlib.sha256(b'my-secret').hexdigest())
PY
```

Then:

```bash
export CTD_AUTH_MODE=api_key
export CTD_API_KEY_HASHES='{"analyst":"<hash>"}'
```

Application code supplies an `ApiKeyIdentityStore` mapping the key ID to a `Principal`. HTTP clients use:

```text
X-CTD-API-Key: analyst.my-secret
```

Because principal metadata is deliberately not inferred from a key ID, the stock
module-level `ctd.api:app` cannot invent API-key roles/scopes from environment
variables. For API-key deployments, expose a small application factory such as
`examples/api_key_app.py` and point Uvicorn at that module.

### HS256 JWT mode

Configure:

```bash
export CTD_AUTH_MODE=hmac_jwt
export CTD_JWT_HS256_SECRET='replace-with-secret-manager-value'
export CTD_JWT_ISSUER='your-idp'
export CTD_JWT_AUDIENCE='ctd'
```

JWT validation is strict HS256 and checks signature, `exp`, optional `nbf`, issuer, audience, and subject. Algorithm substitution is rejected.

Expected optional claims:

```json
{
  "sub": "analyst-123",
  "tenant_id": "acme",
  "roles": ["analyst"],
  "scopes": ["resolve:read", "resolve:advanced"],
  "security_labels": ["PUBLIC", "INTERNAL", "CONFIDENTIAL"]
}
```

The principal's tenant, labels, roles, and explicit scopes are intersected with the requested runtime Focus. A request can make its own Focus narrower, never broader than its authorization.

Default roles:

- `viewer`: `resolve:read`, labels `PUBLIC`/`INTERNAL`
- `analyst`: `resolve:read`, `resolve:advanced`, `intelligence:read`, labels through `CONFIDENTIAL`
- `admin`: wildcard scope/labels

## Natural-language compilation

The v3 compiler is deliberately controlled and deterministic. It is not an unrestricted language model. It maps supported language patterns through a registered ontology and entity resolver into a fully inspectable QCG.

Example input (`examples/supplier_query.txt`):

```text
Find a supplier that produces component X, certification is ISO9001, lead time at most 30 days, price under 500
```

The compiler resolves:

- `supplier` -> type `Supplier`
- `component X` -> entity `component:x`
- `produces` -> relation `PRODUCES`
- `ISO9001` -> entity `cert:y`
- `lead time` -> attribute `lead_time_days`
- `price` -> attribute `unit_price`

Ambiguous or unknown terms are returned in compiler diagnostics rather than guessed silently.

## Query Constraint Graph

A `QueryConstraintGraph` can be supplied directly when callers already have structured intent. It supports:

- typed variables,
- hard and soft relation constraints,
- hard and soft attribute constraints,
- comparison operators,
- temporal `as_of`,
- evidence strength/diversity requirements,
- source-class restrictions,
- exclusions,
- maximum inference depth,
- top-k candidate selection,
- query-class identity.

Natural-language input is therefore a convenience layer over the same trusted QCG execution model.

## Four-barrier runtime control

`RuntimePolicy` operationalizes the original CTD control concepts:

- **Drive**: expansion/provider/byte/candidate budgets plus protected validation reserve.
- **Urgency**: deadline and depth ceilings.
- **Arousal**: deterministic search width and progressive widening.
- **Focus**: admissible node/edge/source types, authorization scope, security labels, and tenant.

Representative policy fields include:

```text
max_expansions
max_depth
deadline_ms
max_provider_calls
max_bytes_read
max_candidates
validation_reserve_ratio
initial_arousal
max_arousal
widen_step
stagnation_before_widen
min_confidence
min_trust
min_evidence_strength
min_source_diversity
allowed_node_types
allowed_edge_types
allowed_source_classes
authorization_scope
allowed_security_labels
tenant_id
```

Tenant ID is principal-derived in authenticated production execution; callers do not get to elevate themselves into another tenant through policy input.

## Deterministic planner + learned advisory layer

The base planner orders operations using inspectable estimates of information gain, selectivity, reliability, and cost.

```text
priority = information_gain × selectivity × reliability / estimated_cost
```

`AdaptivePlannerAdvisor` records outcome statistics per query class and constraint operation. Its recommendations may reorder operations only inside a configured deterministic-priority window and never move soft constraints ahead of hard constraints.

This gives the engine operational learning without handing control of correctness or authorization to a learned policy.

## Evidence and truth maintenance

Evidence records can carry source identity/class, confidence, trust, observation time, validity interval, independence group, extraction method, direct/inferred status, and lineage.

Correlated evidence is discounted. Independent evidence families combine as:

```text
support = 1 - product(1 - effective_weight_i)
```

Claim states include:

```text
SUPPORTED
DISPUTED
SUPERSEDED
RETRACTED
UNKNOWN
INVALID
```

Temporal supersession is distinct from simultaneous contradiction.

## Providers

### In-memory graph

`GraphProvider` is the deterministic reference provider and enforces authorization, security labels, and tenant Focus before returning nodes/edges.

### SQL

`SQLProvider` reads the canonical CTD node/edge schema through a DB-API connection. It enforces tenant/security filters locally before results reach the resolver.

### Documents

`DocumentProvider` loads canonical node/edge records from JSON or JSONL and applies the same graph-provider Focus rules.

### Vectors

`VectorProvider` implements deterministic exact cosine ranking with dimension validation and stable tie breaking. It is primarily an entity-resolution aid; approximate ANN is intentionally deferred.

### Remote API

`RemoteAPIProvider` uses bounded GET calls with explicit timeout and maximum response size, validates all returned Pydantic models, and applies local authorization/tenant/security filtering before returning candidates.

### Neo4j

`Neo4jProvider` lazily imports the Neo4j driver, uses parameterized values, validates dynamic identifiers, and applies authorization/tenant/security filtering before returning candidates. If configured through `EngineSettings.neo4j_uri`, `ProductionEngine` builds it as the evidence provider when no explicit provider factory or graph is supplied.

### Federation

`FederatedProvider` combines providers in stable configured order, can execute sequentially or in a bounded thread pool, aggregates provider cost, de-duplicates IDs deterministically, records payload conflicts, and supports fail-fast or best-effort provider-error policy.

Production applications with custom provider compositions should inject a `provider_factory(RuntimePolicy) -> EvidenceProvider` into `ProductionEngine`. The factory receives the effective principal-constrained runtime policy and is responsible for passing its tenant/security Focus into each configured provider.

## Persistence

### SQLite

`SQLiteStore` is the durable reference store. It uses WAL mode for file-backed databases, parameterized SQL, explicit transactions, idempotent entity/profile/advisor UPSERTs, and immutable execution IDs.

It persists:

- nodes,
- edges,
- evidence,
- claims,
- executions,
- query profiles,
- advisor statistics,
- schema metadata.

A graph can be reconstructed with `load_graph()`.

### PostgreSQL

`PostgresStore` exposes the same logical repository contract as SQLite and uses JSONB/TIMESTAMPTZ plus parameterized `%s` values. Psycopg is imported lazily. The store supports the same node/edge/evidence/claim/execution/profile/advisor operations and graph reconstruction.

The core test suite validates SQL construction with injected DB-API fakes; a live external PostgreSQL instance is intentionally not required for local verification.

## Async subproblem execution

Disconnected QCG components can resolve concurrently. `QueryDecomposer` forms deterministic connected components and `AsyncSubproblemExecutor` partitions global expansion, provider-call, byte, and candidate budgets between them.

If a budget is too small to split safely, the engine runs one bounded whole-query execution rather than minting additional budget.

Component results merge deterministically, and terminal-state precedence prevents one failed component from being hidden by another successful component.

## Tracing

Trace backends:

- in-memory sink for tests/local inspection,
- thread-safe JSONL sink,
- lazy OpenTelemetry bridge.

Instrumented spans cover compilation, planning, resolution, request orchestration, and persistence. Trace attributes automatically redact credential-like keys including authorization, API keys, tokens, passwords, and secrets.

OpenTelemetry is an optional bridge. Exporter/provider configuration remains the deployment's responsibility; CTD does not silently install or configure a collector.

## Durable deterministic replay

Every replay-capable graph-backed production execution persists:

- input text (when used),
- compiled QCG,
- compiler diagnostics,
- effective principal-constrained policy,
- sanitized principal metadata,
- query class,
- profile prior,
- exact planner/advisor snapshot,
- provider manifest,
- authorization-filtered provider graph snapshot,
- snapshot SHA-256,
- result fingerprint,
- full result,
- trace ID.

Replay uses the stored QCG, policy, graph snapshot, profile prior, and exact stored execution plan. Non-deterministic timing fields are excluded from the result fingerprint.

Providers without a materializable replay snapshot explicitly return `replay_supported=false`; CTD does not claim deterministic replay where the evidence snapshot was not captured.

## v3 API

Public operational routes:

```text
GET /health/live
GET /health/ready
GET /v3/capabilities
```

Authenticated when auth is enabled:

```text
GET  /v3/ontology
POST /v3/compile
POST /v3/plan
POST /v3/resolve
POST /v3/resolve/async
GET  /v3/executions/{execution_id}
POST /v3/executions/{execution_id}/verify-replay
GET  /v3/providers/health
GET  /v3/metrics
```

`/v3/plan`, async resolution, replay verification, and metrics require `resolve:advanced`. Standard compile/resolve/execution inspection require `resolve:read`.

Example compile/resolve body:

```json
{
  "text": "Find a supplier that produces component X, certification is ISO9001, lead time at most 30 days, price under 500",
  "as_of": "2026-09-12T00:00:00Z"
}
```

Or provide a structured `query` instead of `text`/`as_of`. Exactly one input form is allowed.

Legacy `/resolve`, `/v2/*`, and `/demo/supplier` remain unchanged for compatibility. Production deployments should expose v3 only at the ingress layer if legacy routes are not required; v1/v2 are intentionally not retroactively forced behind v3 authentication in this release.

## Health and metrics

`/health/live` verifies process liveness.

`/health/ready` verifies persistent-store availability and provider health. Configured but unavailable providers return a non-ready response rather than silently falling back to unrelated evidence.

Production metrics include:

- compile success/ambiguity,
- authentication success/failure by configured method (never credential values),
- sync/async execution counts,
- executions by tenant,
- terminal states and failure reasons,
- advisor observations and accepted/rejected advice,
- async component counts,
- provider latency p50/p95 from execution traces,
- provider/store health,
- replay verification rate.

Tenant principals receive tenant-scoped execution aggregates; global auth/profile/advisor cardinalities are not exposed through tenant-scoped metrics.

## Failure semantics

Resolution failure is data, not an exception to be hidden.

Terminal states:

```text
RESOLVED
PARTIAL
CONTRADICTED
UNRESOLVABLE
BUDGET_EXHAUSTED
PROVIDER_ERROR
```

Structured failure causes include:

```text
MISSING_DATA
UNSATISFIABLE_CONSTRAINT
EVIDENCE_TOO_WEAK
CONTRADICTORY_EVIDENCE
AUTHORIZATION_EXCLUDED
DEADLINE_EXHAUSTED
EXPANSION_BUDGET_EXHAUSTED
DEPTH_BUDGET_EXHAUSTED
PROVIDER_CALL_BUDGET_EXHAUSTED
PROVIDER_UNAVAILABLE
SCHEMA_MISMATCH
SEARCH_SPACE_EXHAUSTED
VALIDATION_RESERVE_EXHAUSTED
```

HTTP request/auth/schema errors map separately to 401/403/404/422/503 as appropriate.

## Testing and release gates

The repository uses test-first development for production features. Release verification includes:

```bash
pytest -q
python -m compileall -q src tests
python -m pip install -e . --no-build-isolation
ctd demo
```

Functional production benchmarks assert invariants rather than machine-dependent latency numbers:

- supplier natural-language compilation remains bounded,
- irrelevant physical graph domains are not scanned,
- tenant isolation exposes zero cross-tenant candidates,
- federated duplicate sources do not duplicate candidate bindings,
- adversarial advisor statistics cannot change terminal correctness.

The release smoke test exercises liveness/readiness, natural-language compile, planning, sync resolution, async resolution, durable execution retrieval, replay verification, metrics, and provider health against a temporary SQLite-backed app.

## Architecture modules

```text
src/ctd/
  advisor.py          learned advisory policy + safety envelope
  api.py              v1/v2 compatibility and authenticated v3 API
  async_exec.py       QCG decomposition and bounded async merge
  auth.py             API key/JWT authentication + role Focus enforcement
  compiler.py         controlled natural-language -> QCG compiler
  config.py           environment-backed production settings
  controller.py       Drive/Urgency/Arousal/Focus runtime budgets
  evidence.py         evidence fusion and rejection diagnostics
  graph.py            sparse in-memory property graph
  inference.py        deterministic rule registry and lineage
  models.py           domain contracts and result/failure types
  ontology.py         ontology/entity definitions and resolution
  persistence.py      SQLite/PostgreSQL durable stores
  planner.py          deterministic cost/information planner
  profiles.py         historical query-class runtime profiles
  providers.py        provider protocol + graph/memory providers
  providers_ext.py    SQL/document/vector/remote/Neo4j/federation
  repositories.py     repository contracts + in-memory implementations
  resolver.py         trusted bounded resolution kernel
  service.py          ProductionEngine orchestration
  telemetry.py        resolver telemetry model
  tracing.py          memory/JSONL/OpenTelemetry tracing
```

## Known production-tier limits

These are explicit scope boundaries, not hidden TODOs:

- no distributed cluster scheduler or remote worker protocol,
- no Kafka/Pulsar ingestion pipeline,
- no full OAuth2/OIDC/JWKS discovery/key rotation,
- no approximate-nearest-neighbor vector index,
- no neural natural-language compiler,
- no automatic ontology induction,
- no reinforcement-learning policy training,
- no multi-region active-active consistency,
- no Kubernetes/Helm/Terraform artifacts.

The interfaces are designed so these can be added without weakening the deterministic resolver or provider-side authorization model.

## Design and implementation documents

- Production specification: `docs/superpowers/specs/2026-09-12-constrained-topological-engine-production-design.md`
- Production implementation plan: `docs/superpowers/plans/2026-09-12-constrained-topological-engine-production-tier.md`
- v2 specification: `docs/superpowers/specs/2026-09-12-constrained-topological-engine-v2-design.md`
- Unified v5 specification: `docs/superpowers/specs/2026-09-13-ctd-unified-intelligence-v5-design.md`
- Unified v5 implementation plan: `docs/superpowers/plans/2026-09-13-ctd-unified-intelligence-v5.md`


## V4 structural intelligence plane

CTD v4 separates truth resolution from hypothesis generation:

```text
Resolution Plane: evidence -> constraints -> validation -> RESOLVED/ABSTAIN
Hypothesis Plane: validated structural cases -> MAC/FAC alignment -> projected hypothesis -> CHECK -> evidence acquisition -> Resolution Plane
```

A projected analogy is never accepted as truth. `StructuralHypothesis.state` begins as `HYPOTHESIS`, and the engine exposes a verification procedure or resolution gap so the hypothesis can be tested with ordinary evidence.

Core v4 capabilities:

- encoding-quality gate (`UNTYPED`, `SHALLOW`, `OFF_VOCAB`, `ORPHAN`, `BARREN`, round-trip review);
- recursive typed structural relations and cases;
- bounded beam structural alignment with predicate-family compatibility;
- independent-domain convergence scoring;
- pre-mortem failure projection with mandatory concrete checks;
- explicit `SATISFIED / VIOLATED / UNKNOWN` attribute semantics;
- `ResolutionGap` acquisition plans on unresolved/violated constraints;
- provider-side hard-constraint prefiltering with local re-verification;
- durable structural case and hypothesis-run repositories;
- tenant/security filtering before structural retrieval/alignment.

Authenticated v4 endpoints:

- `POST /v4/encoding/validate`
- `POST /v4/structural/cases`
- `GET /v4/structural/cases`
- `POST /v4/transfer`
- `POST /v4/premortem`
- `GET /v4/intelligence/metrics`
- `GET /v4/capabilities`

Run the standalone intelligence example:

```bash
python examples/v4_intelligence_demo.py
```

See `docs/INTELLIGENCE_ENGINE_INTEGRATION.md` for the mapping from the supplied prototype into the hardened v4 architecture.
