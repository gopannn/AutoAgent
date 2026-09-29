"""Structural discovery: the extractor, the failure library and the premortem."""
import pytest

pytest.importorskip("ctd")

from verification_compiler.reasoning import discovery  # noqa: E402
from verification_compiler.reasoning.topology import from_codebase, from_contract  # noqa: E402

RISKY = '''
import asyncio
import httpx
from fastapi import FastAPI

app = FastAPI()
SESSIONS = {}
JOBS = asyncio.Queue()
engine = create_engine("postgresql://db")


@app.post("/login")
def login(user: str):
    SESSIONS[user] = "t"
    for _ in range(5):
        try:
            return httpx.post("https://idp/verify", json={})
        except httpx.HTTPError:
            continue
'''

CLEAN = '''
from fastapi import FastAPI

app = FastAPI()


@app.get("/health")
def health():
    return {"status": "ok"}
'''


def cb(content):
    return {"files": [{"path": "app/main.py", "content": content}]}


def test_every_library_case_passes_the_strict_structural_gate():
    cases = discovery.load_library()
    assert len(cases) >= 7
    assert all(case.check_templates and case.severity for case in cases)
    assert discovery.validate_library(cases) == {case.id: [] for case in cases}


def test_extractor_reports_only_what_the_code_shows():
    topo = from_codebase(cb(RISKY))
    rels = set(map(repr, topo.relations))
    assert repr(("DEPENDS", "api_service", "upstream_api")) in rels
    assert repr(("CAUSES", ("RETRIES", "api_service", "upstream_api"),
                 ("INCREASES", "request_load", "upstream_pressure"))) in rels
    assert repr(("SHARED", "sessions", "request_handlers")) in rels
    assert repr(("FLOWS", "request_load", "jobs")) in rels
    assert repr(("DEPENDS", "api_service", "create_engine_pool")) in rels
    assert topo.metadata["outbound_calls_without_timeout"] == ["app/main.py:17"]
    clean = from_codebase(cb(CLEAN))
    assert set(map(repr, clean.relations)) == {repr(("FLOWS", "request_load", "api_gateway")),
                                               repr(("DEPENDS", "api_service", "api_gateway"))}


def test_bounded_or_shrinking_state_is_not_reported_as_growth():
    code = CLEAN + "\nCACHE = {}\n\n@app.delete('/x')\ndef drop(k: str):\n    CACHE[k] = 1\n    CACHE.pop(k)\n"
    rels = set(map(repr, from_codebase(cb(code)).relations))
    assert not any("cache_entries" in r for r in rels)
    assert repr(("SHARED", "cache", "request_handlers")) in rels


def test_premortem_projects_grounded_hypotheses_with_checks():
    risks = discovery.premortem(from_codebase(cb(RISKY)), "svc")
    by_relation = {r["relation"]: r for r in risks}
    assert "SATURATES(upstream_api)" in by_relation
    assert "DEGRADES(sessions)" in by_relation
    for risk in risks:
        assert risk["state"] == "HYPOTHESIS"
        assert risk["check"] and risk["source_cases"] and risk["id"].startswith("RISK-")


def test_clean_service_yields_no_risks():
    assert discovery.premortem(from_codebase(cb(CLEAN)), "svc") == []


def test_requirements_drive_pre_code_discovery():
    encoding = {"statements": [{"predicate": "HOLDS_IN_MEMORY", "args": ["service", "session data"], "excerpt": "x"}]}
    contract = {"api_endpoints": [{"method": "POST", "path": "/login", "description": "log in"}]}
    risks = discovery.premortem(from_contract(contract, encoding), "svc")
    assert [r["relation"] for r in risks] == ["DEGRADES(session_data)"]
    assert discovery.premortem(from_contract(contract), "svc") == []
