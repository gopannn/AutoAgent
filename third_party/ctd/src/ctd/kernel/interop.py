"""Interop with the CTD v4 service package.

Two codebases converged on the same architecture from different directions:

    topo   this package. Algorithmic core: schema-checked encoding, beam
           alignment, guarded projection, assumption tracking, and a held-out
           evaluation harness.
    ctd    the v4 production package. Service shell: evidence graph and
           resolver, three-valued constraint truth, auth/IAM, durable stores,
           provider federation, tracing, async bounded execution, HTTP API.

They are complements, not competitors, and the overlap is almost exactly the
transfer engine. This module is the seam, so the hardened core can be dropped
in behind the production service without either side importing the other.

Conversion is via plain dictionaries matching CTD's `StructuralCase` and
`StructuralTarget` JSON shapes. Deliberately not via `import ctd`: that would
drag pydantic and a FastAPI dependency tree into a package whose entire value
proposition is that it has none, and it would couple the core's release cycle
to the service's.

What crosses the seam, in each direction
----------------------------------------

CTD -> topo   `independence_group` is honoured directly. It is a better signal
              than the structure fingerprint this package derives
              automatically, because it records what a human *knows* to be
              causally independent rather than what happens to look different.
              Both are used: convergence takes the minimum.

topo -> CTD   Every relation crossing into the core is validated against the
              predicate schema first — arity, argument kind, and entity role.
              CTD's encoding gate checks none of the three, so a case that
              CTD accepts can still be structurally incoherent; this is where
              that gets caught.

The plane separation is preserved, and this module will not let it be
violated: a projected relation converts to a CTD hypothesis with state
HYPOTHESIS and nothing else. There is no code path here that emits a resolved
claim. Promotion to truth stays with CTD's evidence resolver, which is the
only component that has evidence to promote it on.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from .encoding import Validation, validate
from .premortem import Prediction
from .schema import CORE_SCHEMA, Schema
from .store import R, Record, Rel
from .transfer import Inference, TargetPattern

__all__ = [
    "HypothesisState", "to_record", "to_target", "from_relation",
    "relation_to_dict", "relation_from_dict", "to_hypothesis",
    "import_cases", "canonical_key",
]


class HypothesisState(str, Enum):
    """Mirrors ctd.transfer.HypothesisState.

    Present so the core can emit CTD-shaped output without importing CTD. Note
    what is missing: there is no RESOLVED. A structural hypothesis cannot
    become a resolved fact by passing through this module, however high its
    priority score, because no amount of structural soundness is evidence.
    """
    HYPOTHESIS = "HYPOTHESIS"
    UNDER_TEST = "UNDER_TEST"
    SUPPORTED = "SUPPORTED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


# --------------------------------------------------------------------------
# Relations
# --------------------------------------------------------------------------

def relation_from_dict(data: Any) -> str | Rel:
    """Accept CTD's `{"pred": ..., "args": [...]}` or a bare entity string."""
    if isinstance(data, str):
        return data
    if isinstance(data, Mapping):
        pred = data.get("pred")
        args = data.get("args", ())
        if not isinstance(pred, str):
            raise ValueError(f"relation has no predicate: {data!r}")
        return R(pred.upper(), *(relation_from_dict(a) for a in args))
    raise TypeError(f"cannot read a relation from {type(data).__name__}")


def relation_to_dict(rel: Rel) -> dict[str, Any]:
    return {
        "pred": rel.pred,
        "args": [relation_to_dict(a) if isinstance(a, Rel) else a
                 for a in rel.args],
    }


def canonical_key(rel: Rel) -> str:
    """CTD's `canonical_key` format, for cross-system deduplication."""
    parts = [canonical_key(a) if isinstance(a, Rel) else a for a in rel.args]
    return f"{rel.pred.upper()}({','.join(parts)})"


def from_relation(rel: Rel) -> dict[str, Any]:
    return relation_to_dict(rel)


# --------------------------------------------------------------------------
# Cases and targets
# --------------------------------------------------------------------------

def to_record(case: Mapping[str, Any]) -> Record:
    """CTD `StructuralCase` dict -> `topo.Record`.

    CTD carries severity and check templates as first-class fields and
    everything else in `metadata`; this package carries all of them in
    `attrs`, because `attrs` is what the index and the cheap filter tier see.
    Losing severity into an un-indexed side channel would make it invisible to
    CLOSE.
    """
    meta = dict(case.get("metadata") or {})
    attrs: dict[str, Any] = dict(meta)
    attrs.setdefault("incident", meta.get("incident", True))
    if case.get("severity") is not None:
        attrs["severity"] = int(case["severity"])
    if case.get("check_templates"):
        attrs["checks"] = {k.upper(): v
                           for k, v in case["check_templates"].items()}
    if case.get("independence_group"):
        attrs["independence_group"] = case["independence_group"]
    for key in ("tenant_id", "security_label", "source_refs"):
        if case.get(key):
            attrs[key] = case[key]

    rels = tuple(
        r for r in (relation_from_dict(x) for x in case.get("relations", ()))
        if isinstance(r, Rel)
    )
    return Record(
        id=str(case["id"]),
        domain=str(case.get("domain", "unknown")),
        attrs=attrs,
        rels=rels,
        types={k: str(v).upper() for k, v in (case.get("types") or {}).items()},
        text=str(case.get("text", "")),
    )


def to_target(target: Mapping[str, Any]) -> TargetPattern:
    rels = tuple(
        r for r in (relation_from_dict(x)
                    for x in target.get("relations", ()))
        if isinstance(r, Rel)
    )
    return TargetPattern(
        name=str(target.get("name", "unnamed target")),
        domain=str(target.get("domain", "unknown")),
        rels=rels,
        types={k: str(v).upper()
               for k, v in (target.get("types") or {}).items()},
    )


@dataclass
class ImportReport:
    accepted: list[Record]
    rejected: list[Validation]

    def __str__(self) -> str:
        return (f"{len(self.accepted)} case(s) accepted, "
                f"{len(self.rejected)} rejected by the schema gate"
                + ("" if not self.rejected else
                   "\n" + "\n".join(v.explain() for v in self.rejected)))


def import_cases(cases: Iterable[Mapping[str, Any]],
                 schema: Schema = CORE_SCHEMA,
                 strict: bool = True) -> ImportReport:
    """Convert CTD cases, applying this package's encoding gate on the way in.

    CTD's own gate checks UNTYPED, SHALLOW, OFF_VOCAB, ORPHAN and BARREN. It
    does not check predicate arity, argument kind, entity role against a
    signature, antonym contradiction, or causal cycles — so a CTD-accepted
    case can still be structurally incoherent in five distinct ways. Importing
    through this function is where those get caught, before anything reaches
    the aligner.
    """
    accepted: list[Record] = []
    rejected: list[Validation] = []
    for case in cases:
        rec = to_record(case)
        v = validate(rec, schema)
        if strict and not v.ok and rec.rels:
            rejected.append(v)
        else:
            accepted.append(rec)
    return ImportReport(accepted, rejected)


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def to_hypothesis(item: Prediction | Inference,
                  target: TargetPattern | None = None) -> dict[str, Any]:
    """Emit a CTD-shaped `StructuralHypothesis`/`PreMortemFinding` payload.

    `state` is always HYPOTHESIS. The fields CTD's resolver needs to refuse
    promotion — the absence of evidence, the presence of a required check —
    travel with it.
    """
    rel = item.rel
    payload: dict[str, Any] = {
        "relation": relation_to_dict(rel),
        "canonical_key": canonical_key(rel),
        "state": HypothesisState.HYPOTHESIS.value,
        "source_cases": list(item.sources),
        "source_domains": sorted(item.domains),
        "independence_groups": sorted(getattr(item, "groups", set())),
        "convergence": item.convergence,
        "systematicity": round(item.systematicity, 6),
        "looseness": round(getattr(item, "looseness", 0.0), 6),
        "mapping_depth": rel.order,
        "conflicts": [canonical_key(c) for c in item.conflicts],
    }
    if isinstance(item, Prediction):
        payload.update({
            "severity": item.severity,
            "structural_priority": round(item.structural_score, 6),
            "priority_score": round(item.priority, 6),
            "verification_procedure": item.check,
            "checkable": item.checkable,
            "conditional": item.conditional,
            "assumptions": [canonical_key(a) for a in item.assumptions],
            "confidence_factor": round(item.confidence_factor, 6),
            "round_index": item.round_index,
        })
    else:
        payload["priority_score"] = round(item.score, 6)
    if target is not None:
        payload["target_name"] = target.name
        payload["target_domain"] = target.domain
    return payload


def export_findings(items: Sequence[Prediction | Inference],
                    target: TargetPattern | None = None,
                    ) -> list[dict[str, Any]]:
    return [to_hypothesis(i, target) for i in items]
