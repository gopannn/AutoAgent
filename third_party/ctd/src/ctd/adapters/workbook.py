"""Workbook adapter.

Reads `04_PROJECTS` and the exception queue out of the v10 master workbook,
builds an evidence-grounded target pattern per live project, runs the pre-mortem
against the incident library, and writes the result back as a new sheet.

The one design decision that matters
------------------------------------

The premise is built **only from what the register actually holds**. It would be
easy, and wrong, to assume a mechanism — to write `CAUSES(EXCESS(scope),
QUEUEING(stage))` because that is how rework usually starts — and then let the
engine "discover" the rest of a chain it was handed. That produces an impressive
demo and a worthless tool.

So every relation in a premise traces to a column:

    DEPENDS(designer, design-stage)                04_PROJECTS D
    INCREASES(QUEUEING(stage), days-in-stage)      AW, computed by the sheet
    INCOMPLETE(brief)                              AT < 100%
    BREACHES(design-stage)                         AY
    WITHHOLDS(client, cash)                        BP payment gate
    RETRIES(client, design-stage)                  Y, revisions logged
    DELAYED(days-in-stage)                         AD days idle
    EXCESS(workload)                               designer on >1 active project

and each carries its evidence class from 37_GOVERNANCE. Everything the engine
returns is what it added on top of that, classified MODEL_PREDICTED — advisory
only, per the workbook's own governance rule.

Why this is the right tool for this workbook specifically
---------------------------------------------------------

18_LEARNING gates every engine on `LN_MinProj` completed projects. The register
currently holds three active projects and **zero completed**, so every learning
engine reports its fallback and will keep doing so for months. Statistical
learning cannot start without history.

Structural transfer does not need the firm's history. It needs the failure
mechanism encoded once, from anywhere — and the firm already enumerated its
mechanisms on 32_EXCEPTION_QUEUE and 35_ENUMERATIONS. That is the gap this
fills, and it closes on its own as FIRM_ACTUAL history accumulates and starts
outranking the transferred cases.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..kernel import Knobs, R, Record, Rel, TargetPattern, TopoStore, run_premortem
from ..kernel.premortem import Prediction

from .incidents import INTERIORS_INCIDENTS
from .schema_interiors import (
    EVIDENCE_PLANE, EXCEPTION_PREDICATE, INTERIORS_SCHEMA,
)

__all__ = [
    "ProjectRow", "read_projects", "build_target", "premortem_for",
    "PredictedException", "to_exception_rows",
]

SHEET = "04_PROJECTS"
FIRST_ROW = 6

# 04_PROJECTS column letters -> the field this adapter needs.
COLS = {
    "id": "A", "client": "B", "designer": "D", "ops": "F",
    "status": "M", "district": "O", "property": "P", "carpet": "Q",
    "scope": "R", "style": "S", "tasks": "U", "revisions": "Y",
    "stage": "AA", "days_idle": "AD", "completeness": "AT",
    "missing": "AU", "days_in_stage": "AW", "sla": "AY",
    "slip": "BI", "gate_flag": "BF", "pay_gate": "BP",
}


@dataclass
class ProjectRow:
    row: int
    values: dict[str, Any]
    active_peers: int = 0            # other live projects on the same designer

    def get(self, key: str, default=None):
        v = self.values.get(key)
        return default if v is None else v

    def num(self, key: str, default: float = 0.0) -> float:
        v = self.values.get(key)
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    def text(self, key: str) -> str:
        v = self.values.get(key)
        return "" if v is None else str(v)

    @property
    def id(self) -> str:
        return self.text("id")

    @property
    def live(self) -> bool:
        return self.text("status") in ("Active", "On Hold", "Not Started")


def read_projects(path: str) -> list[ProjectRow]:
    """Cached values only. The workbook's formulas are the source of truth for
    everything derived, so reading them recomputed here would be a second,
    divergent implementation of numbers 33_SYSTEM_AUDIT already reconciles."""
    import openpyxl

    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    ws = wb[SHEET]
    out: list[ProjectRow] = []
    for r in range(FIRST_ROW, ws.max_row + 1):
        vals = {k: ws[f"{col}{r}"].value for k, col in COLS.items()}
        if not vals.get("id"):
            continue
        out.append(ProjectRow(r, vals))

    load: dict[str, int] = {}
    for p in out:
        if p.live:
            load[p.text("designer")] = load.get(p.text("designer"), 0) + 1
    for p in out:
        p.active_peers = max(0, load.get(p.text("designer"), 0) - 1)
    wb.close()
    return out


# --------------------------------------------------------------------------

@dataclass
class Premise:
    target: TargetPattern
    evidence: list[tuple[Rel, str, str]] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)

    def explain(self) -> str:
        lines = ["evidence in the premise (every line traces to a column):"]
        for rel, col, cls in self.evidence:
            lines.append(f"   {str(rel):<52} {SHEET} {col:<4} {cls}")
        if self.gaps:
            lines.append("not captured, so not asserted:")
            for g in self.gaps:
                lines.append(f"   {g}")
        return "\n".join(lines)


def build_target(p: ProjectRow) -> Premise:
    types = {
        "designer": "DESIGNER", "client": "CLIENT", "ops": "MANAGER",
        "design-stage": "STAGE", "brief": "BRIEF", "cash": "CASH",
        "schedule": "SCHEDULE", "workload": "WORKLOAD",
        "days-in-stage": "ELAPSED",
    }
    rels: list[Rel] = []
    ev: list[tuple[Rel, str, str]] = []
    gaps: list[str] = []

    def add(rel: Rel, col: str, cls: str) -> None:
        rels.append(rel)
        ev.append((rel, col, cls))

    # Structural anchors, always present in the register.
    add(R("DEPENDS", "designer", "design-stage"), "D", "SYSTEM_CALCULATED")
    add(R("DEPENDS", "ops", "cash"), "F/K", "SYSTEM_CALCULATED")

    # The sheet itself computes days-in-stage from the stage clock, so this
    # higher-order relation is evidence, not an assumed mechanism. It is also
    # what gives the aligner something of order 2 to bind against.
    add(R("INCREASES", R("QUEUEING", "design-stage"), "days-in-stage"),
        "AW", "SYSTEM_CALCULATED")

    if p.num("completeness", 1.0) < 1.0:
        add(R("INCOMPLETE", "brief"), "AT", "SYSTEM_CALCULATED")
    if p.text("sla").lower().startswith(("breach", "over")):
        add(R("BREACHES", "design-stage"), "AY", "SYSTEM_CALCULATED")
    if p.text("pay_gate").upper().startswith("BLOCKED"):
        add(R("WITHHOLDS", "client", "cash"), "BP", "SYSTEM_CALCULATED")
    if p.num("revisions") > 0:
        add(R("RETRIES", "client", "design-stage"), "Y", "STAFF_OBSERVED")
    if p.num("days_idle") >= 7:
        add(R("DELAYED", "days-in-stage"), "AD", "SYSTEM_CALCULATED")
    if p.active_peers >= 1:
        add(R("EXCESS", "workload"), "D", "SYSTEM_CALCULATED")
        add(R("DEPENDS", "designer", "schedule"), "D", "SYSTEM_CALCULATED")

    # Named absences. Stating what is NOT asserted is the difference between a
    # thin premise and a premise that quietly invented its own evidence.
    for key, label in (("district", "district"), ("property", "property type"),
                       ("carpet", "carpet area"), ("scope", "scope type"),
                       ("style", "style direction")):
        if not p.text(key) or p.text(key) == "None":
            gaps.append(f"{label} — 04_PROJECTS {COLS[key]} is empty")

    return Premise(
        TargetPattern(
            name=f"{p.id} · {p.text('client')}",
            domain=f"live-project::{p.id}",
            rels=tuple(rels),
            types=types,
        ),
        ev, gaps,
    )


# --------------------------------------------------------------------------

@dataclass
class PredictedException:
    project: str
    exception_type: str
    relation: str
    priority: float
    convergence: int
    severity: int
    precedent: str
    check: str
    conditional: bool
    evidence_class: str = "MODEL_PREDICTED"

    @property
    def plane(self) -> str:
        return EVIDENCE_PLANE[self.evidence_class][1]


def _exception_type(pred: Prediction) -> str:
    """Map a projected relation back onto the firm's own exception taxonomy.

    Classify on the CONSEQUENT, not the outermost predicate. A first pass
    matched breadth-first from the root, so every `CAUSES(...)` relation
    classified as SEQUENCE_VIOLATION regardless of what it actually predicted
    and six distinct findings arrived under one label. What a reviewer needs to
    know is what the chain ends in.
    """
    inverse: dict[str, str] = {}
    for exc, predicate in EXCEPTION_PREDICATE.items():
        if predicate != "CAUSES":
            inverse.setdefault(predicate, exc)

    def consequent(r: Rel) -> Rel:
        if r.pred in ("CAUSES", "PRECEDES", "PREVENTS") and len(r.args) == 2:
            tail = r.args[1]
            return consequent(tail) if isinstance(tail, Rel) else r
        return r

    end = consequent(pred.rel)
    if end.pred in inverse:
        return inverse[end.pred]
    for r in [end] + [a for a in pred.rel.args if isinstance(a, Rel)]:
        if r.pred in inverse:
            return inverse[r.pred]
    return f"UNCLASSIFIED ({end.pred})"


def premortem_for(p: ProjectRow, extra_library=(), knobs: Knobs | None = None,
                  limit: int = 8) -> tuple[Premise, list[Prediction]]:
    premise = build_target(p)
    store = TopoStore()
    for rec in list(INTERIORS_INCIDENTS) + list(extra_library):
        store.ingest(Record(id=rec.id, domain=rec.domain,
                            attrs=dict(rec.attrs), types=dict(rec.types),
                            rels=rec.rels, text=rec.text))
    k = knobs or Knobs(reach=1.0, mac_keep=6, min_depth=2, rounds=2, beam=8)
    rounds = run_premortem(store, premise.target, k, enrich_top=3,
                           schema=INTERIORS_SCHEMA)
    preds: list[Prediction] = []
    seen: set[Rel] = set()
    for rr in rounds:
        for pr in rr.predictions:
            if pr.rel not in seen:
                seen.add(pr.rel)
                preds.append(pr)
    return premise, preds[:limit]


def to_exception_rows(project: str,
                      preds: list[Prediction]) -> list[PredictedException]:
    return [
        PredictedException(
            project=project,
            exception_type=_exception_type(pr),
            relation=str(pr.rel),
            priority=round(pr.priority, 1),
            convergence=pr.convergence,
            severity=pr.severity,
            precedent=", ".join(sorted(pr.domains)),
            check=pr.check or "no check available — treat as a lead",
            conditional=pr.conditional,
        )
        for pr in preds
    ]
