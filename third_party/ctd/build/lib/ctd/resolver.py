from __future__ import annotations

from dataclasses import dataclass, field
from heapq import heappop, heappush
from itertools import count
from time import perf_counter
from typing import Any, Iterable

from .controller import BarrierController, RuntimePolicy, RuntimeState
from .constraint_truth import ConstraintTruth, evaluate_attribute_constraint
from .graph import EvidenceGraph
from .models import (
    AttributeConstraint,
    Claim,
    ClaimStatus,
    Edge,
    FailureReason,
    Node,
    QueryConstraintGraph,
    RelationConstraint,
    ResolutionGap,
    ResolutionResult,
    ResolutionState,
    ResolutionUncertainty,
)
from .planner import ExecutionPlan, QueryPlanner
from .providers import EvidenceProvider, GraphProvider, ProviderResult
from .telemetry import ResolutionTelemetry
from .truth import EvidenceAssessment, EvidenceFusion, TruthMaintainer


@dataclass
class _Candidate:
    bindings: dict[str, str] = field(default_factory=dict)
    relation_edges: dict[str, Edge] = field(default_factory=dict)
    evidence_assessments: dict[str, EvidenceAssessment] = field(default_factory=dict)
    remaining_relations: tuple[str, ...] = ()
    depth: int = 0
    accumulated_cost: float = 0.0


@dataclass
class _Evaluation:
    result: ResolutionResult
    hard_satisfied: int
    hard_violated: int
    total_resolved: int
    soft_score: float
    evidence_strength: float
    source_diversity: int
    candidate: _Candidate

    @property
    def score(self) -> tuple[int, int, int, float, float, int, int]:
        # Lexicographic safety: a non-violating candidate always outranks one
        # that violates a hard rule, independent of soft/evidence score.
        return (
            1 if self.hard_violated == 0 else 0,
            self.hard_satisfied,
            self.total_resolved,
            round(self.evidence_strength, 12),
            round(self.soft_score, 12),
            self.source_diversity,
            len(self.result.bindings),
        )


class _ProviderBudgetError(RuntimeError):
    pass


class Resolver:
    """Adaptive bounded evidence resolver.

    v2 separates planning, provider access, evidence fusion, search and
    validation. The legacy ``Resolver(graph)`` constructor remains supported.
    """

    def __init__(
        self,
        graph: EvidenceGraph | None = None,
        controller: BarrierController | None = None,
        *,
        provider: EvidenceProvider | None = None,
        planner: QueryPlanner | None = None,
        fusion: EvidenceFusion | None = None,
        truth: TruthMaintainer | None = None,
    ) -> None:
        if graph is None and provider is None:
            raise ValueError("Resolver requires a graph or evidence provider")
        self.graph = graph
        self._provider = provider
        self.controller = controller or BarrierController()
        self.planner = planner or QueryPlanner()
        self.fusion = fusion or EvidenceFusion()
        self.truth = truth or TruthMaintainer()

    def resolve(
        self,
        query: QueryConstraintGraph,
        policy: RuntimePolicy | None = None,
        *,
        profile: dict[str, Any] | None = None,
    ) -> ResolutionResult:
        policy = policy or RuntimePolicy()
        telemetry = ResolutionTelemetry()
        runtime = self.controller.initialize(policy)
        provider = self._provider_for(policy)

        if not query.variables:
            return self._finalize_result(
                ResolutionResult(
                    state=ResolutionState.RESOLVED,
                    constraint_coverage=1.0,
                    uncertainty=ResolutionUncertainty(
                        structural_coverage=1.0,
                        evidence_coverage=1.0,
                        evidence_strength=1.0,
                        temporal_freshness=1.0,
                        resolution_confidence=1.0,
                    ),
                ),
                telemetry,
                runtime,
            )

        excluded_types = [
            variable.node_type
            for variable in query.variables
            if not self._node_type_allowed(variable.node_type, policy)
        ]
        if excluded_types:
            return self._terminal_without_search(
                query,
                telemetry,
                runtime,
                state=ResolutionState.UNRESOLVABLE,
                reason=FailureReason.AUTHORIZATION_EXCLUDED,
                action=f"Focus policy excludes required node type {excluded_types[0]}.",
            )

        try:
            plan = self.planner.plan(query, provider, profile=profile)
            telemetry.planner_operations = [operation.model_dump(mode="json") for operation in plan.operations]
            telemetry.event(
                "plan_generated",
                fingerprint=plan.query_fingerprint,
                operation_order=[operation.constraint_id for operation in plan.operations],
            )
            domains, node_cache, authorization_excluded = self._build_domains(
                query=query,
                policy=policy,
                provider=provider,
                telemetry=telemetry,
                runtime=runtime,
                plan=plan,
            )
        except _ProviderBudgetError:
            return self._budget_terminal(query, telemetry, runtime)
        except Exception as exc:  # provider adapters are external boundaries
            telemetry.event("provider_error", error=type(exc).__name__)
            return self._terminal_without_search(
                query,
                telemetry,
                runtime,
                state=ResolutionState.PROVIDER_ERROR,
                reason=FailureReason.PROVIDER_UNAVAILABLE,
                action="Restore the evidence provider or retry with another provider.",
            )

        if authorization_excluded:
            return self._terminal_without_search(
                query,
                telemetry,
                runtime,
                state=ResolutionState.UNRESOLVABLE,
                reason=FailureReason.AUTHORIZATION_EXCLUDED,
                action="Authorization Focus excludes all candidates for a required variable.",
            )

        empty_domain = next((name for name, nodes in domains.items() if not nodes), None)
        if empty_domain is not None:
            # If hard attribute filtering emptied a non-empty physical domain,
            # the problem is unsatisfiable. Otherwise required data is missing.
            var_type = next(variable.node_type for variable in query.variables if variable.name == empty_domain)
            physical_count = provider.node_count(var_type)
            reason = (
                FailureReason.UNSATISFIABLE_CONSTRAINT
                if physical_count > 0
                else FailureReason.MISSING_DATA
            )
            blocking = self._blocking_hard_attributes(query, empty_domain) if reason == FailureReason.UNSATISFIABLE_CONSTRAINT else []
            result = self._terminal_without_search(
                query,
                telemetry,
                runtime,
                state=ResolutionState.UNRESOLVABLE,
                reason=reason,
                action=f"No admissible candidate exists for variable {empty_domain} ({var_type}).",
            )
            result.violated_constraints = blocking
            result.unresolved_constraints = [item for item in result.unresolved_constraints if item not in blocking]
            return result

        relation_order = [
            operation.constraint_id for operation in plan.operations if operation.kind == "relation"
        ]
        evaluations: list[_Evaluation] = []
        seen_signatures: set[tuple[tuple[str, str], ...]] = set()

        while True:
            pass_evaluations = self._search_pass(
                query=query,
                policy=policy,
                provider=provider,
                telemetry=telemetry,
                runtime=runtime,
                domains=domains,
                node_cache=node_cache,
                relation_order=relation_order,
            )
            for evaluation in pass_evaluations:
                signature = tuple(sorted(evaluation.result.bindings.items()))
                if signature not in seen_signatures:
                    evaluations.append(evaluation)
                    seen_signatures.add(signature)

            best_pass = max(pass_evaluations, key=lambda item: item.score, default=None)
            if best_pass is not None:
                self.controller.record_progress(runtime, satisfied_constraints=best_pass.total_resolved)

            resolved_count = sum(
                1
                for evaluation in evaluations
                if evaluation.result.state == ResolutionState.RESOLVED
            )
            if resolved_count >= query.top_k:
                break
            if self._budget_exhausted(telemetry):
                break
            if runtime.arousal >= policy.max_arousal:
                break
            self.controller.widen(runtime, policy, telemetry)

        if not evaluations:
            if self._budget_exhausted(telemetry):
                return self._budget_terminal(query, telemetry, runtime)
            return self._terminal_without_search(
                query,
                telemetry,
                runtime,
                state=ResolutionState.UNRESOLVABLE,
                reason=FailureReason.SEARCH_SPACE_EXHAUSTED,
                action="No structurally admissible candidate was found.",
            )

        evaluations.sort(key=lambda item: item.score, reverse=True)
        validated: list[_Evaluation] = []
        try:
            for evaluation in evaluations[: max(query.top_k, 3)]:
                if not self.controller.can_validate(runtime, policy, telemetry):
                    break
                self.controller.record_validation(runtime, telemetry)
                validated.append(
                    self._validate_candidate(
                        evaluation=evaluation,
                        query=query,
                        policy=policy,
                        provider=provider,
                        telemetry=telemetry,
                        runtime=runtime,
                    )
                )
        except _ProviderBudgetError:
            # We still return the best structurally evaluated candidate, but the
            # explicit budget state prevents accidental acceptance as resolved.
            validated = []
        except Exception as exc:
            telemetry.event("provider_error", error=type(exc).__name__)
            result = evaluations[0].result
            result.state = ResolutionState.PROVIDER_ERROR
            result.failure_reasons = self._merge_reasons(
                result.failure_reasons, [FailureReason.PROVIDER_UNAVAILABLE]
            )
            return self._finalize_result(result, telemetry, runtime)

        ranked = validated or evaluations
        ranked.sort(key=lambda item: item.score, reverse=True)
        best = self._choose_validated_best(ranked)

        if self._budget_exhausted(telemetry) and best.result.state != ResolutionState.CONTRADICTED:
            best.result.state = ResolutionState.BUDGET_EXHAUSTED
            best.result.failure_reasons = self._merge_reasons(
                best.result.failure_reasons, self.controller.failure_reasons(telemetry)
            )

        best.result.candidate_rank = 1
        best.result.alternatives = [
            {
                "rank": index,
                "state": evaluation.result.state,
                "bindings": evaluation.result.bindings,
                "constraint_coverage": evaluation.result.constraint_coverage,
                "resolution_confidence": evaluation.result.uncertainty.resolution_confidence,
            }
            for index, evaluation in enumerate(ranked[1 : query.top_k], start=2)
        ]
        telemetry.hard_satisfied = best.hard_satisfied
        return self._finalize_result(best.result, telemetry, runtime)

    def _provider_for(self, policy: RuntimePolicy) -> EvidenceProvider:
        if self._provider is not None:
            return self._provider
        assert self.graph is not None
        return GraphProvider(
            self.graph,
            authorization_scope=policy.authorization_scope,
            allowed_security_labels=policy.allowed_security_labels,
            tenant_id=policy.tenant_id,
        )

    def _build_domains(
        self,
        *,
        query: QueryConstraintGraph,
        policy: RuntimePolicy,
        provider: EvidenceProvider,
        telemetry: ResolutionTelemetry,
        runtime: RuntimeState,
        plan: ExecutionPlan,
    ) -> tuple[dict[str, list[Node]], dict[str, Node], bool]:
        domains: dict[str, list[Node]] = {}
        cache: dict[str, Node] = {}
        physical_counts: dict[str, int] = {}
        hard_attributes = {
            variable.name: [
                constraint
                for constraint in query.attributes
                if constraint.variable == variable.name and constraint.hard
            ]
            for variable in query.variables
        }

        for variable in query.variables:
            physical_counts[variable.name] = (
                len(self.graph.nodes_of_type(variable.node_type))
                if self.graph is not None
                else provider.node_count(variable.node_type)
            )
            constraints_for_variable = hard_attributes[variable.name]
            if constraints_for_variable and hasattr(provider, "prefilter_nodes"):
                result = self._provider_call(
                    lambda v=variable, cs=constraints_for_variable: provider.prefilter_nodes(
                        v.node_type, cs, limit=policy.max_candidates
                    ),
                    runtime=runtime,
                    policy=policy,
                    telemetry=telemetry,
                )
                telemetry.prefilter_candidates_examined += result.cost.items_examined
                telemetry.prefilter_candidates_returned += len(result.items)
                telemetry.prefilter_candidates_pruned += max(0, result.cost.items_examined - len(result.items))
            else:
                result = self._provider_call(
                    lambda v=variable: provider.nodes_of_type(
                        v.node_type, limit=policy.max_candidates
                    ),
                    runtime=runtime,
                    policy=policy,
                    telemetry=telemetry,
                )
            nodes = result.items
            telemetry.nodes_considered += result.cost.items_examined
            for node in nodes:
                cache[node.id] = node
            filtered = [
                node
                for node in nodes
                if all(self._evaluate_attribute_on_node(constraint, node) is not False for constraint in hard_attributes[variable.name])
            ]
            domains[variable.name] = filtered
            telemetry.event(
                "domain_built",
                variable=variable.name,
                node_type=variable.node_type,
                candidates=len(filtered),
                examined=result.cost.items_examined,
            )

        authorization_excluded = bool(
            policy.authorization_scope is not None or policy.allowed_security_labels is not None
        ) and any(physical_counts[name] > 0 and not domains[name] for name in domains)
        return domains, cache, authorization_excluded

    def _search_pass(
        self,
        *,
        query: QueryConstraintGraph,
        policy: RuntimePolicy,
        provider: EvidenceProvider,
        telemetry: ResolutionTelemetry,
        runtime: RuntimeState,
        domains: dict[str, list[Node]],
        node_cache: dict[str, Node],
        relation_order: list[str],
    ) -> list[_Evaluation]:
        relations = {relation.id: relation for relation in query.relations}
        domain_ids = {name: {node.id for node in nodes} for name, nodes in domains.items()}
        root_name = min(domains, key=lambda name: (len(domains[name]), name))
        sequence = count()
        heap: list[tuple[tuple[Any, ...], int, _Candidate]] = []
        evaluations: list[_Evaluation] = []

        for node in domains[root_name][: runtime.arousal]:
            if not self.controller.can_create_candidate(runtime, policy, telemetry):
                break
            candidate = _Candidate(
                bindings={root_name: node.id},
                remaining_relations=tuple(relation_order),
            )
            self.controller.record_candidate(runtime, telemetry)
            heappush(heap, (self._heap_priority(candidate), next(sequence), candidate))

        while heap:
            _, _, candidate = heappop(heap)
            hard_violation = self._hard_attribute_violation(query, candidate.bindings, node_cache)
            if hard_violation:
                telemetry.branches_pruned += 1
                evaluations.append(
                    self._evaluate_candidate(
                        query=query,
                        candidate=candidate,
                        policy=policy,
                        telemetry=telemetry,
                        node_cache=node_cache,
                    )
                )
                continue

            if not candidate.remaining_relations:
                evaluations.append(
                    self._evaluate_candidate(
                        query=query,
                        candidate=candidate,
                        policy=policy,
                        telemetry=telemetry,
                        node_cache=node_cache,
                    )
                )
                continue

            relation_id = self._select_relation(candidate, relations)
            relation = relations[relation_id]
            if not self._edge_type_allowed(relation.relation, policy):
                telemetry.branches_pruned += 1
                evaluations.append(
                    self._evaluate_candidate(
                        query=query,
                        candidate=candidate,
                        policy=policy,
                        telemetry=telemetry,
                        node_cache=node_cache,
                    )
                )
                continue

            subject_id = candidate.bindings.get(relation.subject_var)
            object_id = candidate.bindings.get(relation.object_var)

            if subject_id is None and object_id is None:
                seeded = False
                for node in domains[relation.subject_var][: runtime.arousal]:
                    if not self.controller.can_create_candidate(runtime, policy, telemetry):
                        break
                    seeded = True
                    child = self._copy_candidate(candidate)
                    child.bindings[relation.subject_var] = node.id
                    self.controller.record_candidate(runtime, telemetry)
                    heappush(heap, (self._heap_priority(child), next(sequence), child))
                if not seeded:
                    evaluations.append(
                        self._evaluate_candidate(
                            query=query,
                            candidate=candidate,
                            policy=policy,
                            telemetry=telemetry,
                            node_cache=node_cache,
                        )
                    )
                continue

            edges = self._relation_edges(
                provider=provider,
                relation=relation,
                subject_id=subject_id,
                object_id=object_id,
                arousal=runtime.arousal,
                runtime=runtime,
                policy=policy,
                telemetry=telemetry,
            )
            matched = False
            for edge in edges:
                if edge.source not in domain_ids[relation.subject_var] or edge.target not in domain_ids[relation.object_var]:
                    telemetry.branches_pruned += 1
                    continue
                if subject_id is not None and edge.source != subject_id:
                    continue
                if object_id is not None and edge.target != object_id:
                    continue
                if not self.controller.can_search(
                    runtime, policy, telemetry, depth=candidate.depth + 1
                ):
                    break
                if not self.controller.can_create_candidate(runtime, policy, telemetry):
                    break
                matched = True
                self.controller.record_expansion(
                    runtime, telemetry, depth=candidate.depth + 1
                )
                assessment = self._assess_relation_evidence(
                    edge=edge,
                    relation=relation,
                    query=query,
                    policy=policy,
                    telemetry=telemetry,
                )
                child = self._copy_candidate(candidate)
                child.bindings[relation.subject_var] = edge.source
                child.bindings[relation.object_var] = edge.target
                child.relation_edges[relation.id] = edge
                child.evidence_assessments[relation.id] = assessment
                child.remaining_relations = tuple(
                    item for item in candidate.remaining_relations if item != relation.id
                )
                child.depth = candidate.depth + 1
                child.accumulated_cost += 1.0
                self.controller.record_candidate(runtime, telemetry)
                heappush(heap, (self._heap_priority(child), next(sequence), child))

            if not matched:
                evaluations.append(
                    self._evaluate_candidate(
                        query=query,
                        candidate=candidate,
                        policy=policy,
                        telemetry=telemetry,
                        node_cache=node_cache,
                    )
                )

        return evaluations

    @staticmethod
    def _copy_candidate(candidate: _Candidate) -> _Candidate:
        return _Candidate(
            bindings=dict(candidate.bindings),
            relation_edges=dict(candidate.relation_edges),
            evidence_assessments=dict(candidate.evidence_assessments),
            remaining_relations=tuple(candidate.remaining_relations),
            depth=candidate.depth,
            accumulated_cost=candidate.accumulated_cost,
        )

    @staticmethod
    def _heap_priority(candidate: _Candidate) -> tuple[Any, ...]:
        strengths = [assessment.strength for assessment in candidate.evidence_assessments.values()]
        evidence_strength = sum(strengths) / len(strengths) if strengths else 0.0
        return (
            len(candidate.remaining_relations),
            -len(candidate.bindings),
            -round(evidence_strength, 12),
            tuple(sorted(candidate.bindings.items())),
        )

    @staticmethod
    def _select_relation(
        candidate: _Candidate, relations: dict[str, RelationConstraint]
    ) -> str:
        for relation_id in candidate.remaining_relations:
            relation = relations[relation_id]
            if relation.subject_var in candidate.bindings or relation.object_var in candidate.bindings:
                return relation_id
        return candidate.remaining_relations[0]

    def _relation_edges(
        self,
        *,
        provider: EvidenceProvider,
        relation: RelationConstraint,
        subject_id: str | None,
        object_id: str | None,
        arousal: int,
        runtime: RuntimeState,
        policy: RuntimePolicy,
        telemetry: ResolutionTelemetry,
    ) -> list[Edge]:
        if subject_id is not None:
            result = self._provider_call(
                lambda: provider.outgoing(subject_id, relation.relation, limit=arousal),
                runtime=runtime,
                policy=policy,
                telemetry=telemetry,
            )
            return [edge for edge in result.items if object_id is None or edge.target == object_id]
        assert object_id is not None
        result = self._provider_call(
            lambda: provider.incoming(object_id, relation.relation, limit=arousal),
            runtime=runtime,
            policy=policy,
            telemetry=telemetry,
        )
        return result.items

    def _provider_call(
        self,
        fn,
        *,
        runtime: RuntimeState,
        policy: RuntimePolicy,
        telemetry: ResolutionTelemetry,
    ) -> ProviderResult:
        if not self.controller.can_call_provider(runtime, policy, telemetry):
            raise _ProviderBudgetError
        started = perf_counter()
        result = fn()
        latency_ms = (perf_counter() - started) * 1000.0
        self.controller.record_provider_call(
            runtime, telemetry, bytes_read=result.cost.bytes_read
        )
        telemetry.provider_latency_ms += latency_ms
        telemetry.authorization_pruned += result.cost.authorization_pruned
        telemetry.event(
            "provider_called",
            provider=result.provider,
            items=len(result.items),
            bytes_read=result.cost.bytes_read,
            authorization_pruned=result.cost.authorization_pruned,
            provider_latency_ms=round(latency_ms, 6),
        )
        if runtime.bytes_read >= policy.max_bytes_read:
            telemetry.byte_budget_exhausted = True
        return result

    def _assess_relation_evidence(
        self,
        *,
        edge: Edge,
        relation: RelationConstraint,
        query: QueryConstraintGraph,
        policy: RuntimePolicy,
        telemetry: ResolutionTelemetry,
    ) -> EvidenceAssessment:
        evidence: list[Evidence] = []
        policy_rejections: dict[str, str] = {}
        for item in edge.evidence:
            if item.confidence < policy.min_confidence:
                policy_rejections[item.id] = "confidence_below_policy"
                continue
            if item.trust < policy.min_trust:
                policy_rejections[item.id] = "trust_below_policy"
                continue
            if policy.allowed_source_classes is not None and item.source_type not in policy.allowed_source_classes:
                policy_rejections[item.id] = "source_class_not_allowed_by_policy"
                continue
            evidence.append(item)

        assessment = self.fusion.fuse(
            evidence,
            as_of=query.as_of,
            required_source_classes=relation.required_source_classes,
            max_inference_depth=relation.max_inference_depth,
        )
        if policy_rejections:
            assessment = assessment.model_copy(
                update={
                    "rejected_evidence_ids": sorted(
                        set(assessment.rejected_evidence_ids) | set(policy_rejections)
                    ),
                    "rejection_reasons": {
                        **policy_rejections,
                        **assessment.rejection_reasons,
                    },
                }
            )
        telemetry.evidence_accepted += len(assessment.accepted_evidence_ids)
        telemetry.max_inference_depth_observed = max(
            telemetry.max_inference_depth_observed, assessment.max_inference_depth
        )
        telemetry.evidence_rejected += max(
            len(edge.evidence) - len(assessment.accepted_evidence_ids),
            len(assessment.rejected_evidence_ids),
        )
        telemetry.event(
            "evidence_assessed",
            edge_id=edge.id,
            strength=assessment.strength,
            source_diversity=assessment.source_diversity,
            accepted=len(assessment.accepted_evidence_ids),
            rejected=max(
                len(edge.evidence) - len(assessment.accepted_evidence_ids),
                len(assessment.rejected_evidence_ids),
            ),
            rejection_reasons=assessment.rejection_reasons,
        )
        return assessment

    def _evaluate_candidate(
        self,
        *,
        query: QueryConstraintGraph,
        candidate: _Candidate,
        policy: RuntimePolicy,
        telemetry: ResolutionTelemetry,
        node_cache: dict[str, Node],
    ) -> _Evaluation:
        resolved: list[str] = []
        unresolved: list[str] = []
        violated: list[str] = []
        failure_reasons: list[FailureReason] = []
        gaps: list[ResolutionGap] = []
        evidence_ids: set[str] = set()
        evidence_assessments: list[EvidenceAssessment] = []
        soft_score = 0.0

        hard_ids = {
            constraint.id
            for constraint in [*query.relations, *query.attributes]
            if constraint.hard
        }

        for relation in query.relations:
            telemetry.constraints_evaluated += 1
            edge = candidate.relation_edges.get(relation.id)
            assessment = candidate.evidence_assessments.get(relation.id)
            if edge is None or assessment is None:
                unresolved.append(relation.id)
                gaps.append(
                    ResolutionGap(
                        constraint_id=relation.id,
                        constraint_kind="relation",
                        truth="UNKNOWN",
                        variables=[relation.subject_var, relation.object_var],
                        reason=f"No admissible {relation.relation} relation evidence was found for the current bindings.",
                        satisfied_dependencies=sorted(candidate.bindings),
                        candidate_count_before_failure=1 if candidate.bindings else 0,
                        required_evidence=f"Evidence for relation {relation.relation} between {relation.subject_var} and {relation.object_var}.",
                        suggested_provider="evidence-provider",
                        suggested_action=f"Acquire or query evidence establishing {relation.relation} for the bound entities.",
                    )
                )
                continue
            threshold = (
                relation.required_evidence_strength
                if relation.required_evidence_strength is not None
                else policy.min_evidence_strength
            )
            diversity_required = policy.min_source_diversity
            if assessment.strength < threshold or assessment.source_diversity < diversity_required:
                unresolved.append(relation.id)
                failure_reasons.append(FailureReason.EVIDENCE_TOO_WEAK)
                gaps.append(
                    ResolutionGap(
                        constraint_id=relation.id,
                        constraint_kind="relation",
                        truth="UNKNOWN",
                        variables=[relation.subject_var, relation.object_var],
                        reason=(f"Relation evidence strength/diversity is insufficient: strength={assessment.strength:.3f}, "
                                f"diversity={assessment.source_diversity}."),
                        satisfied_dependencies=sorted(candidate.bindings),
                        candidate_count_before_failure=1,
                        required_evidence=f"Stronger or more independent evidence for relation {relation.relation}.",
                        suggested_provider="independent-evidence-provider",
                        suggested_action="Acquire corroborating evidence from an independent source class or group.",
                    )
                )
                continue
            resolved.append(relation.id)
            evidence_assessments.append(assessment)
            evidence_ids.update(assessment.accepted_evidence_ids)
            if not relation.hard:
                soft_score += relation.soft_weight

        for constraint in query.attributes:
            telemetry.constraints_evaluated += 1
            node_id = candidate.bindings.get(constraint.variable)
            node = node_cache.get(node_id) if node_id else None
            evaluation = evaluate_attribute_constraint(constraint, node)
            if evaluation.truth == ConstraintTruth.UNKNOWN:
                unresolved.append(constraint.id)
                gaps.append(
                    ResolutionGap(
                        constraint_id=constraint.id,
                        constraint_kind="attribute",
                        truth="UNKNOWN",
                        variables=[constraint.variable],
                        reason=evaluation.reason,
                        satisfied_dependencies=[constraint.variable] if node_id else [],
                        candidate_count_before_failure=1 if node_id else 0,
                        required_evidence=f"Attribute {constraint.attribute} for variable {constraint.variable}.",
                        suggested_provider="attribute-provider",
                        suggested_action=f"Acquire or derive {constraint.attribute} for the bound {constraint.variable} entity.",
                    )
                )
            elif evaluation.truth == ConstraintTruth.SATISFIED:
                resolved.append(constraint.id)
                if not constraint.hard:
                    soft_score += constraint.soft_weight
            else:
                violated.append(constraint.id)
                gaps.append(
                    ResolutionGap(
                        constraint_id=constraint.id,
                        constraint_kind="attribute",
                        truth="VIOLATED",
                        variables=[constraint.variable],
                        reason=evaluation.reason,
                        satisfied_dependencies=[constraint.variable] if node_id else [],
                        candidate_count_before_failure=1 if node_id else 0,
                        required_evidence=f"A candidate whose {constraint.attribute} satisfies {constraint.op} {constraint.value!r}.",
                        suggested_provider="candidate-provider",
                        suggested_action="Search for another candidate or revise the hard constraint.",
                    )
                )

        hard_satisfied = len(hard_ids.intersection(resolved))
        hard_violated = len(hard_ids.intersection(violated))
        hard_unresolved = hard_ids.intersection(unresolved)

        if hard_violated:
            state = ResolutionState.UNRESOLVABLE
            failure_reasons.append(FailureReason.UNSATISFIABLE_CONSTRAINT)
        elif not hard_unresolved:
            state = ResolutionState.RESOLVED
        elif self._budget_exhausted(telemetry):
            state = ResolutionState.BUDGET_EXHAUSTED
            failure_reasons.extend(self.controller.failure_reasons(telemetry))
        else:
            state = ResolutionState.PARTIAL
            if not any(reason == FailureReason.EVIDENCE_TOO_WEAK for reason in failure_reasons):
                failure_reasons.append(FailureReason.MISSING_DATA)

        total_constraints = len(query.relations) + len(query.attributes)
        structural_coverage = len(resolved) / total_constraints if total_constraints else 1.0
        evidence_coverage = (
            len([relation for relation in query.relations if relation.id in resolved]) / len(query.relations)
            if query.relations
            else 1.0
        )
        evidence_strength = (
            sum(item.strength for item in evidence_assessments) / len(evidence_assessments)
            if evidence_assessments
            else (1.0 if not query.relations else 0.0)
        )
        temporal_freshness = (
            sum(item.freshness for item in evidence_assessments) / len(evidence_assessments)
            if evidence_assessments
            else (1.0 if not query.relations else 0.0)
        )
        source_diversity = len(
            {
                group
                for relation in query.relations
                for edge in [candidate.relation_edges.get(relation.id)]
                if edge is not None
                for evidence in edge.evidence
                for group in [evidence.independence_group or evidence.source_id]
                if evidence.id in evidence_ids
            }
        )
        inference_depth = max(
            (item.max_inference_depth for item in evidence_assessments), default=0
        )
        inference_penalty = min(1.0, inference_depth / max(1, policy.max_depth))
        confidence = self._resolution_confidence(
            structural_coverage=structural_coverage,
            evidence_coverage=evidence_coverage,
            evidence_strength=evidence_strength,
            freshness=temporal_freshness,
            source_diversity=source_diversity,
            min_diversity=policy.min_source_diversity,
            contradiction_risk=0.0,
            inference_penalty=inference_penalty,
        )
        uncertainty = ResolutionUncertainty(
            structural_coverage=structural_coverage,
            evidence_coverage=evidence_coverage,
            evidence_strength=evidence_strength,
            temporal_freshness=temporal_freshness,
            source_diversity=source_diversity,
            contradiction_risk=0.0,
            inference_depth_penalty=inference_penalty,
            resolution_confidence=confidence,
        )
        result = ResolutionResult(
            state=state,
            bindings=dict(candidate.bindings),
            constraint_coverage=structural_coverage,
            resolved_constraints=sorted(resolved),
            unresolved_constraints=sorted(unresolved),
            violated_constraints=sorted(violated),
            evidence_ids=sorted(evidence_ids),
            failure_reasons=self._dedupe_reasons(failure_reasons),
            uncertainty=uncertainty,
            gaps=gaps,
            next_actions=self._next_actions(
                unresolved=unresolved,
                violated=violated,
                contradictions=[],
                failure_reasons=failure_reasons,
            ),
        )
        return _Evaluation(
            result=result,
            hard_satisfied=hard_satisfied,
            hard_violated=hard_violated,
            total_resolved=len(resolved),
            soft_score=soft_score,
            evidence_strength=evidence_strength,
            source_diversity=source_diversity,
            candidate=candidate,
        )

    def _validate_candidate(
        self,
        *,
        evaluation: _Evaluation,
        query: QueryConstraintGraph,
        policy: RuntimePolicy,
        provider: EvidenceProvider,
        telemetry: ResolutionTelemetry,
        runtime: RuntimeState,
    ) -> _Evaluation:
        contradictions: list[str] = []
        contradiction_evidence: set[str] = set()
        contradiction_risk = 0.0

        for relation in query.relations:
            edge = evaluation.candidate.relation_edges.get(relation.id)
            if edge is None:
                continue
            claim_key = edge.attributes.get("claim_key")
            if claim_key is None:
                continue
            siblings_result = self._provider_call(
                lambda e=edge: provider.outgoing(
                    e.source, e.type, limit=policy.max_candidates
                ),
                runtime=runtime,
                policy=policy,
                telemetry=telemetry,
            )
            siblings = [
                sibling
                for sibling in siblings_result.items
                if sibling.attributes.get("claim_key") == claim_key
            ]
            claims: list[Claim] = []
            for sibling in siblings:
                supporting = [
                    item
                    for item in sibling.evidence
                    if item.confidence >= policy.min_confidence
                    and item.trust >= policy.min_trust
                    and (policy.allowed_source_classes is None or item.source_type in policy.allowed_source_classes)
                ]
                if not supporting:
                    continue
                valid_from = max(
                    (item.valid_from for item in supporting if item.valid_from is not None),
                    default=None,
                )
                valid_until = min(
                    (item.valid_until for item in supporting if item.valid_until is not None),
                    default=None,
                )
                claims.append(
                    Claim(
                        key=str(claim_key),
                        value=sibling.attributes.get("claim_value"),
                        status=ClaimStatus.SUPPORTED,
                        evidence_ids=[item.id for item in supporting],
                        valid_from=valid_from,
                        valid_until=valid_until,
                    )
                )
            claim_assessment = self.truth.evaluate_claims(claims, as_of=query.as_of)
            contradiction_risk = max(contradiction_risk, claim_assessment.contradiction_risk)
            if claim_assessment.status == ClaimStatus.DISPUTED:
                telemetry.contradictions_detected += 1
                values = sorted({str(claim.value) for claim in claim_assessment.active_claims})
                contradictions.append(f"{claim_key}: conflicting values {values}")
                for claim in claim_assessment.active_claims:
                    contradiction_evidence.update(claim.evidence_ids)
            elif claim_assessment.status == ClaimStatus.SUPERSEDED:
                telemetry.contradictions_resolved += 1
                telemetry.event(
                    "contradiction_resolved",
                    claim_key=str(claim_key),
                    resolution="temporal_supersession",
                )

        result = evaluation.result.model_copy(deep=True)
        if contradictions:
            result.state = ResolutionState.CONTRADICTED
            result.contradictions = contradictions
            result.evidence_ids = sorted(set(result.evidence_ids).union(contradiction_evidence))
            result.failure_reasons = self._merge_reasons(
                result.failure_reasons, [FailureReason.CONTRADICTORY_EVIDENCE]
            )
            result.next_actions = self._next_actions(
                unresolved=result.unresolved_constraints,
                violated=result.violated_constraints,
                contradictions=contradictions,
                failure_reasons=result.failure_reasons,
            )
        uncertainty = result.uncertainty.model_copy(deep=True)
        uncertainty.contradiction_risk = contradiction_risk
        uncertainty.resolution_confidence = self._resolution_confidence(
            structural_coverage=uncertainty.structural_coverage,
            evidence_coverage=uncertainty.evidence_coverage,
            evidence_strength=uncertainty.evidence_strength,
            freshness=uncertainty.temporal_freshness,
            source_diversity=uncertainty.source_diversity,
            min_diversity=policy.min_source_diversity,
            contradiction_risk=contradiction_risk,
            inference_penalty=uncertainty.inference_depth_penalty,
        )
        result.uncertainty = uncertainty
        return _Evaluation(
            result=result,
            hard_satisfied=evaluation.hard_satisfied,
            hard_violated=evaluation.hard_violated,
            total_resolved=evaluation.total_resolved,
            soft_score=evaluation.soft_score,
            evidence_strength=evaluation.evidence_strength,
            source_diversity=evaluation.source_diversity,
            candidate=evaluation.candidate,
        )

    @staticmethod
    def _choose_validated_best(evaluations: list[_Evaluation]) -> _Evaluation:
        non_contradicted = [
            item
            for item in evaluations
            if item.result.state != ResolutionState.CONTRADICTED
            and item.hard_violated == 0
        ]
        return max(non_contradicted or evaluations, key=lambda item: item.score)

    def _blocking_hard_attributes(self, query: QueryConstraintGraph, variable_name: str) -> list[str]:
        constraints = [
            constraint
            for constraint in query.attributes
            if constraint.variable == variable_name and constraint.hard
        ]
        if self.graph is None:
            return [constraint.id for constraint in constraints]
        node_type = next(variable.node_type for variable in query.variables if variable.name == variable_name)
        nodes = self.graph.nodes_of_type(node_type)
        blocking: list[str] = []
        for constraint in constraints:
            if nodes and all(self._evaluate_attribute_on_node(constraint, node) is False for node in nodes):
                blocking.append(constraint.id)
        return blocking

    def _hard_attribute_violation(
        self,
        query: QueryConstraintGraph,
        bindings: dict[str, str],
        node_cache: dict[str, Node],
    ) -> bool:
        for constraint in query.attributes:
            if not constraint.hard or constraint.variable not in bindings:
                continue
            node = node_cache.get(bindings[constraint.variable])
            if node is None:
                continue
            outcome = self._evaluate_attribute_on_node(constraint, node)
            if outcome is False:
                return True
        return False

    @staticmethod
    def _evaluate_attribute_on_node(
        constraint: AttributeConstraint, node: Node | None
    ) -> bool | None:
        evaluation = evaluate_attribute_constraint(constraint, node)
        if evaluation.truth == ConstraintTruth.UNKNOWN:
            return None
        return evaluation.truth == ConstraintTruth.SATISFIED

    @staticmethod
    def _node_type_allowed(node_type: str, policy: RuntimePolicy) -> bool:
        return policy.allowed_node_types is None or node_type in policy.allowed_node_types

    @staticmethod
    def _edge_type_allowed(edge_type: str, policy: RuntimePolicy) -> bool:
        return policy.allowed_edge_types is None or edge_type in policy.allowed_edge_types

    @staticmethod
    def _budget_exhausted(telemetry: ResolutionTelemetry) -> bool:
        return (
            telemetry.expansion_budget_exhausted
            or telemetry.depth_budget_exhausted
            or telemetry.deadline_exhausted
            or telemetry.provider_call_budget_exhausted
            or telemetry.byte_budget_exhausted
            or telemetry.candidate_budget_exhausted
        )

    @staticmethod
    def _resolution_confidence(
        *,
        structural_coverage: float,
        evidence_coverage: float,
        evidence_strength: float,
        freshness: float,
        source_diversity: int,
        min_diversity: int,
        contradiction_risk: float,
        inference_penalty: float,
    ) -> float:
        diversity_score = min(1.0, source_diversity / max(1, min_diversity))
        score = (
            0.40 * structural_coverage
            + 0.15 * evidence_coverage
            + 0.20 * evidence_strength
            + 0.10 * freshness
            + 0.15 * diversity_score
            - 0.35 * contradiction_risk
            - 0.05 * inference_penalty
        )
        return round(min(1.0, max(0.0, score)), 12)

    def _terminal_without_search(
        self,
        query: QueryConstraintGraph,
        telemetry: ResolutionTelemetry,
        runtime: RuntimeState,
        *,
        state: ResolutionState,
        reason: FailureReason,
        action: str,
    ) -> ResolutionResult:
        constraint_ids = [constraint.id for constraint in [*query.relations, *query.attributes]]
        result = ResolutionResult(
            state=state,
            constraint_coverage=0.0 if constraint_ids else 1.0,
            unresolved_constraints=constraint_ids,
            failure_reasons=[reason],
            uncertainty=ResolutionUncertainty(structural_coverage=0.0),
            next_actions=[action],
        )
        return self._finalize_result(result, telemetry, runtime)

    def _budget_terminal(
        self,
        query: QueryConstraintGraph,
        telemetry: ResolutionTelemetry,
        runtime: RuntimeState,
    ) -> ResolutionResult:
        reasons = self.controller.failure_reasons(telemetry)
        if telemetry.byte_budget_exhausted and not reasons:
            reasons = [FailureReason.VALIDATION_RESERVE_EXHAUSTED]
        constraint_ids = [constraint.id for constraint in [*query.relations, *query.attributes]]
        result = ResolutionResult(
            state=ResolutionState.BUDGET_EXHAUSTED,
            constraint_coverage=0.0,
            unresolved_constraints=constraint_ids,
            failure_reasons=reasons or [FailureReason.VALIDATION_RESERVE_EXHAUSTED],
            uncertainty=ResolutionUncertainty(structural_coverage=0.0),
            next_actions=["Increase the runtime budget or narrow the query Focus."],
        )
        return self._finalize_result(result, telemetry, runtime)

    def _finalize_result(
        self,
        result: ResolutionResult,
        telemetry: ResolutionTelemetry,
        runtime: RuntimeState,
    ) -> ResolutionResult:
        telemetry.elapsed_ms = round(self.controller.elapsed_ms(runtime), 3)
        if not telemetry.arousal_history:
            telemetry.arousal_history = list(runtime.arousal_trajectory)
        result.telemetry = telemetry.as_dict()
        result.planner_summary = {
            "operation_count": len(telemetry.planner_operations),
            "operation_order": [
                item.get("constraint_id") for item in telemetry.planner_operations
            ],
        }
        result.evidence_summary = {
            "accepted_evidence": len(result.evidence_ids),
            "evidence_strength": result.uncertainty.evidence_strength,
            "source_diversity": result.uncertainty.source_diversity,
            "accepted_observations": telemetry.evidence_accepted,
            "rejected_observations": telemetry.evidence_rejected,
        }
        result.budget_summary = {
            "expansions": runtime.expansions,
            "provider_calls": runtime.provider_calls,
            "bytes_read": runtime.bytes_read,
            "candidates": runtime.candidates,
            "validation_steps": runtime.validations,
            "validation_reserve_entered": telemetry.validation_reserve_entered,
            "deadline_exhausted": telemetry.deadline_exhausted,
            "expansion_budget_exhausted": telemetry.expansion_budget_exhausted,
            "provider_call_budget_exhausted": telemetry.provider_call_budget_exhausted,
            "byte_budget_exhausted": telemetry.byte_budget_exhausted,
        }
        event_types: dict[str, int] = {}
        for event in telemetry.events:
            name = str(event.get("event", "unknown"))
            event_types[name] = event_types.get(name, 0) + 1
        result.trace_summary = {
            "event_count": len(telemetry.events),
            "event_types": event_types,
            "branches_pruned": telemetry.branches_pruned,
            "authorization_pruned": telemetry.authorization_pruned,
            "contradictions_detected": telemetry.contradictions_detected,
            "contradictions_resolved": telemetry.contradictions_resolved,
        }
        return result

    @staticmethod
    def _dedupe_reasons(reasons: Iterable[FailureReason]) -> list[FailureReason]:
        seen: set[FailureReason] = set()
        result: list[FailureReason] = []
        for reason in reasons:
            if reason not in seen:
                seen.add(reason)
                result.append(reason)
        return result

    def _merge_reasons(
        self, current: list[FailureReason], extra: Iterable[FailureReason]
    ) -> list[FailureReason]:
        return self._dedupe_reasons([*current, *extra])

    @staticmethod
    def _next_actions(
        *,
        unresolved: list[str],
        violated: list[str],
        contradictions: list[str],
        failure_reasons: Iterable[FailureReason],
    ) -> list[str]:
        actions: list[str] = []
        if contradictions:
            actions.append("Resolve contradictory evidence before accepting the result.")
        if unresolved:
            actions.append(
                "Acquire or validate data for unresolved constraints: " + ", ".join(sorted(unresolved))
            )
        if violated:
            actions.append(
                "No current candidate satisfies constraints: " + ", ".join(sorted(violated))
            )
        reasons = set(failure_reasons)
        if reasons.intersection(
            {
                FailureReason.DEADLINE_EXHAUSTED,
                FailureReason.EXPANSION_BUDGET_EXHAUSTED,
                FailureReason.DEPTH_BUDGET_EXHAUSTED,
                FailureReason.PROVIDER_CALL_BUDGET_EXHAUSTED,
                FailureReason.VALIDATION_RESERVE_EXHAUSTED,
            }
        ):
            actions.append("Increase the runtime budget or narrow the query Focus before retrying.")
        if FailureReason.EVIDENCE_TOO_WEAK in reasons:
            actions.append("Acquire stronger or more independent evidence for unresolved relations.")
        return actions
