"""Schema — the declared ontology the whole engine runs on.

The v4 package asserted that "encoding is the bottleneck, not the engine" and
then left the encoding almost entirely unconstrained: a shared vocabulary was
a flat `set[str]` of predicate names, entity roles were free-form strings
compared for equality, and predicate arity was never checked at all. `R("CAUSES",
"x")` was accepted. `SENSES(backend, caller)` — a resource sensing an agent —
was accepted. Both then failed silently at alignment time, which is exactly the
failure class the package says it exists to prevent.

This module closes that gap. Everything the aligner and the validator need to
know about *what a well-formed relation is* lives here as data:

    Signature       arity, and per-argument admissibility (nested relation,
                    or an entity whose declared role is in a permitted set)
    Role lattice    roles with parents, so `POOL` satisfies `RESOURCE` without
                    every encoder agreeing on granularity up front
    Families        predicates describing the same KIND of move by different
                    mechanisms; kin matches align at a discount
    Antonyms        predicate pairs that cannot both hold of the same arguments,
                    which is what makes contradiction detection possible

Two deliberate non-decisions, both load-bearing:

  * Structural predicates (DEPENDS, SHARED, FLOWS, CONTAINS) are in **no**
    family. They are the anchors the rest of a mapping hangs off. Letting
    DEPENDS kin-match SHARED would loosen every downstream binding at once.

  * CAUSES is in **no** family, and in particular is not kin to PRECEDES.
    Temporal sequence is not causation, and a system that silently aligns the
    two will project correlations as mechanisms.

A Schema is immutable once built. Extending the vocabulary is a deliberate act
with a diff, not a runtime side effect.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

__all__ = [
    "ArgSpec", "Signature", "Schema", "CORE_SCHEMA",
    "ENT", "REL", "ANY", "SchemaError",
]


class SchemaError(ValueError):
    """Raised when a relation cannot be reconciled with the schema."""


# --------------------------------------------------------------------------
# Argument specifications
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ArgSpec:
    """What may occupy one argument position.

    rel_ok    a nested relation is admissible here
    roles     admissible entity roles; None means "any declared role", and an
              empty frozenset means "no entity may appear here"
    """
    rel_ok: bool = False
    roles: frozenset[str] | None = None

    @property
    def entity_ok(self) -> bool:
        return self.roles is None or bool(self.roles)

    def describe(self) -> str:
        parts = []
        if self.rel_ok:
            parts.append("<relation>")
        if self.roles is None:
            parts.append("<any entity>")
        elif self.roles:
            parts.append("|".join(sorted(self.roles)))
        return " or ".join(parts) or "<nothing>"


def ENT(*roles: str) -> ArgSpec:
    """An entity argument restricted to the given roles (none = unrestricted)."""
    return ArgSpec(rel_ok=False, roles=frozenset(roles) if roles else None)


REL = ArgSpec(rel_ok=True, roles=frozenset())
ANY = ArgSpec(rel_ok=True, roles=None)


def REL_OR(*roles: str) -> ArgSpec:
    return ArgSpec(rel_ok=True, roles=frozenset(roles) if roles else None)


# --------------------------------------------------------------------------
# Signatures
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Signature:
    pred: str
    args: tuple[ArgSpec, ...]
    gloss: str
    family: str | None = None
    note: str = ""

    @property
    def arity(self) -> int:
        return len(self.args)


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------

class Schema:
    """An immutable predicate/role ontology."""

    def __init__(
        self,
        signatures: Iterable[Signature],
        role_parents: Mapping[str, str] | None = None,
        antonyms: Iterable[tuple[str, str]] = (),
        kin_discount: float = 0.5,
    ) -> None:
        self._sigs: dict[str, Signature] = {}
        for s in signatures:
            if s.pred in self._sigs:
                raise SchemaError(f"duplicate signature for {s.pred}")
            self._sigs[s.pred] = s

        self._role_parent: dict[str, str] = dict(role_parents or {})
        self._check_role_lattice()

        self._family: dict[str, str] = {
            p: s.family for p, s in self._sigs.items() if s.family
        }
        self._families: dict[str, frozenset[str]] = {}
        for p, f in self._family.items():
            self._families[f] = self._families.get(f, frozenset()) | {p}

        self._antonyms: frozenset[frozenset[str]] = frozenset(
            frozenset(pair) for pair in antonyms
        )
        for pair in self._antonyms:
            for p in pair:
                if p not in self._sigs:
                    raise SchemaError(f"antonym references unknown predicate {p}")

        self.kin_discount = kin_discount

    # ---- roles --------------------------------------------------------

    def _check_role_lattice(self) -> None:
        for role in self._role_parent:
            seen = {role}
            cur = role
            while cur in self._role_parent:
                cur = self._role_parent[cur]
                if cur in seen:
                    raise SchemaError(f"cycle in role lattice at {role}")
                seen.add(cur)

    def role_ancestry(self, role: str) -> list[str]:
        out = [role]
        cur = role
        while cur in self._role_parent:
            cur = self._role_parent[cur]
            out.append(cur)
        return out

    def role_satisfies(self, role: str, required: str) -> bool:
        """A role satisfies a requirement if it is that role or refines it."""
        return required in self.role_ancestry(role)

    def roles_compatible(self, a: str | None, b: str | None) -> bool:
        """Two declared roles may bind if their ancestries intersect.

        Compatibility is a *least common ancestor* test, not a refinement test.
        A connection POOL and an API GATEWAY are sibling refinements of
        RESOURCE: neither refines the other, and requiring one to would forbid
        exactly the cross-domain bindings this engine exists to make. What the
        guard still forbids is a disjoint ancestry — AGENT against RESOURCE,
        LOAD against SIGNAL — which is the failure mode it was built for.

        `None` (undeclared) is permissive so typing can be adopted
        incrementally. The validator flags undeclared roles as errors, so that
        permissiveness never silently reaches the aligner in strict mode.
        """
        if a is None or b is None:
            return True
        return bool(set(self.role_ancestry(a)) & set(self.role_ancestry(b)))

    # ---- predicates ---------------------------------------------------

    @property
    def vocabulary(self) -> frozenset[str]:
        return frozenset(self._sigs)

    def signature(self, pred: str) -> Signature | None:
        return self._sigs.get(pred)

    def known(self, pred: str) -> bool:
        return pred in self._sigs

    def family_of(self, pred: str) -> str | None:
        return self._family.get(pred)

    def kin(self, a: str, b: str) -> bool:
        """Different predicates in the same family."""
        if a == b:
            return False
        fa = self._family.get(a)
        return fa is not None and fa == self._family.get(b)

    def match_weight(self, a: str, b: str) -> float | None:
        """1.0 for an identical predicate, kin_discount for a family match,
        None when the two may not align at all."""
        if a == b:
            return 1.0
        if self.kin(a, b):
            return self.kin_discount
        return None

    def antonym(self, a: str, b: str) -> bool:
        return frozenset((a, b)) in self._antonyms

    # ---- validation ---------------------------------------------------

    def check_arity(self, pred: str, n: int) -> str | None:
        sig = self._sigs.get(pred)
        if sig is None:
            return None
        if sig.arity != n:
            return (f"{pred} takes {sig.arity} argument(s), got {n}")
        return None

    def check_args(self, pred: str, args: tuple[Any, ...],
                   types: Mapping[str, str]) -> list[str]:
        """Per-argument admissibility. Returns a list of human-readable
        problems; empty means the relation is well-formed under this schema."""
        sig = self._sigs.get(pred)
        if sig is None:
            return []
        if sig.arity != len(args):
            return [f"{pred} takes {sig.arity} argument(s), got {len(args)}"]

        out: list[str] = []
        for i, (spec, a) in enumerate(zip(sig.args, args)):
            is_rel = not isinstance(a, str)
            if is_rel:
                if not spec.rel_ok:
                    out.append(
                        f"{pred} argument {i} must be {spec.describe()}, "
                        f"got a nested relation")
                continue
            if not spec.entity_ok:
                out.append(
                    f"{pred} argument {i} must be {spec.describe()}, "
                    f"got the entity '{a}'")
                continue
            role = types.get(a)
            if role is None or spec.roles is None:
                continue
            if not any(self.role_satisfies(role, r) for r in spec.roles):
                out.append(
                    f"{pred} argument {i} expects {spec.describe()}, "
                    f"but '{a}' is declared {role}")
        return out

    # ---- rendering ----------------------------------------------------

    def gloss_template(self, pred: str) -> str | None:
        sig = self._sigs.get(pred)
        return sig.gloss if sig else None

    def summary(self) -> str:
        fams = ", ".join(
            f"{f}={{{', '.join(sorted(ps))}}}"
            for f, ps in sorted(self._families.items()))
        return (f"{len(self._sigs)} predicates, "
                f"{len(set(self._role_parent) | set(self._role_parent.values()))} "
                f"lattice roles, {len(self._antonyms)} antonym pair(s)\n"
                f"families: {fams}")


# --------------------------------------------------------------------------
# The core schema
# --------------------------------------------------------------------------

_ROLE_PARENTS = {
    # refinements; an encoder may use the specific term and still bind against
    # a signature written in terms of the general one
    "POOL": "RESOURCE",
    "BUFFER": "RESOURCE",
    "CHANNEL": "RESOURCE",
    "GATEWAY": "RESOURCE",
    "SERVICE": "AGENT",
    "HUMAN": "AGENT",
    "AGGREGATE": "LOAD",
    "RATE": "LOAD",
    "MEASUREMENT": "SIGNAL",
}

_SIGNATURES = [
    # ---- structure: the anchors. Deliberately family-less. ---------------
    Signature("DEPENDS", (ENT("AGENT"), ENT("RESOURCE")),
              "{0} depends on {1}",
              note="anchor predicate; no family by design"),
    Signature("SHARED", (ENT("RESOURCE"), ENT("AGENT")),
              "{0} is shared across {1}",
              note="anchor predicate; no family by design"),
    Signature("FLOWS", (ENT("AGENT", "LOAD"), ENT("RESOURCE")),
              "{0} moves through {1}",
              note="anchor predicate; no family by design"),
    Signature("CONTAINS", (ENT("RESOURCE"), ENT()),
              "{0} contains {1}",
              note="anchor predicate; no family by design"),

    # ---- causal backbone: also family-less, and deliberately not kin ------
    Signature("CAUSES", (REL, REL), "{0}, which causes {1}",
              note="not kin to PRECEDES: sequence is not mechanism"),
    Signature("PRECEDES", (REL, REL), "{0}, and then {1}",
              note="observational ordering only"),
    Signature("PREVENTS", (REL, REL), "{0}, which prevents {1}"),

    # ---- states -----------------------------------------------------------
    Signature("EXCESS", (ENT("LOAD"),), "there is too much {0}"),
    Signature("SCARCE", (ENT("RESOURCE", "LOAD"),), "{0} is scarce"),
    Signature("QUEUEING", (ENT("RESOURCE"),), "work piles up at {0}"),

    # ---- overload family --------------------------------------------------
    Signature("SATURATES", (ENT("RESOURCE"),), "{0} runs out of headroom",
              family="OVERLOAD"),
    Signature("EXHAUSTS", (ENT("LOAD", "AGENT"), ENT("RESOURCE")),
              "{0} exhausts {1}", family="OVERLOAD"),
    Signature("FAILS", (ENT("RESOURCE", "AGENT"),), "{0} fails outright",
              family="OVERLOAD"),
    Signature("DEGRADES", (ENT("RESOURCE", "AGENT"),),
              "{0} degrades under load", family="OVERLOAD"),

    # ---- growth / decay ---------------------------------------------------
    Signature("INCREASES", (REL_OR("LOAD", "SIGNAL"), ENT("SIGNAL", "LOAD")),
              "{0} drives up {1}", family="GROWTH"),
    Signature("AMPLIFIES", (ENT("LOAD"), ENT("RESOURCE")),
              "{0} is amplified at {1}", family="GROWTH"),
    Signature("DECREASES", (REL_OR("LOAD", "SIGNAL"), ENT("SIGNAL", "LOAD")),
              "{0} brings down {1}", family="DECAY"),
    Signature("DAMPENS", (ENT("LOAD", "SIGNAL"), ENT("RESOURCE")),
              "{0} is damped at {1}", family="DECAY"),

    # ---- dynamics ---------------------------------------------------------
    Signature("OSCILLATES", (ENT("LOAD", "SIGNAL"),), "{0} swings up and down"),
    Signature("CASCADES", (ENT("LOAD"), ENT("RESOURCE")),
              "{0} spreads failure through {1}"),

    # ---- signal quality ---------------------------------------------------
    Signature("DELAYED", (ENT("SIGNAL"),), "{0} arrives late", family="LAG"),
    Signature("STALE", (ENT("SIGNAL"),), "{0} is out of date", family="LAG"),
    Signature("SENSES", (ENT("AGENT"), ENT("SIGNAL")), "{0} observes {1}"),

    # ---- action family ----------------------------------------------------
    Signature("RETRIES", (ENT("AGENT"), ENT("RESOURCE")),
              "{0} retries against {1}", family="ACTION"),
    Signature("REDUCES", (ENT("AGENT"), ENT("LOAD")),
              "{0} cuts back {1}", family="ACTION"),
    Signature("REDISTRIBUTES", (ENT("LOAD"), ENT("RESOURCE")),
              "{0} spreads across {1}", family="ACTION"),
    Signature("THROTTLES", (ENT("AGENT"), ENT("LOAD")),
              "{0} throttles {1}", family="ACTION"),
    Signature("ISOLATES", (ENT("AGENT"), ENT("RESOURCE")),
              "{0} isolates {1}", family="ACTION"),
    Signature("ESCALATES", (ENT("AGENT"), ENT("SIGNAL")),
              "{0} escalates on {1}", family="ACTION"),
]

_ANTONYMS = [
    ("INCREASES", "DECREASES"),
    ("AMPLIFIES", "DAMPENS"),
    ("CAUSES", "PREVENTS"),
]

CORE_SCHEMA = Schema(_SIGNATURES, _ROLE_PARENTS, _ANTONYMS)
