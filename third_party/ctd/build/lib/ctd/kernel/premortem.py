"""Pre-mortem — failure-mode projection.

Same aligner as TRANSFER, pointed at a different job. Given a proposed design
and a library of incidents from structurally distant domains, project the
failure modes those incidents would produce here.

Why this job rather than solution generation:

  testable     "This will oscillate if the signal lags" is something you go and
               check. A projected *solution* is not checkable, so a wrong one
               costs a quarter. A wrong prediction costs an afternoon.
  the library  This does not need to be built. Postmortems, incident reports
  exists       and audit logs already are the corpus.
  cheap wrong  A false prediction is discarded after one check, which is what
               makes an unbounded-error generator safe to deploy at all.

Every prediction carries a CHECK: what to measure to confirm or refute it. A
prediction without a check is not a deliverable, it is a vibe.

The change that matters in v5: **assumption tracking**
------------------------------------------------------

v4 ran multiple rounds, enriching the design with its own top predictions
between rounds, and then printed round 2's output in the same format and with
the same authority as round 1's. But a round-2 prediction derived from a
mapping that used a round-1 *prediction* is conditional: it holds only if the
earlier, unverified guess holds. A reviewer working down the checklist had no
way to see that, and would spend real engineering time checking a prediction
whose premise had already been refuted two lines above.

Each prediction now records exactly which enrichments its mapping actually
rested on — determined from the mapping, not assumed from the round number —
is marked CONDITIONAL when that set is non-empty, and is discounted
accordingly. Check the premises first; the tree collapses if they fail.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .schema import CORE_SCHEMA, Schema
from .store import Record, Rel, TopoStore
from .transfer import (
    Blocked, Knobs, TargetPattern, _Aligner, _shortlist, link_conflicts,
    project, subsumed_relations,
)

__all__ = [
    "Prediction", "PremortemRound", "run_premortem", "report", "check_for",
]


def check_for(rel: Rel, templates: dict[str, str]) -> str | None:
    """Resolve a check procedure for a predicted relation.

    Order-1 predictions (SATURATES, AMPLIFIES, FAILS) carry their own check.
    Higher-order causal predictions inherit the check of their consequent,
    which is the part you can actually instrument.
    """
    if rel.pred in templates:
        args = [str(a) for a in rel.args]
        try:
            return templates[rel.pred].format(*args)
        except (IndexError, KeyError):
            return templates[rel.pred]
    for a in reversed(rel.args):
        if isinstance(a, Rel):
            sub = check_for(a, templates)
            if sub:
                return sub
    return None


@dataclass
class Prediction:
    rel: Rel
    sources: list[str] = field(default_factory=list)
    domains: set[str] = field(default_factory=set)
    fingerprints: set[str] = field(default_factory=set)
    groups: set[str] = field(default_factory=set)
    systematicity: float = 0.0
    looseness: float = 0.0
    severity: int = 0               # worst observed severity among sources
    check: str | None = None
    round_index: int = 1
    subsumed: bool = False
    assumptions: tuple[Rel, ...] = ()
    conflicts: list[Rel] = field(default_factory=list)

    @property
    def convergence(self) -> int:
        """Independent precedents projecting the same prediction.

        Counted as the smaller of (distinct domains, distinct structure
        fingerprints). Two incidents that are the same story about different
        nouns, filed under different domain labels, would otherwise
        manufacture agreement — and convergence is the strongest signal this
        engine emits, so it is the one most worth protecting.
        """
        counts = [len(self.domains)]
        if self.fingerprints:
            counts.append(len(self.fingerprints))
        if self.groups:
            counts.append(len(self.groups))
        return min(counts)

    @property
    def conditional(self) -> bool:
        return bool(self.assumptions)

    @property
    def structural_score(self) -> float:
        """Structural soundness only. A proxy for whether the transfer is
        well-founded, NOT for whether the failure will occur."""
        return (self.systematicity
                * (1.0 + 0.75 * (self.convergence - 1))
                * (1.0 + 0.5 * (self.rel.order - 1))
                * (1.0 - 0.35 * self.looseness))

    @property
    def confidence_factor(self) -> float:
        """Discount for resting on unverified premises. One assumption halves
        the priority, two thirds it. Reported separately so the discount is
        visible rather than baked invisibly into a single number."""
        return 1.0 / (1.0 + len(self.assumptions))

    @property
    def priority(self) -> float:
        """Structural soundness, weighted by severity actually observed when
        this failure happened elsewhere, discounted by how much unverified
        inference it rests on.

        Severity is real data from the incident record; the structural term is
        a proxy; the confidence factor is bookkeeping. Keeping all three
        multiplied but separately reported is what stops the proxy from being
        mistaken for evidence.
        """
        return (self.structural_score * max(self.severity, 1)
                * self.confidence_factor)

    @property
    def checkable(self) -> bool:
        return self.check is not None


@dataclass
class PremortemRound:
    index: int
    considered: list[tuple[str, float, float, str]]   # id, mac, syst, note
    predictions: list[Prediction]
    alignments: int
    blocked: list[Blocked] = field(default_factory=list)
    ms: float = 0.0


def run_premortem(
    store: TopoStore,
    design: TargetPattern,
    knobs: Knobs | None = None,
    enrich_top: int = 3,
    schema: Schema = CORE_SCHEMA,
    enrich_requires_check: bool = False,
) -> list[PremortemRound]:
    """Rounds of retrieve -> align -> project -> enrich.

    Enrichment adds the top `enrich_top` predictions rather than only the best
    one. Unlike TRANSFER this is not converging on a single answer — it is
    accumulating a failure picture, and a partial failure chain in the target
    is what makes deeper incidents reachable on the next pass.

    Set `enrich_requires_check=True` to refuse to build on a prediction nobody
    can go and verify. That is the stricter setting and costs reach.
    """
    k = knobs or Knobs()
    aligner = _Aligner(schema, k)
    rounds: list[PremortemRound] = []
    current = design
    seen: set[Rel] = set()
    assumed: set[Rel] = set()          # enrichments, i.e. unverified premises

    for i in range(1, k.rounds + 1):
        t0 = time.perf_counter()
        pool: dict[Rel, Prediction] = {}
        considered: list[tuple[str, float, float, str]] = []
        blocked_all: list[Blocked] = []

        shortlist, macs = _shortlist(
            store, current, k,
            predicate=lambda r: bool(r.attrs.get("incident")))

        for rec in shortlist:
            mappings = aligner.run(rec, current)[:k.mappings_per_case]
            m = mappings[0]
            note = ""
            if m.depth < k.min_depth:
                # Accurate wording: the RECORD may well have higher-order
                # structure; what is missing is a higher-order MATCH.
                note = "rejected: no higher-order match with this design"
            elif m.systematicity < (1.0 - k.reach) * 12.0:
                note = "rejected: below reach floor"
            elif m.kin_matches:
                note = "kin match: " + ", ".join(
                    f"{a}~{b}" for a, b in m.kin_matches)
            considered.append((rec.id, macs[rec.id], m.systematicity, note))
            if note.startswith("rejected"):
                continue

            templates = rec.attrs.get("checks", {}) or {}
            group = str(rec.attrs.get("independence_group")
                        or rec.structure_fingerprint())

            for mp in mappings:
                if mp.depth < k.min_depth:
                    continue
                # Which unverified enrichments did THIS mapping actually use?
                # Derived from the mapping rather than inferred from the round
                # number, so a later-round prediction that happens to rest only
                # on given structure is correctly reported as unconditional.
                used = tuple(sorted(
                    (r for r in mp.rel_map.values() if r in assumed), key=str))
                admitted, blocked = project(mp, current, k, schema)
                blocked_all.extend(blocked)
                for r in admitted:
                    if r in seen:
                        continue
                    p = pool.get(r)
                    if p is None:
                        p = Prediction(r, round_index=i, assumptions=used)
                        pool[r] = p
                    elif len(used) < len(p.assumptions):
                        # The weakest premise set wins: if any source derived
                        # this without assumptions, it is unconditional.
                        p.assumptions = used
                    if rec.id not in p.sources:
                        p.sources.append(rec.id)
                    p.domains.add(rec.domain)
                    p.fingerprints.add(rec.structure_fingerprint())
                    p.groups.add(group)
                    if mp.systematicity > p.systematicity:
                        p.systematicity = mp.systematicity
                        p.looseness = mp.looseness
                    p.severity = max(p.severity,
                                     int(rec.attrs.get("severity", 1)))
                    if p.check is None:
                        p.check = check_for(r, templates)

        sub = (subsumed_relations(pool) if k.demote_subsumed else set())
        for pr in pool.values():
            pr.subsumed = pr.rel in sub
        ranked = sorted(pool.values(),
                        key=lambda p: (p.subsumed, -p.priority, str(p.rel)))
        link_conflicts(ranked, current, schema)
        rounds.append(PremortemRound(i, considered, ranked, len(shortlist),
                                     blocked_all,
                                     (time.perf_counter() - t0) * 1000))
        if not ranked:
            break

        promoted = [p for p in ranked
                    if p.checkable or not enrich_requires_check][:enrich_top]
        for p in promoted:
            seen.add(p.rel)
            assumed.add(p.rel)
            current = current.with_rel(p.rel)

    return rounds


# --------------------------------------------------------------------------

def report(rounds: list[PremortemRound], limit: int = 6,
           show_blocked: bool = True) -> str:
    """Reviewable output: what to check, in what order, and why."""
    out: list[str] = []
    for rr in rounds:
        out.append(f"\nROUND {rr.index}  "
                   f"({rr.alignments} alignments, {rr.ms:.1f}ms)")
        for rid, mac, syst, note in rr.considered:
            tail = f"  {note}" if note else ""
            out.append(f"    {rid:<24} mac={mac:.2f} syst={syst:5.1f}{tail}")
        if show_blocked and rr.blocked:
            out.append("    projections refused by the type guard:")
            shown = {str(b.rel): b for b in rr.blocked}
            for b in list(shown.values())[:3]:
                out.append(f"      {b.rel}  --  {b.reason}")
        if not rr.predictions:
            out.append("    no admissible predictions")
            continue
        out.append("")
        for i, p in enumerate(rr.predictions[:limit], 1):
            flag = "  [CONDITIONAL]" if p.conditional else ""
            if p.subsumed:
                flag += "  [contained in a finding above]"
            out.append(f"  {i}. {p.rel}{flag}")
            out.append(f"     priority={p.priority:6.1f}  "
                       f"structural={p.structural_score:5.1f}  "
                       f"convergence={p.convergence}  severity={p.severity}"
                       + (f"  confidence x{p.confidence_factor:.2f}"
                          if p.conditional else ""))
            out.append(f"     precedent: {', '.join(sorted(p.domains))} "
                       f"({', '.join(p.sources)})")
            if p.conditional:
                out.append("     HOLDS ONLY IF: " +
                           "; ".join(str(a) for a in p.assumptions))
            if p.conflicts:
                out.append("     CONFLICTS WITH: " +
                           "; ".join(str(c) for c in p.conflicts) +
                           "  — at most one of these can hold")
            if p.check:
                out.append(f"     CHECK: {p.check}")
            else:
                out.append("     CHECK: none available — treat as a lead, "
                           "not a finding")
            out.append("")
    return "\n".join(out)
