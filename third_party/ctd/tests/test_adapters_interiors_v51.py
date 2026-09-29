"""ERP integration tests.

The two layers this exercises are the ones where a mistake is expensive in a
way the core benchmarks cannot see: the plane boundary between prediction and
commitment, and the tri-state handling of a register field that is absent
rather than negative.

    python3 tests/test_interiors.py
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from ctd.adapters.incidents import INTERIORS_INCIDENTS
from ctd.adapters.resolution import (
    COMMITMENT_CLASSES, TRUST, build_graph, resolve_feasibility,
)
from ctd.adapters.schema_interiors import (
    EVIDENCE_PLANE, EXCEPTION_PREDICATE, INTERIORS_SCHEMA,
)
from ctd.adapters.workbook import ProjectRow, build_target, to_exception_rows
from ctd.kernel import Knobs, R, Record, TopoStore, run_premortem, validate, walk

MASTER = os.environ.get("CTD_ERP_MASTER", "")


def _row(**over) -> ProjectRow:
    vals = {"id": "TEST-1", "client": "C", "designer": "D", "ops": "O",
            "status": "Active", "district": None, "property": None,
            "carpet": None, "scope": None, "style": None, "tasks": 3,
            "revisions": 0, "stage": "2D Design", "days_idle": 1,
            "completeness": 1.0, "missing": "", "days_in_stage": 4,
            "sla": "On time", "slip": 0, "gate_flag": "No gated work logged "
            "yet", "pay_gate": "OK"}
    vals.update(over)
    return ProjectRow(6, vals)


# --------------------------------------------------------------------------
# Encoding
# --------------------------------------------------------------------------

def test_every_interiors_incident_passes_the_schema_gate():
    bad = [v for v in (validate(r, INTERIORS_SCHEMA)
                       for r in INTERIORS_INCIDENTS) if not v.ok]
    assert not bad, [v.explain() for v in bad]


def test_incomplete_is_restricted_to_information_bearing_resources():
    """REGRESSION: declared as ENT('RESOURCE') this type-checked cleanly and
    let the engine assert INCOMPLETE(cash) — 'cash is missing required
    information'. Grammatical, guarded, nonsense."""
    s = INTERIORS_SCHEMA
    assert s.check_args("INCOMPLETE", ("cash",), {"cash": "CASH"})
    assert not s.check_args("INCOMPLETE", ("brief",), {"brief": "BRIEF"})
    assert not s.check_args("INCOMPLETE", ("site",), {"site": "SITE"})


def test_breaches_is_kin_to_the_overload_family():
    """An SLA breach is structurally an overload. This is what lets a power
    grid cascade reach a project schedule, and it is a deliberate choice, so
    it gets a test rather than a comment."""
    s = INTERIORS_SCHEMA
    assert s.kin("BREACHES", "SATURATES")
    assert s.kin("BREACHES", "FAILS")
    assert not s.kin("BREACHES", "INCOMPLETE")


def test_interiors_roles_bind_across_domains():
    """A DESIGNER must be able to bind to a SERVICE in a software incident.
    Under exact-equality role matching — what both v4 and CTD use — none of
    these bindings form and the whole cross-domain layer is dead."""
    s = INTERIORS_SCHEMA
    assert s.roles_compatible("DESIGNER", "SERVICE")
    assert s.roles_compatible("STAGE", "GATEWAY")
    assert s.roles_compatible("SCOPE", "RATE")
    assert not s.roles_compatible("DESIGNER", "STAGE")
    assert not s.roles_compatible("SCOPE", "ELAPSED")


# --------------------------------------------------------------------------
# Premise construction
# --------------------------------------------------------------------------

def test_premise_asserts_only_what_the_register_holds():
    p = _row(completeness=1.0, pay_gate="OK", sla="On time", revisions=0,
             days_idle=0)
    prem = build_target(p)
    preds = {r.pred for r in walk(list(prem.target.rels))}
    assert "INCOMPLETE" not in preds
    assert "WITHHOLDS" not in preds
    assert "BREACHES" not in preds
    assert "RETRIES" not in preds
    # every asserted relation is traced to a column
    assert len(prem.evidence) == len(prem.target.rels)


def test_premise_names_what_it_could_not_assert():
    prem = build_target(_row())
    assert len(prem.gaps) == 5          # district, property, carpet, scope, style
    assert all("04_PROJECTS" in g for g in prem.gaps)


def test_premise_reacts_to_real_register_state():
    prem = build_target(_row(completeness=0.33, pay_gate="BLOCKED · Design",
                             sla="Breached", revisions=2, days_idle=9))
    preds = {r.pred for r in walk(list(prem.target.rels))}
    assert {"INCOMPLETE", "WITHHOLDS", "BREACHES", "RETRIES",
            "DELAYED"} <= preds


def test_every_premise_relation_is_schema_clean():
    prem = build_target(_row(completeness=0.5, pay_gate="BLOCKED",
                             sla="Breached", revisions=1, days_idle=10))
    for rel in walk(list(prem.target.rels)):
        assert not INTERIORS_SCHEMA.check_args(
            rel.pred, rel.args, prem.target.types), rel


# --------------------------------------------------------------------------
# The plane boundary
# --------------------------------------------------------------------------

def test_predictions_are_always_on_the_hypothesis_plane():
    prem = build_target(_row(completeness=0.33, pay_gate="BLOCKED"))
    store = TopoStore()
    store.ingest_all([Record(id=r.id, domain=r.domain, attrs=dict(r.attrs),
                             types=dict(r.types), rels=r.rels, text=r.text)
                      for r in INTERIORS_INCIDENTS])
    rounds = run_premortem(store, prem.target, Knobs(rounds=2),
                           schema=INTERIORS_SCHEMA)
    preds = [p for rr in rounds for p in rr.predictions]
    assert preds
    for e in to_exception_rows("TEST-1", preds):
        assert e.evidence_class == "MODEL_PREDICTED"
        assert e.plane == "HYPOTHESIS"


def test_advisory_classes_can_never_satisfy_a_commercial_gate():
    """The boundary, enforced rather than documented.

    37_GOVERNANCE says RESEARCH_DERIVED is 'planning only — never quote from
    it' and MODEL_PREDICTED is 'advisory only'. A convention people remember
    is not a control; membership of COMMITMENT_CLASSES is.
    """
    assert "MODEL_PREDICTED" not in COMMITMENT_CLASSES
    assert "RESEARCH_DERIVED" not in COMMITMENT_CLASSES
    for cls, (_wording, plane) in EVIDENCE_PLANE.items():
        assert plane in ("RESOLUTION", "CONDITIONAL", "HYPOTHESIS"), cls
        # RESOLUTION classes commit. CONDITIONAL and HYPOTHESIS do not, and
        # CONDITIONAL is the interesting one: 37_GOVERNANCE permits
        # CUSTOMER_ENTERED to commit once confirmed in writing, and no field
        # in the workbook records that confirmation. Until one exists the
        # class cannot be admitted, because the gate would be resting on a
        # condition nobody can check.
        assert (cls in COMMITMENT_CLASSES) == (plane == "RESOLUTION"), cls
        assert cls in TRUST
    # and the trust ordering matches the workbook's stated ordering
    assert TRUST["FIRM_ACTUAL"] > TRUST["RESEARCH_DERIVED"]
    assert TRUST["STAFF_OBSERVED"] >= max(TRUST.values())


def test_absent_is_not_the_same_as_negative():
    """REGRESSION: 04_PROJECTS BF reads 'No gated work logged yet' when
    nothing has been logged. A first pass compared it against "" and reported
    a sequence violation on all three live projects — ignorance scored as a
    negative finding."""
    absent = build_graph([_row(gate_flag="No gated work logged yet")])
    assert "sequence_flag" not in absent.get_node("TEST-1").attributes
    flagged = build_graph([_row(gate_flag="Finishes before wet-work sign-off")])
    assert flagged.get_node("TEST-1").attributes["sequence_flag"]
    # site_verified is never defaulted to False either
    assert "site_verified" not in absent.get_node("TEST-1").attributes


def test_violated_blocks_and_unknown_reviews():
    at = datetime.now(timezone.utc)
    blocked = resolve_feasibility([_row(completeness=0.3)], at)[0]
    assert blocked.verdict == "BLOCKED"
    assert "brief-complete" in blocked.violated

    review = resolve_feasibility([_row()], at)[0]
    assert review.verdict == "REVIEW"
    assert not review.violated
    assert review.unresolved
    for gap in review.gaps:
        assert gap["truth"] == "UNKNOWN"
        assert gap["action"], "an UNKNOWN with no action is just a shrug"


def test_every_gap_says_what_would_settle_it():
    for outcome in resolve_feasibility([_row(completeness=0.3, sla="Breached")]):
        for gap in outcome.gaps:
            assert gap["required_evidence"]
            assert gap["reason"]
            assert gap["priority"] > 0


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------

def test_exception_type_classifies_on_the_consequent():
    """REGRESSION: matching breadth-first from the root classified every
    CAUSES(...) as SEQUENCE_VIOLATION, so six distinct findings arrived under
    one label. What a reviewer needs is what the chain ends in."""
    from ctd.adapters.workbook import _exception_type
    from ctd.kernel.premortem import Prediction

    chain = R("CAUSES", R("QUEUEING", "s"), R("SATURATES", "s"))
    assert _exception_type(Prediction(chain)) == "OVER_CAPACITY"
    direct = R("BREACHES", "s")
    assert _exception_type(Prediction(direct)) == "SLA_BREACH"
    assert all(v for v in EXCEPTION_PREDICATE.values())


# --------------------------------------------------------------------------
# Live workbook
# --------------------------------------------------------------------------

def test_runs_against_the_real_master_if_present():
    if not os.path.exists(MASTER):
        return
    from interiors.workbook import premortem_for, read_projects
    rows = [p for p in read_projects(MASTER) if p.live]
    assert rows, "expected live projects in the register"
    for outcome in resolve_feasibility(rows):
        assert outcome.verdict in ("PASS", "REVIEW", "BLOCKED")
    prem, preds = premortem_for(rows[0])
    assert preds, "expected at least one prediction"
    for p in preds:
        assert p.check or not p.checkable
        for part in walk([p.rel]):
            assert not INTERIORS_SCHEMA.check_args(
                part.pred, part.args, prem.target.types), part


def test_the_master_workbook_is_never_written():
    """The addendum builder must not open the master for writing. Re-saving it
    through openpyxl silently drops 42 conditional-formatting extension
    blocks."""
    import ctd.adapters.build_addendum as mod
    src = open(mod.__file__).read()
    assert "wb.save(out)" in src
    assert "save(master)" not in src and "save(MASTER)" not in src
