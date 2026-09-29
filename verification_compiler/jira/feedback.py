"""Reports the compiler's verdict back to the Jira ticket that opened the pull request.

    python -m verification_compiler.jira.feedback --head-ref feature/jira-ABC-12-auto-impl \
        --exit-code 0 --report-dir "$RUNNER_TEMP/vc-report" --pr-url https://github.com/o/r/pull/7

Only branches created by the bridge are reported. A release moves the issue to the review status;
anything else only comments, so a person decides what happens next. Missing Jira configuration
is a skip, never a failure: the release gate is the workflow's job, not this step's.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from .clients import Jira, JiraClient

BRANCH = re.compile(r"^feature/jira-([A-Z][A-Z0-9_]+-[1-9][0-9]*)-auto-impl$")
_HEADLINE = re.compile(r"^## AI Software Compiler: (.+)$", re.M)


def issue_key(head_ref: str) -> str | None:
    match = BRANCH.match(head_ref or "")
    return match.group(1) if match else None


def compose(exit_code: str, report_dir: Path, pr_url: str, run_url: str = "") -> str:
    summary = report_dir / "summary.md"
    text = summary.read_text(encoding="utf-8") if summary.is_file() else ""
    match = _HEADLINE.search(text)
    headline = match.group(1).strip() if match else f"no report produced (exit {exit_code or 'missing'})"
    lines = [f"AI Software Compiler: {headline}", f"Pull request: {pr_url}"]
    manifest = report_dir / "release_manifest.json"
    if exit_code == "0" and manifest.is_file():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except ValueError:
            data = {}
        if data.get("artifact_hash"):
            lines.append(f"Artifact hash: {data['artifact_hash']}")
        decided = data.get("decision") or {}
        if decided.get("ctd_outcome") or decided.get("epistemic_verdict"):
            lines.append(f"Decision: CTD {decided.get('ctd_outcome', '?')}, "
                         f"epistemic {decided.get('epistemic_verdict', '?')}")
    if run_url:
        lines.append(f"Run: {run_url}")
    lines.append("Released builds are signed; the pull request still needs human review before merge."
                 if exit_code == "0" else "Not released. See the pull request report for the evidence.")
    return "\n".join(lines)


def report(jira: Jira, key: str, exit_code: str, report_dir: Path, pr_url: str,
           review_status: str, run_url: str = "") -> bool:
    """Comments on the issue; transitions it on release. Returns True if the issue was transitioned."""
    jira.comment(key, compose(exit_code, report_dir, pr_url, run_url))
    return exit_code == "0" and jira.transition(key, review_status)


def main(argv: list[str] | None = None, jira: Jira | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--head-ref", required=True)
    parser.add_argument("--exit-code", default="")
    parser.add_argument("--report-dir", type=Path, required=True)
    parser.add_argument("--pr-url", required=True)
    parser.add_argument("--run-url", default="")
    parser.add_argument("--review-status", default=os.environ.get("JIRA_REVIEW_STATUS") or "In Code Review")
    args = parser.parse_args(argv)

    key = issue_key(args.head_ref)
    if key is None:
        print(f"{args.head_ref!r} was not opened by the Jira bridge; nothing to report")
        return 0
    if jira is None:
        base, email, token = (os.environ.get(n) for n in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"))
        if not (base and email and token):
            print("JIRA_BASE_URL, JIRA_EMAIL or JIRA_API_TOKEN not set; skipping Jira feedback")
            return 0
        jira = JiraClient(base, email, token)
    try:
        moved = report(jira, key, args.exit_code, args.report_dir, args.pr_url, args.review_status, args.run_url)
    except Exception as err:  # noqa: BLE001 - feedback is best-effort; the gate already decided
        print(f"::warning::Jira feedback for {key} failed: {type(err).__name__}: {err}")
        return 0
    print(f"reported to {key}" + (f"; moved to {args.review_status!r}" if moved else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
