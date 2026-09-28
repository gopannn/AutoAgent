"""Graph topology.

    req_compiler -> verification_compiler -> architect -> policy_gate -> auditor -> dependency_gate
                                                              ^             |              |
                                                              |             v              v
                                                           builder <---- (repair) <- sandbox_verify -> semantic_review -> release

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
        if status == "validation_failed":
            return "budget" if state.get("iteration", 0) >= max_rounds else "repair"
        return "next"

    return route


def build_graph(cfg: CompilerConfig, nodes: CompilerNodes, checkpointer=None):
    retry = transient_retry_policy()
    g = StateGraph(SystemState)

    g.add_node("req_compiler", nodes.req_compiler, retry_policy=retry)
    g.add_node("verification_compiler", nodes.verification_compiler, retry_policy=retry)
    g.add_node("architect", nodes.architect, retry_policy=retry)
    g.add_node("policy_gate", nodes.policy_gate)
    g.add_node("auditor", nodes.auditor, retry_policy=retry)
    g.add_node("builder", nodes.builder, retry_policy=retry)
    g.add_node("dependency_gate", nodes.dependency_gate)
    g.add_node("sandbox_verify", nodes.sandbox_verify)
    g.add_node("semantic_review", nodes.semantic_review, retry_policy=retry)
    g.add_node("release", nodes.release)
    g.add_node("budget_exhausted", nodes.budget_exhausted)
    g.add_node("aborted", nodes.aborted)

    route = make_router(cfg.max_repair_rounds)
    terminal = {"repair": "builder", "budget": "budget_exhausted", "abort": "aborted"}

    g.add_edge(START, "req_compiler")
    g.add_edge("req_compiler", "verification_compiler")
    g.add_conditional_edges("verification_compiler", route, {**terminal, "next": "architect"})
    g.add_edge("architect", "policy_gate")
    g.add_conditional_edges("policy_gate", route, {**terminal, "next": "auditor"})
    g.add_conditional_edges("auditor", route, {**terminal, "next": "dependency_gate"})
    g.add_edge("builder", "policy_gate")
    g.add_conditional_edges("dependency_gate", route, {**terminal, "next": "sandbox_verify"})
    g.add_conditional_edges("sandbox_verify", route, {**terminal, "next": "semantic_review"})
    g.add_conditional_edges("semantic_review", route, {**terminal, "next": "release"})
    g.add_conditional_edges("release", route, {**terminal, "next": END})
    g.add_edge("budget_exhausted", END)
    g.add_edge("aborted", END)

    return g.compile(checkpointer=checkpointer)
