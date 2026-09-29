from fastapi.testclient import TestClient

from ctd.api import create_app
from ctd.examples import build_supplier_demo


def test_health_and_inspector_are_available():
    client = TestClient(create_app())

    assert client.get("/health").json() == {"status": "ok"}
    inspector = client.get("/")
    assert inspector.status_code == 200
    assert "Constrained Topological Engine" in inspector.text


def test_supplier_demo_resolves_with_explainable_payload():
    client = TestClient(create_app())

    response = client.post("/demo/supplier")

    assert response.status_code == 200
    payload = response.json()
    assert payload["state"] == "RESOLVED"
    assert payload["bindings"]["supplier"] == "supplier:a"
    assert payload["evidence_ids"]
    assert payload["telemetry"]["widening_steps"] == 0
    assert payload["telemetry"]["planner_operations"]


def test_generic_resolve_uses_current_graph_and_supplied_policy():
    graph, query, _ = build_supplier_demo()
    client = TestClient(create_app(graph=graph))

    response = client.post(
        "/resolve",
        json={
            "query": query.model_dump(mode="json"),
            "policy": {"initial_arousal": 2, "max_arousal": 2},
        },
    )

    assert response.status_code == 200
    assert response.json()["state"] == "RESOLVED"


def test_graph_endpoint_exposes_nodes_and_evidence_edges():
    client = TestClient(create_app())

    payload = client.get("/graph").json()

    assert len(payload["nodes"]) >= 5
    assert any(edge["evidence"] for edge in payload["edges"])
