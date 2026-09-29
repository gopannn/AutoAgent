"""Graph topology.

    req_compiler -> requirement_gate -> discovery -> verification_compiler -> architect -> policy_gate -> auditor
                    (contradiction: abstained)                                             ^            |
                                                                                           |            v
    release <- decision_gate <- semantic_review <- sandbox_verify <- dependency_gate <-- builder <- (repair)
              (unjustified: abstained)

When the input state already carries a `codebase` (e.g. a pull request), the
architect is skipped and the existing code enters at the policy gate.

Every builder patch goes back through the policy gate and a fresh audit, so the
code that is released is exactly the code that was audited, verified and reviewed.
Repairable failures route to the builder until the budget runs out;
infrastructure failures abort immediately.
"""
from __future__ import annotations

from typing import Callable

from langgraph.graph import END, START, StateGraph

from .config import CompilerConfig, transient_retry_policy
from .nodes import CompilerNodes
from .state import SystemState


def make_router(max_rounds: int) -> Callable[[SystemState], str]:
    def route(state: SystemState) -> str:
        status = state.get("status")
        if status == "execution_failed":
            return "abort"
        if status == "abstained":
            return "abstain"
        if status == "validation_failed":
            return "budget" if state.get("iteration", 0) >= max_rounds else "repair"
        return "next"

    return route


def after_spec(route: Callable[[SystemState], str]) -> Callable[[SystemState], str]:
    """Repair mode: when the run starts from an existing codebase, skip the architect."""

    def route_after_spec(state: SystemState) -> str:
        decision = route(state)
        if decision == "next" and state.get("codebase"):
            return "existing_codebase"
        return decision

    return route_after_spec


def build_graph(cfg: CompilerConfig, nodes: CompilerNodes, checkpointer=None):
    retry = transient_retry_policy()
    g = StateGraph(SystemState)

    g.add_node("req_compiler", nodes.req_compiler, retry_policy=retry)
    g.add_node("requirement_gate", nodes.requirement_gate, retry_policy=retry)
    g.add_node("discovery", nodes.discovery)
    g.add_node("verification_compiler", nodes.verification_compiler, retry_policy=retry)
    g.add_node("architect", nodes.architect, retry_policy=retry)
    g.add_node("policy_gate", nodes.policy_gate)
    g.add_node("auditor", nodes.auditor, retry_policy=retry)
    g.add_node("builder", nodes.builder, retry_policy=retry)
    g.add_node("dependency_gate", nodes.dependency_gate)
    g.add_node("sandbox_verify", nodes.sandbox_verify)
    g.add_node("semantic_review", nodes.semantic_review, retry_policy=retry)
    g.add_node("decision_gate", nodes.decision_gate)
    g.add_node("release", nodes.release)
    g.add_node("budget_exhausted", nodes.budget_exhausted)
    g.add_node("aborted", nodes.aborted)
    g.add_node("abstained", nodes.abstained)

    route = make_router(cfg.max_repair_rounds)
    # Each node routes only to the outcomes it can produce; the edge set must equal the proven
    # lifecycle in protocol/compiler-lifecycle.json (enforced by tests/test_lifecycle.py).
    repair = {"repair": "builder", "budget": "budget_exhausted"}
    abort = {"abort": "aborted"}
    abstain = {"abstain": "abstained"}

    g.add_edge(START, "req_compiler")
    g.add_edge("req_compiler", "requirement_gate")
    g.add_conditional_edges("requirement_gate", route, {**abort, **abstain, "next": "discovery"})
    g.add_conditional_edges("discovery", route, {**abort, "next": "verification_compiler"})
    g.add_conditional_edges(
        "verification_compiler", after_spec(route),
        {**abort, "next": "architect", "existing_codebase": "policy_gate"},
    )
    g.add_edge("architect", "policy_gate")
    g.add_conditional_edges("policy_gate", route, {**repair, "next": "auditor"})
    g.add_conditional_edges("auditor", route, {**repair, "next": "dependency_gate"})
    g.add_edge("builder", "policy_gate")
    g.add_conditional_edges("dependency_gate", route, {**repair, **abort, "next": "sandbox_verify"})
    g.add_conditional_edges("sandbox_verify", route, {**repair, **abort, "next": "semantic_review"})
    g.add_conditional_edges("semantic_review", route, {**repair, "next": "decision_gate"})
    g.add_conditional_edges("decision_gate", route, {**abort, **abstain, "next": "release"})
    g.add_conditional_edges("release", route, {**abort, "next": END})
    g.add_edge("budget_exhausted", END)
    g.add_edge("aborted", END)
    g.add_edge("abstained", END)

    return g.compile(checkpointer=checkpointer)
