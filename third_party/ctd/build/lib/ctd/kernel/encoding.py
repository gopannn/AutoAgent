"""Encoding validation — the actual bottleneck.

Both modes share one bottleneck wearing two hats: CLOSE depends on spec
extraction, TRANSFER depends on relational encoding. The engines are cheap; the
encoding is the entire cost, and a bad encoding fails SILENTLY — it either
produces nonsense that looks structured, or it sits in the library never
matching anything and nobody notices.

Improving the aligner buys almost nothing. This file is where the leverage is.

v4 shipped six checks. Four of them (UNTYPED, SHALLOW, OFF-VOCAB, ORPHAN)
compared against a flat `set[str]` of predicate names, which cannot catch the
two most common real encoding errors:

    wrong arity        R("CAUSES", "x") — accepted by v4, then silently
                       mis-glossed and never aligned
    wrong role         SENSES(backend, caller) — a resource sensing an agent.
                       v4 caught this only if the *aligner* later happened to
                       bind conflicting entities, i.e. sometimes, by accident

Both are now decided at ingest against `schema.py`, before anything is stored.

Three further checks are new because they catch encodings that are internally
inconsistent rather than merely unusable:

    CONTRADICTION      the same record asserts a predicate and its antonym over
                       identical arguments
    CYCLE              the causal graph contains a loop, so 'what causes what'
                       is not answerable from this encoding
    SELF-CAUSE         CAUSES(P, P)

ROUNDTRIP is unchanged and still the only check that catches the last category:
*encoded correctly, encodes the wrong thing*. A human reads the gloss against
the source document and confirms it still says the same thing. Nothing
automates that.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .schema import CORE_SCHEMA, Schema
from .store import Record, Rel

__all__ = [
    "Finding", "Validation", "validate", "validate_all", "summary",
    "gloss", "CORE_VOCABULARY",
]

# Kept as a module-level name for compatibility with v4 call sites; it is now
# derived from the schema rather than maintained separately, so the two cannot
# drift apart.
CORE_VOCABULARY = CORE_SCHEMA.vocabulary

SEVERITY_ORDER = {"error": 0, "warn": 1, "info": 2}


def gloss(rel: Rel, schema: Schema = CORE_SCHEMA) -> str:
    """Render a relation as English for human round-trip verification."""
    parts = [gloss(a, schema) if isinstance(a, Rel) else str(a)
             for a in rel.args]
    tmpl = schema.gloss_template(rel.pred)
    if not tmpl:
        return f"{rel.pred}({', '.join(parts)})"
    try:
        return tmpl.format(*parts)
    except (IndexError, KeyError):
        return f"{rel.pred}({', '.join(parts)})"


# --------------------------------------------------------------------------

@dataclass
class Finding:
    level: str          # "error" | "warn" | "info"
    code: str
    detail: str


@dataclass
class Validation:
    record_id: str
    findings: list[Finding] = field(default_factory=list)
    roundtrip: list[str] = field(default_factory=list)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "warn"]

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def codes(self) -> set[str]:
        return {f.code for f in self.findings}

    def explain(self) -> str:
        head = "OK" if self.ok else f"{len(self.errors)} ERROR(S)"
        out = [f"{self.record_id}: {head}"]
        for f in sorted(self.findings, key=lambda f: SEVERITY_ORDER[f.level]):
            out.append(f"   [{f.level:<5}] {f.code:<13} {f.detail}")
        if self.roundtrip:
            out.append("   round-trip (verify this says what the source said):")
            for line in self.roundtrip:
                out.append(f"     - {line}")
        return "\n".join(out)


# --------------------------------------------------------------------------

def _causal_cycle(rels: list[Rel], schema: Schema) -> list[Rel] | None:
    """Detect a loop in the CAUSES graph.

    A cycle means the encoding cannot answer 'what causes what', which is the
    only question TRANSFER projects an answer to. Flagged rather than rejected,
    because genuine positive-feedback loops are sometimes encoded deliberately
    — but the encoder should have to say so.
    """
    edges: dict[Rel, list[Rel]] = {}
    for r in rels:
        if r.pred != "CAUSES" or len(r.args) != 2:
            continue
        a, b = r.args
        if isinstance(a, Rel) and isinstance(b, Rel):
            edges.setdefault(a, []).append(b)

    WHITE, GREY, BLACK = 0, 1, 2
    colour: dict[Rel, int] = {}
    stack: list[Rel] = []

    def dfs(n: Rel) -> list[Rel] | None:
        colour[n] = GREY
        stack.append(n)
        for m in edges.get(n, ()):
            c = colour.get(m, WHITE)
            if c == GREY:
                return stack[stack.index(m):] + [m]
            if c == WHITE:
                found = dfs(m)
                if found:
                    return found
        colour[n] = BLACK
        stack.pop()
        return None

    for n in list(edges):
        if colour.get(n, WHITE) == WHITE:
            found = dfs(n)
            if found:
                return found
    return None


def validate(rec: Record, schema: Schema = CORE_SCHEMA,
             min_order: int = 2) -> Validation:
    v = Validation(rec.id)
    rels = rec.all_rels()
    vocab = schema.vocabulary

    if not rels:
        v.findings.append(Finding(
            "info", "NO-RELS",
            "attributes only; usable by CLOSE, invisible to TRANSFER"))
        return v

    # ---- UNTYPED ------------------------------------------------------
    ents = rec.entities()
    untyped = sorted(e for e in ents if e not in rec.types)
    if untyped:
        v.findings.append(Finding(
            "error", "UNTYPED",
            f"no declared role for {', '.join(untyped)} — these can bind to "
            f"entities of any kind and produce nonsense projections"))

    # ---- ARITY / SIGNATURE --------------------------------------------
    # This is the check v4 had no machinery for. Both problems it catches are
    # invisible downstream: a wrong-arity relation never aligns, and a
    # wrong-role relation aligns and projects a sentence that is grammatical
    # and false.
    arity_problems: list[str] = []
    sig_problems: list[str] = []
    for r in rels:
        if not schema.known(r.pred):
            continue
        a = schema.check_arity(r.pred, r.arity)
        if a:
            arity_problems.append(a)
            continue
        sig_problems.extend(schema.check_args(r.pred, r.args, rec.types))

    if arity_problems:
        v.findings.append(Finding(
            "error", "ARITY",
            "; ".join(sorted(set(arity_problems)))))
    if sig_problems:
        v.findings.append(Finding(
            "error", "SIGNATURE",
            "; ".join(sorted(set(sig_problems)))))

    # ---- unknown declared roles ---------------------------------------
    known_roles = {r for sig in (schema.signature(p) for p in vocab)
                   if sig for spec in sig.args
                   for r in (spec.roles or ())}
    stray = sorted({role for e, role in rec.types.items()
                    if e in ents and role not in known_roles
                    and not any(anc in known_roles
                                for anc in schema.role_ancestry(role))})
    if stray:
        v.findings.append(Finding(
            "warn", "UNKNOWN-ROLE",
            f"{', '.join(stray)} not in the role lattice — signature checks "
            f"cannot be applied to entities carrying these roles"))

    # ---- SHALLOW ------------------------------------------------------
    depth = max(r.order for r in rels)
    if depth < min_order:
        v.findings.append(Finding(
            "error", "SHALLOW",
            f"max relational order {depth}; no causal structure to align on, "
            f"so this record can only ever match on surface resemblance"))

    # ---- OFF-VOCAB ----------------------------------------------------
    off = sorted({r.pred for r in rels} - vocab)
    if off:
        v.findings.append(Finding(
            "warn", "OFF-VOCAB",
            f"{', '.join(off)} not in the shared vocabulary — relations using "
            f"these can never align with another domain"))

    # ---- ORPHAN / BARREN ----------------------------------------------
    appearances: dict[str, int] = {}
    for r in rels:
        for a in r.args:
            if isinstance(a, str):
                appearances[a] = appearances.get(a, 0) + 1
    orphans = sorted(e for e, n in appearances.items() if n == 1)
    if orphans:
        v.findings.append(Finding(
            "warn", "ORPHAN",
            f"{', '.join(orphans)} appear in one relation only; mappable only "
            f"if that exact relation aligns"))

    anchored = {a for r in rels if r.pred in vocab
                for a in r.args if isinstance(a, str)}
    barren = [r for r in rels
              if r.pred in vocab
              and any(isinstance(a, str) and a not in anchored
                      for a in r.args)]
    if barren:
        v.findings.append(Finding(
            "error", "BARREN",
            f"{len(barren)} relation(s) reference unanchored entities and can "
            f"never be projected: {gloss(barren[0], schema)}"))

    # ---- CONTRADICTION -------------------------------------------------
    by_args: dict[tuple, set[str]] = {}
    for r in rels:
        by_args.setdefault(r.args, set()).add(r.pred)
    clashes = [
        (a, b, args)
        for args, preds in by_args.items()
        for a in preds for b in preds
        if a < b and schema.antonym(a, b)
    ]
    if clashes:
        a, b, args = clashes[0]
        v.findings.append(Finding(
            "error", "CONTRADICTION",
            f"{a} and {b} are asserted over the same arguments "
            f"({', '.join(str(x) for x in args)}); at most one can hold"))

    # ---- SELF-CAUSE / CYCLE -------------------------------------------
    selfies = [r for r in rels
               if r.pred == "CAUSES" and len(r.args) == 2
               and r.args[0] == r.args[1]]
    if selfies:
        v.findings.append(Finding(
            "error", "SELF-CAUSE",
            f"{gloss(selfies[0], schema)} — a relation cannot be its own cause"))

    cycle = _causal_cycle(rels, schema)
    if cycle:
        v.findings.append(Finding(
            "warn", "CYCLE",
            "causal loop: " + " -> ".join(gloss(r, schema) for r in cycle) +
            " — if this is a deliberate feedback loop say so in the record "
            "text; otherwise the causal direction is not recoverable"))

    # ---- ROUNDTRIP ----------------------------------------------------
    v.roundtrip = [gloss(r, schema) for r in rec.rels]
    return v


def validate_all(records: Iterable[Record], **kw) -> list[Validation]:
    return [validate(r, **kw) for r in records]


def summary(vals: list[Validation]) -> str:
    bad = [v for v in vals if not v.ok]
    warn = [v for v in vals if v.ok and v.warnings]
    codes: dict[str, int] = {}
    for v in vals:
        for f in v.findings:
            if f.level != "info":
                codes[f.code] = codes.get(f.code, 0) + 1
    tail = ", ".join(f"{k}x{n}" for k, n in sorted(codes.items()))
    return (f"{len(vals)} record(s): {len(vals) - len(bad)} usable, "
            f"{len(bad)} rejected, {len(warn)} with warnings"
            + (f"  [{tail}]" if tail else ""))
