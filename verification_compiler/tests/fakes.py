"""Test doubles and fixtures shared by the test modules."""
from __future__ import annotations

from collections import deque
from typing import Any, Callable

from verification_compiler.config import CompilerConfig, SandboxConfig
from verification_compiler.hashing import codebase_hash, hash_obj, sha256_hex
from verification_compiler.schemas import AcceptanceResult, CheckResult, VerificationResult

IMAGE = "sha256:" + "a" * 64

APP = '''\
from fastapi import FastAPI

app = FastAPI()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
'''

TEST_HEALTH = '''\
import os

import httpx


def test_health():
    r = httpx.get(os.environ["SUT_BASE_URL"] + "/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
'''


def codebase(app: str = APP, deps: list[str] | None = None) -> dict:
    return {
        "files": [{"path": "service/__init__.py", "content": ""}, {"path": "service/main.py", "content": app}],
        "entrypoint": "service.main:app",
        "project_type": "python",
        "dependencies": deps if deps is not None else ["fastapi==0.141.1", "uvicorn==0.54.0"],
    }


def spec(code: str = TEST_HEALTH) -> dict:
    return {
        "acceptance_tests": [
            {"id": "AT_health", "description": "health endpoint", "invariant_ids": ["INV_auth"],
             "requirement_ids": ["FR1"], "executable_python_code": code}
        ],
        "security_invariants": [{"id": "INV_auth", "description": "all tenant data requires a valid token"}],
    }


CONTRACT = {
    "summary": "health service",
    "architecture_decision": "FastAPI",
    "functional_requirements": ["GET /health returns ok"],
    "api_endpoints": [{"method": "GET", "path": "/health", "description": "liveness"}],
}


def config(**kw) -> CompilerConfig:
    return CompilerConfig(sandbox=SandboxConfig(verifier_image=IMAGE, runtime_image=IMAGE), **kw)


class FakeLLMs:
    """Scripted structured-output LLMs. Each role gets a queue of responses (objects or callables)."""

    def __init__(self, script: dict[str, list[Any]]):
        self.queues = {role: deque(items) for role, items in script.items()}
        self.calls: list[tuple[str, list]] = []

    def __call__(self, role: str, schema):
        outer = self

        class _LLM:
            def invoke(self, messages):
                outer.calls.append((role, messages))
                queue = outer.queues[role]
                item = queue[0] if len(queue) == 1 else queue.popleft()   # last response repeats
                return schema.model_validate(item(messages) if callable(item) else item)

        return _LLM()


def passing_result(cb: dict, sp: dict, lock: dict) -> VerificationResult:
    ids = [t["id"] for t in sp["acceptance_tests"]]
    return VerificationResult(
        checks={n: CheckResult(name=n, passed=True, exit_code=0) for n in
                ("syntax", "secret_scan", "dependency_scan", "dependency_install", "ruff", "pyright", "semgrep",
                 "service_startup", "acceptance_tests")},
        acceptance=[AcceptanceResult(id=i, passed=True, cases=1) for i in ids],
        expected_acceptance_ids=ids,
        runtime="runsc",
        images={"verifier": IMAGE, "runtime": IMAGE},
        toolchain_versions={"pytest": "8.2.2"},
        codebase_hash=codebase_hash(cb),
        lockfile_hash=lock["sha256"],
        spec_hash=hash_obj(sp),
        evidence_hashes={"junit.xml": "0" * 64},
    )


class FakeSandbox:
    def __init__(self, behaviour: Callable[[dict, dict, dict], VerificationResult] = passing_result):
        self.behaviour = behaviour
        self.calls = 0

    def verify(self, cb, sp, lock):
        self.calls += 1
        return self.behaviour(cb, sp, lock)

    def calibrate_spec(self, sp):
        """Every test fails against every null service unless listed in `self.vacuous`."""
        vacuous = getattr(self, "vacuous", set())
        return {t["id"]: {m: t["id"] in vacuous for m in ("not_found", "server_error", "empty_ok")}
                for t in sp["acceptance_tests"]}


class FakeResolver:
    def __init__(self, audit_passed: bool = True):
        self.audit_passed = audit_passed

    def resolve(self, deps):
        text = "\n".join(sorted(deps))
        return {
            "text": text,
            "sha256": sha256_hex(text),
            "packages": sorted(deps),
            "audit": CheckResult(name="dependency_scan", passed=self.audit_passed,
                                 detail="" if self.audit_passed else "fastapi==0.1: CVE-X").model_dump(),
        }


def toolkit():
    """The vendored epistemic-toolkit (third_party/), importable without installation."""
    import sys
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "third_party" / "epistemic_toolkit" / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    import epistemic_toolkit

    return epistemic_toolkit
