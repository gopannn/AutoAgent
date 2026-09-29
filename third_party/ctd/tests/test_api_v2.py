from fastapi.testclient import TestClient

from ctd.api import create_app
from ctd.examples import build_supplier_demo


def payload():
    _, query, policy = build_supplier_demo()
    return {
        "query": query.model_dump(mode="json"),
        "policy": policy.model_dump(mode="json"),
    }


def test_v2_plan_and_resolve_expose_plan_uncertainty_and_execution_record():
    client = TestClient(create_app())

    plan = client.post("/v2/plan", json=payload())
    response = client.post("/v2/resolve", json=payload())

    assert plan.status_code == 200
    assert plan.json()["operations"]
    assert response.status_code == 200
    result = response.json()
    assert result["state"] == "RESOLVED"
    assert "uncertainty" in result
    assert result["execution_id"]

    execution = client.get(f"/v2/executions/{result['execution_id']}")
    assert execution.status_code == 200
    record = execution.json()
    assert record["query"]["objective"] == "find compliant supplier for component X"
    assert record["result"]["state"] == "RESOLVED"
    assert record["snapshot_hash"]


def test_v2_execution_can_be_replayed_from_stored_graph_snapshot():
    client = TestClient(create_app())
    original = client.post("/v2/resolve", json=payload()).json()

    replay = client.post(f"/v2/executions/{original['execution_id']}/replay")

    assert replay.status_code == 200
    replayed = replay.json()
    assert replayed["state"] == original["state"]
    assert replayed["bindings"] == original["bindings"]
    assert replayed["resolved_constraints"] == original["resolved_constraints"]

    verification = client.post(f"/v2/executions/{original['execution_id']}/verify-replay").json()
    assert verification["match"] is True
    assert verification["expected_fingerprint"] == verification["replay_fingerprint"]


def test_v2_capabilities_are_machine_readable():
    client = TestClient(create_app())

    capabilities = client.get("/v2/capabilities").json()

    assert "cost_based_planning" in capabilities["features"]
    assert "BUDGET_EXHAUSTED" in capabilities["terminal_states"]
