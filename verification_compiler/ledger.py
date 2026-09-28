"""Audit findings ledger.

Findings are keyed by a fingerprint the compiler computes, never by an id the
LLM chooses, so repeated audits cannot overwrite or hide each other's findings.
A finding closes only when a later audit of changed code explicitly reports it
resolved; release additionally requires every linked acceptance test to pass.
"""
from __future__ import annotations

from typing import Iterable

from .hashing import hash_obj
from .schemas import AuditFinding, AuditReport, VerificationResult


def fingerprint(finding: AuditFinding | dict) -> str:
    f = finding.model_dump() if isinstance(finding, AuditFinding) else finding
    key = {
        "category": " ".join(f["category"].lower().split()),
        "files": sorted({p.strip() for p in f["affected_files"]}),
        "invariants": sorted(set(f.get("invariant_ids", []))),
    }
    return "FND-" + hash_obj(key)[:16]


def open_findings(ledger: dict[str, dict]) -> list[dict]:
    return [f for f in ledger.values() if not f["closed"]]


def blocking_open_findings(ledger: dict[str, dict], blocking: Iterable[str]) -> list[dict]:
    blocking = set(blocking)
    return [f for f in open_findings(ledger) if f["severity"] in blocking]


def reconcile(
    ledger: dict[str, dict],
    report: AuditReport,
    *,
    audit_round: int,
    codebase_hash: str,
    known_test_ids: set[str],
    known_invariant_ids: set[str],
) -> dict[str, dict]:
    updated = {fp: {**rec, "history": list(rec["history"])} for fp, rec in ledger.items()}
    statuses = {s.fingerprint: s for s in report.open_finding_statuses}

    # Previously open findings: close only on an explicit "resolved" verdict. Omission keeps them open.
    for fp, rec in updated.items():
        verdict = statuses.get(fp)
        if rec["closed"] or verdict is None:
            continue
        if verdict.status == "resolved":
            rec.update(closed=True, closed_round=audit_round, closed_codebase_hash=codebase_hash)
            rec["history"].append({"round": audit_round, "event": "resolved", "note": verdict.justification})
        else:
            rec["history"].append({"round": audit_round, "event": "still_present", "note": verdict.justification})

    for finding in report.new_findings:
        fp = fingerprint(finding)
        data = finding.model_dump()
        data["acceptance_test_ids"] = sorted(set(data["acceptance_test_ids"]) & known_test_ids)
        data["invariant_ids"] = sorted(set(data["invariant_ids"]) & known_invariant_ids)
        rec = updated.get(fp)
        if rec is None:
            updated[fp] = {
                **data,
                "fingerprint": fp,
                "closed": False,
                "opened_round": audit_round,
                "closed_round": None,
                "closed_codebase_hash": None,
                "history": [{"round": audit_round, "event": "opened", "note": data["description"]}],
            }
            continue
        # Re-reported: reopen if it had been closed, and merge links so evidence requirements only grow.
        rec["acceptance_test_ids"] = sorted(set(rec["acceptance_test_ids"]) | set(data["acceptance_test_ids"]))
        if _severity_rank(data["severity"]) > _severity_rank(rec["severity"]):
            rec["severity"] = data["severity"]
        if rec["closed"]:
            rec.update(closed=False, closed_round=None, closed_codebase_hash=None)
            rec["history"].append({"round": audit_round, "event": "reopened", "note": data["description"]})
    return updated


def release_blockers(
    ledger: dict[str, dict], verification: VerificationResult, blocking: Iterable[str]
) -> list[str]:
    blocking = set(blocking)
    passed = {a.id for a in verification.acceptance if a.passed}
    reasons = []
    for rec in ledger.values():
        if rec["severity"] not in blocking:
            continue
        if not rec["closed"]:
            reasons.append(f"{rec['fingerprint']} ({rec['severity']}) is still open")
            continue
        missing = [t for t in rec["acceptance_test_ids"] if t not in passed]
        if missing:
            reasons.append(f"{rec['fingerprint']} closed but linked tests did not pass: {missing}")
    return reasons


def _severity_rank(sev: str) -> int:
    return {"low": 0, "medium": 1, "high": 2, "critical": 3}[sev]
