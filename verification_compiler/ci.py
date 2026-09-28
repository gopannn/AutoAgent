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
from typing import Callable  # noqa: E402

from .repo_io import ProjectError, load_codebase, write_back  # noqa: E402
from .report import render_summary  # noqa: E402

MANIFEST_DIR = ".verification"
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


def _default_invoke(requirements: str, codebase: dict, thread_id: str) -> dict:
    from .config import CompilerConfig
    from .dependencies import DependencyResolver
    from .graph import build_graph
    from .nodes import CompilerNodes, default_llm_factory
    from .sandbox import DockerSandbox

    cfg = CompilerConfig.from_env()
    nodes = CompilerNodes(cfg, default_llm_factory(cfg.models), DockerSandbox(cfg.sandbox), DependencyResolver(cfg.sandbox))
    run_config = {"configurable": {"thread_id": thread_id}, "recursion_limit": cfg.recursion_limit()}
    db_uri = os.environ.get("LANGGRAPH_POSTGRES_URI")
    if db_uri:
        from langgraph.checkpoint.postgres import PostgresSaver

        with PostgresSaver.from_conn_string(db_uri) as saver:
            saver.setup()
            return build_graph(cfg, nodes, checkpointer=saver).invoke(
                {"requirements": requirements, "codebase": codebase}, config=run_config
            )
    from langgraph.checkpoint.memory import InMemorySaver

    return build_graph(cfg, nodes, checkpointer=InMemorySaver()).invoke(
        {"requirements": requirements, "codebase": codebase}, config=run_config
    )


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
    invoke: Callable[[str, dict, str], dict] = _default_invoke,
) -> int:
    report_dir.mkdir(parents=True, exist_ok=True)
    summary_path = report_dir / "summary.md"
    project = (repo_root / project_root).resolve()
    if not project.is_relative_to(repo_root.resolve()):
        summary_path.write_text(render_summary({"status": "execution_failed", "error": "project root escapes repository"}))
        return 2

    try:
        loaded = load_codebase(project, entrypoint)
    except ProjectError as err:
        summary_path.write_text(render_summary({"status": "execution_failed", "error": str(err)}))
        log.error("%s", err)
        return 2

    try:
        final = invoke(pr_requirements(title, body), loaded.codebase, thread_id)
    except Exception as err:  # noqa: BLE001 - a crash is a failed gate, never a release
        log.exception("compiler crashed")
        final = {"status": "execution_failed", "error": f"compiler crashed: {err!r}"}

    manifest = final.get("release_manifest") or {}
    released = final.get("status") == "released" and manifest.get("status") == "release_ready"
    changed: list[str] = []
    if released:
        try:
            changed = write_back(project, loaded.codebase, final["codebase"])
        except ProjectError as err:
            final = {**final, "status": "execution_failed", "error": str(err)}
            released = False

    manifest_updated = False
    if released:
        manifest_updated = _store_manifest(repo_root / MANIFEST_DIR, manifest, final["lockfile"]["text"])
        (report_dir / "release_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))

    summary_path.write_text(render_summary(final, loaded.skipped, run_url, changed), encoding="utf-8")
    _set_output("manifest_updated", str(manifest_updated).lower())
    _set_output("changed_files", str(len(changed)))
    if released:
        return 0
    return 1 if final.get("status") == "budget_exceeded" else 2


def _store_manifest(directory: Path, manifest: dict, lock_text: str) -> bool:
    """Writes manifest + lockfile unless an existing manifest already attests the same code and lock."""
    directory.mkdir(exist_ok=True)
    path = directory / "release_manifest.json"
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
        if (current.get("codebase_hash"), current.get("lockfile_hash")) == (
            manifest["codebase_hash"], manifest["lockfile_hash"],
        ):
            return False
    except (OSError, ValueError):
        pass
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (directory / "requirements.lock").write_text(lock_text, encoding="utf-8")
    (directory / "release_manifest.sigstore.json").unlink(missing_ok=True)   # stale signature
    return True


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
