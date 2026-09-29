from verification_compiler.ledger import (
    blocking_open_findings,
    fingerprint,
    reconcile,
    release_blockers,
)
from verification_compiler.schemas import AcceptanceResult, AuditFinding, AuditReport

from . import fakes

BLOCKING = {"critical", "high", "medium"}


def finding(category="Auth bypass", files=("service/main.py",), severity="high", tests=("AT_health",), desc="d"):
    return AuditFinding(
        severity=severity, category=category, description=desc, affected_files=list(files),
        remediation_requirement="fix", invariant_ids=["INV_auth", "INV_unknown"], acceptance_test_ids=list(tests),
    )


def rec(ledger, report, rnd, h="h"):
    return reconcile(ledger, report, audit_round=rnd, codebase_hash=h,
                     known_test_ids={"AT_health"}, known_invariant_ids={"INV_auth"})


def test_fingerprint_is_stable_across_wording_and_ordering():
    a = finding(category="Auth  Bypass", files=("b.py", "a.py"), desc="one wording")
    b = finding(category="auth bypass", files=("a.py", "b.py"), desc="another wording")
    assert fingerprint(a) == fingerprint(b)
    assert fingerprint(a) != fingerprint(finding(category="SQL injection"))


def test_new_findings_are_never_dropped_by_colliding_llm_ids():
    ledger = rec({}, AuditReport(new_findings=[finding()]), 1)
    ledger = rec(ledger, AuditReport(new_findings=[finding(category="SQL injection")]), 2)
    assert len(ledger) == 2
    assert all(not f["closed"] for f in ledger.values())


def test_unknown_links_are_dropped():
    ledger = rec({}, AuditReport(new_findings=[finding(tests=("AT_health", "AT_bogus"))]), 1)
    (f,) = ledger.values()
    assert f["acceptance_test_ids"] == ["AT_health"]
    assert f["invariant_ids"] == ["INV_auth"]


def test_closure_requires_explicit_resolution():
    ledger = rec({}, AuditReport(new_findings=[finding()]), 1)
    fp = next(iter(ledger))
    omitted = rec(ledger, AuditReport(), 2)
    assert not omitted[fp]["closed"]
    resolved = rec(ledger, AuditReport(open_finding_statuses=[
        {"fingerprint": fp, "status": "resolved", "justification": "fixed"}]), 2, h="h2")
    assert resolved[fp]["closed"] and resolved[fp]["closed_codebase_hash"] == "h2"
    assert not ledger[fp]["closed"], "reconcile must not mutate its input"


def test_rereported_finding_reopens():
    ledger = rec({}, AuditReport(new_findings=[finding()]), 1)
    fp = next(iter(ledger))
    ledger = rec(ledger, AuditReport(open_finding_statuses=[
        {"fingerprint": fp, "status": "resolved", "justification": "fixed"}]), 2)
    ledger = rec(ledger, AuditReport(new_findings=[finding(severity="critical")]), 3)
    assert not ledger[fp]["closed"]
    assert ledger[fp]["severity"] == "critical"
    assert [h["event"] for h in ledger[fp]["history"]] == ["opened", "resolved", "reopened"]


def test_low_severity_does_not_block():
    ledger = rec({}, AuditReport(new_findings=[finding(severity="low")]), 1)
    assert blocking_open_findings(ledger, BLOCKING) == []


def test_release_blockers_require_linked_tests_to_pass():
    ledger = rec({}, AuditReport(new_findings=[finding()]), 1)
    fp = next(iter(ledger))
    ledger = rec(ledger, AuditReport(open_finding_statuses=[
        {"fingerprint": fp, "status": "resolved", "justification": "fixed"}]), 2)
    result = fakes.passing_result(fakes.codebase(), fakes.spec(), {"sha256": "x"})
    assert release_blockers(ledger, result, BLOCKING) == []
    result.acceptance = [AcceptanceResult(id="AT_health", passed=False, cases=1, failures=["boom"])]
    assert release_blockers(ledger, result, BLOCKING)


def _closed(tests, category="Auth bypass"):
    ledger = rec({}, AuditReport(new_findings=[finding(category=category, tests=tests, severity="critical")]), 1)
    fp = next(iter(ledger))
    return rec(ledger, AuditReport(open_finding_statuses=[
        {"fingerprint": fp, "status": "resolved", "justification": "fixed"}]), 2)


def test_closure_basis_distinguishes_tested_from_asserted():
    from verification_compiler.ledger import MODEL_ASSERTION, OPEN, VERIFIED_BY_TESTS, closure_basis

    result = fakes.passing_result(fakes.codebase(), fakes.spec(), {"sha256": "x"})
    assert closure_basis(next(iter(_closed(("AT_health",)).values())), result) == VERIFIED_BY_TESTS
    assert closure_basis(next(iter(_closed(()).values())), result) == MODEL_ASSERTION
    open_ledger = rec({}, AuditReport(new_findings=[finding()]), 1)
    assert closure_basis(next(iter(open_ledger.values())), result) == OPEN


def test_policy_can_require_test_evidence_for_critical_findings():
    result = fakes.passing_result(fakes.codebase(), fakes.spec(), {"sha256": "x"})
    asserted_only = _closed(())
    assert release_blockers(asserted_only, result, BLOCKING) == []
    reasons = release_blockers(asserted_only, result, BLOCKING, require_test_evidence={"critical"})
    assert reasons and "auditor's judgement alone" in reasons[0]
    assert release_blockers(_closed(("AT_health",)), result, BLOCKING, require_test_evidence={"critical"}) == []


def test_report_does_not_present_model_closure_as_verified():
    from verification_compiler.report import render_summary

    result = fakes.passing_result(fakes.codebase(), fakes.spec(), {"sha256": "x"})
    ledger = {**_closed(("AT_health",)), **_closed((), category="Token replay")}
    text = render_summary({"status": "released", "verification_result": result.model_dump(), "findings_ledger": ledger})
    assert "1 closed and verified by passing tests" in text
    assert "1 closed on the auditor's judgement only" in text
