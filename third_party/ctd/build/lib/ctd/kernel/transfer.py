"""TRANSFER mode — return the leftover.

CLOSE finds a record satisfying the whole specification and hands back the
record. TRANSFER finds a record whose relational structure aligns with a
partial target and hands back the part that did NOT align, rewritten in the
target's vocabulary. Those are hypotheses about the target.

Two stages, for the same reason CLOSE has tiers: maximum common subgraph is
NP-hard, so a cheap structure-blind filter runs first and an expensive aligner
runs only on survivors. Published as MAC/FAC (Forbus, Gentner & Law 1995); the
alignment follows SME (Falkenhainer, Forbus & Gentner 1989).

Alignment matches RELATIONS, not attributes. Surface similarity retrieves
near-domain cases, which are the useless ones.

What changed from v4
--------------------

**The aligner is a beam search, not a single greedy pass.** v4 committed the
deepest available hypothesis and never reconsidered, so whenever two
high-order matches conflicted it took whichever came first and silently lost
the better global mapping. It acknowledged this in a comment and left it. A
bounded beam over commit/skip decisions recovers most of what full SME would
find at a fraction of the cost, and the greedy path is retained as
`Knobs.aligner="greedy"` so the difference is measurable rather than asserted
(see `evaluate.py`).

**Projection is type-guarded.** v4 checked entity roles when *binding* during
alignment but never re-checked the relation it *emitted*. A projected relation
could therefore satisfy every binding constraint individually and still violate
its own predicate signature — the grammatical-and-false case. Projections are
now validated against the schema in the target's type system, and refusals are
counted and reported rather than dropped quietly.

**Contradictions are detected.** Two sources projecting a predicate and its
antonym over the same arguments used to appear as two confident neighbouring
predictions. They are now linked and flagged.

**Every guard is switchable.** `use_types`, `use_kinship`, `guard_projection`
and `aligner` exist so each can be ablated and its contribution measured. A
guard nobody has measured is a guard nobody should trust.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .schema import CORE_SCHEMA, Schema
from .store import Record, Rel, TopoStore, walk

__all__ = [
    "TargetPattern", "Knobs", "Mapping", "Inference", "Round",
    "mac_score", "align", "align_k_best", "project", "run_transfer",
]

ORDER_WEIGHT = 3.0     # higher-order structure counts for more


# --------------------------------------------------------------------------

@dataclass
class TargetPattern:
    """A problem stated relationally, deliberately incomplete."""
    name: str
    domain: str
    rels: tuple[Rel, ...]
    types: dict[str, str] = field(default_factory=dict)

    def all_rels(self) -> list[Rel]:
        return walk(self.rels)

    def predicates(self) -> set[str]:
        return {r.pred for r in self.all_rels()}

    def entities(self) -> set[str]:
        return {a for r in self.all_rels() for a in r.args
                if isinstance(a, str)}

    def with_rel(self, r: Rel) -> "TargetPattern":
        if r in self.all_rels():
            return self
        return TargetPattern(self.name, self.domain, self.rels + (r,),
                             dict(self.types))


@dataclass
class Knobs:
    """
    reach       Exploration. 0 = near-domain only (safe, incremental). 1 =
                admit distant domains (mostly noise, occasionally the entire
                point). Trades transfer distance against precision; it is NOT
                sampling randomness.
    mac_keep    Survivors of the cheap filter. The per-round compute budget.
    min_depth   Reject mappings with no higher-order match, which is what stops
                superficial attribute matches from scoring well.
    rounds      Loop iterations. Accepted inferences enrich the target and the
                search re-runs, reaching cases invisible in round 1.
    beam        Width of the alignment search. 1 reproduces v4's greedy pass.
    mappings_per_case
                How many distinct mappings per retrieved case get projected.
                Taking only the best one throws away the second reading of an
                ambiguous case, which is often where the non-obvious transfer
                lives.
    aligner     "beam" or "greedy".
    use_types   Entity-role compatibility during binding.
    use_kinship Allow family matches at a discount.
    guard_projection
                Validate emitted relations against their predicate signature
                in the target's type system.
    max_hypotheses
                Hard cap on candidate relation pairs considered per alignment.
                Pairs are generated as |source relations| x |target relations|,
                so a 60-relation case against a 40-relation target is 2,400
                pairs times the beam width times a dict copy each. The cap is
                applied after the deepest-first sort, so what gets dropped is
                the shallowest and least informative.
    demote_subsumed
                Rank a prediction below any higher-scoring prediction that
                already contains it as a sub-relation. `SATURATES(gateway)` is
                not a second finding when `CAUSES(AMPLIFIES(...),
                SATURATES(gateway))` is already on the list above it; it is the
                same finding with its cause removed, and it displaces something
                that is not.
    """
    reach: float = 1.0
    mac_keep: int = 5
    min_depth: int = 2
    rounds: int = 2
    beam: int = 8
    max_hypotheses: int = 600
    mappings_per_case: int = 4
    aligner: str = "beam"
    use_types: bool = True
    use_kinship: bool = True
    guard_projection: bool = True
    demote_subsumed: bool = True

    def __post_init__(self) -> None:
        if self.aligner not in ("beam", "greedy"):
            raise ValueError("aligner must be 'beam' or 'greedy'")
        if self.beam < 1:
            raise ValueError("beam must be at least 1")
        if self.mappings_per_case < 1:
            raise ValueError("mappings_per_case must be at least 1")
        if self.max_hypotheses < 1:
            raise ValueError("max_hypotheses must be at least 1")
        if not 0.0 <= self.reach <= 1.0:
            raise ValueError("reach must be in [0, 1]")


# --------------------------------------------------------------------------
# Stage 1: MAC
# --------------------------------------------------------------------------

def mac_score(target: TargetPattern, rec: Record,
              schema: Schema = CORE_SCHEMA, use_kinship: bool = True) -> float:
    """Cheap predicate-vocabulary overlap, family-aware.

    Deliberately structure-blind; its only job is to cut the library to a size
    the aligner can afford. But plain Jaccard over exact predicate names — what
    v4 used — scores a case at zero when its overlap with the target is
    entirely kin. A control-theory case written with REDUCES could therefore
    never be *retrieved* for a target written with RETRIES, no matter how well
    the aligner would have handled it, because the cheap filter dropped it
    first. The kin discount belongs in retrieval as well as in scoring.
    """
    tv, bv = target.predicates(), rec.predicates()
    if not tv or not bv:
        return 0.0
    total = 0.0
    for sp in bv:
        best = 0.0
        for tp in tv:
            if sp == tp:
                best = 1.0
                break
            if use_kinship and schema.kin(sp, tp):
                best = max(best, schema.kin_discount)
        total += best
    return total / max(len(tv), len(bv))


# --------------------------------------------------------------------------
# Stage 2: FAC
# --------------------------------------------------------------------------

@dataclass
class Mapping:
    source: Record
    ent_map: dict[str, str] = field(default_factory=dict)
    rel_map: dict[Rel, Rel] = field(default_factory=dict)
    systematicity: float = 0.0

    @property
    def depth(self) -> int:
        return max((b.order for b in self.rel_map), default=0)

    @property
    def exact_matches(self) -> int:
        return sum(1 for b, t in self.rel_map.items() if b.pred == t.pred)

    @property
    def family_matches(self) -> int:
        return sum(1 for b, t in self.rel_map.items() if b.pred != t.pred)

    @property
    def looseness(self) -> float:
        """Fraction of the mapping carried by family rather than identical
        predicates. A mapping held together entirely by kinship is a weaker
        analogy than one held together by shared predicates, and ranking has
        to reflect that or kinship becomes a free pass."""
        n = len(self.rel_map)
        return (self.family_matches / n) if n else 0.0

    @property
    def kin_matches(self) -> list[tuple[str, str]]:
        """Family matches rather than identical predicates. Surfaced so a
        reviewer can see where the analogy is loosest."""
        return sorted({(b.pred, t.pred) for b, t in self.rel_map.items()
                       if b.pred != t.pred})

    @property
    def key(self) -> frozenset:
        return frozenset(self.rel_map.items())


class _Aligner:
    """Holds the per-call configuration so the hot loop is not re-reading
    knobs out of a dataclass on every comparison."""

    def __init__(self, schema: Schema, knobs: Knobs) -> None:
        self.schema = schema
        self.k = knobs
        self.capped = False

    # ---- compatibility -------------------------------------------------

    def pred_weight(self, a: str, b: str) -> float | None:
        if a == b:
            return 1.0
        if not self.k.use_kinship:
            return None
        return self.schema.kin_discount if self.schema.kin(a, b) else None

    def compatible(self, b: Rel, t: Rel) -> bool:
        if len(b.args) != len(t.args):
            return False
        if self.pred_weight(b.pred, t.pred) is None:
            return False
        return all(isinstance(x, Rel) == isinstance(y, Rel)
                   for x, y in zip(b.args, t.args))

    def type_ok(self, x: str, y: str, bt: dict, tt: dict) -> bool:
        """Entity roles must agree when both are declared; undeclared is
        permissive so typing can be adopted incrementally.

        This is the guard against the dominant failure mode. Without it a
        greedy aligner binds entities of incompatible kind on a shallow match
        and projects nonsense — 'vehicles sense travel-time'. Entity
        granularity differs across domains (an ant is both carrier and decider;
        a commuter and a car are not) and nothing else catches that.
        """
        if not self.k.use_types:
            return True
        return self.schema.roles_compatible(bt.get(x), tt.get(y))

    # ---- unification ---------------------------------------------------

    def unify(self, b: Rel, t: Rel, ent, rel, rev_ent, rev_rel, bt, tt) -> bool:
        """One-to-one structural unification.

        The consistency test compares by VALUE. v4 wrote `rel.get(b, t) is not
        t`, an identity test over frozen dataclasses that are equal but not
        identical whenever the same sub-relation is written twice in a record —
        which is the normal case, since `R("QUEUEING", "backend")` appearing in
        two different top-level relations constructs two distinct objects.
        The effect was that any relation sharing a sub-relation with an
        already-committed one failed to unify, silently, and the mapping came
        out truncated. On this corpus that cost roughly half of every mapping
        and, downstream, the entire sensing-and-response arm of every
        projection. Equality is the correct test.
        """
        if not self.compatible(b, t):
            return False
        if rel.get(b, t) != t or rev_rel.get(t, b) != b:
            return False
        rel[b] = t
        rev_rel[t] = b
        for x, y in zip(b.args, t.args):
            if isinstance(x, Rel):
                if not self.unify(x, y, ent, rel, rev_ent, rev_rel, bt, tt):
                    return False
            else:
                if ent.get(x, y) != y or rev_ent.get(y, x) != x:
                    return False
                if not self.type_ok(x, y, bt, tt):
                    return False
                ent[x] = y
                rev_ent[y] = x
        return True

    def score(self, rel_map: dict[Rel, Rel]) -> float:
        total = 0.0
        for b, t in rel_map.items():
            w = self.pred_weight(b.pred, t.pred)
            if w is None:          # cannot happen post-unify; defensive
                continue
            total += (ORDER_WEIGHT ** (b.order - 1)) * w
        return total

    # ---- search --------------------------------------------------------

    def hypotheses(self, rec: Record, target: TargetPattern):
        """Candidate relation pairs, deterministically ordered.

        Deepest first, then by predicate name and rendered form. v4 relied on
        insertion order for ties, which is stable but arbitrary; making the
        tiebreak explicit is what lets two runs — and two ablation arms — be
        compared at all.
        """
        pairs = [(b, t) for b in rec.all_rels() for t in target.all_rels()
                 if self.compatible(b, t)]
        pairs.sort(key=lambda p: (-p[0].order, p[0].pred, str(p[0]),
                                  p[1].pred, str(p[1])))
        if len(pairs) > self.k.max_hypotheses:
            self.capped = True
            pairs = pairs[:self.k.max_hypotheses]
        return pairs

    def run(self, rec: Record, target: TargetPattern) -> list[Mapping]:
        pairs = self.hypotheses(rec, target)
        width = 1 if self.k.aligner == "greedy" else self.k.beam

        # A state is (ent, rel, rev_ent, rev_rel). The empty mapping seeds it.
        states: list[tuple[dict, dict, dict, dict, float]] = [
            ({}, {}, {}, {}, 0.0)]

        for b, t in pairs:
            nxt: list[tuple[dict, dict, dict, dict, float]] = []
            for ent, rel, rev_ent, rev_rel, sc in states:
                nxt.append((ent, rel, rev_ent, rev_rel, sc))   # skip
                if b in rel or t in rev_rel:
                    continue
                e2, r2, re2, rr2 = (dict(ent), dict(rel),
                                    dict(rev_ent), dict(rev_rel))
                if self.unify(b, t, e2, r2, re2, rr2, rec.types, target.types):
                    nxt.append((e2, r2, re2, rr2, self.score(r2)))
            # Deduplicate on the committed relation mapping, then prune.
            seen: dict[frozenset, tuple] = {}
            for st in nxt:
                k = frozenset(st[1].items())
                if k not in seen or st[4] > seen[k][4]:
                    seen[k] = st
            states = sorted(seen.values(),
                            key=lambda s: (-s[4], len(s[1])))[:width]

        out = [Mapping(rec, ent, rel, sc)
               for ent, rel, _re, _rr, sc in states]
        out.sort(key=lambda m: (-m.systematicity, -len(m.rel_map)))

        # Keep only MAXIMAL mappings. Because the beam always retains a "skip"
        # branch, most surviving states are proper subsets of the best one:
        # same bindings, fewer of them. Those are not alternative readings of
        # the case, they are truncated copies, and projecting from them yields
        # nothing the best mapping did not already yield (their entity maps are
        # strictly smaller, so substitution fails more often, not less).
        # Filtering to maximal mappings leaves the genuinely competing
        # interpretations — the ones that committed incompatible hypotheses —
        # which is what taking several mappings per case was for.
        maximal: list[Mapping] = []
        for m in out:
            keys = set(m.rel_map.items())
            if any(keys < set(k.rel_map.items()) for k in maximal):
                continue
            maximal.append(m)
        return maximal or out or [Mapping(rec)]


def align(rec: Record, target: TargetPattern, knobs: Knobs | None = None,
          schema: Schema = CORE_SCHEMA) -> Mapping:
    """Best single mapping under the configured aligner."""
    return _Aligner(schema, knobs or Knobs()).run(rec, target)[0]


def align_k_best(rec: Record, target: TargetPattern, k: int = 3,
                 knobs: Knobs | None = None,
                 schema: Schema = CORE_SCHEMA) -> list[Mapping]:
    return _Aligner(schema, knobs or Knobs()).run(rec, target)[:k]


# --------------------------------------------------------------------------
# Stage 3: projection
# --------------------------------------------------------------------------

def _substitute(r: Rel, m: Mapping) -> Rel | None:
    args: list[Any] = []
    for a in r.args:
        if isinstance(a, Rel):
            sub = m.rel_map.get(a) or _substitute(a, m)
            if sub is None:
                return None
            args.append(sub)
        else:
            if a not in m.ent_map:
                return None      # unmapped argument: the analogy licenses
            args.append(m.ent_map[a])   # nothing, and nothing is invented
    return Rel(r.pred, tuple(args))


@dataclass
class Blocked:
    """A projection the guards refused, kept so refusals are visible.

    v4 dropped these silently, which made the type guard impossible to audit:
    you could not tell 'the analogy licensed nothing' from 'the analogy
    licensed something and we threw it away'.
    """
    rel: Rel
    reason: str


def project(m: Mapping, target: TargetPattern, knobs: Knobs | None = None,
            schema: Schema = CORE_SCHEMA
            ) -> tuple[list[Rel], list[Blocked]]:
    """Source structure with no target counterpart, in target vocabulary.

    Returns (admitted, blocked).
    """
    k = knobs or Knobs()
    existing = set(target.all_rels())
    out: list[Rel] = []
    blocked: list[Blocked] = []
    for b in m.source.all_rels():
        if b in m.rel_map:
            continue
        sub = _substitute(b, m)
        if sub is None or sub in existing or sub in out:
            continue
        if k.guard_projection:
            # Validate the WHOLE tree, not just the outermost predicate.
            # Checking only the root let a nonsense sub-relation ride along
            # inside a well-formed container: CAUSES(INCOMPLETE(cash),
            # QUEUEING(stage)) passed because CAUSES takes two relations and
            # nobody looked inside, so the engine asserted that cash was
            # missing required information -- grammatical, guarded, false.
            # Any relation that would be refused standing alone must be
            # refused as an argument.
            problems = [msg for part in walk([sub])
                        for msg in schema.check_args(part.pred, part.args,
                                                     target.types)]
            if problems:
                blocked.append(Blocked(sub, problems[0]))
                continue
        out.append(sub)
    return out, blocked


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

@dataclass
class Inference:
    rel: Rel
    sources: list[str] = field(default_factory=list)
    domains: set[str] = field(default_factory=set)
    fingerprints: set[str] = field(default_factory=set)
    groups: set[str] = field(default_factory=set)
    systematicity: float = 0.0
    looseness: float = 0.0
    conflicts: list[Rel] = field(default_factory=list)

    @property
    def convergence(self) -> int:
        """Independent domains projecting the same inference. The honest
        replacement for 'entropy drops to zero': two unrelated domains agreeing
        is evidence, one suggesting is a lead.

        Counted over *distinct structure fingerprints* as well as domains, so
        two near-identical incidents filed under different domain labels cannot
        manufacture agreement out of a duplicated record.
        """
        counts = [len(self.domains)]
        if self.fingerprints:
            counts.append(len(self.fingerprints))
        if self.groups:
            counts.append(len(self.groups))
        return min(counts)

    @property
    def score(self) -> float:
        return (self.systematicity
                * (1.0 + 0.75 * (self.convergence - 1))
                * (1.0 + 0.5 * (self.rel.order - 1))
                * (1.0 - 0.35 * self.looseness))


@dataclass
class Round:
    index: int
    considered: list[tuple[str, float, float, str]]
    inferences: list[Inference]
    alignments: int
    blocked: list[Blocked] = field(default_factory=list)
    ms: float = 0.0


def _shortlist(store_recs: Iterable[Record], current: TargetPattern,
               k: Knobs, predicate=lambda r: True,
               schema: Schema = CORE_SCHEMA):
    scored = sorted(
        ((r, mac_score(current, r, schema, k.use_kinship)) for r in store_recs
         if r.rels and predicate(r)
         and (r.domain != current.domain or k.reach < 0.5)),
        key=lambda p: (-p[1], p[0].id),
    )
    keep = [r for r, s in scored[:k.mac_keep] if s > 0]
    return keep, {r.id: s for r, s in scored}


def subsumed_relations(rels: Iterable[Rel]) -> set[Rel]:
    """Relations that appear as a proper sub-relation of another in the set.

    Projection walks every relation of a source case including nested ones, so
    a single causal chain yields both `CAUSES(A, B)` and `A` and `B`
    separately. All three are structurally sound and only one of them is news.
    """
    pool = set(rels)
    out: set[Rel] = set()
    for r in pool:
        for sub in walk([r])[1:]:
            if sub in pool:
                out.add(sub)
    return out


def link_conflicts(pool: Sequence[Inference], target: TargetPattern,
                   schema: Schema = CORE_SCHEMA) -> None:
    """Mark inferences that contradict each other or the target.

    Two sources can project `AMPLIFIES(load, gw)` and `DAMPENS(load, gw)` from
    different precedents. Both are structurally well-founded; at most one is
    true. Presenting them as two independent findings is the failure mode this
    prevents.
    """
    by_args: dict[tuple, list[Inference]] = {}
    for inf in pool:
        by_args.setdefault(inf.rel.args, []).append(inf)
    for args, group in by_args.items():
        for a in group:
            for b in group:
                if a is not b and schema.antonym(a.rel.pred, b.rel.pred):
                    a.conflicts.append(b.rel)
    tgt = {r.args: r for r in target.all_rels()}
    for inf in pool:
        other = tgt.get(inf.rel.args)
        if other is not None and schema.antonym(inf.rel.pred, other.pred):
            inf.conflicts.append(other)


def run_transfer(
    store: TopoStore,
    target: TargetPattern,
    knobs: Knobs | None = None,
    schema: Schema = CORE_SCHEMA,
) -> list[Round]:
    """Retrieve, align, project, enrich, repeat.

    Termination is not entropy reaching zero: it is no admissible new inference
    this round, or the round budget spent.
    """
    k = knobs or Knobs()
    aligner = _Aligner(schema, k)
    rounds: list[Round] = []
    current = target

    for i in range(1, k.rounds + 1):
        t0 = time.perf_counter()
        pool: dict[Rel, Inference] = {}
        considered: list[tuple[str, float, float, str]] = []
        blocked_all: list[Blocked] = []

        shortlist, macs = _shortlist(store, current, k)

        for rec in shortlist:
            mappings = aligner.run(rec, current)[:k.mappings_per_case]
            best = mappings[0]
            note = ""
            if best.depth < k.min_depth:
                note = "rejected: no higher-order match with this target"
            elif best.systematicity < (1.0 - k.reach) * 12.0:
                note = "rejected: below reach floor"
            elif best.kin_matches:
                note = "kin match: " + ", ".join(
                    f"{a}~{b}" for a, b in best.kin_matches)
            considered.append((rec.id, macs[rec.id], best.systematicity, note))
            if note.startswith("rejected"):
                continue

            for m in mappings:
                if m.depth < k.min_depth:
                    continue
                admitted, blocked = project(m, current, k, schema)
                blocked_all.extend(blocked)
                for r in admitted:
                    inf = pool.setdefault(r, Inference(r))
                    if rec.id not in inf.sources:
                        inf.sources.append(rec.id)
                    inf.domains.add(rec.domain)
                    inf.fingerprints.add(rec.structure_fingerprint())
                    inf.groups.add(str(rec.attrs.get("independence_group")
                                       or rec.structure_fingerprint()))
                    if m.systematicity > inf.systematicity:
                        inf.systematicity = m.systematicity
                        inf.looseness = m.looseness

        sub = (subsumed_relations(pool) if k.demote_subsumed else set())
        ranked = sorted(pool.values(),
                        key=lambda x: (x.rel in sub, -x.score, str(x.rel)))
        link_conflicts(ranked, current, schema)
        rounds.append(Round(i, considered, ranked, len(shortlist),
                            blocked_all, (time.perf_counter() - t0) * 1000))
        if not ranked:
            break
        current = current.with_rel(ranked[0].rel)

    return rounds
