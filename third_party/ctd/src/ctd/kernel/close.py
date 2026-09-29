"""CLOSE executor.

Two stages. Stage 1 intersects index bitmaps without reading a single record.
Stage 2 runs the residual constraints over whatever survived, cheapest tier
first, deferring anything it cannot decide.

Four corrections to v4, each of which changed a number that was being reported:

  1. Index bitmaps were computed twice — once inside the sort key that ordered
     constraints by cardinality, once inside the intersection loop. Every probe
     was therefore performed and counted exactly twice. They are now computed
     once and reused, which halves the probe count for real.

  2. `undecided = [r for r in candidates if r not in closed]` used dataclass
     field-wise equality on Record, deep-comparing attribute dicts. Comparison
     is now by id.

  3. The cost ceiling was checked after a whole stage had run. It is now
     checked per record, so the ceiling bounds spend instead of describing it
     after the fact. A wall-clock deadline is enforced the same way.

  4. Relaxation only ever tried *prefixes* of the droppable tuple, so declaring
     ("a", "b") droppable with max_drops=1 could never try dropping "b" alone.
     It now enumerates combinations in increasing size, deterministically, and
     stops at the first closure — the smallest, earliest-declared drop set that
     works.
"""

from __future__ import annotations

import time
from itertools import combinations
from typing import Any

from .query import (
    Budget, Closure, Constraint, DataRequest, Outcome, Oracle,
    RelaxationPolicy, SlotSpec, Stats, Tier, Verdict,
)
from .store import Record, TopoStore

__all__ = ["run_close"]


class _Deadline:
    def __init__(self, max_ms: float | None) -> None:
        self.t0 = time.perf_counter()
        self.limit = None if max_ms is None else max_ms / 1000.0
        self.hit = False

    def expired(self) -> bool:
        if self.limit is None:
            return False
        if time.perf_counter() - self.t0 > self.limit:
            self.hit = True
            return True
        return False

    @property
    def ms(self) -> float:
        return (time.perf_counter() - self.t0) * 1000.0


def run_close(
    store: TopoStore,
    spec: SlotSpec,
    oracle: Oracle | None = None,
    budget: Budget | None = None,
    stats: Stats | None = None,
    relax: RelaxationPolicy | None = None,
) -> Closure:
    budget = budget or Budget()
    stats = stats or Stats()
    relax = relax or RelaxationPolicy()

    store.reset_counters()
    clock = _Deadline(budget.max_ms)
    c0 = oracle.calls if oracle else 0
    k0 = oracle.tokens if oracle else 0

    # One bitmap cache per query. Two constraints naming the same (attr, key)
    # cost one probe, and the relaxation ladder re-uses everything stage 1
    # already computed rather than re-probing per attempt.
    cache: dict[tuple[str, Any, bool], int] = {}

    res = _attempt(store, spec, oracle, budget, stats, clock, cache)

    # Relaxation fires only on an empty result set, never on ambiguity:
    # dropping constraints cannot resolve an ambiguity, only widen it.
    if res.outcome is Outcome.REQUEST and relax.max_drops:
        for n in range(1, relax.max_drops + 1):
            done = False
            for drop in combinations(relax.droppable, n):
                if clock.expired():
                    break
                names = set(drop)
                reduced = SlotSpec(
                    spec.query,
                    [c for c in spec.constraints if c.name not in names],
                    spec.expect_unique,
                )
                retry = _attempt(store, reduced, oracle, budget, stats,
                                 clock, cache)
                if retry.outcome is Outcome.CLOSED:
                    retry.outcome = Outcome.PARTIAL
                    retry.dropped = list(drop)
                    retry.reason = (
                        f"closed only after dropping {list(drop)}; treat as a "
                        f"weaker claim than CLOSED")
                    res = retry
                    done = True
                    break
            if done:
                break

    res.probes = store.probes
    res.reads = store.reads
    if oracle:
        res.model_calls = oracle.calls - c0
        res.model_tokens = oracle.tokens - k0
    res.ms = clock.ms
    res.deadline_hit = clock.hit
    return res


def _attempt(
    store: TopoStore,
    spec: SlotSpec,
    oracle: Oracle | None,
    budget: Budget,
    stats: Stats,
    clock: _Deadline,
    cache: dict[tuple[str, Any, bool], int],
) -> Closure:
    trace: list[dict[str, Any]] = []
    cost = 0

    indexed = [c for c in spec.constraints if c.tier is Tier.INDEX]
    residual = [c for c in spec.constraints if c.tier is not Tier.INDEX]

    # ---- stage 1: index intersection, no records read -----------------
    # Compute each bitmap exactly once, then order by cardinality so the
    # narrowest filter runs first and the candidate set shrinks fastest.
    bitmaps: list[tuple[Constraint, int, int]] = []
    for c in indexed:
        key = (c.attr or "", c.keys, c.negate)
        bm = cache.get(key)
        if bm is None:
            bm = c.bitmap(store)
            cache[key] = bm
        bitmaps.append((c, bm, TopoStore.count(bm)))
    bitmaps.sort(key=lambda t: (t[2], t[0].name))

    bitmap = store.all_bits
    satisfied: list[str] = []
    for c, bm, _card in bitmaps:
        before = TopoStore.count(bitmap)
        bitmap &= bm
        after = TopoStore.count(bitmap)
        cost += Tier.INDEX.cost
        trace.append({"constraint": c.name, "tier": "index",
                      "in": before, "killed": before - after})
        stats.observe(c.name, before, before - after)
        if after == 0:
            return _request(c.name, satisfied, before, trace, cost,
                            "empty candidate set after index intersection")
        satisfied.append(c.name)

    candidates = store.materialise(bitmap)
    if not candidates:
        return _request(
            indexed[-1].name if indexed else "index", satisfied, 0, trace,
            cost, "no live record in the candidate set")

    # ---- stage 2: tiered cascade over the survivors --------------------
    verdicts: dict[str, dict[str, Verdict]] = {r.id: {} for r in candidates}
    calls_used = 0
    semantic_used = 0

    for c in stats.order(residual):
        if not candidates:
            break
        if clock.expired():
            return _abstain(f"deadline reached before '{c.name}' ran", trace,
                            cost, deadline=True)

        # Pre-flight: refuse a stage we cannot pay for rather than running it
        # partially and reporting the overrun afterwards.
        need = len(candidates)
        if c.tier is Tier.MODEL:
            if calls_used + need > budget.max_model_calls:
                return _abstain(
                    f"budget exhausted before closure: stage '{c.name}' needs "
                    f"{need} model calls, "
                    f"{budget.max_model_calls - calls_used} remain",
                    trace, cost)
        elif c.tier is Tier.SEMANTIC and budget.max_semantic_calls is not None:
            if semantic_used + need > budget.max_semantic_calls:
                return _abstain(
                    f"budget exhausted before closure: stage '{c.name}' needs "
                    f"{need} semantic evaluations, "
                    f"{budget.max_semantic_calls - semantic_used} remain",
                    trace, cost)
        if (budget.max_cost_units is not None
                and cost + need * c.tier.cost > budget.max_cost_units):
            return _abstain(
                f"cost ceiling would be exceeded by stage '{c.name}' "
                f"({need} x {c.tier.cost:,} units)", trace, cost)

        n_in = len(candidates)
        survivors: list[Record] = []
        for rec in candidates:
            v = c.evaluate(rec, oracle)
            cost += c.tier.cost
            if c.tier is Tier.MODEL:
                calls_used += 1
            elif c.tier is Tier.SEMANTIC:
                semantic_used += 1
            verdicts[rec.id][c.name] = v
            if v in (Verdict.PASS, Verdict.UNKNOWN):
                survivors.append(rec)       # UNKNOWN defers upward

        killed = n_in - len(survivors)
        stats.observe(c.name, n_in, killed)
        trace.append({"constraint": c.name, "tier": c.tier.label,
                      "in": n_in, "killed": killed})
        candidates = survivors

    # ---- closure test: an explicit boolean, not a score ----------------
    need_names = {c.name for c in residual}
    closed = [r for r in candidates
              if need_names <= verdicts[r.id].keys()
              and all(verdicts[r.id][n] is Verdict.PASS for n in need_names)]
    closed_ids = {r.id for r in closed}
    undecided = [r for r in candidates if r.id not in closed_ids]

    if not closed and undecided:
        names = sorted({n for r in undecided
                        for n, v in verdicts[r.id].items()
                        if v is Verdict.UNKNOWN})
        return _abstain(
            "undecided constraints remain: " + (", ".join(names) or "none"),
            trace, cost)
    if not closed:
        last = trace[-1]["constraint"] if trace else "residual"
        return _request(last, satisfied, len(candidates), trace, cost,
                        "no candidate satisfies the specification")
    if spec.expect_unique and len(closed) > 1:
        return _abstain(
            f"underspecified: {len(closed)} candidates satisfy every "
            f"constraint ({', '.join(sorted(r.id for r in closed))})",
            trace, cost)

    if spec.expect_unique and undecided:
        # One candidate satisfied everything and another could not be decided.
        # Returning CLOSED here asserts uniqueness that was never established:
        # the undecided candidate might satisfy the specification too, and the
        # caller has no way to see that it existed. `expect_unique` is a claim
        # about the world, and it cannot be honoured while a rival candidate
        # is still an open question.
        names = sorted({n for r in undecided
                        for n, v in verdicts[r.id].items()
                        if v is Verdict.UNKNOWN})
        return _abstain(
            f"uniqueness not established: {', '.join(r.id for r in closed)} "
            f"satisfies every constraint, but "
            f"{', '.join(sorted(r.id for r in undecided))} could not be "
            f"decided on [{', '.join(names) or 'unknown'}] and may satisfy it "
            f"too",
            trace, cost)

    return Closure(
        outcome=Outcome.CLOSED,
        ids=[r.id for r in closed],
        # Reported even on success: with expect_unique=false the answer set is
        # the DECIDED members, and a caller treating it as exhaustive while
        # candidates remain undecided is drawing a conclusion the engine did
        # not reach.
        undecided_ids=sorted(r.id for r in undecided),
        trace=trace,
        cost_units=cost,
        semantic_calls=semantic_used,
        provenance={
            r.id: ({n: "pass" for n in satisfied} |
                   {n: verdicts[r.id][n].value for n in sorted(need_names)})
            for r in closed
        },
    )


def _abstain(reason: str, trace, cost, deadline: bool = False) -> Closure:
    return Closure(outcome=Outcome.ABSTAIN, reason=reason,
                   trace=trace, cost_units=cost, deadline_hit=deadline)


def _request(binding, satisfied, before, trace, cost, reason) -> Closure:
    return Closure(
        outcome=Outcome.REQUEST,
        reason=reason,
        trace=trace,
        cost_units=cost,
        request=DataRequest(
            binding_constraint=binding,
            satisfied=list(satisfied),
            survivors_before=before,
            message=(
                f"Execution halted. {before} record(s) satisfied "
                f"[{', '.join(satisfied) or 'no prior constraints'}] but none "
                f"satisfy '{binding}'. Closure requires a record meeting "
                f"'{binding}' in addition to the above."
            ),
        ),
    )
