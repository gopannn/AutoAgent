"""Strict negative tests for the release gate (epistemic-toolkit, component 7).

Each check must pass on known-good evidence, fail on every defect it claims to catch, and
every defect must be caught by at least one check. A check that passes on broken
evidence is VACUOUS; in strict mode an untargeted check or an uncovered defect fails too.
"""
import copy

from verification_compiler.hashing import codebase_hash, sha256_hex
from verification_compiler.ledger import reconcile, release_blockers
from verification_compiler.schemas import AuditReport, VerificationResult

from . import fakes
from .test_ledger import finding

Suite = fakes.toolkit().Suite
BLOCKING = {"critical", "high", "medium"}


def good_bundle() -> dict:
    cb, sp = fakes.codebase(), fakes.spec()
    lock = fakes.FakeResolver().resolve(cb["dependencies"])
    ledger = reconcile({}, AuditReport(new_findings=[finding()]), audit_round=1, codebase_hash="h",
                       known_test_ids={"AT_health"}, known_invariant_ids={"INV_auth"})
    fp = next(iter(ledger))
    ledger = reconcile(ledger, AuditReport(open_finding_statuses=[
        {"fingerprint": fp, "status": "resolved", "justification": "fixed"}]), audit_round=2, codebase_hash="h",
        known_test_ids={"AT_health"}, known_invariant_ids={"INV_auth"})
    evidence = fakes.passing_result(cb, sp, lock).model_dump()
    from verification_compiler.reasoning.decision import decide

    calibration = {"AT_health": {"not_found": False, "server_error": False, "empty_ok": False}}
    decided = decide(fakes.CONTRACT, sp, calibration, VerificationResult(**evidence), {}, BLOCKING,
                     {"is_valid": True, "feedback": "ok", "unmet_requirements": []})
    return {"codebase": cb, "lock_text": lock["text"], "evidence": evidence, "ledger": ledger, "decision": decided,
            "manifest": {"codebase_hash": codebase_hash(cb), "lockfile_hash": lock["sha256"]}}


def evidence_passes(b):
    return VerificationResult(**b["evidence"]).passed


def no_release_blockers(b):
    return release_blockers(b["ledger"], VerificationResult(**b["evidence"]), BLOCKING) == []


def manifest_matches_code_and_lock(b):
    return (b["manifest"]["codebase_hash"] == codebase_hash(b["codebase"])
            and b["manifest"]["lockfile_hash"] == sha256_hex(b["lock_text"]))


def decision_justified(b):
    """Recomputed from per-requirement evidence; the stored `release` flag is not trusted."""
    d = b["decision"]
    reqs = d["requirements"].values()
    return (bool(d["requirements"]) and d["ctd_outcome"] == "RESOLVED" and d["epistemic_verdict"] == "JUSTIFIED"
            and all(r["resolution"]["state"] == "RESOLVED" and r["justification"]["justified"] for r in reqs))


def _set(path, value):
    def mutate(b):
        target = b
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
    return mutate


def _first_finding(b):
    return next(iter(b["ledger"].values()))


MUTATIONS = {
    "acceptance_failed": lambda b: b["evidence"]["acceptance"][0].update(passed=False, failures=["assert 500 == 200"]),
    "acceptance_zero_cases": lambda b: b["evidence"]["acceptance"][0].update(cases=0),
    "acceptance_result_missing": _set(["evidence", "acceptance"], []),
    "expected_ids_emptied": lambda b: b["evidence"].update(expected_acceptance_ids=[], acceptance=[]),
    "static_check_failed": lambda b: b["evidence"]["checks"]["semgrep"].update(passed=False, exit_code=1),
    "checks_missing": _set(["evidence", "checks"], {}),
    "finding_reopened": lambda b: _first_finding(b).update(closed=False),
    "linked_test_unverified": lambda b: _first_finding(b).update(acceptance_test_ids=["AT_health", "AT_other"]),
    "code_changed_after_verification": lambda b: b["codebase"]["files"][1].update(content="BACKDOOR = 1\n"),
    "lockfile_swapped": _set(["lock_text"], "evil==1.0 --hash=sha256:" + "0" * 64 + "\n"),
    "requirement_contradicted": lambda b: b["decision"]["requirements"]["FR1"]["resolution"].update(state="CONTRADICTED"),
    "requirement_unjustified": lambda b: b["decision"]["requirements"]["INV_auth"]["justification"].update(
        justified=False, verdict="WEIGHT_DRIVEN"),
    "release_flag_forged": lambda b: b["decision"].update(release=True, epistemic_verdict="UNJUSTIFIED"),
    "requirements_dropped": lambda b: b["decision"].update(requirements={}),
}


def test_release_evidence_checks_are_meaningful_and_cover_every_defect():
    suite = Suite(strict=True)
    suite.check("evidence_passes", evidence_passes, catches=[
        "acceptance_failed", "acceptance_zero_cases", "acceptance_result_missing", "expected_ids_emptied",
        "static_check_failed", "checks_missing"])
    suite.check("no_release_blockers", no_release_blockers, catches=[
        "finding_reopened", "linked_test_unverified", "acceptance_failed"])
    suite.check("manifest_matches_code_and_lock", manifest_matches_code_and_lock, catches=[
        "code_changed_after_verification", "lockfile_swapped"])
    suite.check("decision_justified", decision_justified, catches=[
        "requirement_contradicted", "requirement_unjustified", "release_flag_forged", "requirements_dropped"])
    base = good_bundle()
    suite.good("released_build", copy.deepcopy(base))
    suite.mutation_suite(base, MUTATIONS)
    result = suite.run()
    assert result.ok, result.text()
    assert not result.uncovered
