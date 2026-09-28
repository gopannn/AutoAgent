from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class EngineSettings(BaseModel):
    environment: str = "development"
    persistence_backend: Literal["memory", "sqlite", "postgres"] = "memory"
    sqlite_path: str = ":memory:"
    postgres_dsn: str | None = None
    neo4j_uri: str | None = None
    neo4j_username: str | None = None
    neo4j_password: str | None = None
    auth_mode: Literal["disabled", "api_key", "hmac_jwt"] = "disabled"
    api_key_hashes: dict[str, str] = Field(default_factory=dict)
    jwt_hs256_secret: str | None = None
    jwt_issuer: str | None = None
    jwt_audience: str | None = None
    trace_backend: Literal["memory", "jsonl", "otel"] = "memory"
    trace_jsonl_path: str | None = None
    otel_service_name: str = "ctd-engine"
    remote_api_timeout_s: float = Field(default=2.0, gt=0.0)
    remote_api_max_bytes: int = Field(default=2_000_000, ge=1)
    federation_parallelism: int = Field(default=4, ge=1, le=64)
    compiler_fuzzy_threshold: float = Field(default=0.82, ge=0.0, le=1.0)
    advisory_priority_window: float = Field(default=0.05, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_dependencies(self) -> "EngineSettings":
        if self.persistence_backend == "postgres" and not self.postgres_dsn:
            raise ValueError("postgres_dsn is required when persistence_backend=postgres")
        if self.auth_mode == "hmac_jwt" and not self.jwt_hs256_secret:
            raise ValueError("jwt_hs256_secret is required when auth_mode=hmac_jwt")
        if self.trace_backend == "jsonl" and not self.trace_jsonl_path:
            raise ValueError("trace_jsonl_path is required when trace_backend=jsonl")
        return self

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "EngineSettings":
        source = os.environ if env is None else env

        def value(name: str, default: str | None = None) -> str | None:
            return source.get(f"CTD_{name}", default)

        hashes_raw = value("API_KEY_HASHES", "{}") or "{}"
        try:
            hashes = json.loads(hashes_raw)
        except json.JSONDecodeError as exc:
            raise ValueError("CTD_API_KEY_HASHES must be valid JSON") from exc
        if not isinstance(hashes, dict) or not all(
            isinstance(key, str) and isinstance(item, str) for key, item in hashes.items()
        ):
            raise ValueError("CTD_API_KEY_HASHES must be a JSON object of string hashes")

        return cls(
            environment=value("ENVIRONMENT", "development") or "development",
            persistence_backend=value("PERSISTENCE_BACKEND", "memory") or "memory",
            sqlite_path=value("SQLITE_PATH", ":memory:") or ":memory:",
            postgres_dsn=value("POSTGRES_DSN"),
            neo4j_uri=value("NEO4J_URI"),
            neo4j_username=value("NEO4J_USERNAME"),
            neo4j_password=value("NEO4J_PASSWORD"),
            auth_mode=value("AUTH_MODE", "disabled") or "disabled",
            api_key_hashes=hashes,
            jwt_hs256_secret=value("JWT_HS256_SECRET"),
            jwt_issuer=value("JWT_ISSUER"),
            jwt_audience=value("JWT_AUDIENCE"),
            trace_backend=value("TRACE_BACKEND", "memory") or "memory",
            trace_jsonl_path=value("TRACE_JSONL_PATH"),
            otel_service_name=value("OTEL_SERVICE_NAME", "ctd-engine") or "ctd-engine",
            remote_api_timeout_s=float(value("REMOTE_API_TIMEOUT_S", "2.0") or "2.0"),
            remote_api_max_bytes=int(value("REMOTE_API_MAX_BYTES", "2000000") or "2000000"),
            federation_parallelism=int(value("FEDERATION_PARALLELISM", "4") or "4"),
            compiler_fuzzy_threshold=float(value("COMPILER_FUZZY_THRESHOLD", "0.82") or "0.82"),
            advisory_priority_window=float(value("ADVISORY_PRIORITY_WINDOW", "0.05") or "0.05"),
        )

    def safe_dict(self) -> dict:
        payload = self.model_dump(mode="json")
        for key in ("api_key_hashes", "jwt_hs256_secret", "neo4j_password", "postgres_dsn"):
            payload.pop(key, None)
        return payload
