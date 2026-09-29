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
        return f"ABSTAINED: {final.get('abstain_reason', '')[:300]}"
    return f"ERROR: {final.get('error') or status or 'compiler did not finish'}"


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

    decided = final.get("decision") or {}
    if decided.get("requirements"):
        lines += ["", f"### Decision: CTD `{decided['ctd_outcome']}` · epistemic `{decided['epistemic_verdict']}`", "",
                  "| Requirement | CTD resolution | Justification | Evidence |", "| :--- | :--- | :--- | :--- |"]
        for rid, r in decided["requirements"].items():
            j = r["justification"]
            band = f" (band {j['band'][0]:.2f}–{j['band'][1]:.2f})" if j.get("band") else ""
            evidence = ", ".join(f"`{o['id']}`" for o in r["observations"]) or "none"
            lines.append(f"| {rid}: {r['requirement'][:60]} | {r['resolution']['state']} | {j['verdict']}{band} | {evidence} |")
        lines.append("\nBands come from declared evidence weights; they are not probabilities that the code is correct.")

    risks = final.get("structural_risks") or {}
    verdicts = final.get("risk_verdicts") or {}
    all_risks = {r["id"]: r for group in ("pre_code", "code") for r in risks.get(group, [])}
    if all_risks:
        lines += ["", "<details><summary>Structural risk hypotheses (CTD premortem)</summary>", "",
                  "| Risk | Projected from | Check | Auditor verdict |", "| :--- | :--- | :--- | :--- |"]
        for rid, r in all_risks.items():
            verdict = verdicts.get(rid, {}).get("verdict", "not audited")
            lines.append(f"| `{r['relation']}` | {', '.join(r['source_cases'])} | {r['check'][:120]} | {verdict} |")
        lines += ["", "Hypotheses guide the spec and the audit; they are never release evidence.", "", "</details>"]

    check = final.get("requirement_check") or {}
    if check.get("contradictions"):
        lines += ["", "### Contradictory requirements"]
        lines += [f"- {' vs '.join(repr(e) for e in c['excerpts'])}" for c in check["contradictions"]]

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
