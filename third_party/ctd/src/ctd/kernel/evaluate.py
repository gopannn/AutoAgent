"""Evaluation — the piece v4 was missing.

v4's own README said it plainly: *"Not measured. Accuracy."* Every number it
reported was a cost number (model calls, records read, probes) or a proxy
(systematicity, convergence). Cost numbers are real and they are worth
reporting, but a projection engine whose output quality has never been measured
is a generator, not a system.

This module supplies the gold label the README said was needed, using the only
honest source available: **the incidents themselves**.

Held-out-tail protocol
----------------------

For each incident in the library:

  1. Split its relations into a *premise* (the structural anchors plus the
     opening of the causal chain) and a *tail* (everything downstream).
  2. Restate the premise as a design under review, relabelled into a domain
     that appears nowhere in the library so the reach filter cannot retrieve
     the source of the answer.
  3. Remove that incident from the library entirely.
  4. Run the pre-mortem and ask whether the held-out tail relations appear in
     the ranked predictions, and where.

The tail is ground truth in the only sense that matters here: it is what
actually happened in that incident, and it was withheld. This measures whether
structural transfer from *other* domains reconstructs a causal chain it was
never shown.

What it does and does not establish
-----------------------------------

It measures the mechanism on this corpus. It does not measure real-world
incident prediction, and it cannot: the corpus is small, hand-encoded, and the
encodings share a vocabulary by construction. Two controls are included
specifically to stop the headline number from flattering itself:

  frequency   ignores structure entirely and predicts the most common
              predicates in the library, filled with role-compatible target
              entities. If the engine cannot beat this, the alignment is
              decorative.
  random      the same candidate generator, shuffled under a fixed seed. The
              floor.

And every guard is ablated, because a guard that changes no number is a guard
that should be deleted rather than described.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from itertools import permutations
from typing import Callable, Iterable, Mapping, Sequence

from .premortem import Prediction, run_premortem
from .query import Closure, Outcome
from .schema import CORE_SCHEMA, Schema
from .store import Record, Rel, TopoStore, walk
from .transfer import Knobs, TargetPattern

__all__ = [
    "HeldOut", "split_record", "QueryResult", "AblationResult",
    "evaluate_library", "run_ablations", "format_ablations",
    "score_closures", "ClosureScorecard",
    "GuardProbe", "guard_probe", "format_guard_probe",
    "PerturbationArm", "run_perturbations", "format_perturbations",
]

ANCHORS = {"DEPENDS", "SHARED", "FLOWS", "CONTAINS"}


# --------------------------------------------------------------------------
# Splitting
# --------------------------------------------------------------------------

@dataclass
class HeldOut:
    record_id: str
    true_domain: str
    premise: TargetPattern
    tail: list[Rel]

    @property
    def usable(self) -> bool:
        """A split is usable only if the premise has higher-order structure to
        align against and the tail has something left to predict."""
        return (bool(self.tail)
                and max((r.order for r in self.premise.all_rels()),
                        default=0) >= 2)


def split_record(rec: Record, opening: int = 2) -> HeldOut:
    """Deterministic premise/tail split.

    premise = every structural anchor, plus the first `opening` non-anchor
    top-level relations in author order.
    tail    = the remaining top-level relations.

    Anchors go in the premise because without them there is no entity binding
    at all and the task becomes impossible rather than hard. The opening
    non-anchor relations go in because a design under review always states
    *some* mechanism; predicting a failure chain from a bare dependency graph
    is a different and much weaker experiment.
    """
    anchors = [r for r in rec.rels if r.pred in ANCHORS]
    rest = [r for r in rec.rels if r.pred not in ANCHORS]
    premise_rels = tuple(anchors + rest[:opening])
    tail = list(rest[opening:])
    return HeldOut(
        record_id=rec.id,
        true_domain=rec.domain,
        premise=TargetPattern(
            name=f"held-out premise of {rec.id}",
            domain=f"heldout::{rec.domain}",
            rels=premise_rels,
            types=dict(rec.types),
        ),
        tail=tail,
    )


# --------------------------------------------------------------------------
# Ranking metrics
# --------------------------------------------------------------------------

def _flatten(rounds) -> list[Rel]:
    """Predictions across all rounds, best rank first, de-duplicated.

    Rounds are concatenated rather than re-sorted: a round-2 prediction is
    conditional on round 1's enrichment, so promoting it above an
    unconditional round-1 prediction on raw score would misrepresent what a
    reviewer should check first.
    """
    out: list[Rel] = []
    seen: set[Rel] = set()
    for rr in rounds:
        for p in rr.predictions:
            if p.rel not in seen:
                seen.add(p.rel)
                out.append(p.rel)
    return out


@dataclass
class QueryResult:
    record_id: str
    n_tail: int
    ranked: list[Rel]
    hits: dict[int, int] = field(default_factory=dict)   # k -> tail rels in top-k
    first_hit_rank: int | None = None
    n_tail_deep: int = 0                 # tail relations of order >= 2
    deep_hits: dict[int, int] = field(default_factory=dict)
    covered: dict[int, int] = field(default_factory=dict)
    kin_hits: dict[int, int] = field(default_factory=dict)
    violations: int = 0                  # emitted relations that break the schema

    def recall_at(self, k: int) -> float:
        return self.hits.get(k, 0) / self.n_tail if self.n_tail else 0.0

    def coverage_at(self, k: int) -> float:
        vals = [q.coverage_at(k) for q in self.per_query]
        return sum(vals) / len(vals) if vals else 0.0

    def deep_recall_at(self, k: int) -> float:
        return (self.deep_hits.get(k, 0) / self.n_tail_deep
                if self.n_tail_deep else 0.0)

    def coverage_at(self, k: int) -> float:
        """Recall crediting containment.

        Confirming `CAUSES(AMPLIFIES(x, y), SATURATES(y))` confirms
        `SATURATES(y)`. Plain recall counts them as two independent wins,
        which rewards a ranking for restating its own findings. This credits a
        tail relation whenever the top k contains it OR contains something it
        is nested inside.
        """
        return self.covered.get(k, 0) / self.n_tail if self.n_tail else 0.0

    def kin_recall_at(self, k: int) -> float:
        return self.kin_hits.get(k, 0) / self.n_tail if self.n_tail else 0.0

    @property
    def violation_rate(self) -> float:
        return self.violations / len(self.ranked) if self.ranked else 0.0

    @property
    def rr(self) -> float:
        return 1.0 / self.first_hit_rank if self.first_hit_rank else 0.0


def _measure(held: HeldOut, ranked: list[Rel], ks: Sequence[int],
             schema: Schema = CORE_SCHEMA) -> QueryResult:
    truth = set(held.tail)
    deep_truth = {r for r in truth if r.order >= 2}
    res = QueryResult(held.record_id, len(truth), ranked,
                      n_tail_deep=len(deep_truth))
    for k in ks:
        res.hits[k] = sum(1 for r in ranked[:k] if r in truth)
        res.deep_hits[k] = sum(1 for r in ranked[:k] if r in deep_truth)
        res.kin_hits[k] = sum(
            1 for t in truth
            if any(kin_equal(r, t, schema) for r in ranked[:k]))
        reachable: set[Rel] = set()
        for r in ranked[:k]:
            reachable.update(walk([r]))
        res.covered[k] = sum(1 for t in truth if t in reachable)
    for i, r in enumerate(ranked, 1):
        if r in truth:
            res.first_hit_rank = i
            break
    # Objective, label-free safety measure: an emitted relation that violates
    # its own predicate signature in the target's type system is nonsense
    # regardless of whether the tail happens to contain it.
    res.violations = sum(
        1 for r in ranked
        if any(schema.check_args(part.pred, part.args, held.premise.types)
               for part in walk([r])))
    return res


# --------------------------------------------------------------------------
# Controls
# --------------------------------------------------------------------------

def _frequency_candidates(library: Sequence[Record], target: TargetPattern,
                          schema: Schema, cap: int = 200) -> list[Rel]:
    """Structure-blind baseline.

    Counts predicate frequency across the library, then instantiates each
    predicate with whatever target entities its signature permits, and finally
    builds order-2 CAUSES relations over the most frequent order-1 results.
    Ranked by frequency alone: no alignment, no mapping, no structure.
    """
    freq: dict[str, int] = {}
    for rec in library:
        for r in rec.all_rels():
            freq[r.pred] = freq.get(r.pred, 0) + 1

    ents = sorted(target.entities())
    existing = set(target.all_rels())
    order1: list[tuple[Rel, int]] = []

    for pred, n in sorted(freq.items(), key=lambda kv: (-kv[1], kv[0])):
        sig = schema.signature(pred)
        if sig is None or any(not spec.entity_ok for spec in sig.args):
            continue
        for combo in permutations(ents, sig.arity):
            problems = schema.check_args(pred, combo, target.types)
            if problems:
                continue
            rel = Rel(pred, tuple(combo))
            if rel not in existing:
                order1.append((rel, n))

    order1.sort(key=lambda p: (-p[1], str(p[0])))
    out = [r for r, _ in order1]

    causal = freq.get("CAUSES", 0)
    if causal:
        heads = out[:8]
        pairs = [(Rel("CAUSES", (a, b)), causal)
                 for a in heads for b in heads if a != b]
        out.extend(r for r, _ in pairs)
    return out[:cap]


def _random_candidates(library, target, schema, seed: int) -> list[Rel]:
    cands = _frequency_candidates(library, target, schema)
    rng = random.Random(seed)
    rng.shuffle(cands)
    return cands


# --------------------------------------------------------------------------
# Arms
# --------------------------------------------------------------------------

@dataclass
class AblationResult:
    name: str
    note: str
    per_query: list[QueryResult]
    ks: tuple[int, ...]

    def recall_at(self, k: int) -> float:
        vals = [q.recall_at(k) for q in self.per_query]
        return sum(vals) / len(vals) if vals else 0.0

    def hit_rate_at(self, k: int) -> float:
        """Fraction of queries with at least one tail relation in the top k."""
        if not self.per_query:
            return 0.0
        return sum(1 for q in self.per_query if q.hits.get(k, 0) > 0) / len(
            self.per_query)

    @property
    def mrr(self) -> float:
        vals = [q.rr for q in self.per_query]
        return sum(vals) / len(vals) if vals else 0.0

    @property
    def mean_candidates(self) -> float:
        vals = [len(q.ranked) for q in self.per_query]
        return sum(vals) / len(vals) if vals else 0.0

    def coverage_at(self, k: int) -> float:
        vals = [q.coverage_at(k) for q in self.per_query]
        return sum(vals) / len(vals) if vals else 0.0

    def kin_hit_rate_at(self, k: int) -> float:
        """Hit rate crediting family substitution."""
        if not self.per_query:
            return 0.0
        return sum(1 for q in self.per_query
                   if q.kin_hits.get(k, 0) > 0) / len(self.per_query)

    def deep_recall_at(self, k: int) -> float:
        vals = [q.deep_recall_at(k) for q in self.per_query if q.n_tail_deep]
        return sum(vals) / len(vals) if vals else 0.0

    def deep_hit_rate_at(self, k: int) -> float:
        rows = [q for q in self.per_query if q.n_tail_deep]
        if not rows:
            return 0.0
        return sum(1 for q in rows if q.deep_hits.get(k, 0) > 0) / len(rows)

    @property
    def violation_rate(self) -> float:
        """Fraction of emitted predictions that violate their own predicate
        signature under the target's declared types. Needs no gold label, so
        it measures the guards directly rather than through recall."""
        tot = sum(len(q.ranked) for q in self.per_query)
        bad = sum(q.violations for q in self.per_query)
        return bad / tot if tot else 0.0


def _build_store(records: Iterable[Record], exclude: str) -> TopoStore:
    st = TopoStore()
    for r in records:
        if r.id == exclude:
            continue
        st.ingest(Record(id=r.id, domain=r.domain, attrs=dict(r.attrs),
                         rels=r.rels, types=dict(r.types), text=r.text))
    return st


def evaluate_library(
    library: Sequence[Record],
    name: str,
    note: str = "",
    knobs: Knobs | None = None,
    schema: Schema = CORE_SCHEMA,
    ks: Sequence[int] = (1, 3, 5, 10),
    opening: int = 2,
    enrich_top: int = 3,
    ranker: Callable[[Sequence[Record], HeldOut], list[Rel]] | None = None,
) -> AblationResult:
    """Leave-one-out over every incident with a usable split.

    `ranker` overrides the engine entirely, which is how the frequency and
    random controls are run through exactly the same protocol and metrics as
    the engine arms. A control evaluated by a different harness proves nothing.
    """
    k = knobs or Knobs()
    incidents = [r for r in library if r.attrs.get("incident") and r.rels]
    results: list[QueryResult] = []

    for rec in incidents:
        held = split_record(rec, opening=opening)
        if not held.usable:
            continue
        others = [r for r in incidents if r.id != rec.id]
        if ranker is not None:
            ranked = ranker(others, held)
        else:
            store = _build_store(others, exclude=rec.id)
            rounds = run_premortem(store, held.premise, k,
                                   enrich_top=enrich_top, schema=schema)
            ranked = _flatten(rounds)
        results.append(_measure(held, ranked, ks, schema))

    return AblationResult(name, note, results, tuple(ks))


def run_ablations(library: Sequence[Record],
                  schema: Schema = CORE_SCHEMA,
                  ks: Sequence[int] = (1, 3, 5, 10),
                  base: Knobs | None = None) -> list[AblationResult]:
    """Every arm on the identical protocol, identical splits, identical metric."""
    base = base or Knobs(reach=1.0, mac_keep=6, min_depth=2, rounds=2,
                         beam=8, aligner="beam")

    def variant(**kw) -> Knobs:
        d = dict(vars(base))
        d.update(kw)
        return Knobs(**d)

    arms: list[AblationResult] = []

    arms.append(evaluate_library(
        library, "full", "beam=8, types on, kinship on, projection guarded",
        base, schema, ks))

    arms.append(evaluate_library(
        library, "greedy aligner", "v4 behaviour: commit deepest, never revisit",
        variant(aligner="greedy", beam=1), schema, ks))

    arms.append(evaluate_library(
        library, "no entity types", "role compatibility not enforced",
        variant(use_types=False), schema, ks))

    arms.append(evaluate_library(
        library, "no kinship", "identical predicates only",
        variant(use_kinship=False), schema, ks))

    arms.append(evaluate_library(
        library, "no projection guard", "emit whatever substitutes cleanly",
        variant(guard_projection=False), schema, ks))

    arms.append(evaluate_library(
        library, "single round", "no enrichment loop",
        variant(rounds=1), schema, ks))

    arms.append(evaluate_library(
        library, "flat ranking", "no subsumption demotion (v5.0 behaviour)",
        variant(demote_subsumed=False), schema, ks))

    arms.append(evaluate_library(
        library, "frequency control", "no structure: rank by predicate frequency",
        base, schema, ks,
        ranker=lambda others, held: _frequency_candidates(
            others, held.premise, schema)))

    arms.append(evaluate_library(
        library, "random control", "same candidates, shuffled (seed 0)",
        base, schema, ks,
        ranker=lambda others, held: _random_candidates(
            others, held.premise, schema, 0)))

    return arms


def format_ablations(arms: Sequence[AblationResult]) -> str:
    if not arms:
        return "no arms evaluated"
    ks = arms[0].ks
    last = ks[-1]
    head = (f"{'arm':<22}{'n':>4}" +
            "".join(f"{'hit@' + str(k):>7}" for k in ks) +
            f"{'rec@' + str(last):>8}{'cov@' + str(last):>8}"
            f"{'deep@' + str(last):>8}{'dRec':>7}{'MRR':>7}"
            f"{'viol':>7}{'cands':>7}")
    lines = [head, "-" * len(head)]
    for a in arms:
        lines.append(
            f"{a.name:<22}{len(a.per_query):>4}" +
            "".join(f"{a.hit_rate_at(k):>7.2f}" for k in ks) +
            f"{a.recall_at(last):>8.2f}{a.coverage_at(last):>8.2f}"
            f"{a.deep_hit_rate_at(last):>8.2f}{a.deep_recall_at(last):>7.2f}"
            f"{a.mrr:>7.2f}{a.violation_rate:>7.2f}"
            f"{a.mean_candidates:>7.0f}")
    lines.append("")
    lines.append("hit@k    fraction of held-out incidents with at least one true")
    lines.append("         tail relation in the top k predictions")
    lines.append(f"rec@{last}   fraction of ALL held-out tail relations recovered")
    lines.append(f"cov@{last}   recall crediting containment: a tail relation counts")
    lines.append("         if the top k contains it or contains something it nests in")
    lines.append(f"deep@{last}  same as hit, restricted to tail relations of order>=2")
    lines.append("dRec     recall restricted to order>=2 tail relations")
    lines.append("MRR      mean reciprocal rank of the first true relation")
    lines.append("viol     fraction of emitted predictions that violate their own")
    lines.append("         predicate signature -- no gold label needed, so this is")
    lines.append("         what actually measures the safety guards")
    lines.append("cands    mean number of predictions produced per query")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLOSE scorecard
# --------------------------------------------------------------------------

@dataclass
class ClosureScorecard:
    """Three numbers that cannot game each other.

    v4's 'Topological Accuracy' — percentage of outputs achieving a perfect
    structural match — is a closure-rate metric wearing an accuracy label. A
    system that closes confidently on the wrong record scores 100%.
    """
    answerable: int = 0
    answered: int = 0
    correct: int = 0
    unanswerable: int = 0
    correctly_refused: int = 0

    @property
    def closure_rate(self) -> float:
        return self.answered / self.answerable if self.answerable else 0.0

    @property
    def precision(self) -> float:
        return self.correct / self.answered if self.answered else 0.0

    @property
    def abstention_correctness(self) -> float:
        return (self.correctly_refused / self.unanswerable
                if self.unanswerable else 0.0)

    def __str__(self) -> str:
        return (f"closure rate            {self.closure_rate:.0%}  "
                f"({self.answered}/{self.answerable})\n"
                f"precision | closure     {self.precision:.0%}  "
                f"({self.correct}/{self.answered})\n"
                f"abstention correctness  {self.abstention_correctness:.0%}  "
                f"({self.correctly_refused}/{self.unanswerable})")


def score_closures(results: Sequence[tuple[Closure, str | None]]
                   ) -> ClosureScorecard:
    """`results` pairs each closure with the id it should have returned, or
    None when the query is genuinely unanswerable."""
    sc = ClosureScorecard()
    for c, expected in results:
        if expected is None:
            sc.unanswerable += 1
            if c.outcome in (Outcome.REQUEST, Outcome.ABSTAIN):
                sc.correctly_refused += 1
        else:
            sc.answerable += 1
            if c.answered:
                sc.answered += 1
                if expected in c.ids:
                    sc.correct += 1
    return sc


# --------------------------------------------------------------------------
# Guard probe
# --------------------------------------------------------------------------

@dataclass
class GuardProbe:
    """Direct measurement of the safety guards.

    The held-out-tail study cannot see the type and projection guards at all,
    and reporting "the ablation changed nothing" without saying why would be
    misleading. The reason is that every incident in the library is encoded
    with the same four roles lined up the same way, so the guards never have an
    incompatible binding to refuse. That is a property of the corpus, not
    evidence that the guards are inert.

    This probe supplies the missing case explicitly: a target whose entity
    granularity does NOT match the library's, which is the documented dominant
    failure mode — the early build of this engine projected
    `SENSES(vehicles, travel-time)`, i.e. cars sensing their own journey time,
    because a greedy aligner bound an agent to a resource on a shallow match.

    Reported as counts, not a rate, because the question is binary: does the
    guard refuse the nonsense binding or not.
    """
    arm: str
    emitted: int
    schema_violations: int
    refused_by_guard: int

    @property
    def clean(self) -> bool:
        return self.schema_violations == 0


def guard_probe(library: Sequence[Record],
                schema: Schema = CORE_SCHEMA,
                base: Knobs | None = None) -> list[GuardProbe]:
    """Run one deliberately mis-typed target through each guard configuration."""
    base = base or Knobs(reach=1.0, mac_keep=6, min_depth=2, rounds=1, beam=8)

    # Entity granularity is wrong on purpose: `vehicles` is the moving resource,
    # not the deciding agent, and `congestion` is a load rather than a signal.
    # A correct engine must refuse to let a source AGENT bind to `vehicles`.
    mistyped = TargetPattern(
        name="traffic corridor, entities at the wrong granularity",
        domain="traffic",
        types={"vehicles": "RESOURCE", "corridor": "RESOURCE",
               "congestion": "LOAD", "travel-time": "SIGNAL"},
        rels=(
            Rel("DEPENDS", ("vehicles", "corridor")),
            Rel("CAUSES", (Rel("EXCESS", ("congestion",)),
                           Rel("QUEUEING", ("corridor",)))),
            Rel("INCREASES", (Rel("QUEUEING", ("corridor",)), "travel-time")),
        ),
    )

    store = TopoStore()
    for r in library:
        store.ingest(Record(id=r.id, domain=r.domain, attrs=dict(r.attrs),
                            rels=r.rels, types=dict(r.types), text=r.text))

    out: list[GuardProbe] = []
    for name, kw in (("both guards on", {}),
                     ("type guard off", {"use_types": False}),
                     ("projection guard off", {"guard_projection": False}),
                     ("both guards off", {"use_types": False,
                                          "guard_projection": False})):
        d = dict(vars(base))
        d.update(kw)
        knobs = Knobs(**d)
        rounds = run_premortem(store, mistyped, knobs, enrich_top=3,
                               schema=schema)
        ranked = _flatten(rounds)
        refused = sum(len(rr.blocked) for rr in rounds)
        bad = sum(1 for r in ranked
                  if schema.check_args(r.pred, r.args, mistyped.types))
        out.append(GuardProbe(name, len(ranked), bad, refused))
    return out


def format_guard_probe(rows: Sequence[GuardProbe]) -> str:
    head = f"{'configuration':<24}{'emitted':>9}{'nonsense':>10}{'refused':>9}"
    lines = [head, "-" * len(head)]
    for r in rows:
        lines.append(f"{r.arm:<24}{r.emitted:>9}{r.schema_violations:>10}"
                     f"{r.refused_by_guard:>9}")
    lines.append("")
    lines.append("nonsense = emitted relations that violate their own predicate")
    lines.append("           signature under the target's declared entity roles")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Perturbation study
# --------------------------------------------------------------------------

@dataclass
class PerturbationArm:
    name: str
    note: str
    result: "AblationResult"
    invariant: bool = False       # this arm SHOULD score identically


def _kin_swap(rel: Rel, types: Mapping[str, str], schema: Schema) -> Rel:
    """Rewrite a predicate to an ADMISSIBLE family sibling, recursively.

    Two constraints make this narrower than it first looks, and both are worth
    knowing before reading the result:

    * anchors (DEPENDS, SHARED, FLOWS, CONTAINS) and CAUSES are deliberately
      family-less, so they never move;
    * most family siblings carry different argument signatures -- INCREASES
      takes a relation, AMPLIFIES takes a load -- so a swap that type-checks is
      the exception rather than the rule.

    A first version tried only the alphabetically first sibling and reverted on
    any signature failure, which meant it changed nothing at all on this corpus
    and the arm reported a perfect score while performing no perturbation. It
    now tries every sibling and keeps the first admissible one.
    """
    args = tuple(_kin_swap(a, types, schema) if isinstance(a, Rel) else a
                 for a in rel.args)
    family = schema.family_of(rel.pred)
    if family:
        for alt in sorted(p for p in schema.vocabulary
                          if schema.family_of(p) == family and p != rel.pred):
            sig = schema.signature(alt)
            if sig is None or sig.arity != len(args):
                continue
            if not schema.check_args(alt, args, types):
                return Rel(alt, args)
    return Rel(rel.pred, args)


def _kin_library(library: Sequence[Record], schema: Schema) -> list[Record]:
    """Rewrite the LIBRARY's vocabulary, leaving the target alone.

    This is the experiment that actually answers the standing criticism. The
    complaint is that the corpus shares a vocabulary by construction, so the
    engine never has to bridge a genuine wording difference. Perturbing the
    target cannot test that -- the target's predicates are anchors and CAUSES,
    which have no siblings. Perturbing the sources can: afterwards the two
    sides no longer use the same words for the same mechanism, and anything
    that still transfers is transferring on structure and kinship rather than
    on shared authorship.
    """
    out: list[Record] = []
    for rec in library:
        out.append(Record(
            id=rec.id, domain=rec.domain, attrs=dict(rec.attrs),
            types=dict(rec.types), text=rec.text,
            rels=tuple(_kin_swap(r, rec.types, schema) for r in rec.rels)))
    return out


def kin_equal(a: Rel, b: Rel, schema: Schema = CORE_SCHEMA) -> bool:
    """Equal up to family substitution, recursively.

    Needed because strict equality punishes the engine for doing exactly what
    kinship is for. If a source case says SATURATES and the engine correctly
    transfers it to a target whose incident happened to be written as FAILS,
    the prediction is right and `==` calls it wrong. Measuring the re-worded
    arm under strict equality therefore measures the metric, not the engine.
    """
    if a.pred != b.pred and not schema.kin(a.pred, b.pred):
        return False
    if len(a.args) != len(b.args):
        return False
    for x, y in zip(a.args, b.args):
        if isinstance(x, Rel) != isinstance(y, Rel):
            return False
        if isinstance(x, Rel):
            if not kin_equal(x, y, schema):
                return False
        elif x != y:
            return False
    return True


def kin_swap_count(library: Sequence[Record],
                   schema: Schema = CORE_SCHEMA) -> tuple[int, int]:
    """(relations changed, relations total). Guards against a vacuous arm."""
    changed = total = 0
    for before, after in zip(library, _kin_library(library, schema)):
        for a, b in zip(walk(list(before.rels)), walk(list(after.rels))):
            total += 1
            changed += (a.pred != b.pred)
    return changed, total


def _dropped(target: TargetPattern, n: int = 1) -> TargetPattern:
    """Remove the last n non-anchor premise relations: an incomplete encoding."""
    anchors = [r for r in target.rels if r.pred in ANCHORS]
    rest = [r for r in target.rels if r.pred not in ANCHORS]
    kept = rest[:-n] if len(rest) > n else rest[:1]
    return TargetPattern(target.name, target.domain,
                         tuple(anchors + kept), dict(target.types))


def _rename_map(target: TargetPattern) -> dict[str, str]:
    return {e: f"zz-{i}" for i, e in enumerate(sorted(target.entities()))}


def _apply_names(rel: Rel, mapping: Mapping[str, str]) -> Rel:
    return Rel(rel.pred, tuple(
        _apply_names(a, mapping) if isinstance(a, Rel) else mapping.get(a, a)
        for a in rel.args))


def _renamed(target: TargetPattern) -> TargetPattern:
    """Rename every entity. This arm MUST be invariant.

    Alignment matches relations, not names, so a pure rename cannot change the
    result of a correct implementation. If this arm moves, something is
    matching on surface form and the whole cross-domain claim is unsound, so
    it is the most important arm in the study and the only one with a
    predicted value.

    The predictions come back in the renamed vocabulary and are mapped BACK
    before scoring. Without that the arm scores zero by construction: a
    prediction about `zz-3` cannot equal a held-out tail relation about
    `backend`, and the study would report the engine as name-sensitive when
    the only name-sensitive thing in the experiment was the comparison. The
    first run of this study did exactly that and read BROKEN.
    """
    mapping = _rename_map(target)
    return TargetPattern(target.name, target.domain,
                         tuple(_apply_names(r, mapping) for r in target.rels),
                         {mapping.get(k, k): v for k, v in target.types.items()})



def _distracted(target: TargetPattern) -> TargetPattern:
    """Add an unrelated relation: a noisier, more realistic premise."""
    ents = sorted(target.entities())
    extra: list[Rel] = []
    agents = [e for e in ents if target.types.get(e, "").endswith("AGENT")
              or target.types.get(e) in ("AGENT", "SERVICE", "HUMAN",
                                         "DESIGNER", "CLIENT", "MANAGER")]
    resources = [e for e in ents if target.types.get(e) in
                 ("RESOURCE", "POOL", "GATEWAY", "CHANNEL", "STAGE", "SITE")]
    if agents and resources:
        extra.append(Rel("CONTAINS", (resources[0], agents[0])))
    return TargetPattern(target.name, target.domain,
                         target.rels + tuple(extra), dict(target.types))


def run_perturbations(library: Sequence[Record],
                      schema: Schema = CORE_SCHEMA,
                      ks: Sequence[int] = (1, 3, 5, 10),
                      knobs: Knobs | None = None) -> list[PerturbationArm]:
    """How much does the result depend on the premise being encoded the way it
    happened to be encoded?

    The standing criticism of this benchmark is that the corpus shares a
    vocabulary by construction, so the transfer looks better than it would on
    independently encoded material. That criticism is usually left as an
    unfalsifiable caveat. It does not have to be: perturb the premise in the
    ways a second encoder would differ, and measure the degradation.
    """
    base = knobs or Knobs(reach=1.0, mac_keep=6, min_depth=2, rounds=2,
                          beam=8)

    def arm(name: str, note: str, fn, invariant: bool = False
            ) -> PerturbationArm:
        def ranker(others: Sequence[Record], held: HeldOut) -> list[Rel]:
            store = _build_store(others, exclude=held.record_id)
            perturbed = fn(held.premise)
            rounds = run_premortem(store, perturbed, base, enrich_top=3,
                                   schema=schema)
            ranked = _flatten(rounds)
            if invariant:
                # Undo the renaming so the comparison is like for like.
                back = {v: k for k, v in _rename_map(held.premise).items()}
                ranked = [_apply_names(r, back) for r in ranked]
            return ranked

        # Metrics are still scored against the UNPERTURBED tail: the question
        # is whether the engine still recovers the real answer, not whether it
        # is self-consistent under noise.
        return PerturbationArm(
            name, note,
            evaluate_library(library, name, note, base, schema, ks,
                             ranker=ranker),
            invariant)

    def library_arm(name: str, note: str,
                    perturbed: Sequence[Record]) -> PerturbationArm:
        """Perturb the SOURCES, hold the target and the scoring fixed."""
        def ranker(others: Sequence[Record], held: HeldOut) -> list[Rel]:
            by_id = {r.id: r for r in perturbed}
            swapped = [by_id.get(r.id, r) for r in others]
            store = _build_store(swapped, exclude=held.record_id)
            rounds = run_premortem(store, held.premise, base, enrich_top=3,
                                   schema=schema)
            return _flatten(rounds)

        return PerturbationArm(
            name, note,
            evaluate_library(library, name, note, base, schema, ks,
                             ranker=ranker), False)

    kin_lib = _kin_library(library, schema)
    changed, total = kin_swap_count(library, schema)

    return [
        arm("unperturbed", "the premise as encoded", lambda t: t),
        library_arm("library re-worded",
                    f"{changed}/{total} source relations swapped to a family "
                    f"sibling", kin_lib),
        arm("entities renamed",
            "every entity renamed; MUST be invariant", _renamed,
            invariant=True),

        arm("one relation dropped",
            "an incomplete encoding", _dropped),
        arm("distractor added", "an unrelated relation in the premise",
            _distracted),
    ]


def format_perturbations(arms: Sequence[PerturbationArm]) -> str:
    if not arms:
        return "no arms"
    ks = arms[0].result.ks
    last = ks[-1]
    baseline = arms[0].result
    head = (f"{'perturbation':<24}{'hit@1':>7}{'hit@' + str(last):>8}"
            f"{'kin@' + str(last):>8}{'deep@' + str(last):>8}{'MRR':>7}"
            f"{'vs base':>9}")
    lines = [head, "-" * len(head)]
    for a in arms:
        r = a.result
        delta = (r.mrr - baseline.mrr) / baseline.mrr if baseline.mrr else 0.0
        flag = ""
        if a.invariant:
            flag = "  OK" if abs(r.mrr - baseline.mrr) < 1e-9 else "  BROKEN"
        lines.append(
            f"{a.name:<24}{r.hit_rate_at(1):>7.2f}{r.hit_rate_at(last):>8.2f}"
            f"{r.kin_hit_rate_at(last):>8.2f}{r.deep_hit_rate_at(last):>8.2f}"
            f"{r.mrr:>7.2f}{delta:>+8.0%}{flag}")
    lines.append("")
    lines.append("kin@k credits a prediction that matches the tail up to family")
    lines.append("substitution -- SATURATES where the tail says FAILS is a")
    lines.append("correct transfer, and strict equality scores it wrong.")
    lines.append("")
    lines.append("Scored against the UNPERTURBED held-out tail throughout: the")
    lines.append("question is whether the real answer still comes back, not")
    lines.append("whether the engine is self-consistent under noise.")
    lines.append("'entities renamed' has a predicted value -- alignment matches")
    lines.append("relations, not names, so it must not move. If it does, the")
    lines.append("implementation is matching surface form.")
    return "\n".join(lines)
