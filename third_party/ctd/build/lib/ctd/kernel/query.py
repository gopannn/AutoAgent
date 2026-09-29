"""CLOSE mode — return the record that satisfies the whole specification.

The target geometry is a set of constraints, each checkable and each carrying a
cost tier. The planner resolves them cheapest-first so the expensive tier only
ever sees what survived everything cheap.

Outcomes are typed, and there is deliberately no fifth option called
"best guess":

    CLOSED    every constraint decided PASS
    PARTIAL   closed only after named constraints were dropped
    REQUEST   nothing satisfies; here is the binding constraint
    ABSTAIN   ambiguous, undecidable, or the budget could not cover closure

Changes from v4
---------------

**Budget is now three-dimensional and enforced before the spend, not after.**
v4 checked model-call count before a model stage and cost units only *after* a
whole stage had already run — so a SEMANTIC stage over 10,000 survivors blew
the ceiling by 10,000 evaluations and then reported it. There is now a
per-record check, a semantic-call ceiling, and a wall-clock deadline. The
deadline is the honest form of the "urgency" parameter: a real system's bound
is time, not an abstract cost unit.

**`lexical` no longer scans every attribute value.** v4 stringified
`r.attrs.values()`, which on this corpus meant matching query terms against the
*check-procedure text* stored in an attribute — so a lexical constraint could
pass on a document whose only mention of the term was in unrelated metadata.
Searchable text is now explicit.

**Index bitmaps are computed once.** v4 called `c.bitmap(store)` inside the
sort key and then again inside the loop, so every index probe was counted and
performed exactly twice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Protocol, Sequence

from .store import Record, TopoStore

__all__ = [
    "Verdict", "Tier", "Oracle", "Constraint", "SlotSpec",
    "has", "where", "lexical", "ask",
    "Stats", "Budget", "RelaxationPolicy",
    "Outcome", "DataRequest", "Closure",
]


class Verdict(Enum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"    # undecidable at this tier: defer, never assume


class Tier(Enum):
    """Relative orders of magnitude, not wall-clock."""
    INDEX = ("index", 1)            # bitmap intersection, no record read
    SCAN = ("scan", 100)            # attribute test on a materialised record
    SEMANTIC = ("semantic", 1_000)  # embedding or lexical scoring
    MODEL = ("model", 1_000_000)    # an LLM call

    def __init__(self, label: str, cost: int) -> None:
        self.label = label
        self.cost = cost


class Oracle(Protocol):
    calls: int
    tokens: int

    def judge(self, rec: Record, question: str) -> Verdict: ...


# --------------------------------------------------------------------------
# Constraints
# --------------------------------------------------------------------------

@dataclass
class Constraint:
    name: str
    tier: Tier
    fn: Callable[[Record, Oracle | None], Verdict] | None = None
    attr: str | None = None                 # index pushdown
    keys: tuple[Any, ...] = ()
    negate: bool = False
    question: str = ""

    def __post_init__(self) -> None:
        if self.tier is Tier.INDEX:
            if not self.attr:
                raise ValueError(
                    f"index constraint '{self.name}' has no attribute")
            if not self.keys and not self.negate:
                raise ValueError(
                    f"index constraint '{self.name}' has no keys; an empty key "
                    f"set matches nothing, which is almost never intended")
        elif self.fn is None:
            raise ValueError(
                f"constraint '{self.name}' at tier {self.tier.label} has no "
                f"evaluation function")

    def bitmap(self, store: TopoStore) -> int:
        """Raises UnindexedAttribute if the attribute has no index — see
        store.bitmap. With negate=True an unindexed attribute would otherwise
        return the whole universe and silently disable the filter."""
        acc = 0
        for k in self.keys:
            acc |= store.bitmap(self.attr, k)
        if self.negate:
            if not self.keys:
                store.bitmap(self.attr, object())   # existence check + probe
            return store.all_bits & ~acc
        return acc

    def evaluate(self, rec: Record, oracle: Oracle | None) -> Verdict:
        if self.fn is None:
            raise RuntimeError(f"constraint '{self.name}' is not evaluable")
        return self.fn(rec, oracle)


def has(name: str, attr: str, *keys: Any, negate: bool = False) -> Constraint:
    """Index-resolvable constraint. Costs one probe per key, reads no records."""
    return Constraint(name, Tier.INDEX, attr=attr, keys=tuple(keys),
                      negate=negate)


def where(name: str, pred: Callable[[Record], bool]) -> Constraint:
    """Attribute test requiring the record but not a model.

    The predicate is wrapped: a KeyError or TypeError inside a user predicate
    now yields UNKNOWN (defer upward) rather than crashing the whole query or,
    worse, being caught somewhere generic and read as FAIL.
    """
    def fn(r: Record, _o: Oracle | None) -> Verdict:
        try:
            return Verdict.PASS if pred(r) else Verdict.FAIL
        except (KeyError, AttributeError, TypeError, ValueError):
            return Verdict.UNKNOWN

    return Constraint(name, Tier.SCAN, fn=fn)


def lexical(name: str, terms: Sequence[str], threshold: int = 1,
            fields: Sequence[str] = ("text",)) -> Constraint:
    """Cheap text signal over explicitly named fields.

    Returns UNKNOWN near the threshold rather than FAIL: a weak signal is not
    evidence of absence.
    """
    low = [t.lower() for t in terms]
    if threshold < 1:
        raise ValueError("threshold must be at least 1")

    def body_of(r: Record) -> str:
        parts: list[str] = []
        for f in fields:
            v = r.attrs.get(f, getattr(r, f, None))
            if isinstance(v, str):
                parts.append(v)
            elif isinstance(v, (list, tuple, set, frozenset)):
                parts.extend(str(x) for x in v)
            elif v is not None:
                parts.append(str(v))
        return " ".join(parts).lower()

    def fn(r: Record, _o: Oracle | None) -> Verdict:
        body = body_of(r)
        hits = sum(1 for t in low if t in body)
        if hits >= threshold:
            return Verdict.PASS
        return Verdict.FAIL if hits == 0 else Verdict.UNKNOWN

    return Constraint(name, Tier.SEMANTIC, fn=fn)


def ask(name: str, question: str) -> Constraint:
    """Requires actually reading the record with a model."""
    def fn(r: Record, o: Oracle | None) -> Verdict:
        return o.judge(r, question) if o else Verdict.UNKNOWN

    return Constraint(name, Tier.MODEL, fn=fn, question=question)


@dataclass
class SlotSpec:
    """The target geometry: the shape of a valid answer."""
    query: str
    constraints: list[Constraint]
    expect_unique: bool = True

    def __post_init__(self) -> None:
        if not self.constraints:
            raise ValueError("a slot spec with no constraints closes on "
                             "everything; state at least one")
        names = [c.name for c in self.constraints]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(
                f"duplicate constraint name(s): {', '.join(sorted(dupes))} — "
                f"names are used for provenance and relaxation and must be "
                f"unique")


# --------------------------------------------------------------------------
# Planner statistics — offline calibration, not runtime RL
# --------------------------------------------------------------------------

class Stats:
    """Learned selectivity per constraint.

    Updated after execution from logged outcomes. Deliberately NOT a runtime
    reinforcement learner: at query time there is no reward signal (you do not
    know whether the answer was right), policy inference would cost more than
    the savings, and the distribution is non-stationary. This is ordinary
    cost-based query planning, which is what every DBMS does.
    """

    def __init__(self, prior: float = 0.5, prior_weight: int = 2) -> None:
        self.seen: dict[str, int] = {}
        self.killed: dict[str, int] = {}
        self.prior = prior
        self.prior_weight = prior_weight

    def observe(self, name: str, evaluated: int, eliminated: int) -> None:
        self.seen[name] = self.seen.get(name, 0) + evaluated
        self.killed[name] = self.killed.get(name, 0) + eliminated

    def selectivity(self, name: str) -> float:
        """Laplace-smoothed toward the prior.

        v4 jumped from a 0.5 default to a hard 1.0 after a single observation,
        so one lucky query pinned a constraint to the front of the plan
        permanently. Smoothing makes early estimates move but not leap.
        """
        n = self.seen.get(name, 0)
        k = self.killed.get(name, 0)
        w = self.prior_weight
        return (k + self.prior * w) / (n + w)

    def order(self, cs: Sequence[Constraint]) -> list[Constraint]:
        return sorted(
            cs, key=lambda c: (-(self.selectivity(c.name) / c.tier.cost),
                               c.tier.cost, c.name))

    def report(self) -> str:
        return "\n".join(
            f"  {n:<34} selectivity={self.selectivity(n):.2f} "
            f"(seen {self.seen[n]})"
            for n in sorted(self.seen))


@dataclass
class Budget:
    """Per-query compute allocation. A runtime parameter, not a build-time
    constant. When it cannot cover closure the engine abstains rather than
    degrading quietly.

    max_ms is the honest form of the 'urgency' bound: real systems are bounded
    by latency, and an abstract cost ceiling is only a proxy for it.
    """
    max_model_calls: int = 10
    max_semantic_calls: int | None = None
    max_cost_units: int | None = None
    max_ms: float | None = None

    def __post_init__(self) -> None:
        if self.max_model_calls < 0:
            raise ValueError("max_model_calls must be non-negative")


@dataclass
class RelaxationPolicy:
    """Bounded, opt-in, always reported.

    An unbounded relaxation ladder turns a fail-closed system into a fail-open
    one wearing the same badge, so drops are capped, enumerated in a
    deterministic order, and named in the result.
    """
    droppable: tuple[str, ...] = ()
    max_drops: int = 0

    def __post_init__(self) -> None:
        if self.max_drops > len(self.droppable):
            raise ValueError(
                f"max_drops={self.max_drops} exceeds the {len(self.droppable)} "
                f"constraint(s) declared droppable")


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------

class Outcome(Enum):
    CLOSED = "closed"
    PARTIAL = "partial"
    REQUEST = "request"
    ABSTAIN = "abstain"


@dataclass
class DataRequest:
    binding_constraint: str
    satisfied: list[str]
    survivors_before: int
    message: str


@dataclass
class Closure:
    outcome: Outcome
    ids: list[str] = field(default_factory=list)
    # Candidates that were neither confirmed nor eliminated. Non-empty
    # alongside a CLOSED outcome means the answer set is the decided members
    # only, not an exhaustive one.
    undecided_ids: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    request: DataRequest | None = None
    reason: str = ""
    provenance: dict[str, dict[str, str]] = field(default_factory=dict)
    trace: list[dict[str, Any]] = field(default_factory=list)
    probes: int = 0
    reads: int = 0
    model_calls: int = 0
    model_tokens: int = 0
    semantic_calls: int = 0
    cost_units: int = 0
    ms: float = 0.0
    deadline_hit: bool = False

    @property
    def answered(self) -> bool:
        return self.outcome in (Outcome.CLOSED, Outcome.PARTIAL)

    def explain(self) -> str:
        out = [f"outcome: {self.outcome.value}"]
        if self.ids:
            out.append(f"ids: {', '.join(self.ids)}")
        if self.undecided_ids:
            out.append(f"UNDECIDED (may also qualify): "
                       f"{', '.join(self.undecided_ids)}")
        if self.dropped:
            out.append(f"DROPPED: {', '.join(self.dropped)}")
        if self.request:
            out.append(f"binding: {self.request.binding_constraint}")
            out.append(f"request: {self.request.message}")
        if self.reason:
            out.append(f"reason: {self.reason}")
        for s in self.trace:
            out.append(f"  {s['constraint']:<26} [{s['tier']:<8}] "
                       f"{s['in']:>4} -> {s['in'] - s['killed']:<4}")
        out.append(f"  probes={self.probes} reads={self.reads} "
                   f"model={self.model_calls} tokens={self.model_tokens} "
                   f"cost={self.cost_units:,} {self.ms:.1f}ms"
                   + ("  DEADLINE" if self.deadline_hit else ""))
        return "\n".join(out)
