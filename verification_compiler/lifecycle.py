"""The compiler lifecycle as declared in protocol/compiler-lifecycle.json.

The JSON is the single source: tests prove it with the vendored epistemic-toolkit
(determinism, reachability, universal termination for every repair budget) and
check that graph.py wires exactly its edges. Nothing here is needed at runtime.
"""
from __future__ import annotations

import json
from pathlib import Path

SPEC_PATH = Path(__file__).parent / "protocol" / "compiler-lifecycle.json"
TERMINAL_NODES = {"RELEASED": "__end__", "BUDGET_EXHAUSTED": "budget_exhausted",
                  "ABORTED": "aborted", "ABSTAINED": "abstained"}


def load_spec(repair_budget: int | None = None) -> dict:
    spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    if repair_budget is not None:
        spec["resources"][0].update(initial=repair_budget, max=repair_budget)
    return spec


def node_for(state: str) -> str:
    return TERMINAL_NODES.get(state, state.lower())


def expected_edges(spec: dict | None = None) -> set[tuple[str, str]]:
    spec = spec or load_spec()
    return {(node_for(t["from"]), node_for(t["to"])) for t in spec["transitions"]}
