"""MODEL tier tests.

The `ask` tier was unreachable through the unified API until v5.1, which meant
the cascade's central claim — that cheap filtering keeps expensive calls down —
could not be demonstrated through the public surface at all. These tests
exercise it and, more importantly, measure it: the last test is the one that
would catch the cascade silently ceasing to filter.
"""

from __future__ import annotations

import pytest

from ctd.controller import RuntimePolicy
from ctd.graph import EvidenceGraph
from ctd.kernel.query import Verdict
from ctd.models import Node
from ctd.oracles import (
    CallableOracle, EvidenceOracle, NullOracle, build_oracle,
)
from ctd.unified_close import (
    CloseConstraintSpec, CloseRecordModel, UnifiedCloseOperator,
    UnifiedCloseRequest,
)


def _records(n: int, **attrs) -> list[CloseRecordModel]:
    return [
        CloseRecordModel(
            id=f"r{i}", domain="d",
            attrs={"kind": "x", "tier": "gold" if i == 0 else "bronze",
                   **attrs},
            text=f"record {i} body text")
        for i in range(n)
    ]


# --------------------------------------------------------------------------
# Reachability
# --------------------------------------------------------------------------

def test_ask_constraint_requires_a_question():
    with pytest.raises(ValueError):
        CloseConstraintSpec(name="judge", kind="ask")


def test_unbound_model_constraint_abstains_rather_than_passing():
    """The default oracle decides nothing.

    A model constraint with no model behind it is an open question. Passing it
    to keep the pipeline moving is how a cascade quietly stops filtering, and
    the failure is invisible because the output still looks like an answer.
    """
    result = UnifiedCloseOperator().run(UnifiedCloseRequest(
        query="q",
        records=_records(3),
        constraints=[
            CloseConstraintSpec(name="kind", kind="has", attr="kind",
                                values=["x"]),
            CloseConstraintSpec(name="judge", kind="ask",
                                question="is this the right one?"),
        ],
        expect_unique=False,
    ))
    assert result.outcome == "ABSTAIN"
    assert result.gaps, "an undecided model constraint must produce a gap"
    assert result.gaps[0].constraint_id == "judge"
    assert result.telemetry["oracle"] == "NullOracle"
    assert result.telemetry["model_calls"] == 3


def test_a_bound_judge_closes_the_query():
    op = UnifiedCloseOperator(
        oracle_fn=lambda rec, q: rec.attrs.get("tier") == "gold")
    result = op.run(UnifiedCloseRequest(
        query="q",
        records=_records(4),
        constraints=[
            CloseConstraintSpec(name="kind", kind="has", attr="kind",
                                values=["x"]),
            CloseConstraintSpec(name="judge", kind="ask",
                                question="is this gold tier?"),
        ],
        expect_unique=True,
    ))
    assert result.outcome == "CLOSED"
    assert result.ids == ["r0"]
    assert result.telemetry["oracle"] == "CallableOracle"
    assert result.telemetry["model_calls"] == 4
    assert result.telemetry["model_tokens"] > 0


def test_budget_refuses_the_stage_before_spending_it():
    """Pre-flight, not post-mortem. A ceiling that reports the overrun after
    the fact is a log line, not a budget."""
    calls: list[str] = []

    def judge(rec, question):
        calls.append(rec.id)
        return True

    op = UnifiedCloseOperator(oracle_fn=judge)
    result = op.run(UnifiedCloseRequest(
        query="q",
        records=_records(10),
        constraints=[
            CloseConstraintSpec(name="kind", kind="has", attr="kind",
                                values=["x"]),
            CloseConstraintSpec(name="judge", kind="ask", question="?"),
        ],
        expect_unique=False,
        policy=RuntimePolicy(max_provider_calls=3),
    ))
    assert result.outcome == "ABSTAIN"
    assert not calls, "budget must be checked BEFORE the stage runs"
    assert result.gaps[0].constraint_kind == "budget"


def test_a_judge_that_raises_yields_unknown_not_fail():
    """A crashed judge did not decide the question in the negative. Mapping an
    exception to FAIL silently deletes candidates that may be the answer."""
    def judge(rec, question):
        raise RuntimeError("model unavailable")

    result = UnifiedCloseOperator(oracle_fn=judge).run(UnifiedCloseRequest(
        query="q",
        records=_records(2),
        constraints=[
            CloseConstraintSpec(name="kind", kind="has", attr="kind",
                                values=["x"]),
            CloseConstraintSpec(name="judge", kind="ask", question="?"),
        ],
        expect_unique=False,
    ))
    assert result.outcome == "ABSTAIN"
    assert "undecided" in result.reason


# --------------------------------------------------------------------------
# Oracle implementations
# --------------------------------------------------------------------------

def test_evidence_oracle_answers_only_from_stored_claims():
    graph = EvidenceGraph()
    graph.add_node(Node(id="r0", type="rec",
                        attributes={"site_verified": True}))
    graph.add_node(Node(id="r1", type="rec",
                        attributes={"site_verified": False}))
    graph.add_node(Node(id="r2", type="rec", attributes={}))

    op = UnifiedCloseOperator(graph=graph)
    result = op.run(UnifiedCloseRequest(
        query="q",
        records=_records(3),
        constraints=[
            CloseConstraintSpec(name="kind", kind="has", attr="kind",
                                values=["x"]),
            CloseConstraintSpec(name="judge", kind="ask",
                                question="is site_verified recorded?"),
        ],
        expect_unique=False,
        oracle="evidence",
    ))
    assert result.telemetry["oracle"] == "EvidenceOracle"
    # r0 passes on a stored claim, r1 fails on one, r2 has no claim bearing on
    # the question and is left undecided rather than guessed at.
    assert result.outcome == "CLOSED"
    assert result.ids == ["r0"]
    assert result.undecided_ids == ["r2"], (
        "a record with no bearing claim must be undecided, not eliminated")
    assert result.gaps and result.gaps[0].constraint_id == "__completeness__"


def test_uniqueness_is_not_claimed_while_a_rival_is_undecided():
    """REGRESSION: with one candidate satisfying every constraint and another
    undecided, expect_unique=True returned CLOSED — asserting uniqueness that
    was never established. The undecided candidate might satisfy the
    specification too, and the caller could not see it existed."""
    graph = EvidenceGraph()
    graph.add_node(Node(id="r0", type="rec",
                        attributes={"site_verified": True}))
    graph.add_node(Node(id="r1", type="rec", attributes={}))

    result = UnifiedCloseOperator(graph=graph).run(UnifiedCloseRequest(
        query="q",
        records=_records(2),
        constraints=[
            CloseConstraintSpec(name="kind", kind="has", attr="kind",
                                values=["x"]),
            CloseConstraintSpec(name="judge", kind="ask",
                                question="is site_verified recorded?"),
        ],
        expect_unique=True,
        oracle="evidence",
    ))
    assert result.outcome == "ABSTAIN"
    assert "uniqueness not established" in result.reason
    assert result.gaps[0].constraint_id == "__uniqueness__"
    assert "expect_unique=false" in result.gaps[0].suggested_action


def test_build_oracle_refuses_an_unknown_kind():
    with pytest.raises(ValueError):
        build_oracle("gpt-over-http")


def test_oracles_count_what_they_spend():
    from ctd.kernel.store import Record

    rec = Record(id="a", domain="d", text="x" * 400)
    for oracle in (NullOracle(), EvidenceOracle(),
                   CallableOracle(fn=lambda r, q: True)):
        oracle.judge(rec, "a fairly long question about this record")
        assert oracle.calls == 1
        assert oracle.tokens > 0, type(oracle).__name__


# --------------------------------------------------------------------------
# The claim the cascade exists to support
# --------------------------------------------------------------------------

def test_cheap_filtering_reduces_model_calls():
    """The cost argument, measured rather than asserted.

    Same corpus, same judge, same question. One run pushes a selective index
    predicate down first; the other does not. The difference is the number of
    times the million-cost tier runs, and if a future change makes the cheap
    tier stop filtering, this is the test that notices.
    """
    records = _records(40)

    def judge(rec, question):
        return rec.attrs.get("tier") == "gold"

    def run(with_prefilter: bool):
        constraints = [CloseConstraintSpec(name="judge", kind="ask",
                                           question="is this gold tier?")]
        if with_prefilter:
            constraints.insert(0, CloseConstraintSpec(
                name="tier", kind="has", attr="tier", values=["gold"]))
        return UnifiedCloseOperator(oracle_fn=judge).run(UnifiedCloseRequest(
            query="q", records=records, constraints=constraints,
            expect_unique=False,
            policy=RuntimePolicy(max_provider_calls=100)))

    filtered = run(True)
    unfiltered = run(False)

    assert filtered.outcome == unfiltered.outcome == "CLOSED"
    assert filtered.ids == unfiltered.ids, "filtering must not change the answer"
    assert unfiltered.telemetry["model_calls"] == 40
    assert filtered.telemetry["model_calls"] == 1
    assert filtered.telemetry["probes"] >= 1
    # and the expensive tier dominates the cost, which is why it is worth
    # avoiding rather than optimising
    assert (unfiltered.telemetry["cost_units"]
            > 30 * filtered.telemetry["cost_units"])
