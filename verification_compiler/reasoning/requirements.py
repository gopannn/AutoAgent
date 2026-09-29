"""REASON, before any code: encode the requirements and refuse contradictory ones.

The requirements are encoded (by a model) into statements over a small, declared requirement
schema: a CTD kernel `Schema` whose antonym pairs define what cannot both hold. Every statement
must quote the requirement text it comes from. The kernel then validates the encoding
deterministically:

  * UNTYPED / ARITY / SIGNATURE / off-vocabulary / unquoted statements → the encoding is
    malformed and is redone (bounded);
  * CONTRADICTION (a predicate and its antonym over the same arguments) → the compiler ABSTAINS
    before spending anything on code, citing both statements and their source text.

Limit: this detects contradictions the encoding states. Quoting stops invented statements, but
it cannot prove that the model did not miss one.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from ..vendored import ctd_module

# role -> parent role
ROLE_PARENTS = {"ENDPOINT": "COMPONENT", "STATE": "DATA", "CREDENTIAL": "DATA", "TENANT": "ACTOR"}

# predicate -> (argument roles, gloss)
PREDICATES: dict[str, tuple[tuple[tuple[str, ...], ...], str]] = {
    "REQUIRES_AUTH": ((("ENDPOINT",),), "{0} requires an authenticated caller"),
    "ALLOWS_ANONYMOUS": ((("ENDPOINT",),), "{0} accepts unauthenticated callers"),
    "HOLDS_IN_MEMORY": ((("COMPONENT",), ("DATA",)), "{0} keeps {1} in process memory between requests"),
    "NEVER_HOLDS_IN_MEMORY": ((("COMPONENT",), ("DATA",)), "{0} never keeps {1} in process memory between requests"),
    "PERSISTS": ((("COMPONENT",), ("DATA",)), "{0} stores {1} durably"),
    "DISCARDS": ((("COMPONENT",), ("DATA",)), "{0} never stores {1} durably"),
    "EXPOSES": ((("COMPONENT",), ("DATA",)), "{0} reveals {1} in responses or logs"),
    "CONCEALS": ((("COMPONENT",), ("DATA",)), "{0} never reveals {1} in responses or logs"),
    "ALLOWS": ((("ACTOR",), ("ENDPOINT",)), "{0} may call {1}"),
    "DENIES": ((("ACTOR",), ("ENDPOINT",)), "{0} may not call {1}"),
    "SHARES_ACROSS_TENANTS": ((("DATA",),), "{0} is visible across tenants"),
    "ISOLATES_BY_TENANT": ((("DATA",),), "{0} is visible only within its tenant"),
    "DEPENDS_ON": ((("COMPONENT",), ("COMPONENT",)), "{0} depends on {1}"),
}
ANTONYMS = [
    ("REQUIRES_AUTH", "ALLOWS_ANONYMOUS"),
    ("HOLDS_IN_MEMORY", "NEVER_HOLDS_IN_MEMORY"),
    ("PERSISTS", "DISCARDS"),
    ("EXPOSES", "CONCEALS"),
    ("ALLOWS", "DENIES"),
    ("SHARES_ACROSS_TENANTS", "ISOLATES_BY_TENANT"),
]
_MALFORMED = {"UNTYPED", "ARITY", "SIGNATURE", "OFF-VOCAB", "UNKNOWN-ROLE"}
_MIN_EXCERPT = 8


class RequirementStatement(BaseModel):
    predicate: str = Field(description="One predicate from the requirement schema.")
    args: list[str] = Field(description="Entity ids, in the predicate's argument order.")
    excerpt: str = Field(description="The exact words from the requirements that state this. Copy them verbatim.")


class RequirementEncoding(BaseModel):
    entities: dict[str, str] = Field(description="Entity id -> role. Reuse one id for one thing throughout.")
    statements: list[RequirementStatement]


@dataclass
class RequirementCheck:
    statements: int
    problems: list[str] = field(default_factory=list)          # malformed encoding: re-encode
    contradictions: list[dict] = field(default_factory=list)   # consistent encoding that contradicts itself

    @property
    def malformed(self) -> bool:
        return bool(self.problems)

    @property
    def contradictory(self) -> bool:
        return not self.problems and bool(self.contradictions)

    def to_dict(self) -> dict:
        return {"statements": self.statements, "problems": self.problems, "contradictions": self.contradictions}


def schema():
    ks = ctd_module("kernel.schema")
    sigs = [
        ks.Signature(pred, tuple(ks.ENT(*roles) for roles in args), gloss)
        for pred, (args, gloss) in PREDICATES.items()
    ]
    return ks.Schema(sigs, ROLE_PARENTS, ANTONYMS)


def describe_schema() -> str:
    roles = sorted({r for args, _ in PREDICATES.values() for spec in args for r in spec} | set(ROLE_PARENTS))
    lines = [f"Roles: {', '.join(roles)} (" + ", ".join(f"{c} is a kind of {p}" for c, p in ROLE_PARENTS.items()) + ")"]
    for pred, (args, gloss) in PREDICATES.items():
        sig = ", ".join("|".join(spec) for spec in args)
        lines.append(f"- {pred}({sig}): {gloss.format(*[f'<{s[0].lower()}>' for s in args])}")
    lines.append("Pairs that cannot both hold over the same arguments: " + "; ".join(f"{a} / {b}" for a, b in ANTONYMS))
    return "\n".join(lines)


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def check(requirements_text: str, encoding: RequirementEncoding) -> RequirementCheck:
    store = ctd_module("kernel.store")
    encoding_mod = ctd_module("kernel.encoding")
    result = RequirementCheck(statements=len(encoding.statements))
    source = _normalise(requirements_text)

    for i, st in enumerate(encoding.statements):
        tag = f"statement {i + 1} {st.predicate}({', '.join(st.args)})"
        excerpt = _normalise(st.excerpt)
        if len(excerpt) < _MIN_EXCERPT or excerpt not in source:
            result.problems.append(f"{tag}: excerpt is not a verbatim quote of the requirements: {st.excerpt!r}")
        if st.predicate not in PREDICATES:
            result.problems.append(f"{tag}: unknown predicate (use only the schema's predicates)")
        missing = [a for a in st.args if a not in encoding.entities]
        if missing:
            result.problems.append(f"{tag}: entities without a declared role: {missing}")
    if not encoding.statements:
        return result

    record = store.Record(
        id="requirements", domain="requirements",
        rels=tuple(store.R(st.predicate, *st.args) for st in encoding.statements),
        types=dict(encoding.entities),
    )
    validation = encoding_mod.validate(record, schema=schema(), min_order=1)
    for finding in validation.findings:
        if finding.code in _MALFORMED:
            result.problems.append(f"{finding.code}: {finding.detail}")
    if result.problems:
        return result

    kernel_says_contradiction = any(f.code == "CONTRADICTION" for f in validation.findings)
    antonym = {frozenset(pair) for pair in ANTONYMS}
    seen = encoding.statements
    for i, a in enumerate(seen):
        for b in seen[i + 1:]:
            if frozenset((a.predicate, b.predicate)) in antonym and a.args == b.args:
                result.contradictions.append({
                    "statements": [f"{a.predicate}({', '.join(a.args)})", f"{b.predicate}({', '.join(b.args)})"],
                    "excerpts": [a.excerpt, b.excerpt],
                })
    if kernel_says_contradiction != bool(result.contradictions):   # the two detectors must agree
        result.problems.append("internal: kernel and statement-level contradiction checks disagree")
    return result
