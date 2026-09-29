# Constrained Topological Engine — MVP Design

## Purpose
Build a runnable reference implementation of the Constrained Topological Dynamics architecture as a controlled information-resolution engine. The system must assemble answers from typed graph evidence under explicit hard/soft constraints, bounded execution budgets, progressive widening, evidence validation, contradiction checks, and explicit abstention.

## MVP boundaries
The MVP is a single-process Python 3.13 service. It uses an in-memory property/evidence graph and deterministic controller; persistence, distributed sharding, learned control policies, LLM extraction, external databases, and authentication are extension points rather than MVP dependencies.

## Core state model
A query is compiled into a `QueryConstraintGraph` containing typed variables, relation constraints, attribute constraints, hard/soft flags, and an objective. The runtime controller exposes four operational parameters:

- Drive: maximum node/edge expansion budget.
- Urgency: deadline and maximum search depth.
- Arousal: progressive exploration width/radius.
- Focus: admissibility mask over node types, edge types, source trust, and time validity.

The evidence graph stores nodes and directed typed edges. Every edge carries provenance (`source_id`, `source_type`, timestamps), confidence, trust score, and optional validity interval. Contradictory edges may coexist and are surfaced during validation.

## Resolution pipeline
1. Validate and normalize the query constraint graph.
2. Initialize runtime budgets and the focus mask.
3. Generate candidates from typed anchor nodes.
4. Traverse only admissible edges within depth, expansion, and time budgets.
5. Evaluate hard and soft constraints against candidate bindings.
6. If progress stagnates and budget remains, progressively widen Arousal within configured limits.
7. Validate provenance, confidence/trust thresholds, temporal validity, and contradictions.
8. Return one of `RESOLVED`, `PARTIAL`, `CONTRADICTED`, or `UNRESOLVABLE` with coverage, evidence, unresolved constraints, contradictions, telemetry, and next actions.

## Correctness invariants
- A `RESOLVED` result has zero violated hard constraints.
- A `RESOLVED` result meets minimum provenance/trust/confidence policy.
- A contradictory required relation cannot silently resolve; it returns `CONTRADICTED` unless policy explicitly selects a temporally valid winner.
- Budget exhaustion never produces an unsupported assertion; it produces `PARTIAL` or `UNRESOLVABLE`.
- Search must stop when maximum expansions, depth, or deadline is reached.
- Every returned assertion includes evidence identifiers.

## Interfaces
### Python package
`ctd.models`: graph/query/result models.
`ctd.graph`: evidence graph interface and in-memory implementation.
`ctd.controller`: deterministic four-barrier runtime controller.
`ctd.resolver`: bounded constraint resolution engine.
`ctd.telemetry`: per-resolution metrics and event trace.
`ctd.examples`: supplier example dataset/query.

### HTTP API
- `GET /health`
- `GET /graph`
- `POST /resolve`
- `POST /demo/supplier`
- `GET /` inspection UI

### CLI
`python -m ctd.cli demo` executes the supplier scenario and emits JSON.

## Example acceptance scenario
The seed graph contains multiple suppliers for a component. The query requires a supplier that produces the component, has an unexpired required certification, lead time <= 30 days, and unit price <= 500. The engine must bind the eligible supplier, explain the evidence chain, and reject candidates violating hard constraints. A variant with conflicting current certification edges must return `CONTRADICTED`. A missing-evidence variant must return `PARTIAL` or `UNRESOLVABLE` rather than inventing a value.

## Telemetry
Each run records elapsed milliseconds, nodes considered, edges traversed, branches pruned, widening steps, constraints evaluated, satisfied hard/soft constraints, budget exhaustion flags, and final state.

## Security and production evolution
The MVP does not execute arbitrary user code or graph predicates. Constraint operators are an allowlist (`eq`, `ne`, `lt`, `lte`, `gt`, `gte`, `in`). Production evolution should add durable graph/storage adapters, policy-based authorization, signed provenance, temporal/versioned indexes, distributed query execution, external-source acquisition, observability export, and optionally contextual-bandit optimization of controller parameters after deterministic baselines are established.
