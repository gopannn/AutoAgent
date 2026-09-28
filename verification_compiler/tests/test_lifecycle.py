"""The compiler lifecycle is proven once (protocol/compiler-lifecycle.json) and the code must match it.

* the spec is deterministic, fully reachable, dead-end free and universally terminating
  for every repair budget the configuration allows;
* graph.py wires exactly the declared edges, no more and no fewer;
* real graph runs replay step by step on the spec's runtime, budget included;
* the published diagram is generated from the spec.
"""
import pytest
from pydantic import ValidationError

pytest.importorskip("langgraph")

from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402

from verification_compiler.graph import build_graph  # noqa: E402
from verification_compiler.ledger import fingerprint  # noqa: E402
from verification_compiler.lifecycle import expected_edges, load_spec, node_for  # noqa: E402
from verification_compiler.nodes import CompilerNodes  # noqa: E402
from verification_compiler.schemas import AcceptanceResult  # noqa: E402

from . import fakes  # noqa: E402
from .test_graph import APPROVE, PATCH, VULN, script  # noqa: E402

et = fakes.toolkit()
DIAGRAM = load_spec.__globals__["SPEC_PATH"].parent / "compiler-lifecycle.mmd"
GRAPH_ONLY_EDGES = {("__start__", "req_compiler"), ("budget_exhausted", "__end__"),
                    ("aborted", "__end__"), ("abstained", "__end__")}


@pytest.mark.parametrize("budget", range(1, 21))
def test_lifecycle_is_proven_for_every_allowed_budget(budget):
    proof = et.Machine.from_dict(load_spec(budget)).prove()
    assert proof.well_formed, proof.text()
    assert all(proof.checks.values()), proof.checks
    assert proof.checks["universal_termination"]


def test_budget_outside_the_proven_range_is_rejected():
    proof = et.Machine.from_dict(load_spec(0)).prove()
    assert "BUILDER" in proof.unreachable_states
    for bad in (0, 21):
        with pytest.raises(ValidationError):
            fakes.config(max_repair_rounds=bad)


def compiled(llms, sandbox=None, resolver=None, **cfg_kw):
    cfg = fakes.config(**cfg_kw)
    nodes = CompilerNodes(cfg, llms, sandbox or fakes.FakeSandbox(), resolver or fakes.FakeResolver())
    return cfg, build_graph(cfg, nodes, checkpointer=InMemorySaver())


def test_graph_wires_exactly_the_declared_edges():
    _, graph = compiled(fakes.FakeLLMs(script()))
    edges = {(e.source, e.target) for e in graph.get_graph().edges}
    assert edges - GRAPH_ONLY_EDGES == expected_edges()


def replay(llm_script, inputs=None, **kw):
    """Runs the real graph and replays its node sequence on the proven machine."""
    llms = fakes.FakeLLMs(llm_script)
    cfg, graph = compiled(llms, **kw)
    visited = [
        next(iter(update))
        for update in graph.stream(inputs or {"requirements": "r"}, stream_mode="updates",
                                   config={"configurable": {"thread_id": "t"}, "recursion_limit": cfg.recursion_limit()})
    ]
    rt = et.Machine.from_dict(load_spec(cfg.max_repair_rounds)).runtime()
    for nxt in visited[1:] + (["__end__"] if visited[-1] == "release" else []):
        candidates = [t for t in rt.m.transitions if t.source == rt.state and node_for(t.target) == nxt and rt._enabled(t)]
        assert len(candidates) == 1, f"{rt.state} -> {nxt} is not a declared, enabled transition"
        rt.fire(candidates[0].event)
    assert rt.terminal, rt.state
    return rt


RESOLVE_VULN = {"open_finding_statuses": [
    {"fingerprint": fingerprint(VULN), "status": "resolved", "justification": "fixed"}]}


def failing_sandbox(cb, sp, lock):
    r = fakes.passing_result(cb, sp, lock)
    r.acceptance = [AcceptanceResult(id="AT_health", passed=False, cases=1, failures=["x"])]
    return r


@pytest.mark.parametrize("scenario,kwargs,terminal", [
    ("clean release", {}, "RELEASED"),
    ("audit repair", {"llm": {"adversarial_auditor": [{"new_findings": [VULN]}, RESOLVE_VULN]}}, "RELEASED"),
    ("unresolved finding exhausts budget", {"llm": {"adversarial_auditor": [{"new_findings": [VULN]}]},
                                            "max_repair_rounds": 1}, "BUDGET_EXHAUSTED"),
    ("verification exhausts budget", {"sandbox": fakes.FakeSandbox(failing_sandbox), "max_repair_rounds": 2},
     "BUDGET_EXHAUSTED"),
    ("semantic rejection then approval",
     {"llm": {"semantic_reviewer": [{"is_valid": False, "feedback": "x", "unmet_requirements": []}, APPROVE]}},
     "RELEASED"),
    ("invalid spec aborts", {"llm": {"verification_compiler": [fakes.spec("def test_x():\n    assert True\n")]}},
     "ABORTED"),
    ("repair mode", {"inputs": {"requirements": "r", "codebase": fakes.codebase()}, "llm": {"architect": [fakes.CONTRACT]}},
     "RELEASED"),
])
def test_real_runs_conform_to_the_proven_lifecycle(scenario, kwargs, terminal):
    llm_script = script(**kwargs.pop("llm", {}))
    llm_script.setdefault("repository_builder", [PATCH])
    inputs = kwargs.pop("inputs", None)
    if inputs is None:
        import collections

        llm_script["architect"] = collections.deque(llm_script["architect"])
    rt = replay(llm_script, inputs=inputs, **kwargs)
    assert rt.state == terminal


def test_published_diagram_is_generated_from_the_spec():
    assert DIAGRAM.read_text(encoding="utf-8") == et.Machine.from_dict(load_spec()).to_mermaid()
