"""Markdown report for pull request comments, built only from recorded evidence."""
from __future__ import annotations

from .config import COMPILER_VERSION
from .ledger import MODEL_ASSERTION, OPEN, VERIFIED_BY_TESTS, closure_basis
from .schemas import VerificationResult

MARKER = "<!-- ai-compiler-report -->"

_GATES = [
    ("syntax", "Syntax", "ast.parse"),
    ("secret_scan", "Secret scan", "pattern scan"),
    ("dependency_scan", "Dependency audit", "pip-audit"),
    ("dependency_install", "Dependency install", "pip --require-hashes"),
    ("ruff", "Linting", "Ruff"),
    ("pyright", "Type checking", "Pyright (standard)"),
    ("semgrep", "SAST", "Semgrep"),
    ("service_startup", "Service startup", "uvicorn"),
    ("acceptance_tests", "Acceptance tests", "pytest (isolated oracle)"),
]


def _headline(final: dict) -> str:
    status = final.get("status")
    if status == "released":
        return "RELEASE_READY"
    if status == "budget_exceeded":
        return "REJECTED: repair budget exhausted"
    if status == "abstained":
        return "ABSTAINED: contradictory or ungrounded requirements"
    return f"ERROR: {final.get('error') or status or 'compiler did not finish'}"


def _inline(value: str) -> str:
    """Keep untrusted requirement quotes from adding new report structure."""
    return " ".join(value.replace("`", "'").split())[:500]


def render_summary(final: dict, skipped: list[tuple[str, str]] | None = None, run_url: str | None = None,
                   changed_files: list[str] | None = None) -> str:
    lines = [MARKER, f"## AI Software Compiler: {_headline(final)}", ""]
    manifest = final.get("release_manifest") or {}
    evidence = final.get("verification_result") or {}

    lines += [f"- **Compiler version:** `{COMPILER_VERSION}`"]
    if manifest:
        lines += [
            f"- **Build ID:** `{manifest['build_id']}`",
            f"- **Artifact hash:** `{manifest['artifact_hash']}`",
            f"- **Codebase hash:** `{manifest['codebase_hash']}`",
        ]
    if evidence:
        lines += [
            f"- **Sandbox runtime:** `{evidence.get('runtime')}`",
            f"- **Verifier image:** `{evidence.get('images', {}).get('verifier')}`",
        ]
    lines.append(f"- **Repair rounds used:** {final.get('iteration', 0)}")
    review = final.get("constraint_review") or {}
    if review:
        lines.append(f"- **Constraint review:** `{review.get('status')}` ({len(review.get('claims', []))} encoded claims)")
        for conflict in review.get("conflicts", []):
            lines.append(f"  - Conflict `{_inline(conflict['key'])}`: " +
                         "; ".join(_inline(q) for q in conflict["source_quotes"]))
        for invalid in review.get("invalid_sources", []):
            lines.append(f"  - Ungrounded claim: {_inline(invalid['claim']['source_quote'])}")
    premortem = final.get("premortem_review") or {}
    if premortem:
        lines.append(f"- **Pre-mortem:** `{premortem.get('status')}`; CTD `{premortem.get('ctd_status')}`; "
                     f"{len(premortem.get('hypotheses', []))} checkable hypotheses")
    if run_url:
        lines.append(f"- **Run:** {run_url}")

    checks = evidence.get("checks") or {}
    if checks:
        acceptance = evidence.get("acceptance") or []
        passed = sum(1 for a in acceptance if a.get("passed"))
        lines += ["", "### Gates", "", "| Gate | Tool | Result |", "| :--- | :--- | :--- |"]
        for key, gate, tool in _GATES:
            check = checks.get(key)
            if check is None:
                result = "not run"
            elif check["passed"]:
                result = "PASSED"
            else:
                result = f"FAILED (exit {check.get('exit_code')})"
            if key == "acceptance_tests" and acceptance:
                result += f" ({passed}/{len(acceptance)} acceptance criteria)"
            lines.append(f"| {gate} | {tool} | {result} |")
        note = "" if evidence.get("runtime") == "runsc" else " Warning: not run under gVisor."
        lines.append(f"\nEvidence is from the last verification run.{note}")

    ledger = final.get("findings_ledger") or {}
    if ledger:
        result = VerificationResult(**evidence) if evidence else None
        basis = [closure_basis(f, result) for f in ledger.values()]
        verified, asserted = basis.count(VERIFIED_BY_TESTS), basis.count(MODEL_ASSERTION)
        accepted = sum(1 for f in ledger.values() if not f["closed"] and f["severity"] == "low")
        still_open = basis.count(OPEN) - accepted
        lines += ["", f"**Audit ledger:** {len(ledger)} findings: {verified} closed and verified by passing tests, "
                      f"{asserted} closed on the auditor's judgement only (not independently verified), "
                      f"{accepted} low-severity accepted, {still_open} open."]

    if changed_files:
        lines += ["", "<details><summary>Files changed by the compiler</summary>", ""]
        lines += [f"- `{p}`" for p in changed_files] + ["", "</details>"]

    if final.get("status") != "released" and final.get("validation_feedback"):
        feedback = final["validation_feedback"][:6000].replace("```", "'''")
        lines += ["", f"<details><summary>Last gate failure ({final.get('feedback_source', 'unknown')})</summary>",
                  "", "```", feedback, "```", "", "</details>"]

    if skipped:
        lines += ["", f"<details><summary>{len(skipped)} files not sent to the compiler (left untouched)</summary>", ""]
        lines += [f"- `{p}`: {reason}" for p, reason in skipped[:50]]
        if len(skipped) > 50:
            lines.append(f"- ... and {len(skipped) - 50} more")
        lines += ["", "</details>"]

    if manifest:
        models = ", ".join(f"{k}=`{v}`" for k, v in sorted(manifest["models_used"].items()))
        lines += ["", f"<sub>Models: {models}</sub>"]
    return "\n".join(lines) + "\n"
