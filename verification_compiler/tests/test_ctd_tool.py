"""The CTD resolver tool: answers only when evidence establishes one, and authorization is operator-owned."""
import json

import pytest

try:
    from autoagent.tools import ctd_tool
    from ctd.examples import build_supplier_demo
except Exception as err:  # noqa: BLE001
    pytest.skip(f"autoagent or ctd not importable: {err}", allow_module_level=True)

QUERY = json.loads(
    (__import__("pathlib").Path(__file__).resolve().parents[2] / "third_party/ctd/examples/supplier_query.json").read_text()
)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv(ctd_tool.ALLOWED_ROOT_ENV, str(tmp_path))
    for env in ctd_tool._AUTHZ_ENV.values():
        monkeypatch.delenv(env, raising=False)
    graph, _, _ = build_supplier_demo()
    (tmp_path / "graph.json").write_text(json.dumps(graph.snapshot(), default=str))
    return tmp_path


def run(query=None, graph="graph.json"):
    out = ctd_tool.resolve_with_evidence(json.dumps(query or {"query": QUERY["query"]}), graph)
    return out if out.startswith("[ERROR]") else json.loads(out)


def test_resolves_when_evidence_establishes_an_answer(workspace):
    out = run()
    assert out["state"] == "RESOLVED" and out["answer_established"]
    assert out["bindings"]["supplier"] == "supplier:a"
    assert out["evidence_ids"]


def test_abstains_instead_of_guessing(workspace):
    query = json.loads(json.dumps(QUERY["query"]))
    for attr in query["attributes"]:
        if attr["id"] == "a:component-code":
            attr["value"] = "NO_SUCH_COMPONENT"
    out = run({"query": query})
    assert out["state"] != "RESOLVED" and not out["answer_established"]
    assert out["bindings"] == {} or out["unresolved_constraints"] or out["violated_constraints"]


@pytest.mark.parametrize("field", ["tenant_id", "allowed_security_labels", "authorization_scope", "tenant_mode"])
def test_model_cannot_set_authorization(workspace, field):
    out = run({"query": QUERY["query"], "policy": {field: "anything"}})
    assert out.startswith("[ERROR]") and "set by the operator" in out


def test_operator_tenant_scope_fails_closed_on_untagged_evidence(workspace, monkeypatch):
    monkeypatch.setenv("CTD_TOOL_TENANT_ID", "acme")
    out = run()
    assert out["tenant_scope"] == "acme"
    assert out["state"] != "RESOLVED", "untagged demo records must be invisible to a tenant-scoped query"


def test_model_can_only_tighten_thresholds_and_budgets():
    policy = ctd_tool.build_policy({"min_confidence": 0.1, "min_trust": 0.95, "max_expansions": 10**9})
    assert policy["min_confidence"] == 0.7          # cannot loosen below the operator floor
    assert policy["min_trust"] == 0.95              # may tighten
    assert policy["max_expansions"] == 5000         # capped at the operator ceiling
    with pytest.raises(ValueError, match="unsupported"):
        ctd_tool.build_policy({"validation_reserve_ratio": 0})


def test_graph_path_is_confined(workspace):
    assert "must stay inside" in run(graph="../../etc/passwd")
    assert "not found" in run(graph="missing.json")
