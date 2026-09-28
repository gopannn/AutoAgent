"""Resolution plane — CTD's evidence resolver, driven from the workbook.

Three systems, one architecture, and the seam between them is the thing worth
getting right:

    workbook   knows the facts. 04_PROJECTS, 17_PAYMENTS, the logs. It also
               already declares, on 37_GOVERNANCE, which class of evidence each
               fact belongs to and whether that class may drive a commitment.
    ctd        resolves. Three-valued constraint truth, correlation-discounted
               evidence fusion, and — the part the workbook cannot do — a
               typed ResolutionGap naming exactly what is missing.
    topo v5    projects. Structural transfer onto what has not happened yet.
               Emits hypotheses and nothing else, ever.

Why bring CTD in at all when 38_DECISION_ENGINE already computes feasibility
-----------------------------------------------------------------------------

Because of row 11. The workbook's own feasibility table says, for "Site
verified": evidence `"Not captured in Stage 1"`, result `"REVIEW"` — a hardcoded
string, because a spreadsheet formula has no way to represent *I do not know*
as distinct from *no* and then say what would settle it. Every uncaptured
condition collapses to the same constant REVIEW, and a manager reading the
dashboard cannot tell which REVIEWs are one phone call from PASS.

CTD's `ConstraintTruth` has the third value, and `ResolutionGap` carries
`required_evidence`, `suggested_action`, and a priority. So the same row becomes
UNKNOWN plus a named, ranked data request. That is the same output shape v5's
CLOSE mode produces for retrieval, arriving here through a different engine, and
the two agreeing on it is not a coincidence — a system that cannot say "I do not
know, and here is what would tell me" has to guess instead.

Evidence classes become fusion parameters
-----------------------------------------

37_GOVERNANCE block A is already a trust ordering: FIRM_ACTUAL "outranks
research", STAFF_OBSERVED is "our strongest input", RESEARCH_DERIVED is
"planning only — never quote from it". Those English judgements are mapped to
CTD trust weights below, and two classes are given `required_source_classes`
exclusions so they can never satisfy a commercial gate no matter how much of
them accumulates. The firm's governance rule stops being a convention people
remember and becomes a constraint the resolver enforces.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..graph import EvidenceGraph
from ..models import (
    AttributeConstraint, Edge, Evidence, Node, QueryConstraintGraph,
    RelationConstraint, ResolutionState, Variable,
)
from ..resolver import Resolver

from .workbook import ProjectRow

__all__ = [
    "TRUST", "COMMITMENT_CLASSES", "GateOutcome", "resolve_feasibility",
    "build_graph",
]


# 37_GOVERNANCE block A, as numbers. The ORDER is the workbook's; the specific
# values are a first calibration and are the obvious thing to tune once
# override outcomes accumulate on 38_DECISION_ENGINE block E.
TRUST: dict[str, float] = {
    "STAFF_OBSERVED": 0.95,        # "our strongest input"
    "FIRM_ACTUAL": 0.95,           # "outranks research"
    "MANUFACTURER_VERIFIED": 0.90,
    "SYSTEM_CALCULATED": 0.85,     # "yes, if the inputs are sound"
    "VENDOR_QUOTED": 0.80,         # "yes, until its expiry date"
    "MANAGER_CLASSIFIED": 0.75,    # "yes, with the manager named"
    "CUSTOMER_ENTERED": 0.60,      # "only after confirmation in writing"
    "RESEARCH_DERIVED": 0.50,      # "planning only — never quote from it"
    "MODEL_PREDICTED": 0.30,       # "advisory only"
}

COMMITMENT_CLASSES = {
    "STAFF_OBSERVED", "FIRM_ACTUAL", "MANUFACTURER_VERIFIED",
    "SYSTEM_CALCULATED", "VENDOR_QUOTED", "MANAGER_CLASSIFIED",
}
"""Classes permitted to satisfy a gate that releases work or money.

RESEARCH_DERIVED and MODEL_PREDICTED are absent by construction. This is the
plane boundary, and it is the reason a pre-mortem prediction — however high its
priority, however many precedents converge on it — can never move a gate to
PASS. It can only ever raise the gap that a human then closes with real
evidence.
"""


def _nothing_logged(flag: str) -> bool:
    low = flag.strip().lower()
    return not low or low.startswith(("no gated", "none", "not logged",
                                      "no flag"))


def _ev(eid: str, cls: str, value: Any, at: datetime,
        group: str | None = None) -> Evidence:
    return Evidence(
        id=eid,
        source_id=f"workbook::{cls}",
        source_type=cls,
        observed_at=at,
        confidence=0.9,
        trust=TRUST.get(cls, 0.5),
        # Facts computed by the same workbook are NOT independent of each
        # other. Grouping them means three sheet-derived facts agreeing counts
        # as one witness after CTD's correlation discount, not three.
        independence_group=group or f"workbook::{cls}",
        claim_value=value,
    )


def build_graph(rows: list[ProjectRow],
                as_of: datetime | None = None) -> EvidenceGraph:
    """One node per project and per stage, with the register's facts attached.

    Attribute values come straight from the cached cells: the workbook's
    formulas are the source of truth and recomputing them here would be a
    second, divergent implementation of numbers 33_SYSTEM_AUDIT already
    reconciles against a third.
    """
    at = as_of or datetime.now(timezone.utc)
    g = EvidenceGraph()

    for p in rows:
        complete = p.num("completeness", 0.0)
        stale = None            # not captured per project; a genuine unknown
        g.add_node(Node(
            id=p.id,
            type="project",
            attributes={
                "client": p.text("client"),
                "designer": p.text("designer"),
                "status": p.text("status"),
                "brief_completeness": complete,
                "payment_gate": (p.text("pay_gate").split("·")[0].strip()
                                 or "UNKNOWN"),
                "sla_status": p.text("sla") or "UNKNOWN",
                # Tri-state, not a string test. 04_PROJECTS BF reads "No
                # gated work logged yet" when nothing has been logged, which
                # is an ABSENCE of evidence. A first pass compared it against
                # "" and reported a sequence violation on all three live
                # projects — ignorance scored as a negative finding, which is
                # the exact error this layer exists to remove. Absent means
                # absent: the key is omitted and the constraint returns
                # UNKNOWN.
                **({} if _nothing_logged(p.text("gate_flag"))
                   else {"sequence_flag": p.text("gate_flag")}),
                "revisions": p.num("revisions"),
                "days_idle": p.num("days_idle"),
                # Deliberately absent, not defaulted: 12_DESIGN_INTELLIGENCE
                # captures no site survey, and inventing site_verified=False
                # would report a refusal where the truth is ignorance.
                **({} if stale is None else {"stale_rates": stale}),
            },
        ))
        stage_id = f"{p.id}::current-stage"
        g.add_node(Node(
            id=stage_id,
            type="stage",
            attributes={
                "name": p.text("stage") or "UNKNOWN",
                "sla_status": p.text("sla") or "UNKNOWN",
                "days_in_stage": p.num("days_in_stage"),
            },
        ))
        g.add_edge(Edge(
            id=f"{p.id}::has-stage",
            source=p.id,
            target=stage_id,
            type="HAS_STAGE",
            evidence=[
                _ev(f"{p.id}-ev-stage", "SYSTEM_CALCULATED",
                    p.text("stage"), at, group=f"{p.id}::register"),
                _ev(f"{p.id}-ev-log", "STAFF_OBSERVED",
                    p.text("stage"), at, group=f"{p.id}::daily-log"),
            ],
        ))
    return g


# --------------------------------------------------------------------------

@dataclass
class GateOutcome:
    """38_DECISION_ENGINE block A, decided on evidence rather than on a
    hardcoded string."""
    project: str
    verdict: str                      # PASS | REVIEW | BLOCKED
    state: str                        # CTD ResolutionState
    coverage: float
    confidence: float
    resolved: list[str] = field(default_factory=list)
    violated: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    gaps: list[dict[str, Any]] = field(default_factory=list)

    @property
    def headline(self) -> str:
        if self.verdict == "BLOCKED":
            return f"BLOCKED — {len(self.violated)} condition(s) violated"
        if self.verdict == "REVIEW":
            return f"REVIEW — {len(self.gaps)} unresolved condition(s)"
        return "PASS — every condition satisfied on admissible evidence"


# One condition per query, mirroring 38_DECISION_ENGINE block A row for row.
#
# A single query carrying all six conditions is the obvious construction and it
# is wrong here: the resolver drops a candidate as soon as a hard constraint is
# violated, so the four conditions after the first failure came back
# "unresolved" and were reported as gaps needing evidence — when two of them
# were in fact satisfiable from data already in the register. That turns a
# clean PASS into a fabricated data request. Resolving each condition on its
# own gives every row its own truth value, which is what the sheet displays and
# what a manager acts on.
#
# `required_source_classes` on the commercial gates enforces the plane
# boundary: no volume of RESEARCH_DERIVED or MODEL_PREDICTED evidence can
# satisfy them.
_CONDITIONS: list[dict[str, Any]] = [
    {"id": "brief-complete", "var": "p", "attr": "brief_completeness",
     "op": "gte", "value": 1.0, "classes": COMMITMENT_CLASSES,
     "label": "Brief completeness"},
    {"id": "payment-gate", "var": "p", "attr": "payment_gate",
     "op": "ne", "value": "BLOCKED", "classes": COMMITMENT_CLASSES,
     "label": "Payment gate"},
    {"id": "sequence-integrity", "var": "p", "attr": "sequence_flag",
     "op": "eq", "value": "", "classes": None,
     "label": "Sequence integrity"},
    {"id": "stage-sla", "var": "s", "attr": "sla_status",
     "op": "ne", "value": "Breached", "classes": None, "label": "Stage SLA"},
    # Absent from every node on purpose. A spreadsheet formula has to return
    # something and returns the constant "REVIEW"; the resolver returns
    # UNKNOWN and a gap that names the survey.
    {"id": "site-verified", "var": "p", "attr": "site_verified",
     "op": "eq", "value": True, "classes": {"STAFF_OBSERVED"},
     "label": "Site verified"},
    {"id": "rate-freshness", "var": "p", "attr": "stale_rates",
     "op": "lte", "value": 0, "classes": None,
     "label": "Research rate freshness"},
]


def _query(cond: dict[str, Any], as_of: datetime) -> QueryConstraintGraph:
    return QueryConstraintGraph(
        variables=[Variable(name="p", node_type="project"),
                   Variable(name="s", node_type="stage")],
        relations=[RelationConstraint(
            id="stage-linked", subject_var="p", relation="HAS_STAGE",
            object_var="s", hard=True,
            required_source_classes=set(COMMITMENT_CLASSES))],
        attributes=[AttributeConstraint(
            id=cond["id"], variable=cond["var"], attribute=cond["attr"],
            op=cond["op"], value=cond["value"], hard=True,
            required_source_classes=set(cond["classes"] or ()))],
        as_of=as_of,
        top_k=1,
        query_class=f"feasibility::{cond['id']}",
    )


_ACTION = {
    "brief-complete": ("Complete 04_PROJECTS AT — AU names the missing "
                       "fields."),
    "payment-gate": "Clear or waive the blocking tranche on 17_PAYMENTS.",
    "sequence-integrity": ("Log gated work so the sequence CAN be checked. 04_PROJECTS BF reads 'No gated work logged yet', which is not a pass."),
    "stage-sla": "Re-plan the breached stage before starting new work.",
    "site-verified": ("Record a measured site survey. 34_ENTITY_DICTIONARY "
                      "already reserves SRV-2026-nnnn and site_surveys; "
                      "12_DESIGN_INTELLIGENCE captures it only partially."),
    "rate-freshness": ("Capture a per-project staleness count from "
                       "21_RESEARCH_COMPONENTS J8:J20."),
}


def resolve_feasibility(rows: list[ProjectRow],
                        as_of: datetime | None = None) -> list[GateOutcome]:
    at = as_of or datetime.now(timezone.utc)
    graph = build_graph(rows, at)
    resolver = Resolver(graph)
    out: list[GateOutcome] = []

    for p in rows:
        resolved: list[str] = []
        violated: list[str] = []
        unresolved: list[str] = []
        gaps: list[dict[str, Any]] = []
        confidences: list[float] = []

        for cond in _CONDITIONS:
            result = resolver.resolve(_query(cond, at))
            cid = cond["id"]
            unc = result.uncertainty
            if unc is not None:
                confidences.append(unc.resolution_confidence)

            if cid in result.violated_constraints:
                violated.append(cid)
                gaps.append({
                    "constraint": cid, "label": cond["label"],
                    "kind": "attribute", "truth": "VIOLATED",
                    "reason": f"{cond['attr']} fails {cond['op']} "
                              f"{cond['value']!r} on the register",
                    "required_evidence": "the condition to be met, not more "
                                         "evidence — this is a fact",
                    "action": _ACTION.get(cid, "resolve the breach"),
                    "priority": 2.0,
                })
            elif (result.state is ResolutionState.RESOLVED
                  and cid in result.resolved_constraints):
                resolved.append(cid)
            else:
                unresolved.append(cid)
                native = next((g for g in result.gaps
                               if g.constraint_id == cid), None)
                gaps.append({
                    "constraint": cid, "label": cond["label"],
                    "kind": native.constraint_kind if native else "attribute",
                    "truth": "UNKNOWN",
                    "reason": (native.reason if native else
                               f"{cond['attr']} is not recorded for {p.id}"),
                    "required_evidence": (
                        native.required_evidence if native else
                        f"a value for {cond['attr']} from "
                        + (", ".join(sorted(cond["classes"]))
                           if cond["classes"] else "any admissible class")),
                    "action": _ACTION.get(cid, "capture the missing value"),
                    "priority": round(native.priority, 3) if native else 1.0,
                })

        # VIOLATED is a fact and blocks. UNKNOWN is ignorance and reviews.
        # Collapsing the two is precisely the distinction the spreadsheet
        # could not make, and getting it backwards in either direction is
        # expensive: a false BLOCKED stops billable work, a false PASS
        # releases it on evidence nobody has.
        verdict = ("BLOCKED" if violated
                   else "PASS" if not unresolved
                   else "REVIEW")

        out.append(GateOutcome(
            project=p.id,
            verdict=verdict,
            state=("UNRESOLVABLE" if violated
                   else "RESOLVED" if not unresolved else "PARTIAL"),
            coverage=round(len(resolved) / len(_CONDITIONS), 3),
            confidence=round(sum(confidences) / len(confidences), 3)
            if confidences else 0.0,
            resolved=resolved,
            violated=violated,
            unresolved=unresolved,
            gaps=sorted(gaps, key=lambda g: -g["priority"]),
        ))
    return out
