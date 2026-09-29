"""Declared traceability from the compiled contract to independent acceptance tests.

These links establish which requirements have tests assigned. They do not prove that
the generated test assertions faithfully implement the natural-language requirement.
"""
from __future__ import annotations


def contract_items(contract: dict) -> list[dict]:
    requirements = [
        {"id": f"FR_{i}", "kind": "functional_requirement", "description": text}
        for i, text in enumerate(contract.get("functional_requirements", []), 1)
    ]
    endpoints = [
        {"id": f"EP_{i}", "kind": "endpoint", "description": f"{ep['method']} {ep['path']}: {ep['description']}"}
        for i, ep in enumerate(contract.get("api_endpoints", []), 1)
    ]
    return requirements + endpoints


def coverage_matrix(contract: dict, spec: dict) -> list[dict]:
    return [
        {**item, "acceptance_test_ids": sorted(t["id"] for t in spec["acceptance_tests"]
                                             if item["id"] in t.get("covers", []))}
        for item in contract_items(contract)
    ]


def coverage_problems(contract: dict, spec: dict) -> list[str]:
    items = contract_items(contract)
    known = {item["id"] for item in items}
    if not known:
        return ["contract has no explicit functional requirements or endpoints to verify"]
    problems = []
    for test in spec.get("acceptance_tests", []):
        unknown = set(test.get("covers", [])) - known
        if unknown:
            problems.append(f"{test['id']}: unknown requirement coverage ids {sorted(unknown)}")
    for row in coverage_matrix(contract, spec):
        if not row["acceptance_test_ids"]:
            problems.append(f"{row['id']}: no acceptance test claims coverage of {row['description']}")
    return problems
