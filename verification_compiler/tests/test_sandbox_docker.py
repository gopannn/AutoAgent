"""Real Docker end-to-end tests. Opt-in:

    VC_E2E_VERIFIER_IMAGE=<digest or image id> VC_E2E_RUNTIME_IMAGE=<digest or image id> \
    [VC_E2E_ALLOW_RUNC=1] python -m pytest verification_compiler/tests/test_sandbox_docker.py
"""
import os

import pytest

from verification_compiler.config import SandboxConfig
from verification_compiler.dependencies import DependencyResolver
from verification_compiler.sandbox import DockerSandbox

from . import fakes

VERIFIER = os.environ.get("VC_E2E_VERIFIER_IMAGE")
RUNTIME = os.environ.get("VC_E2E_RUNTIME_IMAGE")
pytestmark = pytest.mark.skipif(not (VERIFIER and RUNTIME), reason="Docker e2e images not configured")

DEPS = ["fastapi==0.141.1", "uvicorn==0.54.0"]


@pytest.fixture(scope="module")
def cfg(tmp_path_factory):
    return SandboxConfig(
        verifier_image=VERIFIER, runtime_image=RUNTIME,
        require_gvisor=os.environ.get("VC_E2E_ALLOW_RUNC") != "1",
        cache_dir=tmp_path_factory.mktemp("cache"),
    )


@pytest.fixture(scope="module")
def lockfile(cfg):
    return DependencyResolver(cfg).resolve(DEPS)


def verify(cfg, lockfile, app):
    return DockerSandbox(cfg).verify(fakes.codebase(app, DEPS), fakes.spec(), lockfile)


def test_correct_service_passes(cfg, lockfile):
    result = verify(cfg, lockfile, fakes.APP)
    failed = {n: c.detail for n, c in result.checks.items() if not c.passed}
    assert result.passed, failed
    assert result.acceptance[0].cases == 1
    assert "junit.xml" in result.evidence_hashes
    assert result.toolchain_versions.get("ruff") == "0.5.1"
    assert result.toolchain_versions.get("pyright") == "1.1.371"


def test_wrong_behaviour_fails_acceptance(cfg, lockfile):
    result = verify(cfg, lockfile, fakes.APP.replace('"ok"', '"degraded"'))
    assert not result.passed
    assert not result.acceptance[0].passed
    assert result.checks["service_startup"].passed


def test_service_cannot_forge_the_verdict(cfg, lockfile):
    """The SUT tries every trick that worked against the single-process design."""
    hostile = fakes.APP + '''
import atexit, json, os, pathlib

def _forge():
    for target in ("/out/junit.xml", "/tmp/report.json", "/compiler_spec/test_AT_health.py"):
        try:
            pathlib.Path(target).write_text("forged")
        except OSError:
            pass

_forge()
atexit.register(_forge)
app.router.routes.clear()
'''
    result = verify(cfg, lockfile, hostile)
    assert not result.passed
    assert not result.acceptance[0].passed


def test_service_that_cannot_start_fails_closed(cfg, lockfile):
    result = verify(cfg, lockfile, "import does_not_exist\n")
    assert not result.passed
    assert not result.checks["service_startup"].passed
    assert "does_not_exist" in result.checks["service_startup"].detail


def test_static_analysis_blocks_dangerous_code(cfg, lockfile):
    risky = fakes.APP + '''

@app.get("/calc")
def calc(expr: str) -> dict[str, str]:
    return {"result": str(eval(expr))}
'''
    result = verify(cfg, lockfile, risky)
    assert not result.passed
    assert not result.checks["ruff"].passed
    assert not result.checks["semgrep"].passed
