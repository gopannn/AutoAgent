"""VERIFY the verifier: is the hidden suite capable of justifying anything?

Three checks run before any code is written:

1. Coverage: every functional requirement (FR1, FR2, ...) and security invariant is claimed by
   at least one acceptance test.
2. Static assertion lint: every test calls the service over HTTP and asserts something that
   depends on what it observed. `assert True`, `assert 1 == 1` and tests without an assertion
   are rejected.
3. Null-service calibration (sandbox.calibrate_spec): a test that passes against a service
   answering 404, 500 or an empty 200 to everything does not discriminate. Every requirement
   needs at least one discriminating test. Non-discriminating tests still run and must pass,
   but they carry no evidential weight in the decision gate.
"""
from __future__ import annotations

import ast


def requirement_catalog(contract: dict, spec: dict) -> dict[str, str]:
    catalog = {f"FR{i}": text for i, text in enumerate(contract.get("functional_requirements", []), 1)}
    catalog.update({inv["id"]: inv["description"] for inv in spec.get("security_invariants", [])})
    return catalog


def claims(test: dict) -> set[str]:
    return set(test.get("requirement_ids", [])) | set(test.get("invariant_ids", []))


def coverage_problems(spec: dict, contract: dict) -> list[str]:
    catalog = requirement_catalog(contract, spec)
    problems = []
    for test in spec.get("acceptance_tests", []):
        unknown = sorted(set(test.get("requirement_ids", [])) - set(catalog))
        if unknown:
            problems.append(f"{test['id']}: references unknown requirements {unknown}")
    covered = set().union(*(claims(t) for t in spec.get("acceptance_tests", []))) if spec.get("acceptance_tests") else set()
    for rid in sorted(set(catalog) - covered):
        problems.append(f"{rid} ({catalog[rid][:80]}) is not covered by any acceptance test")
    return problems


def lint_assertions(test_id: str, code: str) -> list[str]:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []  # reported by the structural spec check
    http_helpers = {
        f.name for f in tree.body
        if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) and _uses_httpx(f)
    }
    problems = []
    for fn in tree.body:
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or not fn.name.startswith("test"):
            continue
        tag = f"{test_id}.{fn.name}"
        asserts = [n for n in ast.walk(fn) if isinstance(n, ast.Assert)]
        raises = [n for n in ast.walk(fn) if isinstance(n, ast.withitem) and _is_pytest_raises(n.context_expr)]
        if not asserts and not raises:
            problems.append(f"{tag}: has no assertion")
        for a in asserts:
            if not any(isinstance(n, (ast.Name, ast.Attribute, ast.Call, ast.Subscript)) for n in ast.walk(a.test)):
                problems.append(f"{tag}: line {a.lineno} asserts a constant ({ast.unparse(a.test)[:60]})")
        params = {arg.arg for arg in fn.args.args}
        calls = {_name(c.func) for c in ast.walk(fn) if isinstance(c, ast.Call)}
        if not (_uses_httpx(fn) or calls & http_helpers or params & http_helpers):
            problems.append(f"{tag}: never calls the service over HTTP")
    return problems


def discriminating_tests(calibration: dict[str, dict[str, bool]]) -> set[str]:
    return {tid for tid, modes in calibration.items() if modes and not any(modes.values())}


def evidence_problems(spec: dict, contract: dict, calibration: dict[str, dict[str, bool]]) -> list[str]:
    catalog = requirement_catalog(contract, spec)
    strong = discriminating_tests(calibration)
    problems = []
    for rid in sorted(catalog):
        tests = [t["id"] for t in spec["acceptance_tests"] if rid in claims(t)]
        if not any(t in strong for t in tests):
            passes_against = sorted({m for t in tests for m, passed in calibration.get(t, {}).items() if passed})
            problems.append(
                f"{rid} has no discriminating test: {tests or 'none'} also pass against a null service "
                f"({', '.join(passes_against) or 'no stubs'}). Assert on content only a real implementation returns, "
                "and pair negative tests with a positive one."
            )
    return problems


def _uses_httpx(node: ast.AST) -> bool:
    return any(isinstance(n, ast.Name) and n.id == "httpx" for n in ast.walk(node))


def _is_pytest_raises(expr: ast.AST) -> bool:
    return isinstance(expr, ast.Call) and _name(expr.func) == "raises"


def _name(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""
