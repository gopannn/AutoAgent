"""Interiors schema — the workbook's vocabulary, typed.

Deliberately a small extension rather than a parallel ontology. The interesting
claim this engine makes is that a rework spiral in a design studio and a retry
storm in a distributed system are *the same shape*, and that claim only pays
off if both are written in the same predicate vocabulary. Inventing
`DESIGN_REWORK_LOOP` as its own predicate would make the transfer impossible
by construction.

So the mapping is:

    a client asking for another revision        RETRIES   (agent, resource)
    scope creep                                 EXCESS    (load)
    a stage running past its SLA                BREACHES  (resource)
    work piling up in a stage                   QUEUEING  (resource)
    days-in-stage climbing                      INCREASES (rel, signal)

`BREACHES` and `FREEZES` are the only genuinely new predicates, and `BREACHES`
is placed in the OVERLOAD family on purpose: an SLA breach is structurally an
overload, so it kin-matches `SATURATES`, `EXHAUSTS` and `FAILS` at a discount
and lets a power-grid cascade reach a project schedule.

Roles come straight from 34_ENTITY_DICTIONARY. They refine the four core roles
rather than replacing them, which is what lets a `DESIGNER` bind to a `SERVICE`
in a software incident — least common ancestor is `AGENT`, and the type guard
passes. Under exact-equality role matching (which both v4 and the CTD package
use) none of these bindings would form and the whole cross-domain layer would
be dead.
"""

from __future__ import annotations

from ..kernel.schema import CORE_SCHEMA, ENT, REL_OR, Schema, Signature

__all__ = ["INTERIORS_SCHEMA", "EVIDENCE_PLANE", "EXCEPTION_PREDICATE"]

# Roles from 34_ENTITY_DICTIONARY, refining the four core roles.
_ROLES = {
    # agents
    "DESIGNER": "AGENT",
    "CLIENT": "AGENT",
    "MANAGER": "AGENT",
    "VENDOR": "AGENT",
    "CREW": "AGENT",
    # resources
    "STAGE": "RESOURCE",
    "BRIEF": "RESOURCE",
    "SITE": "RESOURCE",
    "CASH": "RESOURCE",
    "SCHEDULE": "RESOURCE",
    # loads
    "SCOPE": "LOAD",
    "REVISIONS": "LOAD",
    "WORKLOAD": "LOAD",
    # signals
    "COMPLETENESS": "SIGNAL",
    "ELAPSED": "SIGNAL",
    "PRICE": "SIGNAL",
}

_NEW = [
    Signature("BREACHES", (ENT("RESOURCE"),),
              "{0} runs past its commitment", family="OVERLOAD",
              note="an SLA breach is structurally an overload; kin to "
                   "SATURATES/EXHAUSTS/FAILS by design, which is what lets a "
                   "grid cascade reach a project schedule"),
    Signature("FREEZES", (ENT("AGENT"), ENT("RESOURCE")),
              "{0} freezes {1}", family="ACTION",
              note="material / appliance / automation freeze — 04_PROJECTS "
                   "BK:BN. A mitigation, so it belongs with the other actions"),
    Signature("INCOMPLETE", (ENT("BRIEF", "SITE"),),
              "{0} is missing required information",
              note="restricted to information-bearing resources. Declared as "
                   "ENT('RESOURCE') it type-checked cleanly and let the "
                   "engine project INCOMPLETE(cash) -- 'cash is missing "
                   "required information' -- which is grammatical, passes "
                   "every guard, and is nonsense. A signature is only as "
                   "tight as the role you give it. No family: an incomplete "
                   "brief is an absence, not an overload, and treating the "
                   "two as kin would let a capacity incident project onto a "
                   "paperwork gap"),
    Signature("APPROVES", (ENT("AGENT"), ENT("RESOURCE")),
              "{0} signs off {1}"),
    Signature("WITHHOLDS", (ENT("AGENT"), ENT("RESOURCE")),
              "{0} withholds {1}", family="ACTION"),
]

INTERIORS_SCHEMA = Schema(
    signatures=[CORE_SCHEMA.signature(p) for p in sorted(CORE_SCHEMA.vocabulary)]
               + _NEW,
    role_parents={**{k: v for k, v in _ROLES.items()},
                  "POOL": "RESOURCE", "BUFFER": "RESOURCE",
                  "CHANNEL": "RESOURCE", "GATEWAY": "RESOURCE",
                  "SERVICE": "AGENT", "HUMAN": "AGENT",
                  "AGGREGATE": "LOAD", "RATE": "LOAD",
                  "MEASUREMENT": "SIGNAL"},
    antonyms=[("INCREASES", "DECREASES"), ("AMPLIFIES", "DAMPENS"),
              ("CAUSES", "PREVENTS"), ("APPROVES", "WITHHOLDS")],
)


# --------------------------------------------------------------------------
# 37_GOVERNANCE section A, as executable policy
# --------------------------------------------------------------------------

EVIDENCE_PLANE = {
    # class                 can it drive a commitment?   plane
    # CONDITIONAL, not RESOLUTION. 37_GOVERNANCE permits this class to drive a
    # commitment "only after confirmation in writing" -- and the workbook
    # captures no field anywhere recording that a confirmation was received.
    # The condition is therefore unverifiable from the register, so the class
    # is excluded from commitments today. It moves to RESOLUTION the moment
    # 12_DESIGN_INTELLIGENCE gains a written-confirmation flag, and not before.
    "CUSTOMER_ENTERED":     ("only after confirmation in writing -- "
                             "no field records it", "CONDITIONAL"),
    "STAFF_OBSERVED":       ("yes — strongest input",      "RESOLUTION"),
    "MANAGER_CLASSIFIED":   ("yes, manager named",         "RESOLUTION"),
    "RESEARCH_DERIVED":     ("planning only, never quote", "HYPOTHESIS"),
    "VENDOR_QUOTED":        ("yes, until expiry",          "RESOLUTION"),
    "MANUFACTURER_VERIFIED": ("yes for specification",     "RESOLUTION"),
    "SYSTEM_CALCULATED":    ("yes if inputs sound",        "RESOLUTION"),
    "FIRM_ACTUAL":          ("yes — outranks research",    "RESOLUTION"),
    "MODEL_PREDICTED":      ("advisory only",              "HYPOTHESIS"),
}
"""37_GOVERNANCE already draws the plane separation the CTD package enforces in
code: MODEL_PREDICTED is 'advisory only, with confidence shown', which is
exactly `state=HYPOTHESIS`. Every pre-mortem prediction leaves this adapter
classified MODEL_PREDICTED, so the workbook's own governance rule carries it
without anybody having to remember the rule."""


EXCEPTION_PREDICATE = {
    # 32_EXCEPTION_QUEUE type          -> the relation a prediction would assert
    "PAYMENT_BLOCK":            "WITHHOLDS",
    "SLA_BREACH":               "BREACHES",
    "SEQUENCE_VIOLATION":       "CAUSES",
    "SITE_INFORMATION_MISSING": "INCOMPLETE",
    "BRIEF_INCOMPLETE":         "INCOMPLETE",
    "APPROVAL_OVERDUE":         "DELAYED",
    "OVER_CAPACITY":            "SATURATES",
    "FORECAST_SLIP":            "DELAYED",
    "REVISION_WITHOUT_REASON":  "RETRIES",
    "STALE_RESEARCH_RATE":      "STALE",
}
"""The exception taxonomy on 32 is already a predicate vocabulary; it just is
not typed. This is the join, and it is what lets a projected relation be written
back into the queue as a *predicted* exception alongside the ones that have
already happened."""
