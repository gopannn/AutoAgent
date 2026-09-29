"""Oracles — the MODEL tier, made reachable.

The cascade is built around four cost tiers spanning six orders of magnitude,
and the top one existed only in the kernel: `CloseConstraintSpec` could express
`has`, `where` and `lexical` but not `ask`, so the 1,000,000-cost tier the whole
design exists to *avoid spending* could never be spent through the unified API.
That made the cost story unfalsifiable. A cascade that has nothing expensive at
the end is just a filter, and the claim "two model calls instead of ten" cannot
be checked if zero is the only reachable number.

Three implementations, and the choice between them is a deployment decision
rather than a default:

    NullOracle       returns UNKNOWN for everything. The honest default: with
                     no judge bound, a model constraint is undecided, the
                     closure abstains, and the caller gets a typed gap naming
                     the constraint. It does NOT quietly pass.

    EvidenceOracle   answers from the evidence graph. Deterministic, free, and
                     auditable — it decides a question only when a claim in the
                     graph actually bears on it, and returns UNKNOWN otherwise
                     rather than guessing. Most "model" questions in a
                     well-instrumented system turn out to be lookups.

    CallableOracle   wraps any function, which is where a real model is bound.
                     It counts calls and tokens so the budget ceiling is
                     enforced against something real, and it converts an
                     exception into UNKNOWN rather than FAIL, because a judge
                     that crashed did not decide the question in the negative.

The asymmetry worth stating: a judge that cannot answer must produce UNKNOWN,
never FAIL. UNKNOWN defers upward and the closure abstains with a data request;
FAIL silently eliminates a candidate that might have been the right answer, and
nothing downstream can tell the two apart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .graph import EvidenceGraph
from .kernel.query import Verdict
from .kernel.store import Record

__all__ = [
    "NullOracle", "EvidenceOracle", "CallableOracle", "CountingOracle",
    "build_oracle", "ORACLE_KINDS",
]


@dataclass
class CountingOracle:
    """Shared accounting. The budget ceiling is meaningless unless somebody
    counts, and it must be counted by the thing being paid for."""

    calls: int = 0
    tokens: int = 0

    def _charge(self, rec: Record, question: str) -> None:
        self.calls += 1
        # A rough proxy, deliberately: the point is that spend is bounded and
        # visible, not that the estimate is exact.
        self.tokens += (len(question) + len(rec.text)) // 4 + 32

    def judge(self, rec: Record, question: str) -> Verdict:   # pragma: no cover
        raise NotImplementedError


@dataclass
class NullOracle(CountingOracle):
    """No judge is bound, so nothing at this tier is decided.

    Deliberately not a pass-through. A model constraint with no model behind it
    is an open question, and answering it affirmatively to keep the pipeline
    moving is how a cascade quietly stops filtering.
    """

    def judge(self, rec: Record, question: str) -> Verdict:
        self._charge(rec, question)
        return Verdict.UNKNOWN


@dataclass
class EvidenceOracle(CountingOracle):
    """Decide from the evidence graph, or decline to decide.

    A question is answerable here only when the record's node carries an
    attribute the question names. That is narrow on purpose: the value of this
    oracle is that every answer traces to a stored claim, and the moment it
    starts inferring it becomes a model with none of a model's breadth and all
    of its opacity.
    """

    graph: EvidenceGraph | None = None
    attribute_map: Mapping[str, str] = field(default_factory=dict)

    def judge(self, rec: Record, question: str) -> Verdict:
        self._charge(rec, question)
        if self.graph is None:
            return Verdict.UNKNOWN
        node = self.graph.get_node(rec.id)
        if node is None:
            return Verdict.UNKNOWN

        attr = self.attribute_map.get(question)
        if attr is None:
            # Fall back to any attribute whose name appears in the question,
            # longest first so `payment_gate` wins over `gate`.
            for name in sorted(node.attributes, key=len, reverse=True):
                if name.lower() in question.lower():
                    attr = name
                    break
        if attr is None or attr not in node.attributes:
            return Verdict.UNKNOWN

        value = node.attributes[attr]
        if isinstance(value, bool):
            return Verdict.PASS if value else Verdict.FAIL
        if value is None:
            return Verdict.UNKNOWN
        return Verdict.PASS if value else Verdict.FAIL


@dataclass
class CallableOracle(CountingOracle):
    """Bind a real judge — a model, a rules service, a human queue.

    `fn` returns a `Verdict`, or a bool, or None for "cannot decide".
    """

    fn: Callable[[Record, str], Any] | None = None

    def judge(self, rec: Record, question: str) -> Verdict:
        self._charge(rec, question)
        if self.fn is None:
            return Verdict.UNKNOWN
        try:
            answer = self.fn(rec, question)
        except Exception:                                   # noqa: BLE001
            # A judge that raised did not decide the question in the negative.
            # Mapping a crash to FAIL would silently delete candidates.
            return Verdict.UNKNOWN
        if isinstance(answer, Verdict):
            return answer
        if answer is None:
            return Verdict.UNKNOWN
        return Verdict.PASS if answer else Verdict.FAIL


ORACLE_KINDS = ("none", "evidence")


def build_oracle(kind: str = "none", *, graph: EvidenceGraph | None = None,
                 attribute_map: Mapping[str, str] | None = None,
                 fn: Callable[[Record, str], Any] | None = None
                 ) -> CountingOracle:
    """Resolve an oracle from a request.

    `fn` takes precedence so an in-process caller can bind a model directly;
    the string kinds are what an HTTP request can name, and no string kind
    reaches a network — a remote judge has to be bound in process, on purpose.
    """
    if fn is not None:
        return CallableOracle(fn=fn)
    if kind == "evidence":
        return EvidenceOracle(graph=graph,
                              attribute_map=dict(attribute_map or {}))
    if kind not in ORACLE_KINDS:
        raise ValueError(
            f"unknown oracle kind {kind!r}; expected one of "
            f"{', '.join(ORACLE_KINDS)}, or bind a callable in process")
    return NullOracle()
