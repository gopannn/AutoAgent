# CTD Unified Intelligence Engine v5 — Design Specification

## Purpose

Unify CTD v4 and Intelligence Engine v5 into one production system without losing any validated behavior. CTD remains the production shell and truth authority. The Intelligence Engine v5 algorithms become the single canonical structural kernel used for indexed CLOSE, schema validation, structural alignment, guarded projection, pre-mortem analysis, and intelligence evaluation.

## Non-regression contract

The release is invalid unless all original CTD v4 tests and all original Intelligence Engine v5 tests pass unchanged through the unified package. The compatibility package `topo` must remain import-compatible with the supplied v5 code. Existing CTD API versions v1-v4 remain additive and unchanged in behavior. New functionality is exposed through v5 routes.

## Safety invariants

1. Structural hypotheses can never directly create `RESOLVED` truth.
2. Authorization and tenant Focus apply before indexed CLOSE, structural retrieval, transfer, or pre-mortem reasoning.
3. `UNKNOWN` never implies `SATISFIED`.
4. Constraint relaxation is opt-in, bounded, named, and returns `PARTIAL` rather than `CLOSED`.
5. Strict-v5 structural ingestion rejects invalid arity, role/signature mismatches, explicit contradictions and self-causation before persistence.
6. Provider/evidence resolution remains the only truth promotion path.
7. Runtime bounds are enforced before expensive work, not reported only after overspend.
8. Compatibility code must be a facade over the canonical kernel, not a second algorithm implementation.

## Canonical architecture

```text
External data / APIs / workbook / SQL / graph / documents / vectors
                              |
                              v
                    CTD production shell
   auth -> tenancy -> ontology -> compiler -> tracing -> persistence
                              |
              +---------------+----------------+
              |                                |
              v                                v
       Resolution / Truth Plane         Hypothesis Plane
       QCG + evidence resolver          canonical structural kernel
              |                                |
       planner / providers                 schema gate
       evidence fusion                     bitmap CLOSE
       truth maintenance                    MAC retrieval
       validation reserve                   beam FAC alignment
              |                             guarded projection
              |                             conflict linking
              |                             convergence
              |                             pre-mortem
              |                                |
              +---------------+----------------+
                              v
                    Evidence acquisition
                              |
                       truth-plane validation
```

## Canonical structural kernel

The original Intelligence Engine v5 modules are moved under `ctd.kernel` and are the only implementation of:

- immutable `Rel`/`Record` representations;
- declared predicate/role schema;
- encoding validation;
- bitmap-indexed `TopoStore`;
- cost-tiered CLOSE;
- beam structural alignment;
- guarded projection;
- conflict linking and subsumption demotion;
- transfer and pre-mortem;
- held-out-tail evaluation, controls, ablations and guard probes.

The installed `topo` package is a compatibility facade that re-exports `ctd.kernel`; it contains no second copy of the algorithms.

## Public/Persistence model boundary

CTD Pydantic models remain the public, persistence and API contract. Conversion to compact kernel dataclasses happens only at the structural boundary. CTD role labels are normalized at the seam where required, while persisted data is not rewritten.

Legacy v4 transfer remains permissive to preserve historical semantics. v5 transfer enables strict schema validation and guarded projection.

## CLOSE integration

`UnifiedCloseOperator` adapts the kernel CLOSE engine to CTD controls. It:

- filters records by tenant, security label and authorization scope before indexing;
- converts JSON-serializable constraint specifications into index, scan and lexical kernel constraints;
- maps CTD `RuntimePolicy` deadline/provider/expansion bounds into kernel budgets;
- maps kernel `DataRequest` into CTD `ResolutionGap`;
- preserves explicit `CLOSED`, `PARTIAL`, `REQUEST` and `ABSTAIN` outcomes;
- never hides dropped constraints.

## Structural transfer integration

`StructuralTransferEngine` remains the CTD public API but delegates MAC/FAC alignment and projection to `ctd.kernel`. CTD-specific result semantics are preserved:

- mapping depth rather than projected-relation depth;
- source case count;
- v4 convergence semantics for legacy calls;
- strict structural-fingerprint-aware convergence for v5 calls;
- lineage converted to CTD Pydantic models;
- blocked projections and conflicts surfaced, not discarded.

## Strict ingestion and validation

v4 ingestion remains available. v5 ingestion adds the hardened schema gate:

- arity;
- argument kind;
- role lattice compatibility;
- unknown role diagnostics;
- shallow structure;
- off-vocabulary predicates;
- orphan relations;
- contradiction detection;
- self-causation;
- causal-cycle warnings;
- human round-trip gloss.

Invalid strict cases cannot enter the structural repository.

## Pre-mortem

v5 pre-mortem forces strict schema validation and guarded projection. Findings remain `HYPOTHESIS` and require a check procedure before production use. Severity is observed source metadata, never generated confidence.

## Intelligence evaluation

The v5 release includes the original held-out-tail evaluation harness in CI. Release gates require:

- the full arm emits zero schema-invalid predictions on the retained reference corpus;
- the full arm beats the deterministic random control on MRR;
- frequency and random controls are present;
- the guard probe emits no schema-invalid relation with both guards enabled;
- disabling both guards reproduces at least one invalid projection, proving the probe is sensitive.

These metrics validate the mechanism on the included corpus; they are not claims of real-world predictive accuracy.

## Persistence and authorization

Existing CTD SQLite/PostgreSQL repositories remain authoritative. Structural cases and hypothesis runs remain durable. Neo4j/SQL/document/vector/remote/federated providers remain unchanged. The local structural index is ephemeral and is rebuilt only from already-authorized records.

## API compatibility

Preserved:

- v1 legacy `/resolve` and demo surface;
- v2 planning/replay/profile surface;
- v3 authenticated production resolution;
- v4 structural-intelligence surface;
- Python `topo` v5 compatibility imports.

Added:

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
- `GET /v5/executions/{id}`
- `POST /v5/executions/{id}/verify-replay`
- `GET /v5/metrics`
- `GET /v5/health`

## Release acceptance

The final release must pass:

1. all CTD v4 regression tests;
2. all original Intelligence Engine v5 tests through `topo` facade;
3. all new unification tests;
4. corpus intelligence release gate;
5. `compileall`;
6. editable install and wheel build;
7. isolated wheel import for both `ctd` and `topo`;
8. CLI demo;
9. v5 API smoke including strict ingest, CLOSE, transfer and pre-mortem;
10. source archive integrity and per-file SHA-256 verification.
