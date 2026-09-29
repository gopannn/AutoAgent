"""Spec governance: coverage, assertion lint and the discriminating-evidence rule."""
from verification_compiler.reasoning import governance

from . import fakes

H = "import os\nimport httpx\n\nBASE = os.environ['SUT_BASE_URL']\n\n"
CONTRACT = {**fakes.CONTRACT, "functional_requirements": ["GET /health returns ok", "POST /login issues a token"]}


def spec(*tests):
    return {"acceptance_tests": list(tests), "security_invariants": [{"id": "INV_auth", "description": "auth required"}]}


def t(tid, reqs=(), invs=(), code=fakes.TEST_HEALTH):
    return {"id": tid, "description": tid, "requirement_ids": list(reqs), "invariant_ids": list(invs),
            "executable_python_code": code}


def test_catalog_numbers_functional_requirements_and_adds_invariants():
    assert governance.requirement_catalog(CONTRACT, spec()) == {
        "FR1": "GET /health returns ok", "FR2": "POST /login issues a token", "INV_auth": "auth required"}


def test_coverage_reports_uncovered_and_unknown_requirements():
    problems = governance.coverage_problems(spec(t("AT_a", ["FR1", "FR9"])), CONTRACT)
    assert any("unknown requirements ['FR9']" in p for p in problems)
    assert any(p.startswith("FR2 ") for p in problems) and any(p.startswith("INV_auth ") for p in problems)
    assert governance.coverage_problems(spec(t("AT_a", ["FR1", "FR2"], ["INV_auth"])), CONTRACT) == []


def test_assertion_lint():
    assert governance.lint_assertions("AT", fakes.TEST_HEALTH) == []
    assert "has no assertion" in governance.lint_assertions("AT", H + "def test_x():\n    httpx.get(BASE)\n")[0]
    assert "asserts a constant" in governance.lint_assertions("AT", H + "def test_x():\n    httpx.get(BASE)\n    assert 1 == 1\n")[0]
    assert "never calls the service" in governance.lint_assertions("AT", "def test_x():\n    x = 2\n    assert x == 2\n")[0]
    helper = H + "def fetch():\n    return httpx.get(BASE + '/h')\n\ndef test_x():\n    assert fetch().status_code == 200\n"
    assert governance.lint_assertions("AT", helper) == []
    raises = H + "import pytest\n\ndef test_x():\n    with pytest.raises(httpx.HTTPStatusError):\n        httpx.get(BASE).raise_for_status()\n"
    assert governance.lint_assertions("AT", raises) == []


def test_every_requirement_needs_a_discriminating_test():
    s = spec(t("AT_a", ["FR1"]), t("AT_b", ["FR2"], ["INV_auth"]))
    strong = {"not_found": False, "server_error": False, "empty_ok": False}
    weak = {"not_found": True, "server_error": False, "empty_ok": False}
    assert governance.evidence_problems(s, CONTRACT, {"AT_a": strong, "AT_b": strong}) == []
    problems = governance.evidence_problems(s, CONTRACT, {"AT_a": strong, "AT_b": weak})
    assert [p.split()[0] for p in problems] == ["FR2", "INV_auth"]
    assert "not_found" in problems[0]
    assert governance.discriminating_tests({"AT_a": strong, "AT_b": weak, "AT_c": {}}) == {"AT_a"}
