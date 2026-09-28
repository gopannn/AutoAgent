"""Compiler configuration: models, limits, sandbox images and retry policy.

Everything that affects a build's identity is recorded in the release manifest,
so configuration is read once at startup and treated as immutable afterwards.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

COMPILER_VERSION = "2.0.0"

# An image is only acceptable when it is content-addressed: either
# `repo[:tag]@sha256:<digest>` or a bare local image ID `sha256:<id>`.
_DIGEST_REF = re.compile(r"^(?:[a-z0-9][a-z0-9._/:-]*@)?sha256:[0-9a-f]{64}$")


def is_digest_pinned(ref: str) -> bool:
    return bool(_DIGEST_REF.match(ref))


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


class ModelConfig(BaseModel):
    """Model per role. A name starting with `claude` is served by Anthropic, anything else by OpenAI."""

    architect: str = "gpt-6-sol"
    verification_compiler: str = "gpt-6-astra"
    adversarial_auditor: str = "gpt-6-astra"
    repository_builder: str = "claude-opus-5-5"
    semantic_reviewer: str = "claude-opus-5-5"
    anthropic_max_tokens: int = 32_000

    @classmethod
    def from_env(cls) -> "ModelConfig":
        defaults = cls()
        return cls(
            architect=_env("VC_MODEL_ARCHITECT", defaults.architect),
            verification_compiler=_env("VC_MODEL_VERIFICATION_COMPILER", defaults.verification_compiler),
            adversarial_auditor=_env("VC_MODEL_AUDITOR", defaults.adversarial_auditor),
            repository_builder=_env("VC_MODEL_BUILDER", defaults.repository_builder),
            semantic_reviewer=_env("VC_MODEL_REVIEWER", defaults.semantic_reviewer),
            anthropic_max_tokens=int(_env("VC_ANTHROPIC_MAX_TOKENS", str(defaults.anthropic_max_tokens))),
        )


class Limits(BaseModel):
    max_files: int = 500
    max_file_bytes: int = 1_000_000
    max_repo_bytes: int = 50_000_000
    max_path_length: int = 255
    max_dependencies: int = 100


class SandboxConfig(BaseModel):
    """Container execution settings. Images must be pinned by digest; there is no host fallback."""

    verifier_image: str = Field(description="Image with ruff/pyright/semgrep/pytest/httpx (see docker/verifier.Dockerfile).")
    runtime_image: str = Field(description="Python runtime image the service under test runs in.")
    require_gvisor: bool = True
    memory: str = "1g"
    cpus: str = "2"
    pids_limit: int = 256
    sut_port: int = 8000
    install_timeout_s: int = 180
    static_timeout_s: int = 300
    sut_ready_timeout_s: int = 30
    oracle_timeout_s: int = 300
    wheel_platform: str = "manylinux_2_28_x86_64"
    uv_python_platform: str = "x86_64-manylinux_2_28"
    python_version: str = "3.12"
    cache_dir: Path = Path.home() / ".cache" / "verification_compiler"

    @field_validator("verifier_image", "runtime_image")
    @classmethod
    def _pinned(cls, ref: str) -> str:
        if not is_digest_pinned(ref):
            raise ValueError(f"image '{ref}' must be pinned by digest (repo@sha256:... or sha256:<image id>)")
        return ref

    @classmethod
    def from_env(cls) -> "SandboxConfig":
        missing = [n for n in ("VC_VERIFIER_IMAGE", "VC_RUNTIME_IMAGE") if not os.environ.get(n)]
        if missing:
            raise RuntimeError(f"Missing required sandbox configuration: {', '.join(missing)}")
        return cls(
            verifier_image=os.environ["VC_VERIFIER_IMAGE"],
            runtime_image=os.environ["VC_RUNTIME_IMAGE"],
            require_gvisor=_env("VC_REQUIRE_GVISOR", "1") != "0",
        )


class CompilerConfig(BaseModel):
    models: ModelConfig = Field(default_factory=ModelConfig)
    limits: Limits = Field(default_factory=Limits)
    sandbox: SandboxConfig
    max_repair_rounds: int = 4
    # Findings of these severities block release. Others are recorded in the manifest as accepted risk.
    blocking_severities: frozenset[str] = frozenset({"critical", "high", "medium"})
    spec_compile_attempts: int = 2
    signing_key_path: str | None = None

    @classmethod
    def from_env(cls) -> "CompilerConfig":
        return cls(
            models=ModelConfig.from_env(),
            sandbox=SandboxConfig.from_env(),
            max_repair_rounds=int(_env("VC_MAX_REPAIR_ROUNDS", "4")),
            signing_key_path=os.environ.get("VC_SIGNING_KEY") or None,
        )

    def recursion_limit(self) -> int:
        # Setup (~5 steps) plus at most ~7 steps per repair round, with headroom.
        return 16 + (self.max_repair_rounds + 1) * 8


# ------------------------------------------------------------------
# Retry policy
# ------------------------------------------------------------------

_TRANSIENT_STATUS = frozenset({408, 429, 500, 502, 503, 504, 529})
_TRANSIENT_EXC_NAMES = frozenset({
    "APIConnectionError", "APITimeoutError",          # openai / anthropic SDKs
    "TransportError", "TimeoutException",             # httpx
    "ConnectError", "ReadTimeout", "RemoteProtocolError",
})


def is_transient(exc: BaseException) -> bool:
    """True only for errors a retry can plausibly fix. Anything unrecognised is permanent."""
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int):
        return status in _TRANSIENT_STATUS
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    return any(cls.__name__ in _TRANSIENT_EXC_NAMES for cls in type(exc).__mro__)


def transient_retry_policy():
    from langgraph.types import RetryPolicy

    return RetryPolicy(
        initial_interval=1.0,
        backoff_factor=2.0,
        max_interval=30.0,
        max_attempts=3,
        jitter=True,
        retry_on=is_transient,
    )
