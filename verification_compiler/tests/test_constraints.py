"""The gate must stop a contradictory intent before writing a spec or code."""
from __future__ import annotations

from .test_graph import run, script, roles
from . import fakes
from verification_compiler.constraints import review_constraints


def test_explicit_opposite_requirements_abstain_before_spec_and_builder():
    requirements = "must use PostgreSQL\nmust not use PostgreSQL"
    review = review_constraints(requirements, fakes.CONTRACT)
    assert review["status"] == "abstained"
    assert review["conflicts"][0]["key"] == "technology.postgresql"

    # The graph's first LLM call compiles the contract; no downstream model may run.
    from langgraph.checkpoint.memory import InMemorySaver
    from verification_compiler.graph import build_graph
    from verification_compiler.nodes import CompilerNodes

    cfg = fakes.config()
    llms = fakes.FakeLLMs(script())
    sandbox = fakes.FakeSandbox()
    final = build_graph(cfg, CompilerNodes(cfg, llms, sandbox, fakes.FakeResolver()),
                        checkpointer=InMemorySaver()).invoke(
        {"requirements": requirements},
        config={"configurable": {"thread_id": "contradiction"}, "recursion_limit": cfg.recursion_limit()},
    )
    assert final["status"] == "abstained"
    assert roles(llms) == ["architect"]
    assert sandbox.calls == 0
    assert "release_manifest" not in final


def test_conflicting_single_value_and_ungrounded_quotes_abstain():
    req = "The only database must be SQLite. The only database must be PostgreSQL."
    contract = {**fakes.CONTRACT, "constraints": [
        {"key": "database.engine", "operator": "eq", "value": "SQLite",
         "source_quote": "The only database must be SQLite."},
        {"key": "database.engine", "operator": "eq", "value": "PostgreSQL",
         "source_quote": "The only database must be PostgreSQL."},
    ]}
    assert review_constraints(req, contract)["status"] == "abstained"
    contract["constraints"][1]["source_quote"] = "Source that was never provided"
    assert review_constraints(req, contract)["invalid_sources"]


def test_consistent_encoded_claims_are_scoped_not_global_proof():
    req = "Use SQLite for the database."
    contract = {**fakes.CONTRACT, "constraints": [
        {"key": "database.engine", "operator": "eq", "value": "SQLite", "source_quote": req},
    ]}
    review = review_constraints(req, contract)
    assert review["status"] == "consistent_with_encoded_constraints"
    assert "incomplete extraction" in review["scope"]


def test_release_refuses_a_changed_requirement_contract_after_review():
    from verification_compiler.nodes import CompilerNodes

    final, _, _ = run(script())
    final["requirement_contract"]["summary"] = "unreviewed change"
    cfg = fakes.config()
    node = CompilerNodes(cfg, fakes.FakeLLMs(script()), fakes.FakeSandbox(), fakes.FakeResolver())
    result = node.release(final, {"configurable": {"thread_id": "test"}})
    assert result["status"] == "execution_failed"
    assert "constraint review changed" in result["error"]
