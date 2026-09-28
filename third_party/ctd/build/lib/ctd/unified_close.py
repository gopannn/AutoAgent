from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from .authz import AuthorizationFilter
from .controller import RuntimePolicy
from .models import ResolutionGap
from .structural import StructuralRelation
from .kernel.close import run_close
from .graph import EvidenceGraph
from .kernel.query import (
    Budget, Outcome, RelaxationPolicy, SlotSpec, ask, has, lexical, where,
)
from .oracles import ORACLE_KINDS, build_oracle
from .kernel.store import Record, Rel, TopoStore


class CloseRecordModel(BaseModel):
    id: str
    domain: str
    attrs: dict[str, Any] = Field(default_factory=dict)
    relations: tuple[StructuralRelation, ...] = ()
    types: dict[str, str] = Field(default_factory=dict)
    text: str = ""


class CloseConstraintSpec(BaseModel):
    name: str
    # "ask" is the MODEL tier. It existed in the kernel from the start and was
    # unreachable here, which made the cascade's whole cost argument
    # unfalsifiable: you cannot demonstrate that filtering saved model calls if
    # zero is the only number of model calls the API can produce.
    kind: Literal["has", "where", "lexical", "ask"]
    attr: str | None = None
    values: list[Any] = Field(default_factory=list)
    negate: bool = False
    op: Literal["eq", "ne", "lt", "lte", "gt", "gte", "in"] = "eq"
    value: Any = None
    terms: list[str] = Field(default_factory=list)
    threshold: int = Field(default=1, ge=1)
    fields: list[str] = Field(default_factory=lambda: ["text"])
    question: str = ""

    @model_validator(mode="after")
    def validate_shape(self) -> "CloseConstraintSpec":
        if self.kind in {"has", "where"} and not self.attr:
            raise ValueError(f"{self.kind} constraint requires attr")
        if self.kind == "has" and not self.values and not self.negate:
            raise ValueError("has constraint requires values unless negate=true")
        if self.kind == "lexical" and not self.terms:
            raise ValueError("lexical constraint requires terms")
        if self.kind == "ask" and not self.question:
            raise ValueError("ask constraint requires a question")
        return self


class UnifiedCloseRequest(BaseModel):
    query: str
    records: list[CloseRecordModel]
    constraints: list[CloseConstraintSpec]
    expect_unique: bool = True
    droppable: list[str] = Field(default_factory=list)
    max_drops: int = Field(default=0, ge=0)
    policy: RuntimePolicy = Field(default_factory=RuntimePolicy)
    # Which judge answers `ask` constraints. The default decides nothing, so an
    # unbound model constraint abstains with a typed gap rather than passing.
    oracle: Literal[ORACLE_KINDS] = "none"  # type: ignore[valid-type]
    oracle_attribute_map: dict[str, str] = Field(default_factory=dict)


class UnifiedCloseResult(BaseModel):
    outcome: Literal["CLOSED", "PARTIAL", "REQUEST", "ABSTAIN"]
    ids: list[str] = Field(default_factory=list)
    dropped: list[str] = Field(default_factory=list)
    reason: str = ""
    gap: ResolutionGap | None = None
    # ABSTAIN used to return prose and nothing else. An engine whose stated
    # contract is "say what would settle it" must do so for every
    # non-answer, not only for REQUEST: ambiguity, an undecided constraint
    # and an exhausted budget are all actionable, and each needs a different
    # action. `gap` remains the first entry for callers written against the
    # earlier shape.
    gaps: list[ResolutionGap] = Field(default_factory=list)
    undecided_ids: list[str] = Field(default_factory=list)
    provenance: dict[str, dict[str, str]] = Field(default_factory=dict)
    trace: list[dict[str, Any]] = Field(default_factory=list)
    telemetry: dict[str, Any] = Field(default_factory=dict)


def _rel(rel: StructuralRelation) -> Rel:
    return Rel(rel.pred.upper(), tuple(_rel(a) if isinstance(a, StructuralRelation) else a for a in rel.args))


def _authorized(record: CloseRecordModel, policy: RuntimePolicy) -> bool:
    """Delegated to ctd.authz — the single decision point."""
    return AuthorizationFilter.from_policy(policy).allows(record.attrs)


def _where_pred(spec: CloseConstraintSpec):
    def pred(record: Record) -> bool:
        actual = record.attrs[spec.attr or ""]
        expected = spec.value
        if spec.op == "eq":
            result = actual == expected
        elif spec.op == "ne":
            result = actual != expected
        elif spec.op == "lt":
            result = actual < expected
        elif spec.op == "lte":
            result = actual <= expected
        elif spec.op == "gt":
            result = actual > expected
        elif spec.op == "gte":
            result = actual >= expected
        else:
            result = actual in expected
        return not result if spec.negate else result
    return pred


def _gaps_for(closure, spec_by_name: dict[str, CloseConstraintSpec]) -> list[ResolutionGap]:
    """Turn every non-answer into a structured, actionable gap.

    REQUEST names its binding constraint directly. ABSTAIN does not, because
    the kernel deliberately distinguishes four reasons for abstaining and each
    needs a different action from the caller:

        underspecified  the spec matched several records. More evidence will
                        not help; a discriminating constraint will.
        undecided       a constraint returned UNKNOWN. Evidence will help, and
                        the constraint names which.
        budget          the work was affordable but not within this budget.
                        Nothing is missing except allowance.

    Collapsing those into one "ABSTAIN" string makes the caller parse prose to
    decide what to do next, which is the failure mode the typed outcomes were
    introduced to remove.
    """
    gaps: list[ResolutionGap] = []

    if closure.request is not None:
        info = closure.request
        raw_spec = spec_by_name.get(info.binding_constraint)
        kind = "attribute" if raw_spec and raw_spec.kind in {"has", "where"} else "domain"
        gaps.append(ResolutionGap(
            constraint_id=info.binding_constraint,
            constraint_kind=kind,
            truth="UNKNOWN",
            reason=info.message,
            satisfied_dependencies=list(info.satisfied),
            candidate_count_before_failure=info.survivors_before,
            required_evidence=f"Evidence capable of deciding constraint '{info.binding_constraint}'.",
            suggested_provider="local_structural_index",
            suggested_action=f"Acquire or update data for '{info.binding_constraint}' and retry closure.",
            priority=1.0,
        ))
        return gaps

    if closure.outcome in (Outcome.CLOSED, Outcome.PARTIAL):
        if closure.undecided_ids:
            # A CLOSED answer with undecided rivals is an answer, not a
            # complete one. Saying so is cheaper than a caller assuming
            # exhaustiveness the engine never claimed.
            gaps.append(ResolutionGap(
                constraint_id="__completeness__",
                constraint_kind="evidence",
                truth="UNKNOWN",
                reason=(f"{len(closure.undecided_ids)} candidate(s) could be "
                        f"neither confirmed nor eliminated: "
                        f"{', '.join(closure.undecided_ids)}"),
                candidate_count_before_failure=len(closure.undecided_ids),
                required_evidence="Data capable of deciding the remaining candidates.",
                suggested_action=("Decide the undecided candidates before "
                                  "treating this answer set as exhaustive."),
                priority=0.5,
            ))
        return gaps

    if closure.outcome is not Outcome.ABSTAIN:
        return gaps

    reason = closure.reason or ""
    survivors = closure.trace[-1]["in"] if closure.trace else 0

    if reason.startswith("underspecified"):
        gaps.append(ResolutionGap(
            constraint_id="__specification__",
            constraint_kind="domain",
            truth="UNKNOWN",
            reason=reason,
            candidate_count_before_failure=len(closure.ids) or survivors,
            required_evidence="A constraint that distinguishes the surviving candidates.",
            suggested_action=("Add a discriminating constraint, or set "
                              "expect_unique=false if several answers are "
                              "acceptable. More evidence about the existing "
                              "constraints will not narrow this."),
            priority=1.0,
        ))
        return gaps

    if reason.startswith("uniqueness not established"):
        gaps.append(ResolutionGap(
            constraint_id="__uniqueness__",
            constraint_kind="evidence",
            truth="UNKNOWN",
            reason=reason,
            candidate_count_before_failure=survivors,
            required_evidence=("Data capable of deciding the rival candidates, "
                               "or expect_unique=false."),
            suggested_action=("Decide the undecided candidates, or set "
                              "expect_unique=false to accept the decided "
                              "member without a uniqueness claim."),
            priority=1.0,
        ))
        return gaps

    if reason.startswith("undecided constraints remain"):
        names = [n.strip() for n in reason.split(":", 1)[-1].split(",") if n.strip()]
        for name in names or ["__undecided__"]:
            raw_spec = spec_by_name.get(name)
            gaps.append(ResolutionGap(
                constraint_id=name,
                constraint_kind="attribute" if raw_spec and raw_spec.kind in {"has", "where"} else "evidence",
                truth="UNKNOWN",
                reason=f"Constraint '{name}' could not be decided on the available data.",
                candidate_count_before_failure=survivors,
                required_evidence=f"A value capable of deciding '{name}'.",
                suggested_provider="local_structural_index",
                suggested_action=f"Supply the data '{name}' tests, then retry closure.",
                priority=1.0,
            ))
        return gaps

    # Budget, deadline or cost ceiling. Nothing is missing except allowance,
    # so the action is about the budget rather than about the data.
    gaps.append(ResolutionGap(
        constraint_id="__budget__",
        constraint_kind="budget",
        truth="UNKNOWN",
        reason=reason or "The budget could not cover closure.",
        candidate_count_before_failure=survivors,
        required_evidence="Additional execution allowance; the data may be sufficient.",
        suggested_action=("Raise the deadline, provider-call or expansion "
                          "allowance and retry. Nothing is known to be "
                          "missing from the data."),
        priority=0.75,
    ))
    return gaps


class UnifiedCloseOperator:
    """Authorization-aware wrapper around the canonical kernel CLOSE operator."""

    def __init__(self, *, graph: EvidenceGraph | None = None,
                 oracle_fn=None) -> None:
        # A model is bound in process, never named over the wire. An HTTP
        # request that could name an arbitrary judge is an SSRF surface with
        # extra steps.
        self._graph = graph
        self._oracle_fn = oracle_fn

    def run(self, request: UnifiedCloseRequest) -> UnifiedCloseResult:
        authorized = [record for record in request.records if _authorized(record, request.policy)]
        store = TopoStore()
        indexed_attrs = sorted({spec.attr for spec in request.constraints if spec.kind == "has" and spec.attr})
        for raw in authorized:
            store.ingest(
                Record(
                    id=raw.id,
                    domain=raw.domain,
                    attrs=dict(raw.attrs),
                    rels=tuple(_rel(item) for item in raw.relations),
                    types={key: str(value).upper() for key, value in raw.types.items()},
                    text=raw.text,
                )
            )
        for attr in indexed_attrs:
            store.declare_index(attr)

        constraints = []
        spec_by_name = {spec.name: spec for spec in request.constraints}
        for spec in request.constraints:
            if spec.kind == "has":
                constraints.append(has(spec.name, spec.attr or "", *spec.values, negate=spec.negate))
            elif spec.kind == "where":
                constraints.append(where(spec.name, _where_pred(spec)))
            elif spec.kind == "lexical":
                constraints.append(
                    lexical(spec.name, spec.terms, threshold=spec.threshold, fields=spec.fields)
                )
            else:
                constraints.append(ask(spec.name, spec.question))

        oracle = build_oracle(
            request.oracle,
            graph=self._graph,
            attribute_map=request.oracle_attribute_map,
            fn=self._oracle_fn,
        )
        closure = run_close(
            store,
            SlotSpec(request.query, constraints, expect_unique=request.expect_unique),
            oracle=oracle,
            budget=Budget(
                max_model_calls=request.policy.max_provider_calls,
                max_semantic_calls=request.policy.max_expansions,
                max_ms=float(request.policy.deadline_ms),
            ),
            relax=RelaxationPolicy(tuple(request.droppable), request.max_drops),
        )
        outcome = closure.outcome.value.upper()
        gaps = _gaps_for(closure, spec_by_name)
        return UnifiedCloseResult(
            outcome=outcome,
            ids=closure.ids,
            undecided_ids=closure.undecided_ids,
            dropped=closure.dropped,
            reason=closure.reason,
            gap=gaps[0] if gaps else None,
            gaps=gaps,
            provenance=closure.provenance,
            trace=closure.trace,
            telemetry={
                "authorized_records": len(authorized),
                "input_records": len(request.records),
                "probes": closure.probes,
                "reads": closure.reads,
                "semantic_calls": closure.semantic_calls,
                "model_calls": closure.model_calls,
                "model_tokens": closure.model_tokens,
                "oracle": type(oracle).__name__,
                "cost_units": closure.cost_units,
                "elapsed_ms": closure.ms,
                "deadline_hit": closure.deadline_hit,
            },
        )
