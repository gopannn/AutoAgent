"""Contracts exchanged between compiler stages. LLM outputs are parsed into these models."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

ID_PATTERN = r"^[A-Za-z][A-Za-z0-9_]{0,63}$"
Severity = Literal["critical", "high", "medium", "low"]


# ---------------------------------------------------------------- requirements

class ApiEndpoint(BaseModel):
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path: str
    description: str


class RequirementContract(BaseModel):
    """The shared interface. Both the builder and the hidden tests are derived from it."""

    summary: str
    architecture_decision: str
    functional_requirements: list[str]
    api_endpoints: list[ApiEndpoint] = Field(
        description="HTTP surface of the service. Acceptance tests exercise only these endpoints."
    )


# ---------------------------------------------------------------- verification spec

class AcceptanceTest(BaseModel):
    id: str = Field(pattern=ID_PATTERN)
    description: str
    invariant_ids: list[str] = Field(default_factory=list, description="Security invariants this test enforces.")
    executable_python_code: str = Field(
        description=(
            "Self-contained pytest module. It must exercise the running service over HTTP only, "
            "using httpx and the base URL in os.environ['SUT_BASE_URL']. It must not import project code."
        )
    )


class SecurityInvariant(BaseModel):
    id: str = Field(pattern=ID_PATTERN)
    description: str


class VerificationSpec(BaseModel):
    """Compiler-owned and immutable once compiled. Never shown to the builder."""

    acceptance_tests: list[AcceptanceTest]
    security_invariants: list[SecurityInvariant]


# ---------------------------------------------------------------- codebase

class GeneratedFile(BaseModel):
    path: str
    content: str


class Codebase(BaseModel):
    files: list[GeneratedFile]
    entrypoint: str = Field(description="ASGI application as 'package.module:attribute', served with uvicorn.")
    project_type: Literal["python"] = "python"
    dependencies: list[str] = Field(description="Exact pins only, e.g. 'fastapi==0.111.0'. Must include uvicorn.")


class CodePatch(BaseModel):
    modified_files: list[GeneratedFile] = Field(default_factory=list)
    deleted_files: list[str] = Field(default_factory=list)
    dependencies: Optional[list[str]] = Field(
        default=None, description="Full replacement dependency list, or null to keep the current one."
    )


# ---------------------------------------------------------------- audit

class AuditFinding(BaseModel):
    severity: Severity
    category: str
    description: str
    affected_files: list[str]
    remediation_requirement: str
    invariant_ids: list[str] = Field(default_factory=list)
    acceptance_test_ids: list[str] = Field(
        default_factory=list, description="Acceptance tests whose passing demonstrates this finding is fixed."
    )


class OpenFindingStatus(BaseModel):
    fingerprint: str
    status: Literal["still_present", "resolved"]
    justification: str


class AuditReport(BaseModel):
    open_finding_statuses: list[OpenFindingStatus] = Field(
        default_factory=list, description="One entry for every previously open finding you were given."
    )
    new_findings: list[AuditFinding] = Field(default_factory=list)


# ---------------------------------------------------------------- verification evidence

class CheckResult(BaseModel):
    name: str
    passed: bool
    exit_code: Optional[int] = None
    detail: str = ""
    log_sha256: Optional[str] = None


class AcceptanceResult(BaseModel):
    id: str
    passed: bool
    cases: int
    failures: list[str] = Field(default_factory=list)


class VerificationResult(BaseModel):
    checks: dict[str, CheckResult]
    acceptance: list[AcceptanceResult]
    expected_acceptance_ids: list[str]
    isolation: Literal["black_box_service"] = "black_box_service"
    runtime: str
    images: dict[str, str]
    toolchain_versions: dict[str, str]
    codebase_hash: str
    lockfile_hash: str
    spec_hash: str
    evidence_hashes: dict[str, str]
    logs: dict[str, str] = Field(default_factory=dict)

    @property
    def passed(self) -> bool:
        if not self.checks or not all(c.passed for c in self.checks.values()):
            return False
        by_id = {a.id: a for a in self.acceptance}
        if not self.expected_acceptance_ids:
            return False
        return all(
            (a := by_id.get(test_id)) is not None and a.passed and a.cases > 0
            for test_id in self.expected_acceptance_ids
        )


# ---------------------------------------------------------------- review & release

class SemanticReview(BaseModel):
    is_valid: bool
    feedback: str
    unmet_requirements: list[str] = Field(default_factory=list)


class ReleaseManifest(BaseModel):
    build_id: str
    run_id: str
    status: Literal["release_ready"]
    requirement_hash: str
    requirement_contract_hash: str
    verification_spec_hash: str
    codebase_hash: str
    lockfile_hash: str
    artifact_hash: str
    compiler_version: str
    runtime: str
    images: dict[str, str]
    toolchain_versions: dict[str, str]
    models_used: dict[str, str]
    source_files: list[str]
    locked_packages: list[str]
    findings: list[dict]
    accepted_findings: list[dict]
    verification_evidence: VerificationResult
    signature: Optional[dict] = None
