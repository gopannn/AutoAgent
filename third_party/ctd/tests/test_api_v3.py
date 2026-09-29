from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from ctd.api import create_app
from ctd.auth import ApiKeyIdentityStore, Principal
from ctd.config import EngineSettings
from ctd.examples import DEMO_AS_OF, build_supplier_demo


def _text_payload():
    return {
        "text": (
            "Find a supplier that produces component X, certification is ISO9001, "
            "lead time at most 30 days, price under 500"
        ),
        "as_of": DEMO_AS_OF.isoformat(),
    }


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _jwt(secret: str, claims: dict) -> str:
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64(json.dumps(claims, separators=(",", ":")).encode())
    signature = hmac.new(secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64(signature)}"


def test_v3_public_health_capabilities_and_ontology_are_available():
    client = TestClient(create_app())

    live = client.get("/health/live")
    ready = client.get("/health/ready")
    capabilities = client.get("/v3/capabilities")
    ontology = client.get("/v3/ontology")

    assert live.status_code == 200 and live.json()["status"] == "ok"
    assert ready.status_code == 200 and ready.json()["ready"] is True
    assert capabilities.json()["version"] == "0.3.0"
    assert "natural_language_compilation" in capabilities.json()["features"]
    assert ontology.status_code == 200
    assert any(item["name"] == "Supplier" for item in ontology.json()["types"])


def test_v3_compile_plan_resolve_async_execution_replay_metrics_and_provider_health():
    client = TestClient(create_app())

    compiled = client.post("/v3/compile", json=_text_payload())
    assert compiled.status_code == 200
    assert compiled.json()["query"]["variables"][0]["node_type"] == "Supplier"

    planned = client.post("/v3/plan", json=_text_payload())
    assert planned.status_code == 200
    assert planned.json()["operations"]

    resolved = client.post("/v3/resolve", json=_text_payload())
    assert resolved.status_code == 200
    body = resolved.json()
    assert body["state"] == "RESOLVED"
    assert body["bindings"]["supplier"] == "supplier:a"

    async_result = client.post("/v3/resolve/async", json=_text_payload())
    assert async_result.status_code == 200
    assert async_result.json()["state"] == "RESOLVED"

    execution_id = body["execution_id"]
    execution = client.get(f"/v3/executions/{execution_id}")
    verification = client.post(f"/v3/executions/{execution_id}/verify-replay")
    metrics = client.get("/v3/metrics")
    provider_health = client.get("/v3/providers/health")

    assert execution.status_code == 200
    assert verification.json()["match"] is True
    assert metrics.json()["executions"] >= 2
    assert provider_health.json()["providers"][0]["status"] == "healthy"


def test_v3_api_key_auth_returns_401_for_invalid_and_403_for_missing_scope_but_legacy_stays_open():
    viewer_secret = "viewer-secret"
    analyst_secret = "analyst-secret"
    settings = EngineSettings(
        auth_mode="api_key",
        api_key_hashes={
            "viewer": hashlib.sha256(viewer_secret.encode()).hexdigest(),
            "analyst": hashlib.sha256(analyst_secret.encode()).hexdigest(),
        },
    )
    identities = ApiKeyIdentityStore(
        {
            "viewer": Principal(
                subject="viewer",
                roles={"viewer"},
                scopes={"resolve:read"},
                security_labels={"PUBLIC", "INTERNAL"},
                auth_method="api_key",
            ),
            "analyst": Principal(
                subject="analyst",
                roles={"analyst"},
                scopes={"resolve:read", "resolve:advanced"},
                security_labels={"PUBLIC", "INTERNAL", "CONFIDENTIAL"},
                auth_method="api_key",
            ),
        }
    )
    client = TestClient(create_app(settings=settings, identity_store=identities))

    assert client.post("/v3/resolve", json=_text_payload()).status_code == 401
    assert client.post("/v3/resolve", json=_text_payload(), headers={"X-CTD-API-Key": "viewer.wrong"}).status_code == 401
    assert client.post("/v3/plan", json=_text_payload(), headers={"X-CTD-API-Key": f"viewer.{viewer_secret}"}).status_code == 403
    assert client.post("/v3/plan", json=_text_payload(), headers={"X-CTD-API-Key": f"analyst.{analyst_secret}"}).status_code == 200

    # Legacy v2 remains backward-compatible unless a future explicit legacy
    # protection setting is introduced.
    _, query, policy = build_supplier_demo()
    legacy = client.post("/v2/resolve", json={"query": query.model_dump(mode="json"), "policy": policy.model_dump(mode="json")})
    assert legacy.status_code == 200


def test_v3_hmac_jwt_auth_accepts_valid_token_and_rejects_tampering():
    now = datetime.now(tz=UTC)
    secret = "jwt-secret"
    settings = EngineSettings(auth_mode="hmac_jwt", jwt_hs256_secret=secret, jwt_issuer="idp", jwt_audience="ctd")
    claims = {
        "sub": "analyst",
        "roles": ["analyst"],
        "scopes": ["resolve:read", "resolve:advanced"],
        "security_labels": ["PUBLIC", "INTERNAL", "CONFIDENTIAL"],
        "iss": "idp",
        "aud": "ctd",
        "exp": int((now + timedelta(minutes=5)).timestamp()),
    }
    token = _jwt(secret, claims)
    client = TestClient(create_app(settings=settings))

    valid = client.post("/v3/resolve", json=_text_payload(), headers={"Authorization": f"Bearer {token}"})
    invalid = client.post("/v3/resolve", json=_text_payload(), headers={"Authorization": f"Bearer {token}x"})

    assert valid.status_code == 200
    assert invalid.status_code == 401


def test_v3_persistent_backend_does_not_silently_fall_back_to_demo_graph(tmp_path):
    settings = EngineSettings(
        persistence_backend="sqlite",
        sqlite_path=str(tmp_path / "empty.db"),
    )
    client = TestClient(create_app(settings=settings))

    result = client.post("/v3/resolve", json=_text_payload())

    assert result.status_code == 200
    assert result.json()["state"] == "UNRESOLVABLE"
    # Legacy demo behavior remains separate and backward compatible.
    assert client.post("/demo/supplier").json()["state"] == "RESOLVED"
