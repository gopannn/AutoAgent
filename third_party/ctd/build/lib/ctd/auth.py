from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

from .config import EngineSettings
from .controller import RuntimePolicy


class AuthenticationError(ValueError):
    pass


class AuthorizationError(PermissionError):
    pass


class Principal(BaseModel):
    subject: str
    tenant_id: str | None = None
    roles: set[str] = Field(default_factory=set)
    scopes: set[str] = Field(default_factory=set)
    security_labels: set[str] = Field(default_factory=set)
    auth_method: Literal["disabled", "api_key", "hmac_jwt", "internal"] = "internal"


class ApiKeyIdentityStore:
    def __init__(self, identities: dict[str, Principal] | None = None) -> None:
        self._identities = {key: value.model_copy(deep=True) for key, value in (identities or {}).items()}

    def get(self, key_id: str) -> Principal | None:
        principal = self._identities.get(key_id)
        return principal.model_copy(deep=True) if principal is not None else None


class Authenticator:
    def __init__(
        self,
        settings: EngineSettings,
        identity_store: ApiKeyIdentityStore | None = None,
    ) -> None:
        self.settings = settings
        self.identity_store = identity_store or ApiKeyIdentityStore()

    def authenticate(self, credential: str | None) -> Principal:
        if self.settings.auth_mode == "disabled":
            return Principal(
                subject="anonymous",
                roles={"admin"},
                scopes={"*"},
                security_labels={"*"},
                auth_method="disabled",
            )
        if not credential:
            raise AuthenticationError("credential required")
        if self.settings.auth_mode == "api_key":
            return self.authenticate_api_key(credential)
        return self.authenticate_jwt(credential)

    def authenticate_api_key(self, value: str) -> Principal:
        try:
            key_id, secret = value.split(".", 1)
        except ValueError as exc:
            raise AuthenticationError("invalid API key format") from exc
        expected = self.settings.api_key_hashes.get(key_id)
        actual = hashlib.sha256(secret.encode("utf-8")).hexdigest()
        # Compare even for missing IDs to avoid an obvious timing distinction.
        valid = hmac.compare_digest(actual, expected or "0" * 64)
        principal = self.identity_store.get(key_id)
        if not valid or principal is None:
            raise AuthenticationError("invalid API key")
        principal.auth_method = "api_key"
        return principal

    def authenticate_jwt(self, token: str, *, now: datetime | None = None) -> Principal:
        now = now or datetime.now(tz=UTC)
        try:
            encoded_header, encoded_payload, encoded_signature = token.split(".")
            header = self._decode_json(encoded_header)
            claims = self._decode_json(encoded_payload)
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise AuthenticationError("malformed JWT") from exc
        if header.get("alg") != "HS256":
            raise AuthenticationError("JWT algorithm must be HS256")
        secret = self.settings.jwt_hs256_secret
        if not secret:
            raise AuthenticationError("JWT authentication is not configured")
        signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
        expected_signature = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
        try:
            actual_signature = self._b64decode(encoded_signature)
        except ValueError as exc:
            raise AuthenticationError("malformed JWT signature") from exc
        if not hmac.compare_digest(actual_signature, expected_signature):
            raise AuthenticationError("invalid JWT signature")

        now_ts = int(now.timestamp())
        exp = claims.get("exp")
        if not isinstance(exp, (int, float)) or now_ts >= int(exp):
            raise AuthenticationError("JWT expired or missing exp")
        nbf = claims.get("nbf")
        if nbf is not None and (not isinstance(nbf, (int, float)) or now_ts < int(nbf)):
            raise AuthenticationError("JWT not yet valid")
        if self.settings.jwt_issuer is not None and claims.get("iss") != self.settings.jwt_issuer:
            raise AuthenticationError("invalid JWT issuer")
        if self.settings.jwt_audience is not None:
            audience = claims.get("aud")
            valid_audience = (
                self.settings.jwt_audience in audience
                if isinstance(audience, list)
                else audience == self.settings.jwt_audience
            )
            if not valid_audience:
                raise AuthenticationError("invalid JWT audience")
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise AuthenticationError("JWT missing subject")

        return Principal(
            subject=subject,
            tenant_id=claims.get("tenant_id") if isinstance(claims.get("tenant_id"), str) else None,
            roles=self._string_set(claims.get("roles")),
            scopes=self._string_set(claims.get("scopes")),
            security_labels=self._string_set(claims.get("security_labels")),
            auth_method="hmac_jwt",
        )

    @staticmethod
    def _string_set(value: object) -> set[str]:
        if isinstance(value, str):
            return {value}
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return set(value)
        return set()

    @staticmethod
    def _b64decode(value: str) -> bytes:
        padding = "=" * (-len(value) % 4)
        try:
            return base64.urlsafe_b64decode((value + padding).encode("ascii"))
        except Exception as exc:
            raise ValueError("invalid base64") from exc

    @classmethod
    def _decode_json(cls, value: str) -> dict:
        decoded = cls._b64decode(value).decode("utf-8")
        payload = json.loads(decoded)
        if not isinstance(payload, dict):
            raise ValueError("JWT segment must be an object")
        return payload


class RolePolicy(BaseModel):
    role_scopes: dict[str, set[str]] = Field(default_factory=dict)
    role_security_labels: dict[str, set[str]] = Field(default_factory=dict)
    role_node_types: dict[str, set[str]] = Field(default_factory=dict)
    role_edge_types: dict[str, set[str]] = Field(default_factory=dict)

    @classmethod
    def default(cls) -> "RolePolicy":
        return cls(
            role_scopes={
                "viewer": {"resolve:read"},
                "analyst": {"resolve:read", "resolve:advanced", "intelligence:read"},
                "admin": {"*"},
            },
            role_security_labels={
                "viewer": {"PUBLIC", "INTERNAL"},
                "analyst": {"PUBLIC", "INTERNAL", "CONFIDENTIAL"},
                "admin": {"*"},
            },
        )


class PolicyEnforcer:
    def __init__(self, role_policy: RolePolicy | None = None) -> None:
        self.role_policy = role_policy or RolePolicy.default()

    def require_scope(self, principal: Principal, scope: str) -> None:
        effective = self._effective_set(principal, self.role_policy.role_scopes, principal.scopes)
        if "*" not in effective and scope not in effective:
            raise AuthorizationError(f"missing required scope: {scope}")

    def apply(self, principal: Principal, requested: RuntimePolicy) -> RuntimePolicy:
        effective_labels = self._effective_set(
            principal, self.role_policy.role_security_labels, principal.security_labels
        )
        role_nodes = self._effective_set(principal, self.role_policy.role_node_types, set())
        role_edges = self._effective_set(principal, self.role_policy.role_edge_types, set())

        payload = requested.model_dump()
        payload["tenant_id"] = principal.tenant_id
        payload["allowed_security_labels"] = self._intersect_focus(
            requested.allowed_security_labels, effective_labels
        )
        payload["allowed_node_types"] = self._intersect_focus(
            requested.allowed_node_types, role_nodes, empty_means_unrestricted=True
        )
        payload["allowed_edge_types"] = self._intersect_focus(
            requested.allowed_edge_types, role_edges, empty_means_unrestricted=True
        )
        return RuntimePolicy.model_validate(payload)

    @staticmethod
    def _effective_set(
        principal: Principal,
        role_mapping: dict[str, set[str]],
        explicit: set[str],
    ) -> set[str]:
        result = set(explicit)
        for role in principal.roles:
            result.update(role_mapping.get(role, set()))
        return result

    @staticmethod
    def _intersect_focus(
        requested: set[str] | None,
        allowed: set[str],
        *,
        empty_means_unrestricted: bool = False,
    ) -> set[str] | None:
        if "*" in allowed:
            return None if requested is None else set(requested)
        if not allowed and empty_means_unrestricted:
            return None if requested is None else set(requested)
        if requested is None:
            return set(allowed)
        return set(requested) & set(allowed)
