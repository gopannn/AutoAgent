from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Callable
from uuid import uuid4

from .advisor import AdaptivePlannerAdvisor, AdvisedQueryPlanner
from .async_exec import AsyncSubproblemExecutor
from .auth import ApiKeyIdentityStore, Authenticator, AuthorizationError, PolicyEnforcer, Principal, RolePolicy
from .compiler import CompileResult, NaturalLanguageCompiler
from .config import EngineSettings
from .controller import RuntimePolicy
from .encoding_quality import EncodingQualityPolicy, EncodingValidation, validate_structural_case, validate_structural_case_strict
from .graph import EvidenceGraph
from .models import Edge, Node, QueryConstraintGraph, ResolutionResult
from .ontology import EntityResolver, OntologyRegistry
from .persistence import PostgresStore, SQLiteStore
from .planner import ExecutionPlan, QueryPlanner
from .profiles import QueryProfileManager
from .premortem import PreMortemEngine, PreMortemPolicy, PreMortemResult
from .providers import EvidenceProvider, GraphProvider
from .resolver import Resolver
from .structural import PredicateVocabulary, StructuralCase, StructuralTarget
from .structural_repository import HypothesisRunRecord
from .transfer import StructuralTransferEngine, TransferPolicy, TransferResult
from .unified_close import UnifiedCloseOperator, UnifiedCloseRequest, UnifiedCloseResult
from .evaluation import IntelligenceEvaluationReport, evaluate_structural_cases
from .tracing import (
    InMemoryTraceSink,
    JsonLineTraceSink,
    OpenTelemetryTraceSink,
    TraceSink,
    Tracer,
)


def _query_class(query: QueryConstraintGraph) -> str:
    if query.query_class:
        return query.query_class
    variable_types = "+".join(sorted(variable.node_type for variable in query.variables)) or "empty"
    return f"qcg:{variable_types}:{len(query.relations)}r:{len(query.attributes)}a"


def _snapshot_hash(snapshot: dict[str, Any]) -> str:
    payload = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _resolution_fingerprint(result: ResolutionResult) -> str:
    payload = result.model_dump(mode="json")
    payload["execution_id"] = None
    payload["trace_summary"] = {}
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
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _graph_from_snapshot(snapshot: dict[str, Any]) -> EvidenceGraph:
    graph = EvidenceGraph()
    for raw_node in snapshot.get("nodes", []):
        graph.add_node(Node.model_validate(raw_node))
    for raw_edge in snapshot.get("edges", []):
        graph.add_edge(Edge.model_validate(raw_edge))
    return graph


def build_default_ontology() -> OntologyRegistry:
    ontology = OntologyRegistry()
    ontology.register_type("Supplier", aliases={"supplier", "vendor"})
    ontology.register_type("Component", aliases={"component", "part"})
    ontology.register_type("Certification", aliases={"certification", "certificate"})
    ontology.register_relation(
        "PRODUCES",
        aliases={"produces", "makes"},
        subject_type="Supplier",
        object_type="Component",
    )
    ontology.register_relation(
        "CERTIFIED_WITH",
        aliases={"certification", "certified with", "certified"},
        subject_type="Supplier",
        object_type="Certification",
    )
    ontology.register_attribute(
        "Supplier", "lead_time_days", aliases={"lead time", "lead time days"}, value_type="int"
    )
    ontology.register_attribute(
        "Supplier", "unit_price", aliases={"price", "unit price"}, value_type="float"
    )
    ontology.register_attribute("Component", "code", aliases={"code"}, value_type="str")
    ontology.register_attribute("Certification", "code", aliases={"code"}, value_type="str")
    ontology.register_attribute("Certification", "status", aliases={"status"}, value_type="str")
    ontology.register_entity(
        "component:x",
        "Component",
        "Component X",
        aliases={"component x", "part x"},
        identifying_attributes={"code": "X"},
    )
    ontology.register_entity(
        "cert:y",
        "Certification",
        "ISO9001",
        aliases={"iso9001", "certification y", "certificate y"},
        identifying_attributes={"code": "Y"},
    )
    return ontology


class _FixedPlanner:
    def __init__(self, plan: ExecutionPlan) -> None:
        self._plan = plan

    def plan(self, query: QueryConstraintGraph, provider: EvidenceProvider, *, profile: dict | None = None) -> ExecutionPlan:
        return self._plan.model_copy(deep=True)


class ProductionEngine:
    def __init__(
        self,
        *,
        settings: EngineSettings | None = None,
        graph: EvidenceGraph | None = None,
        provider_factory: Callable[[RuntimePolicy], EvidenceProvider] | None = None,
        ontology: OntologyRegistry | None = None,
        store: Any | None = None,
        trace_sink: TraceSink | None = None,
        identity_store: ApiKeyIdentityStore | None = None,
        role_policy: RolePolicy | None = None,
    ) -> None:
        self.settings = settings or EngineSettings()
        self.graph = graph
        self._provider_factory = provider_factory
        self.store = store or self._build_store()
        self.ontology = ontology or build_default_ontology()
        self.entity_resolver = EntityResolver(
            self.ontology, fuzzy_threshold=self.settings.compiler_fuzzy_threshold
        )
        self.compiler = NaturalLanguageCompiler(self.ontology, entity_resolver=self.entity_resolver)
        self.authenticator = Authenticator(self.settings, identity_store)
        self.enforcer = PolicyEnforcer(role_policy or RolePolicy.default())
        self.profiles = QueryProfileManager(self.store.profiles)
        self.advisor = AdaptivePlannerAdvisor(self.store.advisor)
        self.structural_vocabulary = PredicateVocabulary.core()
        self.transfer_engine = StructuralTransferEngine(self.structural_vocabulary)
        self.premortem_engine = PreMortemEngine(self.structural_vocabulary)
        self.planner = AdvisedQueryPlanner(
            QueryPlanner(),
            self.advisor,
            advisory_priority_window=self.settings.advisory_priority_window,
        )
        self.trace_sink = trace_sink or self._build_trace_sink()
        self.tracer = Tracer(self.trace_sink)
        self._metrics = {
            "compile_success": 0,
            "compile_ambiguous": 0,
            "resolve_sync": 0,
            "resolve_async": 0,
            "replay_checks": 0,
            "replay_matches": 0,
            "encoding_accepted": 0,
            "encoding_rejected": 0,
            "transfer_runs": 0,
            "premortem_runs": 0,
            "hypotheses_projected": 0,
            "premortem_findings": 0,
        }
        self._auth_success_by_method: dict[str, int] = {}
        self._auth_failure_by_method: dict[str, int] = {}

    def _build_store(self) -> Any:
        if self.settings.persistence_backend in {"memory", "sqlite"}:
            path = ":memory:" if self.settings.persistence_backend == "memory" else self.settings.sqlite_path
            return SQLiteStore(path)
        return PostgresStore(self.settings.postgres_dsn or "")

    def _build_trace_sink(self) -> TraceSink:
        if self.settings.trace_backend == "memory":
            return InMemoryTraceSink()
        if self.settings.trace_backend == "jsonl":
            return JsonLineTraceSink(self.settings.trace_jsonl_path or "ctd-trace.jsonl")
        return OpenTelemetryTraceSink(service_name=self.settings.otel_service_name)

    def _principal(self, principal: Principal | None) -> Principal:
        return principal or self.authenticator.authenticate(None)

    def _provider(self, policy: RuntimePolicy) -> EvidenceProvider:
        if self._provider_factory is not None:
            return self._provider_factory(policy)
        if self.graph is not None:
            return GraphProvider(
                self.graph,
                authorization_scope=policy.authorization_scope,
                allowed_security_labels=policy.allowed_security_labels,
                tenant_id=policy.tenant_id,
            )
        if self.settings.neo4j_uri is not None:
            from .providers_ext import Neo4jProvider

            return Neo4jProvider(
                uri=self.settings.neo4j_uri,
                username=self.settings.neo4j_username,
                password=self.settings.neo4j_password,
                authorization_scope=policy.authorization_scope,
                allowed_security_labels=policy.allowed_security_labels,
                tenant_id=policy.tenant_id,
            )
        if hasattr(self.store, "load_graph"):
            return GraphProvider(
                self.store.load_graph(),
                authorization_scope=policy.authorization_scope,
                allowed_security_labels=policy.allowed_security_labels,
                tenant_id=policy.tenant_id,
            )
        raise RuntimeError("No evidence provider is configured")

    def compile(
        self,
        text: str,
        *,
        as_of: datetime,
        principal: Principal | None = None,
        query_class: str | None = None,
    ) -> CompileResult:
        resolved_principal = self._principal(principal)
        self.enforcer.require_scope(resolved_principal, "resolve:read")
        with self.tracer.span("ctd.compile", attributes={"tenant": resolved_principal.tenant_id or ""}):
            result = self.compiler.compile(text, as_of=as_of, query_class=query_class)
        if result.query is not None:
            self._metrics["compile_success"] += 1
        if result.unresolved_terms:
            self._metrics["compile_ambiguous"] += 1
        return result

    def _intelligence_policy(self, principal: Principal) -> RuntimePolicy:
        return self.enforcer.apply(principal, RuntimePolicy())

    def validate_structural_case(
        self,
        case: StructuralCase,
        *,
        policy: EncodingQualityPolicy | None = None,
        principal: Principal | None = None,
    ) -> EncodingValidation:
        resolved = self._principal(principal)
        self.enforcer.require_scope(resolved, "intelligence:read")
        return validate_structural_case(case, vocabulary=self.structural_vocabulary, policy=policy)

    def ingest_structural_case(
        self,
        case: StructuralCase,
        *,
        policy: EncodingQualityPolicy | None = None,
        principal: Principal | None = None,
    ) -> EncodingValidation:
        resolved = self._principal(principal)
        self.enforcer.require_scope(resolved, "intelligence:write")
        effective = self._intelligence_policy(resolved)
        if resolved.tenant_id is not None and case.tenant_id not in (None, resolved.tenant_id):
            raise AuthorizationError("structural case tenant mismatch")
        payload = case.model_copy(deep=True)
        if payload.tenant_id is None:
            payload.tenant_id = resolved.tenant_id
        if effective.allowed_security_labels is not None and payload.security_label is not None and payload.security_label not in effective.allowed_security_labels:
            raise AuthorizationError("structural case security label is not allowed")
        validation = validate_structural_case(payload, vocabulary=self.structural_vocabulary, policy=policy)
        if not validation.ok:
            self._metrics["encoding_rejected"] += 1
            raise ValueError("structural case failed encoding quality validation")
        self.store.structural_cases.save(payload)
        self._metrics["encoding_accepted"] += 1
        return validation

    def list_structural_cases(self, *, principal: Principal | None = None) -> list[StructuralCase]:
        resolved = self._principal(principal)
        self.enforcer.require_scope(resolved, "intelligence:read")
        effective = self._intelligence_policy(resolved)
        return self.store.structural_cases.list(
            tenant_id=resolved.tenant_id,
            allowed_security_labels=effective.allowed_security_labels,
        )

    def _authorize_target(self, target: StructuralTarget, principal: Principal) -> StructuralTarget:
        effective = self._intelligence_policy(principal)
        if principal.tenant_id is not None and target.tenant_id not in (None, principal.tenant_id):
            raise AuthorizationError("structural target tenant mismatch")
        payload = target.model_copy(deep=True)
        if payload.tenant_id is None:
            payload.tenant_id = principal.tenant_id
        if effective.allowed_security_labels is not None and payload.security_label is not None and payload.security_label not in effective.allowed_security_labels:
            raise AuthorizationError("structural target security label is not allowed")
        return payload

    def transfer(
        self,
        target: StructuralTarget,
        *,
        policy: TransferPolicy | None = None,
        principal: Principal | None = None,
    ) -> TransferResult:
        resolved = self._principal(principal)
        self.enforcer.require_scope(resolved, "intelligence:read")
        target = self._authorize_target(target, resolved)
        cases = self.list_structural_cases(principal=resolved)
        with self.tracer.span("ctd.transfer", attributes={"target": target.name, "tenant": resolved.tenant_id or ""}):
            result = self.transfer_engine.transfer(cases, target, policy)
        run = HypothesisRunRecord(
            run_id=f"hyp:{uuid4().hex}",
            tenant_id=resolved.tenant_id,
            security_label=target.security_label,
            mode="transfer",
            target_name=target.name,
            result=result.model_dump(mode="json"),
            created_at=datetime.now(tz=UTC),
        )
        self.store.hypothesis_runs.save(run)
        self._metrics["transfer_runs"] += 1
        self._metrics["hypotheses_projected"] += len(result.hypotheses)
        return result

    def premortem(
        self,
        target: StructuralTarget,
        *,
        policy: PreMortemPolicy | None = None,
        principal: Principal | None = None,
    ) -> PreMortemResult:
        resolved = self._principal(principal)
        self.enforcer.require_scope(resolved, "intelligence:read")
        target = self._authorize_target(target, resolved)
        cases = self.list_structural_cases(principal=resolved)
        with self.tracer.span("ctd.premortem", attributes={"target": target.name, "tenant": resolved.tenant_id or ""}):
            result = self.premortem_engine.analyze(cases, target, policy)
        run = HypothesisRunRecord(
            run_id=f"hyp:{uuid4().hex}",
            tenant_id=resolved.tenant_id,
            security_label=target.security_label,
            mode="premortem",
            target_name=target.name,
            result=result.model_dump(mode="json"),
            created_at=datetime.now(tz=UTC),
        )
        self.store.hypothesis_runs.save(run)
        self._metrics["premortem_runs"] += 1
        self._metrics["premortem_findings"] += len(result.findings)
        return result

    def validate_hypothesis(self, hypothesis: dict[str, Any]) -> dict[str, Any]:
        return {
            "state": "HYPOTHESIS",
            "relation": hypothesis.get("relation"),
            "verification_procedure": hypothesis.get("verification_procedure"),
            "next_action": "Acquire evidence for the projected relation and submit it through the normal CTD resolution plane.",
            "auto_resolved": False,
        }

    def intelligence_metrics(self, *, principal: Principal | None = None) -> dict[str, Any]:
        resolved = self._principal(principal)
        self.enforcer.require_scope(resolved, "intelligence:read")
        cases = self.list_structural_cases(principal=resolved)
        runs = self.store.hypothesis_runs.list(tenant_id=resolved.tenant_id)
        transfer_runs = [run for run in runs if run.mode == "transfer"]
        premortem_runs = [run for run in runs if run.mode == "premortem"]
        return {
            "encoding": {
                "accepted": self._metrics["encoding_accepted"],
                "rejected": self._metrics["encoding_rejected"],
                "library_cases": len(cases),
            },
            "transfer": {
                "runs": len(transfer_runs),
                "hypotheses_projected": sum(len((run.result or {}).get("hypotheses", [])) for run in transfer_runs),
            },
            "premortem": {
                "runs": len(premortem_runs),
                "findings": sum(len((run.result or {}).get("findings", [])) for run in premortem_runs),
            },
        }


    def validate_structural_case_v5(
        self,
        case: StructuralCase,
        *,
        principal: Principal | None = None,
    ) -> EncodingValidation:
        resolved = self._principal(principal)
        self.enforcer.require_scope(resolved, "intelligence:read")
        return validate_structural_case_strict(case)


    def ingest_structural_case_v5(
        self,
        case: StructuralCase,
        *,
        principal: Principal | None = None,
    ) -> EncodingValidation:
        resolved = self._principal(principal)
        self.enforcer.require_scope(resolved, "intelligence:write")
        effective = self._intelligence_policy(resolved)
        if resolved.tenant_id is not None and case.tenant_id not in (None, resolved.tenant_id):
            raise AuthorizationError("structural case tenant mismatch")
        payload = case.model_copy(deep=True)
        if payload.tenant_id is None:
            payload.tenant_id = resolved.tenant_id
        if (effective.allowed_security_labels is not None and payload.security_label is not None
                and payload.security_label not in effective.allowed_security_labels):
            raise AuthorizationError("structural case security label is not allowed")
        validation = validate_structural_case_strict(payload)
        if not validation.ok:
            self._metrics["encoding_rejected"] += 1
            raise ValueError("structural case failed strict schema validation")
        self.store.structural_cases.save(payload)
        self._metrics["encoding_accepted"] += 1
        return validation

    def close_local(
        self,
        request: UnifiedCloseRequest,
        *,
        principal: Principal | None = None,
    ) -> UnifiedCloseResult:
        resolved = self._principal(principal)
        self.enforcer.require_scope(resolved, "intelligence:read")
        effective = self.enforcer.apply(resolved, request.policy)
        scoped = request.model_copy(update={"policy": effective}, deep=True)
        with self.tracer.span("ctd.close", attributes={"tenant": resolved.tenant_id or ""}):
            return UnifiedCloseOperator().run(scoped)

    def transfer_v5(
        self,
        target: StructuralTarget,
        *,
        policy: TransferPolicy | None = None,
        principal: Principal | None = None,
    ) -> TransferResult:
        base = policy or TransferPolicy()
        strict = base.model_copy(update={"strict_schema": True, "guard_projection": True}, deep=True)
        return self.transfer(target, policy=strict, principal=principal)

    def premortem_v5(
        self,
        target: StructuralTarget,
        *,
        policy: PreMortemPolicy | None = None,
        principal: Principal | None = None,
    ) -> PreMortemResult:
        base = policy or PreMortemPolicy()
        strict_transfer = base.transfer.model_copy(update={"strict_schema": True, "guard_projection": True}, deep=True)
        strict_policy = base.model_copy(update={"transfer": strict_transfer}, deep=True)
        return self.premortem(target, policy=strict_policy, principal=principal)

    def evaluate_intelligence_v5(
        self,
        *,
        principal: Principal | None = None,
    ) -> IntelligenceEvaluationReport:
        resolved = self._principal(principal)
        self.enforcer.require_scope(resolved, "intelligence:read")
        cases = self.list_structural_cases(principal=resolved)
        with self.tracer.span("ctd.evaluate", attributes={"tenant": resolved.tenant_id or ""}):
            return evaluate_structural_cases(cases)

    def plan(
        self,
        query: QueryConstraintGraph,
        *,
        policy: RuntimePolicy | None = None,
        principal: Principal | None = None,
    ) -> ExecutionPlan:
        resolved_principal = self._principal(principal)
        self.enforcer.require_scope(resolved_principal, "resolve:read")
        effective = self.enforcer.apply(resolved_principal, policy or RuntimePolicy())
        query = self._with_query_class(query)
        query_class = query.query_class or _query_class(query)
        profile = self.profiles.get(query_class)
        provider = self._provider(effective)
        with self.tracer.span("ctd.plan", attributes={"query_class": query_class}):
            return self.planner.plan(
                query,
                provider,
                profile=profile,
                query_class=query_class,
            )

    def resolve(
        self,
        *,
        query: QueryConstraintGraph | None = None,
        text: str | None = None,
        as_of: datetime | None = None,
        policy: RuntimePolicy | None = None,
        principal: Principal | None = None,
    ) -> ResolutionResult:
        return self._resolve_sync(
            query=query,
            text=text,
            as_of=as_of,
            policy=policy,
            principal=principal,
            mode="sync",
        )

    def _prepare(
        self,
        *,
        query: QueryConstraintGraph | None,
        text: str | None,
        as_of: datetime | None,
        policy: RuntimePolicy | None,
        principal: Principal | None,
    ) -> tuple[Principal, QueryConstraintGraph, RuntimePolicy, CompileResult | None, str, dict[str, Any], ExecutionPlan, EvidenceProvider]:
        if (query is None) == (text is None):
            raise ValueError("provide exactly one of query or text")
        resolved_principal = self._principal(principal)
        self.enforcer.require_scope(resolved_principal, "resolve:read")
        compile_result: CompileResult | None = None
        if text is not None:
            if as_of is None:
                raise ValueError("as_of is required for text queries")
            compile_result = self.compile(text, as_of=as_of, principal=resolved_principal)
            if compile_result.query is None:
                raise ValueError("text query could not be compiled")
            query = compile_result.query
        assert query is not None
        query = self._with_query_class(query)
        query_class = query.query_class or _query_class(query)
        effective = self.enforcer.apply(resolved_principal, policy or RuntimePolicy())
        profile = self.profiles.get(query_class)
        provider = self._provider(effective)
        with self.tracer.span("ctd.plan", attributes={"query_class": query_class}):
            plan = self.planner.plan(
                query, provider, profile=profile, query_class=query_class
            )
        return resolved_principal, query, effective, compile_result, query_class, profile, plan, provider

    def _resolve_sync(
        self,
        *,
        query: QueryConstraintGraph | None,
        text: str | None,
        as_of: datetime | None,
        policy: RuntimePolicy | None,
        principal: Principal | None,
        mode: str,
    ) -> ResolutionResult:
        with self.tracer.span("ctd.request", attributes={"mode": mode}) as request_span:
            prepared = self._prepare(
                query=query, text=text, as_of=as_of, policy=policy, principal=principal
            )
            resolved_principal, prepared_query, effective, compile_result, query_class, profile, plan, provider = prepared
            with self.tracer.span("ctd.resolve", attributes={"query_class": query_class, "mode": mode}):
                result = Resolver(provider=provider, planner=self.planner).resolve(
                    prepared_query, effective, profile=profile
                )
            result.planner_summary = {
                "planner_version": plan.planner_version,
                "advisor_version": plan.advisor_version,
                "advice_applied": plan.advice_applied,
                "advice_rejected": plan.advice_rejected,
            }
            result.trace_summary = {"trace_id": request_span.trace_id, "mode": mode}
            self._persist_execution(
                result=result,
                query=prepared_query,
                policy=effective,
                principal=resolved_principal,
                compile_result=compile_result,
                query_class=query_class,
                profile=profile,
                plan=plan,
                input_text=text,
                mode=mode,
                trace_id=request_span.trace_id,
                provider=provider,
            )
            self.profiles.observe(query_class, result)
            self.advisor.observe(query_class, plan, result)
            self._metrics["resolve_sync"] += 1
            return result

    async def resolve_async(
        self,
        *,
        query: QueryConstraintGraph | None = None,
        text: str | None = None,
        as_of: datetime | None = None,
        policy: RuntimePolicy | None = None,
        principal: Principal | None = None,
    ) -> ResolutionResult:
        with self.tracer.span("ctd.request", attributes={"mode": "async"}) as request_span:
            prepared = self._prepare(
                query=query, text=text, as_of=as_of, policy=policy, principal=principal
            )
            resolved_principal, prepared_query, effective, compile_result, query_class, profile, plan, provider = prepared
            executor = AsyncSubproblemExecutor(
                lambda: Resolver(provider=provider, planner=self.planner)
            )
            with self.tracer.span("ctd.resolve", attributes={"query_class": query_class, "mode": "async"}):
                result = await executor.resolve(prepared_query, effective, profile=profile)
            result.planner_summary = {
                "planner_version": plan.planner_version,
                "advisor_version": plan.advisor_version,
                "advice_applied": plan.advice_applied,
                "advice_rejected": plan.advice_rejected,
            }
            result.trace_summary = {"trace_id": request_span.trace_id, "mode": "async"}
            self._persist_execution(
                result=result,
                query=prepared_query,
                policy=effective,
                principal=resolved_principal,
                compile_result=compile_result,
                query_class=query_class,
                profile=profile,
                plan=plan,
                input_text=text,
                mode="async",
                trace_id=request_span.trace_id,
                provider=provider,
            )
            self.profiles.observe(query_class, result)
            self.advisor.observe(query_class, plan, result)
            self._metrics["resolve_async"] += 1
            return result

    def _persist_execution(
        self,
        *,
        result: ResolutionResult,
        query: QueryConstraintGraph,
        policy: RuntimePolicy,
        principal: Principal,
        compile_result: CompileResult | None,
        query_class: str,
        profile: dict[str, Any],
        plan: ExecutionPlan,
        input_text: str | None,
        mode: str,
        trace_id: str,
        provider: EvidenceProvider,
    ) -> None:
        execution_id = f"run:{uuid4().hex}"
        result.execution_id = execution_id
        snapshot = self._provider_snapshot(provider)
        replay_supported = snapshot is not None
        record = {
            "execution_id": execution_id,
            "mode": mode,
            "input": {"text": input_text},
            "query_class": query_class,
            "query": query.model_dump(mode="json"),
            "compiler": compile_result.model_dump(mode="json") if compile_result else None,
            "effective_policy": policy.model_dump(mode="json"),
            "principal": {
                "subject": principal.subject,
                "tenant_id": principal.tenant_id,
                "roles": sorted(principal.roles),
                "scopes": sorted(principal.scopes),
                "security_labels": sorted(principal.security_labels),
                "auth_method": principal.auth_method,
            },
            "profile_prior": profile,
            "planner_advice": plan.model_dump(mode="json"),
            "provider_manifest": self._provider_manifest(provider),
            "graph_snapshot": snapshot,
            "snapshot_hash": _snapshot_hash(snapshot) if snapshot is not None else None,
            "replay_supported": replay_supported,
            "trace_id": trace_id,
            "result": result.model_dump(mode="json"),
            "result_fingerprint": _resolution_fingerprint(result),
        }
        with self.tracer.span("ctd.persistence", attributes={"execution_id": execution_id}):
            self.store.executions.save(execution_id, record)

    @staticmethod
    def _provider_snapshot(provider: EvidenceProvider) -> dict[str, Any] | None:
        snapshot_method = getattr(provider, "snapshot", None)
        if callable(snapshot_method):
            return snapshot_method()
        graph = getattr(provider, "graph", None)
        if graph is not None and hasattr(graph, "snapshot"):
            return graph.snapshot()
        return None

    @staticmethod
    def _provider_manifest(provider: EvidenceProvider) -> list[dict[str, Any]]:
        providers = getattr(provider, "providers", None) or [provider]
        manifest = []
        for item in providers:
            health_method = getattr(item, "health", None)
            if health_method:
                health = health_method()
                manifest.append(
                    {
                        "name": item.name,
                        "status": health.status,
                        "details": health.details,
                    }
                )
            else:
                manifest.append({"name": item.name, "status": "unknown"})
        return manifest

    def execution(self, execution_id: str) -> dict[str, Any] | None:
        return self.store.executions.get(execution_id)

    def observe_auth(self, method: str, *, success: bool) -> None:
        """Record authentication outcome without retaining credential material."""
        target = self._auth_success_by_method if success else self._auth_failure_by_method
        target[method] = target.get(method, 0) + 1

    def verify_replay(self, execution_id: str) -> dict[str, Any]:
        self._metrics["replay_checks"] += 1
        record = self.execution(execution_id)
        if record is None:
            raise KeyError(execution_id)
        if not record.get("replay_supported") or not record.get("graph_snapshot"):
            return {"supported": False, "match": False, "reason": "provider snapshot unavailable"}
        graph = _graph_from_snapshot(record["graph_snapshot"])
        query = QueryConstraintGraph.model_validate(record["query"])
        policy = RuntimePolicy.model_validate(record["effective_policy"])
        plan = ExecutionPlan.model_validate(record["planner_advice"])
        provider = GraphProvider(
            graph,
            authorization_scope=policy.authorization_scope,
            allowed_security_labels=policy.allowed_security_labels,
            tenant_id=policy.tenant_id,
        )
        replay = Resolver(provider=provider, planner=_FixedPlanner(plan)).resolve(
            query, policy, profile=record.get("profile_prior") or {}
        )
        replay.execution_id = execution_id
        replay.planner_summary = dict(record.get("result", {}).get("planner_summary") or {})
        replay.trace_summary = dict(record.get("result", {}).get("trace_summary") or {})
        fingerprint = _resolution_fingerprint(replay)
        match = fingerprint == record["result_fingerprint"]
        if match:
            self._metrics["replay_matches"] += 1
        return {
            "supported": True,
            "match": match,
            "expected_fingerprint": record["result_fingerprint"],
            "replay_fingerprint": fingerprint,
        }

    def readiness(self) -> dict[str, Any]:
        store_healthy = bool(self.store.ping())
        try:
            provider = self._provider(RuntimePolicy())
            provider_manifest = self._provider_manifest(provider)
        except Exception as exc:
            provider_manifest = [{"name": "configured", "status": "unavailable", "details": {"error": type(exc).__name__}}]
        providers_ready = all(item["status"] != "unavailable" for item in provider_manifest)
        return {
            "ready": store_healthy and providers_ready,
            "store": {"status": "healthy" if store_healthy else "unavailable", "backend": self.settings.persistence_backend},
            "providers": provider_manifest,
        }

    def metrics(self, *, tenant_id: str | None = None) -> dict[str, Any]:
        all_executions = self.store.executions.list()
        executions = (
            all_executions
            if tenant_id is None
            else [
                record
                for record in all_executions
                if (record.get("principal") or {}).get("tenant_id") == tenant_id
            ]
        )
        terminal_states: dict[str, int] = {}
        tenants: dict[str, int] = {}
        failure_reasons: dict[str, int] = {}
        provider_latency_samples: dict[str, list[float]] = {}
        async_component_counts: list[int] = []
        advice_applied = 0
        advice_rejected = 0

        def collect_provider_events(telemetry: dict[str, Any]) -> None:
            for event in telemetry.get("events", []) or []:
                if event.get("event") != "provider_called":
                    continue
                provider_name = str(event.get("provider", "unknown"))
                provider_latency_samples.setdefault(provider_name, []).append(
                    float(event.get("provider_latency_ms", 0.0) or 0.0)
                )
            for component in telemetry.get("components", []) or []:
                child = component.get("telemetry") if isinstance(component, dict) else None
                if isinstance(child, dict):
                    collect_provider_events(child)

        def percentile(values: list[float], fraction: float) -> float:
            if not values:
                return 0.0
            ordered = sorted(values)
            index = max(0, min(len(ordered) - 1, int(round((len(ordered) - 1) * fraction))))
            return round(ordered[index], 6)

        for record in executions:
            state = str((record.get("result") or {}).get("state", "UNKNOWN"))
            terminal_states[state] = terminal_states.get(state, 0) + 1
            tenant = str((record.get("principal") or {}).get("tenant_id") or "unscoped")
            tenants[tenant] = tenants.get(tenant, 0) + 1
            result = record.get("result") or {}
            for reason in result.get("failure_reasons", []) or []:
                key = str(reason)
                failure_reasons[key] = failure_reasons.get(key, 0) + 1
            telemetry = result.get("telemetry") or {}
            collect_provider_events(telemetry)
            if "async_components" in telemetry:
                async_component_counts.append(int(telemetry.get("async_components", 0) or 0))
            planner = record.get("planner_advice") or {}
            advice_applied += len(planner.get("advice_applied", []) or [])
            advice_rejected += len(planner.get("advice_rejected", []) or [])

        advisor_stats = self.store.advisor.list_stats() if tenant_id is None else []
        advisor_observations = sum(
            int(item.get("successes", 0)) + int(item.get("failures", 0))
            for item in advisor_stats
        )
        readiness = self.readiness()
        provider_latency = {
            name: {
                "samples": len(values),
                "p50": percentile(values, 0.50),
                "p95": percentile(values, 0.95),
            }
            for name, values in sorted(provider_latency_samples.items())
        }
        replay_checks = int(self._metrics["replay_checks"])
        return {
            **self._metrics,
            "scope": "global" if tenant_id is None else "tenant",
            "auth": {
                "success_by_method": (
                    dict(sorted(self._auth_success_by_method.items())) if tenant_id is None else {}
                ),
                "failure_by_method": (
                    dict(sorted(self._auth_failure_by_method.items())) if tenant_id is None else {}
                ),
            },
            "executions": len(executions),
            "terminal_states": terminal_states,
            "failure_reasons": failure_reasons,
            "executions_by_tenant": tenants,
            "profiles": len(self.store.profiles.list()) if tenant_id is None else None,
            "advisor_stats": len(advisor_stats),
            "advisor": {
                "observations": advisor_observations,
                "recommendations_applied": advice_applied,
                "recommendations_rejected": advice_rejected,
            },
            "async": {
                "executions_with_components": len(async_component_counts),
                "components_total": sum(async_component_counts),
                "components_max": max(async_component_counts, default=0),
            },
            "provider_latency_ms": provider_latency,
            "provider_errors": terminal_states.get("PROVIDER_ERROR", 0),
            "store_health": readiness["store"],
            "provider_health": readiness["providers"],
            "replay_verification_rate": (
                round(int(self._metrics["replay_matches"]) / replay_checks, 6)
                if replay_checks and tenant_id is None
                else 0.0
            ),
            "readiness": readiness,
        }

    def _with_query_class(self, query: QueryConstraintGraph) -> QueryConstraintGraph:
        if query.query_class:
            return query
        generated = _query_class(query)
        return query.model_copy(update={"query_class": generated})

    def close(self) -> None:
        close = getattr(self.store, "close", None)
        if close:
            close()
