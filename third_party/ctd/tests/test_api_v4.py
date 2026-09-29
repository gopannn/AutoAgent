from fastapi.testclient import TestClient

from ctd.api import create_app
from ctd.config import EngineSettings
from ctd.persistence import SQLiteStore


def R(pred, *args):
    return {"pred": pred, "args": list(args)}


def case_payload():
    return {
        "id": "incident:1",
        "domain": "distributed",
        "relations": [
            R("CAUSES", R("RETRIES", "client", "service"), R("INCREASES", "load", "pressure")),
            R("SATURATES", "service", "pressure"),
        ],
        "types": {"client": "actor", "service": "resource", "load": "load", "pressure": "state"},
        "metadata": {"incident": True},
        "severity": 5,
        "check_templates": {"SATURATES": "Load-test {0} against {1}."},
    }


def target_payload():
    return {
        "name": "gateway",
        "domain": "software",
        "relations": [R("CAUSES", R("REDUCES", "controller", "queue"), R("INCREASES", "demand", "target_pressure"))],
        "types": {"controller": "actor", "queue": "resource", "demand": "load", "target_pressure": "state"},
    }


def test_v4_encoding_ingest_transfer_premortem_and_metrics_are_additive():
    app = create_app(settings=EngineSettings(auth_mode="disabled"), store=SQLiteStore(":memory:"))
    client = TestClient(app)

    validation = client.post("/v4/encoding/validate", json={"case": case_payload()})
    ingest = client.post("/v4/structural/cases", json={"case": case_payload()})
    listing = client.get("/v4/structural/cases")
    transfer = client.post("/v4/transfer", json={"target": target_payload(), "policy": {"min_depth": 2}})
    premortem = client.post("/v4/premortem", json={"target": target_payload()})
    metrics = client.get("/v4/intelligence/metrics")
    legacy = client.get("/v3/capabilities")

    assert validation.status_code == 200 and validation.json()["ok"] is True
    assert ingest.status_code == 201
    assert listing.status_code == 200 and len(listing.json()["cases"]) == 1
    assert transfer.status_code == 200 and transfer.json()["hypotheses"]
    assert premortem.status_code == 200 and premortem.json()["findings"]
    assert metrics.status_code == 200
    assert legacy.status_code == 200
