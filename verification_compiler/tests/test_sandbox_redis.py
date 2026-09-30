"""Real Docker end-to-end tests for the Redis backing service. Opt-in:

    VC_E2E_VERIFIER_IMAGE=<digest or id> VC_E2E_RUNTIME_IMAGE=<digest or id> VC_E2E_REDIS_IMAGE=<digest or id> \
    [VC_E2E_ALLOW_RUNC=1] python -m pytest verification_compiler/tests/test_sandbox_redis.py
"""
import os

import pytest

from verification_compiler.config import SandboxConfig
from verification_compiler.dependencies import DependencyResolver
from verification_compiler.sandbox import DockerSandbox

from . import fakes

VERIFIER = os.environ.get("VC_E2E_VERIFIER_IMAGE")
RUNTIME = os.environ.get("VC_E2E_RUNTIME_IMAGE")
REDIS = os.environ.get("VC_E2E_REDIS_IMAGE")
pytestmark = pytest.mark.skipif(not (VERIFIER and RUNTIME and REDIS), reason="Docker e2e images not configured")

DEPS = ["fastapi==0.141.1", "uvicorn==0.54.0", "redis==8.1.0"]

REDIS_APP = '''\
import os

import redis
from fastapi import FastAPI
from redis.exceptions import ResponseError

app = FastAPI()
store = redis.Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)


@app.post("/hits/{name}")
def hit(name: str) -> dict[str, int]:
    return {"count": int(store.incr(f"hits:{name}"))}


@app.get("/hits/{name}")
def hits(name: str) -> dict[str, int]:
    return {"count": int(store.get(f"hits:{name}") or 0)}


@app.post("/admin/flush")
def flush() -> dict[str, str]:
    try:
        store.flushall()
    except ResponseError as err:
        return {"result": str(err)}
    return {"result": "flushed"}
'''

# Same API, state in process memory: correct on one replica, wrong on two.
MEMORY_APP = '''\
from fastapi import FastAPI

app = FastAPI()
counts: dict[str, int] = {}


@app.post("/hits/{name}")
def hit(name: str) -> dict[str, int]:
    counts[name] = counts.get(name, 0) + 1
    return {"count": counts[name]}


@app.get("/hits/{name}")
def hits(name: str) -> dict[str, int]:
    return {"count": counts.get(name, 0)}
'''

CROSS_REPLICA = '''\
import os

import httpx


def test_count_is_shared_by_all_replicas():
    first, second = os.environ["SUT_REPLICA_URLS"].split(",")[:2]
    assert httpx.post(first + "/hits/a").json() == {"count": 1}
    assert httpx.post(second + "/hits/a").json() == {"count": 2}
    assert httpx.get(first + "/hits/a").json() == {"count": 2}
'''

# Two tests that both start from zero pass only if state is reset between them.
ISOLATION = '''\
import os

import httpx


def test_starts_empty_one():
    assert httpx.post(os.environ["SUT_BASE_URL"] + "/hits/b").json() == {"count": 1}


def test_starts_empty_two():
    assert httpx.post(os.environ["SUT_BASE_URL"] + "/hits/b").json() == {"count": 1}
'''

NO_ADMIN = '''\
import os

import httpx


def test_service_cannot_flush_the_store():
    base = os.environ["SUT_BASE_URL"]
    httpx.post(base + "/hits/c")
    assert "no permissions" in httpx.post(base + "/admin/flush").json()["result"]
    assert httpx.get(base + "/hits/c").json() == {"count": 1}
'''


def stateful_spec(**tests: str) -> dict:
    return {
        "acceptance_tests": [
            {"id": name, "description": name, "invariant_ids": [], "requirement_ids": ["FR1"],
             "executable_python_code": code}
            for name, code in tests.items()
        ],
        "security_invariants": [],
        "backing_services": ["redis"],
    }


@pytest.fixture(scope="module")
def cfg(tmp_path_factory):
    return SandboxConfig(
        verifier_image=VERIFIER, runtime_image=RUNTIME, redis_image=REDIS,
        require_gvisor=os.environ.get("VC_E2E_ALLOW_RUNC") != "1",
        cache_dir=tmp_path_factory.mktemp("cache"),
    )


@pytest.fixture(scope="module")
def lockfile(cfg):
    return DependencyResolver(cfg).resolve(DEPS)


def verify(cfg, lockfile, app, spec):
    return DockerSandbox(cfg).verify(fakes.codebase(app, DEPS), spec, lockfile)


def test_redis_backed_service_passes_on_two_replicas(cfg, lockfile):
    result = verify(cfg, lockfile, REDIS_APP, stateful_spec(
        AT_shared=CROSS_REPLICA, AT_isolated=ISOLATION, AT_no_admin=NO_ADMIN))
    failed = {n: c.detail for n, c in result.checks.items() if not c.passed}
    assert result.passed, (failed, [a for a in result.acceptance if not a.passed])
    if os.environ.get("VC_E2E_ALLOW_RUNC") != "1":
        assert result.runtime == "runsc"
    assert result.backing_services == ["redis"] and result.service_replicas == 2
    assert result.images["redis"] == REDIS
    assert result.toolchain_versions["redis_server"][0].isdigit()
    assert {a.id: a.cases for a in result.acceptance} == {"AT_shared": 1, "AT_isolated": 2, "AT_no_admin": 1}
    assert "[0] replica log" in result.logs["service"] and "[1] replica log" in result.logs["service"]


def test_state_in_process_memory_fails_the_cross_replica_test(cfg, lockfile):
    result = verify(cfg, lockfile, MEMORY_APP, stateful_spec(AT_shared=CROSS_REPLICA, AT_isolated=ISOLATION))
    assert result.checks["service_startup"].passed, "both replicas must have served"
    by_id = {a.id: a for a in result.acceptance}
    assert not by_id["AT_shared"].passed
    # The per-test reset empties Redis only; state in process memory survives it and leaks into the next test.
    assert not by_id["AT_isolated"].passed
    assert "{'count': 2} == {'count': 1}" in by_id["AT_isolated"].failures[0]
    assert not result.passed


def test_calibration_runs_on_the_stateful_topology(cfg):
    calibration = DockerSandbox(cfg).calibrate_spec(stateful_spec(AT_shared=CROSS_REPLICA, AT_isolated=ISOLATION))
    assert calibration == {
        "AT_shared": {"not_found": False, "server_error": False, "empty_ok": False},
        "AT_isolated": {"not_found": False, "server_error": False, "empty_ok": False},
    }
