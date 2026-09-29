"""Bounded, source-grounded contradiction check before any code or test generation.

This proves conflicts in the supplied formalisation; it does not prove that an LLM
extracted every semantic constraint from arbitrary prose. Unquoted claims abstain.
"""
from __future__ import annotations

import re

from .hashing import hash_obj, sha256_hex

_EXPLICIT_USE = re.compile(r"^\s*must\s+(not\s+)?use\s+([A-Za-z0-9][A-Za-z0-9_. +#-]{1,80})\s*[.!]?\s*$", re.I | re.M)


def review_constraints(requirements: str, contract: dict) -> dict:
    claims = list(contract.get("constraints", []))
    for match in _EXPLICIT_USE.finditer(requirements):
        value = match.group(2).rstrip(" .!").strip().casefold()
        claims.append({"key": "technology." + re.sub(r"[^a-z0-9]+", "_", value).strip("_"),
                       "operator": "neq" if match.group(1) else "eq", "value": "true",
                       "source_quote": match.group(0).strip()})

    invalid = []
    by_key: dict[str, list[dict]] = {}
    for claim in claims:
        quote = claim["source_quote"]
        if quote not in requirements:
            invalid.append({"claim": claim, "reason": "source_quote is absent from the input"})
        by_key.setdefault(claim["key"], []).append(claim)

    conflicts = []
    for key, items in sorted(by_key.items()):
        equal = {c["value"].strip().casefold() for c in items if c["operator"] == "eq"}
        unequal = {c["value"].strip().casefold() for c in items if c["operator"] == "neq"}
        if len(equal) > 1 or equal & unequal:
            conflicts.append({"key": key, "source_quotes": sorted({c["source_quote"] for c in items}),
                              "reason": "mutually exclusive equality or equality/inequality"})

    return {"status": "abstained" if invalid or conflicts else "consistent_with_encoded_constraints",
            "requirements_hash": sha256_hex(requirements), "contract_hash": hash_obj(contract),
            "claims": claims, "invalid_sources": invalid, "conflicts": conflicts,
            "scope": "encoded hard constraints only; incomplete extraction cannot establish global consistency"}
