from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from ctd.auth import (
    ApiKeyIdentityStore,
    AuthenticationError,
    Authenticator,
    PolicyEnforcer,
    Principal,
    RolePolicy,
)
from ctd.config import EngineSettings
from ctd.controller import RuntimePolicy


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _jwt(secret: str, payload: dict, header: dict | None = None) -> str:
    header = header or {"alg": "HS256", "typ": "JWT"}
    h = _b64(json.dumps(header, separators=(",", ":")).encode())
    p = _b64(json.dumps(payload, separators=(",", ":")).encode())
    signature = hmac.new(secret.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest()
    return f"{h}.{p}.{_b64(signature)}"


def test_engine_settings_from_env_parses_production_options_and_safe_dict_hides_secrets():
    secret_hash = hashlib.sha256(b"secret").hexdigest()
    settings = EngineSettings.from_env(
        {
            "CTD_ENVIRONMENT": "production",
            "CTD_PERSISTENCE_BACKEND": "sqlite",
            "CTD_SQLITE_PATH": "/tmp/ctd.db",
            "CTD_AUTH_MODE": "api_key",
            "CTD_API_KEY_HASHES": json.dumps({"ops": secret_hash}),
            "CTD_TRACE_BACKEND": "jsonl",
            "CTD_TRACE_JSONL_PATH": "/tmp/ctd-trace.jsonl",
            "CTD_FEDERATION_PARALLELISM": "8",
        }
    )

    assert settings.environment == "production"
    assert settings.persistence_backend == "sqlite"
    assert settings.sqlite_path == "/tmp/ctd.db"
    assert settings.api_key_hashes == {"ops": secret_hash}
    assert settings.federation_parallelism == 8
    safe = settings.safe_dict()
    assert "api_key_hashes" not in safe
    assert "jwt_hs256_secret" not in safe
    assert "neo4j_password" not in safe


def test_engine_settings_validates_configured_dependencies():
    with pytest.raises(ValidationError):
        EngineSettings(persistence_backend="postgres")
    with pytest.raises(ValidationError):
        EngineSettings(auth_mode="hmac_jwt")
    with pytest.raises(ValidationError):
        EngineSettings(trace_backend="jsonl")


def test_api_key_authentication_uses_hash_and_constant_identity_lookup():
    digest = hashlib.sha256(b"s3cret").hexdigest()
    settings = EngineSettings(auth_mode="api_key", api_key_hashes={"ops": digest})
    identities = ApiKeyIdentityStore(
        {
            "ops": Principal(
                subject="analyst@example",
                tenant_id="acme",
                roles={"analyst"},
                scopes={"resolve:read", "resolve:advanced"},
                security_labels={"PUBLIC", "INTERNAL", "CONFIDENTIAL"},
                auth_method="api_key",
            )
        }
    )
    authenticator = Authenticator(settings, identities)

    principal = authenticator.authenticate_api_key("ops.s3cret")
    assert principal.subject == "analyst@example"
    assert principal.tenant_id == "acme"

    for invalid in ["ops.wrong", "unknown.s3cret", "malformed"]:
        with pytest.raises(AuthenticationError):
            authenticator.authenticate_api_key(invalid)


def test_hs256_jwt_authentication_validates_algorithm_signature_exp_issuer_and_audience():
    now = datetime(2026, 9, 12, tzinfo=UTC)
    secret = "jwt-secret"
    settings = EngineSettings(
        auth_mode="hmac_jwt",
        jwt_hs256_secret=secret,
        jwt_issuer="ctd-idp",
        jwt_audience="ctd-api",
    )
    authenticator = Authenticator(settings)
    claims = {
        "sub": "user:1",
        "tenant_id": "acme",
        "roles": ["analyst"],
        "scopes": ["resolve:read"],
        "security_labels": ["PUBLIC", "CONFIDENTIAL"],
        "iss": "ctd-idp",
        "aud": "ctd-api",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
    }

    principal = authenticator.authenticate_jwt(_jwt(secret, claims), now=now)
    assert principal.subject == "user:1"
    assert principal.auth_method == "hmac_jwt"

    bad_algorithm = _jwt(secret, claims, {"alg": "none", "typ": "JWT"})
    with pytest.raises(AuthenticationError):
        authenticator.authenticate_jwt(bad_algorithm, now=now)

    tampered = _jwt("other-secret", claims)
    with pytest.raises(AuthenticationError):
        authenticator.authenticate_jwt(tampered, now=now)

    expired_claims = {**claims, "exp": int((now - timedelta(seconds=1)).timestamp())}
    with pytest.raises(AuthenticationError):
        authenticator.authenticate_jwt(_jwt(secret, expired_claims), now=now)

    wrong_issuer = {**claims, "iss": "other"}
    with pytest.raises(AuthenticationError):
        authenticator.authenticate_jwt(_jwt(secret, wrong_issuer), now=now)

    wrong_audience = {**claims, "aud": "other"}
    with pytest.raises(AuthenticationError):
        authenticator.authenticate_jwt(_jwt(secret, wrong_audience), now=now)


def test_disabled_auth_returns_internal_unrestricted_principal():
    principal = Authenticator(EngineSettings()).authenticate(None)
    assert principal.auth_method == "disabled"
    assert "*" in principal.scopes
    assert "admin" in principal.roles


def test_policy_enforcer_intersects_role_focus_and_sets_tenant_without_broadening_request():
    role_policy = RolePolicy(
        role_scopes={"analyst": {"resolve:read", "resolve:advanced"}},
        role_security_labels={"analyst": {"PUBLIC", "INTERNAL", "CONFIDENTIAL"}},
        role_node_types={"analyst": {"Supplier", "Component", "Certification"}},
        role_edge_types={"analyst": {"PRODUCES", "CERTIFIED_BY"}},
    )
    enforcer = PolicyEnforcer(role_policy)
    principal = Principal(
        subject="u",
        tenant_id="acme",
        roles={"analyst"},
        scopes={"resolve:read", "resolve:advanced"},
        security_labels={"PUBLIC", "CONFIDENTIAL"},
        auth_method="internal",
    )
    requested = RuntimePolicy(
        max_expansions=25,
        allowed_node_types={"Supplier", "SecretType"},
        allowed_edge_types={"PRODUCES", "SECRET_EDGE"},
        allowed_security_labels={"PUBLIC", "TOP_SECRET"},
    )

    effective = enforcer.apply(principal, requested)

    assert effective.max_expansions == 25
    assert effective.tenant_id == "acme"
    assert effective.allowed_node_types == {"Supplier"}
    assert effective.allowed_edge_types == {"PRODUCES"}
    assert effective.allowed_security_labels == {"PUBLIC"}


def test_scope_check_supports_admin_wildcard_and_rejects_missing_scope():
    enforcer = PolicyEnforcer(RolePolicy.default())
    admin = Principal(subject="a", roles={"admin"}, scopes={"*"}, auth_method="internal")
    viewer = Principal(subject="v", roles={"viewer"}, scopes={"resolve:read"}, auth_method="internal")

    enforcer.require_scope(admin, "resolve:advanced")
    enforcer.require_scope(viewer, "resolve:read")
    with pytest.raises(PermissionError):
        enforcer.require_scope(viewer, "resolve:advanced")
