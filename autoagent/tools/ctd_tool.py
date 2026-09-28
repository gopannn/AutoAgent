"""AutoAgent tool around the CTD constrained evidence resolver (third_party/ctd).

The resolver answers a structured query against an evidence graph and abstains
(PARTIAL / UNRESOLVABLE / CONTRADICTED / BUDGET_EXHAUSTED) instead of guessing.

Authorization is the operator's decision, never the model's: tenant, security labels,
scopes and source classes come only from environment variables, and a query that tries
to set them is refused. The model may tighten evidence thresholds and search budgets,
but never loosen them past the operator's floor or ceiling.

Install with: pip install ./third_party/ctd
"""
import json
import os
from pathlib import Path

from autoagent.registry import register_tool

ALLOWED_ROOT_ENV = "CTD_TOOL_ALLOWED_ROOT"          # graph files must live under this directory (default: cwd)
MAX_GRAPH_BYTES = 50_000_000

# Operator-owned authorization context. Tenant-scoped deployments must set CTD_TOOL_TENANT_ID.
_AUTHZ_ENV = {
    "tenant_id": "CTD_TOOL_TENANT_ID",
    "allowed_security_labels": "CTD_TOOL_SECURITY_LABELS",
    "authorization_scope": "CTD_TOOL_AUTHORIZATION_SCOPE",
    "allowed_source_classes": "CTD_TOOL_SOURCE_CLASSES",
}
_AUTHZ_FIELDS = set(_AUTHZ_ENV) | {"tenant_mode"}
# Fields the model may only make stricter: (floor for thresholds) / (ceiling for budgets).
_THRESHOLD_FLOORS = {"min_confidence": 0.7, "min_trust": 0.7, "min_evidence_strength": 0.7, "min_source_diversity": 1}
_BUDGET_CEILINGS = {"max_expansions": 5000, "max_depth": 6, "deadline_ms": 10_000, "max_provider_calls": 500,
                    "max_candidates": 5000}
_MODEL_SETTABLE = set(_THRESHOLD_FLOORS) | set(_BUDGET_CEILINGS) | {"allowed_node_types", "allowed_edge_types"}


def _operator_policy() -> dict:
    policy: dict = {"tenant_mode": "strict"}
    for field, env in _AUTHZ_ENV.items():
        raw = os.environ.get(env, "").strip()
        if not raw:
            continue
        policy[field] = raw if field == "tenant_id" else {v.strip() for v in raw.split(",") if v.strip()}
    return policy


def build_policy(requested: dict | None) -> dict:
    """Merges model-requested search settings into the operator's policy. Raises ValueError on violations."""
    requested = dict(requested or {})
    forbidden = sorted(set(requested) & _AUTHZ_FIELDS)
    if forbidden:
        raise ValueError(f"authorization fields are set by the operator, not the query: {forbidden}")
    unknown = sorted(set(requested) - _MODEL_SETTABLE)
    if unknown:
        raise ValueError(f"unsupported policy fields: {unknown}")
    policy = _operator_policy()
    for key, floor in _THRESHOLD_FLOORS.items():
        policy[key] = max(floor, requested.get(key, floor))
    for key, ceiling in _BUDGET_CEILINGS.items():
        if key in requested:
            policy[key] = max(1, min(ceiling, int(requested[key])))
    for key in ("allowed_node_types", "allowed_edge_types"):
        if requested.get(key) is not None:
            policy[key] = set(requested[key])
    return policy


def _resolve_graph_path(graph_path: str) -> Path:
    allowed = Path(os.environ.get(ALLOWED_ROOT_ENV) or os.getcwd()).resolve()
    path = (allowed / graph_path).resolve()
    if not path.is_relative_to(allowed):
        raise ValueError(f"graph_path must stay inside {allowed}")
    if not path.is_file():
        raise ValueError(f"graph file {graph_path!r} not found")
    if path.stat().st_size > MAX_GRAPH_BYTES:
        raise ValueError(f"graph file exceeds {MAX_GRAPH_BYTES} bytes")
    return path


@register_tool("resolve_with_evidence")
def resolve_with_evidence(query_json: str, graph_path: str) -> str:
    """
    Answer a structured question against an evidence graph using the CTD constrained resolver.
    Only state RESOLVED is an answer. Any other state (PARTIAL, CONTRADICTED, UNRESOLVABLE, BUDGET_EXHAUSTED)
    means the evidence does not establish one. Report the gaps instead of guessing.

    Args:
        query_json: JSON object {"query": <QueryConstraintGraph>, "policy": {<optional search settings>}}, or a bare
            QueryConstraintGraph. A QueryConstraintGraph has "variables" (name, node_type), "relations"
            (id, subject_var, relation, object_var, hard) and "attributes" (id, variable, attribute, op, value, hard).
            Policy may only tighten thresholds (min_confidence, min_trust, ...) or budgets. Authorization fields are rejected.
        graph_path: Evidence graph snapshot JSON ({"nodes": [...], "edges": [...]}), relative to the allowed root.
    Returns:
        JSON with state, bindings, constraint status, contradictions, evidence ids and typed gaps.
    """
    try:
        from ctd.controller import RuntimePolicy
        from ctd.models import QueryConstraintGraph
        from ctd.resolver import Resolver
        from ctd.service import _graph_from_snapshot
    except ImportError as err:
        return f"[ERROR] CTD is not installed ({err}). Install with: pip install ./third_party/ctd"
    try:
        payload = json.loads(query_json)
        if not isinstance(payload, dict):
            raise ValueError("query_json must be a JSON object")
        raw_query = payload["query"] if "query" in payload else payload
        policy = RuntimePolicy(**build_policy(payload.get("policy")))
        query = QueryConstraintGraph.model_validate(raw_query)
        graph = _graph_from_snapshot(json.loads(_resolve_graph_path(graph_path).read_text(encoding="utf-8")))
    except (ValueError, KeyError, TypeError) as err:
        return f"[ERROR] {type(err).__name__}: {err}"

    result = Resolver(graph).resolve(query, policy)
    return json.dumps({
        "state": str(result.state),
        "answer_established": str(result.state) == "RESOLVED",
        "bindings": result.bindings,
        "resolved_constraints": result.resolved_constraints,
        "unresolved_constraints": result.unresolved_constraints,
        "violated_constraints": result.violated_constraints,
        "contradictions": result.contradictions,
        "evidence_ids": result.evidence_ids,
        "gaps": [g.model_dump(include={"constraint_id", "constraint_kind", "truth", "reason", "required_evidence",
                                       "suggested_action"}) for g in result.gaps],
        "next_actions": result.next_actions,
        "tenant_scope": policy.tenant_id,
    }, indent=2, default=str)
