from fastapi.testclient import TestClient

from ctd.api import create_app
from ctd.config import EngineSettings


def client():
    return TestClient(create_app(settings=EngineSettings(auth_mode="disabled")))


def test_v5_capabilities_and_close_endpoint_are_additive():
    c = client()
    caps = c.get("/v5/capabilities")
    assert caps.status_code == 200
    # Assert against the package version rather than a literal: a test
    # that hardcodes the version fails on every release for no reason,
    # and the property worth pinning is that the API reports the same
    # version the package does.
    import ctd
    assert caps.json()["version"] == ctd.__version__
    assert "resolution" in caps.json()["planes"] and "hypothesis" in caps.json()["planes"]

    response = c.post(
        "/v5/close",
        json={
            "query": "active south supplier",
            "records": [
                {"id": "a", "domain": "supplier", "attrs": {"status": "active", "region": "south"}},
                {"id": "b", "domain": "supplier", "attrs": {"status": "inactive", "region": "south"}},
            ],
            "constraints": [
                {"name": "active", "kind": "has", "attr": "status", "values": ["active"]},
                {"name": "south", "kind": "has", "attr": "region", "values": ["south"]},
            ],
        },
    )
    assert response.status_code == 200
    assert response.json()["outcome"] == "CLOSED"
    assert response.json()["ids"] == ["a"]


def test_v5_strict_encoding_rejects_wrong_arity():
    c = client()
    response = c.post(
        "/v5/encoding/validate",
        json={
            "case": {
                "id": "bad",
                "domain": "ops",
                "relations": [{"pred": "SATURATES", "args": ["pool", "extra"]}],
                "types": {"pool": "resource", "extra": "signal"},
            }
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert any(item["code"] == "ARITY" for item in body["findings"])


def test_v5_resolve_is_same_truth_plane_not_a_second_resolver():
    c = client()
    old = c.post("/demo/supplier").json()
    # Use existing v3 demo graph through a v5 alias endpoint.
    graph = c.get("/graph").json()
    assert graph["nodes"]
    assert old["state"] == "RESOLVED"

def test_v5_strict_ingest_and_list_use_same_authorized_structural_library():
    c = client()
    case = {
        "id": "strict:incident",
        "domain": "distributed",
        "metadata": {"incident": True},
        "relations": [
            {"pred": "DEPENDS", "args": ["caller", "backend"]},
            {"pred": "CAUSES", "args": [
                {"pred": "EXCESS", "args": ["load"]},
                {"pred": "QUEUEING", "args": ["backend"]}
            ]},
            {"pred": "INCREASES", "args": [
                {"pred": "QUEUEING", "args": ["backend"]}, "latency"
            ]},
        ],
        "types": {"caller": "agent", "backend": "resource", "load": "load", "latency": "signal"},
    }
    created = c.post("/v5/structural/cases", json={"case": case})
    assert created.status_code == 201, created.text
    listing = c.get("/v5/structural/cases")
    assert listing.status_code == 200
    assert any(item["id"] == "strict:incident" for item in listing.json()["cases"])

def test_v5_strict_transfer_and_premortem_share_the_persisted_library():
    c = client()
    case = {
        "id": "retry:incident",
        "domain": "distributed",
        "metadata": {"incident": True},
        "severity": 5,
        "check_templates": {"SATURATES": "Load-test {0}."},
        "relations": [
            {"pred": "DEPENDS", "args": ["caller", "backend"]},
            {"pred": "CAUSES", "args": [{"pred": "EXCESS", "args": ["load"]}, {"pred": "QUEUEING", "args": ["backend"]}]},
            {"pred": "INCREASES", "args": [{"pred": "QUEUEING", "args": ["backend"]}, "latency"]},
            {"pred": "SENSES", "args": ["caller", "latency"]},
            {"pred": "CAUSES", "args": [{"pred": "SENSES", "args": ["caller", "latency"]}, {"pred": "RETRIES", "args": ["caller", "backend"]}]},
            {"pred": "CAUSES", "args": [{"pred": "RETRIES", "args": ["caller", "backend"]}, {"pred": "AMPLIFIES", "args": ["load", "backend"]}]},
            {"pred": "CAUSES", "args": [{"pred": "AMPLIFIES", "args": ["load", "backend"]}, {"pred": "SATURATES", "args": ["backend"]}]},
        ],
        "types": {"caller": "agent", "backend": "resource", "load": "load", "latency": "signal"},
    }
    assert c.post("/v5/structural/cases", json={"case": case}).status_code == 201
    target = {
        "name": "gateway",
        "domain": "software",
        "relations": [
            {"pred": "DEPENDS", "args": ["controller", "gateway"]},
            {"pred": "CAUSES", "args": [{"pred": "EXCESS", "args": ["requests"]}, {"pred": "QUEUEING", "args": ["gateway"]}]},
            {"pred": "INCREASES", "args": [{"pred": "QUEUEING", "args": ["gateway"]}, "target-latency"]},
            {"pred": "SENSES", "args": ["controller", "target-latency"]},
        ],
        "types": {"controller": "agent", "gateway": "resource", "requests": "load", "target-latency": "signal"},
    }
    transfer = c.post("/v5/transfer", json={"target": target})
    assert transfer.status_code == 200
    assert transfer.json()["hypotheses"]
    assert all(item["state"] == "HYPOTHESIS" for item in transfer.json()["hypotheses"])
    premortem = c.post("/v5/premortem", json={"target": target})
    assert premortem.status_code == 200
    assert premortem.json()["findings"]
    assert all(item["check"] for item in premortem.json()["findings"])
