"""Pull request entrypoint used by .github/workflows/ai-compiler.yml.

    python -m verification_compiler.ci --repo-root pr --project-root services/api \
        --entrypoint app.main:app --report-dir "$RUNNER_TEMP/vc-report"

Reads the PR title and body from the environment (PR_TITLE, PR_BODY, PR_NUMBER),
runs the compiler in repair mode on the project, and on release writes the
verified changes, the manifest and the lockfile back into the PR checkout.
Exit codes: 0 release ready, 1 rejected, 2 infrastructure/configuration error.
"""
from __future__ import annotations

import os

os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")

import argparse  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

from .api import Invoke, compile_project, default_invoke  # noqa: E402
from .report import render_summary  # noqa: E402

MAX_PR_TEXT = 20_000

log = logging.getLogger("verification_compiler.ci")


def pr_requirements(title: str, body: str) -> str:
    title = (title or "").strip() or "Automated maintenance"
    body = (body or "").strip() or "Review the service and fix defects without changing its intended behaviour."
    return f"Pull request title: {title}\n\nPull request description:\n{body[:MAX_PR_TEXT]}"


def _set_output(name: str, value: str) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"{name}={value}\n")


def run(
    repo_root: Path,
    project_root: str,
    entrypoint: str,
    report_dir: Path,
    *,
    title: str,
    body: str,
    thread_id: str,
    run_url: str | None = None,
    invoke: Invoke = default_invoke,
) -> int:
    report_dir.mkdir(parents=True, exist_ok=True)
    summary_path = report_dir / "summary.md"
    project = (repo_root / project_root).resolve()
    if not project.is_relative_to(repo_root.resolve()):
        summary_path.write_text(render_summary({"status": "execution_failed", "error": "project root escapes repository"}))
        return 2

    result = compile_project(project, entrypoint, pr_requirements(title, body),
                             manifest_root=repo_root, thread_id=thread_id, invoke=invoke)
    if result.released:
        (report_dir / "release_manifest.json").write_text(json.dumps(result.manifest, indent=2, sort_keys=True))
    summary_path.write_text(render_summary(result.final, result.skipped, run_url, result.changed_files), encoding="utf-8")
    _set_output("manifest_updated", str(result.manifest_updated).lower())
    _set_output("changed_files", str(len(result.changed_files)))
    return result.exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="verification_compiler.ci")
    parser.add_argument("--repo-root", type=Path, required=True, help="checkout of the pull request head")
    parser.add_argument("--project-root", required=True, help="service directory, relative to --repo-root")
    parser.add_argument("--entrypoint", required=True, help="ASGI app, e.g. app.main:app")
    parser.add_argument("--report-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    pr = os.environ.get("PR_NUMBER", "local")
    sha = os.environ.get("HEAD_SHA", "")[:12] or "head"
    run_url = None
    if os.environ.get("GITHUB_RUN_ID"):
        run_url = f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    code = run(
        args.repo_root, args.project_root, args.entrypoint, args.report_dir,
        title=os.environ.get("PR_TITLE", ""), body=os.environ.get("PR_BODY", ""),
        thread_id=f"PR-{pr}-{sha}", run_url=run_url,
    )
    _set_output("exit_code", str(code))
    return code


if __name__ == "__main__":
    sys.exit(main())
