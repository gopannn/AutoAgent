from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, model_validator

from .auth import (
    ApiKeyIdentityStore,
    AuthenticationError,
    AuthorizationError,
    Principal,
    RolePolicy,
)
from .config import EngineSettings
from .controller import RuntimePolicy
from .encoding_quality import EncodingQualityPolicy
from .evaluation import IntelligenceEvaluationReport
from .examples import build_supplier_demo
from .graph import EvidenceGraph
from .models import Edge, Node, QueryConstraintGraph, ResolutionResult, ResolutionState
from .planner import ExecutionPlan, QueryPlanner
from .profiles import QueryProfileManager
from .premortem import PreMortemPolicy
from .providers import GraphProvider
from .repositories import InMemoryExecutionRepository, InMemoryQueryProfileRepository
from .resolver import Resolver
from .service import ProductionEngine
from .structural import StructuralCase, StructuralTarget
from .transfer import TransferPolicy
from .tracing import TraceSink
from .unified_close import UnifiedCloseRequest, UnifiedCloseResult


class ResolveRequest(BaseModel):
    query: QueryConstraintGraph
    policy: RuntimePolicy = Field(default_factory=RuntimePolicy)


class V3CompileRequest(BaseModel):
    text: str = Field(min_length=1)
    as_of: datetime
    query_class: str | None = None


class V3ResolveRequest(BaseModel):
    query: QueryConstraintGraph | None = None
    text: str | None = None
    as_of: datetime | None = None
    policy: RuntimePolicy = Field(default_factory=RuntimePolicy)

    @model_validator(mode="after")
    def validate_input(self) -> "V3ResolveRequest":
        if (self.query is None) == (self.text is None):
            raise ValueError("provide exactly one of query or text")
        if self.text is not None and self.as_of is None:
            raise ValueError("as_of is required for text queries")
        return self



class V4EncodingRequest(BaseModel):
    case: StructuralCase
    policy: EncodingQualityPolicy = Field(default_factory=EncodingQualityPolicy)


class V4StructuralCaseRequest(BaseModel):
    case: StructuralCase
    policy: EncodingQualityPolicy = Field(default_factory=EncodingQualityPolicy)


class V4TransferRequest(BaseModel):
    target: StructuralTarget
    policy: TransferPolicy = Field(default_factory=TransferPolicy)


class V4PremortemRequest(BaseModel):
    target: StructuralTarget
    policy: PreMortemPolicy = Field(default_factory=PreMortemPolicy)


class V5EncodingRequest(BaseModel):
    case: StructuralCase


class V5TransferRequest(BaseModel):
    target: StructuralTarget
    policy: TransferPolicy = Field(default_factory=TransferPolicy)


class V5PremortemRequest(BaseModel):
    target: StructuralTarget
    policy: PreMortemPolicy = Field(default_factory=PreMortemPolicy)

def _query_class(query: QueryConstraintGraph) -> str:
    if query.query_class:
        return query.query_class
    variable_types = "+".join(sorted(variable.node_type for variable in query.variables)) or "empty"
    return f"qcg:{variable_types}:{len(query.relations)}r:{len(query.attributes)}a"


def _snapshot_hash(snapshot: dict) -> str:
    payload = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()



def _resolution_fingerprint(result: ResolutionResult) -> str:
    payload = result.model_dump(mode="json")
    payload["execution_id"] = None
    telemetry = dict(payload.get("telemetry") or {})
    telemetry["elapsed_ms"] = 0.0
    telemetry["provider_latency_ms"] = 0.0
    normalized_events = []
    for event in telemetry.get("events", []):
        normalized = dict(event)
        normalized.pop("provider_latency_ms", None)
        normalized_events.append(normalized)
    telemetry["events"] = normalized_events
    payload["telemetry"] = telemetry
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _graph_from_snapshot(snapshot: dict) -> EvidenceGraph:
    graph = EvidenceGraph()
    for raw_node in snapshot.get("nodes", []):
        graph.add_node(Node.model_validate(raw_node))
    for raw_edge in snapshot.get("edges", []):
        graph.add_edge(Edge.model_validate(raw_edge))
    return graph


def create_app(
    graph: EvidenceGraph | None = None,
    *,
    settings: EngineSettings | None = None,
    identity_store: ApiKeyIdentityStore | None = None,
    role_policy: RolePolicy | None = None,
    store: Any | None = None,
    trace_sink: TraceSink | None = None,
) -> FastAPI:
    settings = settings or EngineSettings()
    app = FastAPI(
        title="Constrained Topological Engine",
        version="0.5.1",
        description=(
            "Production constrained evidence resolution with natural-language compilation, "
            "authorization, persistence, adaptive planning, indexed CLOSE, structural transfer, pre-mortem intelligence, evaluation and deterministic replay."
        ),
    )
    supplied_graph = graph
    if graph is None:
        graph, _, _ = build_supplier_demo()
    app.state.graph = graph
    app.state.executions = InMemoryExecutionRepository()
    app.state.profile_repository = InMemoryQueryProfileRepository()
    app.state.profiles = QueryProfileManager(app.state.profile_repository)
    production_graph = supplied_graph
    if production_graph is None and settings.persistence_backend == "memory" and settings.neo4j_uri is None:
        production_graph = graph
    app.state.production = ProductionEngine(
        settings=settings,
        graph=production_graph,
        store=store,
        trace_sink=trace_sink,
        identity_store=identity_store,
        role_policy=role_policy,
    )

    def authenticate_v3(request: Request) -> Principal:
        engine: ProductionEngine = app.state.production
        try:
            if settings.auth_mode == "disabled":
                principal = engine.authenticator.authenticate(None)
            elif settings.auth_mode == "api_key":
                credential = request.headers.get("X-CTD-API-Key")
                principal = engine.authenticator.authenticate(credential)
            else:
                authorization = request.headers.get("Authorization", "")
                if not authorization.startswith("Bearer "):
                    raise AuthenticationError("bearer token required")
                principal = engine.authenticator.authenticate(authorization[7:].strip())
            engine.observe_auth(settings.auth_mode, success=True)
            return principal
        except AuthenticationError as exc:
            engine.observe_auth(settings.auth_mode, success=False)
            # Never return credential validation details to the caller.
            raise HTTPException(
                status_code=401,
                detail="authentication failed",
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc

    def require_scope(principal: Principal, scope: str) -> None:
        try:
            app.state.production.enforcer.require_scope(principal, scope)
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail="insufficient scope") from exc

    def query_from_v3_request(
        request: V3ResolveRequest,
        principal: Principal,
    ) -> QueryConstraintGraph:
        if request.query is not None:
            return request.query
        assert request.text is not None and request.as_of is not None
        compiled = app.state.production.compile(
            request.text,
            as_of=request.as_of,
            principal=principal,
        )
        if compiled.query is None:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "query could not be compiled",
                    "diagnostics": [item.model_dump(mode="json") for item in compiled.diagnostics],
                    "unresolved_terms": compiled.unresolved_terms,
                },
            )
        return compiled.query

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/live")
    def health_live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    def health_ready() -> dict:
        readiness = app.state.production.readiness()
        if readiness["ready"]:
            return readiness
        return JSONResponse(status_code=503, content=readiness)

    @app.get("/graph")
    def graph_snapshot() -> dict:
        return app.state.graph.snapshot()

    @app.post("/resolve", response_model=ResolutionResult)
    def resolve(request: ResolveRequest) -> ResolutionResult:
        return Resolver(app.state.graph).resolve(request.query, request.policy)

    @app.post("/demo/supplier", response_model=ResolutionResult)
    def supplier_demo() -> ResolutionResult:
        demo_graph, query, policy = build_supplier_demo()
        return Resolver(demo_graph).resolve(query, policy)

    @app.get("/v2/capabilities")
    def v2_capabilities() -> dict:
        return {
            "version": "0.2.0",
            "features": [
                "cost_based_planning",
                "best_first_resolution",
                "evidence_fusion",
                "correlation_discounting",
                "temporal_truth_maintenance",
                "authorization_focus",
                "validation_budget_reserve",
                "runtime_profiles",
                "deterministic_replay",
            ],
            "terminal_states": [state.value for state in ResolutionState],
        }

    @app.post("/v2/plan", response_model=ExecutionPlan)
    def v2_plan(request: ResolveRequest) -> ExecutionPlan:
        provider = GraphProvider(
            app.state.graph,
            authorization_scope=request.policy.authorization_scope,
            allowed_security_labels=request.policy.allowed_security_labels,
        )
        query_class = _query_class(request.query)
        profile = app.state.profiles.get(query_class)
        return QueryPlanner().plan(request.query, provider, profile=profile)

    @app.post("/v2/resolve", response_model=ResolutionResult)
    def v2_resolve(request: ResolveRequest) -> ResolutionResult:
        query_class = _query_class(request.query)
        profile = app.state.profiles.get(query_class)
        result = Resolver(app.state.graph).resolve(
            request.query,
            request.policy,
            profile=profile,
        )
        execution_id = f"run:{uuid4().hex}"
        result.execution_id = execution_id
        snapshot = app.state.graph.snapshot()
        record = {
            "execution_id": execution_id,
            "query_class": query_class,
            "query": request.query.model_dump(mode="json"),
            "policy": request.policy.model_dump(mode="json"),
            "graph_snapshot": snapshot,
            "snapshot_hash": _snapshot_hash(snapshot),
            "profile_prior": profile,
            "result_fingerprint": _resolution_fingerprint(result),
            "result": result.model_dump(mode="json"),
        }
        app.state.executions.save(execution_id, record)
        app.state.profiles.observe(query_class, result)
        return result

    @app.get("/v2/executions/{execution_id:path}")
    def v2_execution(execution_id: str) -> dict:
        record = app.state.executions.get(execution_id)
        if record is None:
            raise HTTPException(status_code=404, detail="execution not found")
        return record

    @app.post("/v2/executions/{execution_id:path}/replay", response_model=ResolutionResult)
    def v2_replay(execution_id: str) -> ResolutionResult:
        record = app.state.executions.get(execution_id)
        if record is None:
            raise HTTPException(status_code=404, detail="execution not found")
        replay_graph = _graph_from_snapshot(record["graph_snapshot"])
        query = QueryConstraintGraph.model_validate(record["query"])
        policy = RuntimePolicy.model_validate(record["policy"])
        profile = record.get("profile_prior") or {}
        result = Resolver(replay_graph).resolve(query, policy, profile=profile)
        result.execution_id = execution_id
        return result

    @app.post("/v2/executions/{execution_id:path}/verify-replay")
    def v2_verify_replay(execution_id: str) -> dict:
        record = app.state.executions.get(execution_id)
        if record is None:
            raise HTTPException(status_code=404, detail="execution not found")
        replay_graph = _graph_from_snapshot(record["graph_snapshot"])
        query = QueryConstraintGraph.model_validate(record["query"])
        policy = RuntimePolicy.model_validate(record["policy"])
        result = Resolver(replay_graph).resolve(
            query, policy, profile=record.get("profile_prior") or {}
        )
        replay_fingerprint = _resolution_fingerprint(result)
        return {
            "match": replay_fingerprint == record["result_fingerprint"],
            "expected_fingerprint": record["result_fingerprint"],
            "replay_fingerprint": replay_fingerprint,
        }

    @app.get("/v2/profiles")
    def v2_profiles() -> dict:
        return {"profiles": app.state.profile_repository.list()}

    @app.get("/v2/metrics")
    def v2_metrics() -> dict:
        records = app.state.executions.list()
        elapsed = sorted(
            float((record.get("result", {}).get("telemetry", {}) or {}).get("elapsed_ms", 0.0))
            for record in records
        )

        def percentile(values: list[float], fraction: float) -> float:
            if not values:
                return 0.0
            index = max(0, min(len(values) - 1, int(round((len(values) - 1) * fraction))))
            return round(values[index], 6)

        def mean(values: list[float]) -> float:
            return round(sum(values) / len(values), 6) if values else 0.0

        states: dict[str, int] = {}
        provider_calls: dict[str, int] = {}
        provider_latency: dict[str, float] = {}
        budget_categories: dict[str, int] = {}
        total_evidence_accepted = 0
        total_evidence_rejected = 0
        contradictions_detected = 0
        contradictions_resolved = 0
        validation_reserve_entries = 0
        nodes_considered: list[float] = []
        edges_traversed: list[float] = []
        candidates_created: list[float] = []
        candidates_pruned: list[float] = []
        candidate_reduction: list[float] = []
        structural_coverage: list[float] = []
        evidence_coverage: list[float] = []
        source_diversity: list[float] = []
        inference_depth_penalty: list[float] = []
        max_inference_depth: list[float] = []

        for record in records:
            result = record.get("result", {})
            state = str(result.get("state", "UNKNOWN"))
            states[state] = states.get(state, 0) + 1
            telemetry = result.get("telemetry", {}) or {}
            uncertainty = result.get("uncertainty", {}) or {}

            considered = float(telemetry.get("nodes_considered", 0))
            created = float(telemetry.get("candidates_created", 0))
            nodes_considered.append(considered)
            edges_traversed.append(float(telemetry.get("edges_traversed", 0)))
            candidates_created.append(created)
            candidates_pruned.append(float(telemetry.get("branches_pruned", 0)))
            if considered > 0:
                candidate_reduction.append(max(0.0, 1.0 - min(1.0, created / considered)))

            structural_coverage.append(float(uncertainty.get("structural_coverage", 0.0)))
            evidence_coverage.append(float(uncertainty.get("evidence_coverage", 0.0)))
            source_diversity.append(float(uncertainty.get("source_diversity", 0.0)))
            inference_depth_penalty.append(float(uncertainty.get("inference_depth_penalty", 0.0)))
            max_inference_depth.append(float(telemetry.get("max_inference_depth_observed", 0.0)))

            total_evidence_accepted += int(telemetry.get("evidence_accepted", 0))
            total_evidence_rejected += int(telemetry.get("evidence_rejected", 0))
            contradictions_detected += int(telemetry.get("contradictions_detected", 0))
            contradictions_resolved += int(telemetry.get("contradictions_resolved", 0))
            validation_reserve_entries += int(bool(telemetry.get("validation_reserve_entered", False)))

            for key in (
                "deadline_exhausted",
                "expansion_budget_exhausted",
                "depth_budget_exhausted",
                "provider_call_budget_exhausted",
                "byte_budget_exhausted",
                "candidate_budget_exhausted",
            ):
                if telemetry.get(key):
                    budget_categories[key] = budget_categories.get(key, 0) + 1

            for event in telemetry.get("events", []):
                if event.get("event") == "provider_called":
                    name = str(event.get("provider", "unknown"))
                    provider_calls[name] = provider_calls.get(name, 0) + 1
                    provider_latency[name] = provider_latency.get(name, 0.0) + float(
                        event.get("provider_latency_ms", 0.0)
                    )

        resolved = states.get("RESOLVED", 0)
        executions = len(records)
        return {
            "executions": executions,
            "terminal_states": states,
            "successful_resolution_rate": round(resolved / executions, 6) if executions else 0.0,
            "abstention_rate": round((executions - resolved) / executions, 6) if executions else 0.0,
            "time_to_closure_ms": {
                "p50": percentile(elapsed, 0.50),
                "p95": percentile(elapsed, 0.95),
                "p99": percentile(elapsed, 0.99),
            },
            "search": {
                "mean_nodes_considered": mean(nodes_considered),
                "mean_edges_traversed": mean(edges_traversed),
                "mean_candidates_generated": mean(candidates_created),
                "mean_candidates_pruned": mean(candidates_pruned),
                "mean_candidate_reduction_ratio": mean(candidate_reduction),
            },
            "provider_calls": provider_calls,
            "provider_latency_ms": {
                name: round(value, 6) for name, value in sorted(provider_latency.items())
            },
            "evidence": {
                "accepted": total_evidence_accepted,
                "rejected": total_evidence_rejected,
            },
            "contradictions": {
                "detected": contradictions_detected,
                "resolved": contradictions_resolved,
            },
            "uncertainty": {
                "mean_structural_coverage": mean(structural_coverage),
                "mean_evidence_coverage": mean(evidence_coverage),
                "mean_source_diversity": mean(source_diversity),
                "mean_inference_depth_penalty": mean(inference_depth_penalty),
                "mean_max_inference_depth": mean(max_inference_depth),
            },
            "validation_reserve_entries": validation_reserve_entries,
            "budget_exhaustion_categories": budget_categories,
            "unsupported_assertion_rate": None,
            "unsupported_assertion_rate_note": "Requires labeled benchmark outcomes; not inferred from runtime success.",
        }

    @app.get("/v2/profiles/{query_class:path}")
    def v2_profile(query_class: str) -> dict:
        return app.state.profiles.get(query_class)

    # V3 is additive. Authentication/authorization applies only to the V3
    # operational surface so existing V1/V2 clients remain compatible.
    @app.get("/v3/capabilities")
    def v3_capabilities() -> dict:
        return {
            "version": "0.3.0",
            "features": [
                "natural_language_compilation",
                "ontology_entity_resolution",
                "tenant_authorization",
                "durable_repositories",
                "federated_providers",
                "adaptive_planner_advice",
                "bounded_async_resolution",
                "structured_tracing",
                "deterministic_replay",
            ],
            "terminal_states": [state.value for state in ResolutionState],
            "configuration": settings.safe_dict(),
        }

    @app.get("/v3/ontology")
    def v3_ontology(http_request: Request) -> dict:
        principal = authenticate_v3(http_request)
        require_scope(principal, "resolve:read")
        return app.state.production.ontology.snapshot()

    @app.post("/v3/compile")
    def v3_compile(payload: V3CompileRequest, http_request: Request) -> dict:
        principal = authenticate_v3(http_request)
        require_scope(principal, "resolve:read")
        result = app.state.production.compile(
            payload.text,
            as_of=payload.as_of,
            principal=principal,
            query_class=payload.query_class,
        )
        return result.model_dump(mode="json")

    @app.post("/v3/plan", response_model=ExecutionPlan)
    def v3_plan(payload: V3ResolveRequest, http_request: Request) -> ExecutionPlan:
        principal = authenticate_v3(http_request)
        require_scope(principal, "resolve:advanced")
        query = query_from_v3_request(payload, principal)
        return app.state.production.plan(
            query,
            policy=payload.policy,
            principal=principal,
        )

    @app.post("/v3/resolve", response_model=ResolutionResult)
    def v3_resolve(payload: V3ResolveRequest, http_request: Request) -> ResolutionResult:
        principal = authenticate_v3(http_request)
        require_scope(principal, "resolve:read")
        try:
            return app.state.production.resolve(
                query=payload.query,
                text=payload.text,
                as_of=payload.as_of,
                policy=payload.policy,
                principal=principal,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/v3/resolve/async", response_model=ResolutionResult)
    async def v3_resolve_async(
        payload: V3ResolveRequest,
        http_request: Request,
    ) -> ResolutionResult:
        principal = authenticate_v3(http_request)
        require_scope(principal, "resolve:advanced")
        try:
            return await app.state.production.resolve_async(
                query=payload.query,
                text=payload.text,
                as_of=payload.as_of,
                policy=payload.policy,
                principal=principal,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/v3/executions/{execution_id:path}")
    def v3_execution(execution_id: str, http_request: Request) -> dict:
        principal = authenticate_v3(http_request)
        require_scope(principal, "resolve:read")
        record = app.state.production.execution(execution_id)
        if record is None:
            raise HTTPException(status_code=404, detail="execution not found")
        # Tenant isolation also applies to persisted execution inspection.
        record_tenant = (record.get("principal") or {}).get("tenant_id")
        if principal.tenant_id is not None and record_tenant != principal.tenant_id:
            raise HTTPException(status_code=404, detail="execution not found")
        return record

    @app.post("/v3/executions/{execution_id:path}/verify-replay")
    def v3_verify_replay(execution_id: str, http_request: Request) -> dict:
        principal = authenticate_v3(http_request)
        require_scope(principal, "resolve:advanced")
        record = app.state.production.execution(execution_id)
        if record is None:
            raise HTTPException(status_code=404, detail="execution not found")
        record_tenant = (record.get("principal") or {}).get("tenant_id")
        if principal.tenant_id is not None and record_tenant != principal.tenant_id:
            raise HTTPException(status_code=404, detail="execution not found")
        try:
            return app.state.production.verify_replay(execution_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="execution not found") from exc

    @app.get("/v3/providers/health")
    def v3_provider_health(http_request: Request) -> dict:
        principal = authenticate_v3(http_request)
        require_scope(principal, "resolve:read")
        return {"providers": app.state.production.readiness()["providers"]}

    @app.get("/v3/metrics")
    def v3_metrics(http_request: Request) -> dict:
        principal = authenticate_v3(http_request)
        require_scope(principal, "resolve:advanced")
        return app.state.production.metrics(tenant_id=principal.tenant_id)

    @app.get("/v4/capabilities")
    def v4_capabilities() -> dict:
        return {
            "version": "0.4.0",
            "planes": ["resolution", "hypothesis"],
            "features": [
                "encoding_quality_gate",
                "three_valued_constraints",
                "resolution_gaps",
                "provider_prefiltering",
                "bounded_structural_transfer",
                "independent_domain_convergence",
                "premortem_checks",
                "durable_structural_library",
            ],
            "safety_invariant": "structural hypotheses cannot directly produce RESOLVED",
        }

    @app.post("/v4/encoding/validate")
    def v4_encoding_validate(payload: V4EncodingRequest, http_request: Request) -> dict:
        principal = authenticate_v3(http_request)
        require_scope(principal, "intelligence:read")
        try:
            return app.state.production.validate_structural_case(
                payload.case, policy=payload.policy, principal=principal
            ).model_dump(mode="json")
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail="insufficient scope") from exc

    @app.post("/v4/structural/cases", status_code=201)
    def v4_ingest_case(payload: V4StructuralCaseRequest, http_request: Request) -> dict:
        principal = authenticate_v3(http_request)
        require_scope(principal, "intelligence:write")
        try:
            validation = app.state.production.ingest_structural_case(
                payload.case, policy=payload.policy, principal=principal
            )
            return {"case_id": payload.case.id, "validation": validation.model_dump(mode="json")}
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail="insufficient scope") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/v4/structural/cases")
    def v4_list_cases(http_request: Request) -> dict:
        principal = authenticate_v3(http_request)
        require_scope(principal, "intelligence:read")
        return {
            "cases": [case.model_dump(mode="json") for case in app.state.production.list_structural_cases(principal=principal)]
        }

    @app.post("/v4/transfer")
    def v4_transfer(payload: V4TransferRequest, http_request: Request) -> dict:
        principal = authenticate_v3(http_request)
        require_scope(principal, "intelligence:read")
        try:
            return app.state.production.transfer(
                payload.target, policy=payload.policy, principal=principal
            ).model_dump(mode="json")
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail="insufficient scope") from exc

    @app.post("/v4/premortem")
    def v4_premortem(payload: V4PremortemRequest, http_request: Request) -> dict:
        principal = authenticate_v3(http_request)
        require_scope(principal, "intelligence:read")
        try:
            return app.state.production.premortem(
                payload.target, policy=payload.policy, principal=principal
            ).model_dump(mode="json")
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail="insufficient scope") from exc

    @app.get("/v4/intelligence/metrics")
    def v4_intelligence_metrics(http_request: Request) -> dict:
        principal = authenticate_v3(http_request)
        require_scope(principal, "intelligence:read")
        return app.state.production.intelligence_metrics(principal=principal)

    @app.get("/v5/capabilities")
    def v5_capabilities() -> dict:
        return {
            "version": "0.5.1",
            "planes": ["resolution", "hypothesis"],
            "features": [
                "canonical_structural_kernel",
                "strict_schema_gate",
                "authorization_aware_indexed_close",
                "three_valued_constraints",
                "resolution_gaps",
                "adaptive_evidence_resolution",
                "beam_structural_transfer",
                "guarded_projection",
                "conflict_linking",
                "premortem_checks",
                "held_out_evaluation",
                "ablation_controls",
                "guard_probes",
                "legacy_topo_compatibility",
                "deterministic_replay",
            ],
            "safety_invariant": "structural hypotheses cannot directly produce RESOLVED",
            "compatibility": ["v1", "v2", "v3", "v4", "topo-v5"],
        }

    @app.post("/v5/encoding/validate")
    def v5_encoding_validate(payload: V5EncodingRequest, http_request: Request) -> dict:
        principal = authenticate_v3(http_request)
        require_scope(principal, "intelligence:read")
        return app.state.production.validate_structural_case_v5(
            payload.case, principal=principal
        ).model_dump(mode="json")

    @app.post("/v5/structural/cases", status_code=201)
    def v5_ingest_case(payload: V5EncodingRequest, http_request: Request) -> dict:
        principal = authenticate_v3(http_request)
        require_scope(principal, "intelligence:write")
        try:
            validation = app.state.production.ingest_structural_case_v5(
                payload.case, principal=principal
            )
            return {"case_id": payload.case.id, "validation": validation.model_dump(mode="json")}
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail="insufficient scope") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/v5/structural/cases")
    def v5_list_cases(http_request: Request) -> dict:
        principal = authenticate_v3(http_request)
        require_scope(principal, "intelligence:read")
        return {
            "cases": [case.model_dump(mode="json") for case in app.state.production.list_structural_cases(principal=principal)]
        }

    @app.post("/v5/close", response_model=UnifiedCloseResult)
    def v5_close(payload: UnifiedCloseRequest, http_request: Request) -> UnifiedCloseResult:
        principal = authenticate_v3(http_request)
        require_scope(principal, "intelligence:read")
        return app.state.production.close_local(payload, principal=principal)

    @app.post("/v5/resolve", response_model=ResolutionResult)
    def v5_resolve(payload: V3ResolveRequest, http_request: Request) -> ResolutionResult:
        principal = authenticate_v3(http_request)
        require_scope(principal, "resolve:read")
        try:
            return app.state.production.resolve(
                query=payload.query, text=payload.text, as_of=payload.as_of,
                policy=payload.policy, principal=principal,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/v5/resolve/async", response_model=ResolutionResult)
    async def v5_resolve_async(payload: V3ResolveRequest, http_request: Request) -> ResolutionResult:
        principal = authenticate_v3(http_request)
        require_scope(principal, "resolve:advanced")
        try:
            return await app.state.production.resolve_async(
                query=payload.query, text=payload.text, as_of=payload.as_of,
                policy=payload.policy, principal=principal,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/v5/transfer")
    def v5_transfer(payload: V5TransferRequest, http_request: Request) -> dict:
        principal = authenticate_v3(http_request)
        require_scope(principal, "intelligence:read")
        return app.state.production.transfer_v5(
            payload.target, policy=payload.policy, principal=principal
        ).model_dump(mode="json")

    @app.post("/v5/premortem")
    def v5_premortem(payload: V5PremortemRequest, http_request: Request) -> dict:
        principal = authenticate_v3(http_request)
        require_scope(principal, "intelligence:read")
        return app.state.production.premortem_v5(
            payload.target, policy=payload.policy, principal=principal
        ).model_dump(mode="json")

    @app.get("/v5/evaluate", response_model=IntelligenceEvaluationReport)
    def v5_evaluate(http_request: Request) -> IntelligenceEvaluationReport:
        principal = authenticate_v3(http_request)
        require_scope(principal, "intelligence:read")
        return app.state.production.evaluate_intelligence_v5(principal=principal)

    @app.get("/v5/executions/{execution_id:path}")
    def v5_execution(execution_id: str, http_request: Request) -> dict:
        principal = authenticate_v3(http_request)
        require_scope(principal, "resolve:read")
        record = app.state.production.execution(execution_id)
        if record is None:
            raise HTTPException(status_code=404, detail="execution not found")
        record_tenant = (record.get("principal") or {}).get("tenant_id")
        if principal.tenant_id is not None and record_tenant != principal.tenant_id:
            raise HTTPException(status_code=404, detail="execution not found")
        return record

    @app.post("/v5/executions/{execution_id:path}/verify-replay")
    def v5_verify_replay(execution_id: str, http_request: Request) -> dict:
        principal = authenticate_v3(http_request)
        require_scope(principal, "resolve:advanced")
        record = app.state.production.execution(execution_id)
        if record is None:
            raise HTTPException(status_code=404, detail="execution not found")
        record_tenant = (record.get("principal") or {}).get("tenant_id")
        if principal.tenant_id is not None and record_tenant != principal.tenant_id:
            raise HTTPException(status_code=404, detail="execution not found")
        return app.state.production.verify_replay(execution_id)

    @app.get("/v5/metrics")
    def v5_metrics(http_request: Request) -> dict:
        principal = authenticate_v3(http_request)
        require_scope(principal, "resolve:advanced")
        metrics = app.state.production.metrics(tenant_id=principal.tenant_id)
        metrics["intelligence"] = app.state.production.intelligence_metrics(principal=principal)
        return metrics

    @app.get("/v5/health")
    def v5_health() -> dict:
        return app.state.production.readiness()

    @app.get("/", include_in_schema=False)
    def inspector() -> FileResponse:
        return FileResponse(Path(__file__).parent / "static" / "index.html")

    return app


app = create_app(settings=EngineSettings.from_env())
