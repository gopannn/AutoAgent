"""AutoAgent tool around the verification compiler.

Heavy dependencies (langgraph, model SDKs, Docker) are imported only when the tool
runs, because AutoAgent imports every module in this package at startup.
Install them with: pip install -e ".[compiler]"
"""
import os
from pathlib import Path

from autoagent.registry import register_tool

ENTRYPOINT_ENV = "VC_ENTRYPOINT"
ALLOWED_ROOT_ENV = "VC_ALLOWED_ROOT"   # tool may only compile projects under this directory (default: cwd)


def _resolve_project(project_path: str) -> Path:
    allowed = Path(os.environ.get(ALLOWED_ROOT_ENV) or os.getcwd()).resolve()
    project = (allowed / project_path).resolve()
    if not project.is_relative_to(allowed):
        raise ValueError(f"project_path must stay inside {allowed}")
    if not project.is_dir():
        raise ValueError(f"project_path {project_path!r} is not a directory")
    return project


@register_tool("compile_and_verify")
def compile_and_verify(requirements: str, project_path: str, entrypoint: str = "") -> str:
    """
    Implement or repair a Python ASGI service through the verification compiler, and apply the result only if it is verified.
    The compiler writes hidden acceptance tests from the requirements. It runs audit and repair rounds, and verifies the code in a gVisor sandbox
    (ruff, pyright, semgrep, dependency audit, black-box HTTP tests). It changes files on disk ONLY when the build is release-ready.
    Never edit the project's files yourself; call this tool instead.

    Args:
        requirements: Precise description of the change or feature, including acceptance criteria.
        project_path: Service directory, relative to the allowed root (default: current directory).
        entrypoint: ASGI application as 'package.module:app'. Defaults to the VC_ENTRYPOINT environment variable.
    Returns:
        A markdown report: RELEASE_READY with the changed files and artifact hash, or the gate that failed and why.
    """
    entrypoint = entrypoint or os.environ.get(ENTRYPOINT_ENV, "")
    if not entrypoint:
        return "[ERROR] entrypoint is required (e.g. 'app.main:app'), or set VC_ENTRYPOINT."
    try:
        project = _resolve_project(project_path)
    except ValueError as err:
        return f"[ERROR] {err}"
    try:
        from verification_compiler.api import compile_project
        from verification_compiler.report import render_summary
    except ImportError as err:
        return f"[ERROR] verification compiler dependencies are missing ({err}). Install with: pip install -e \".[compiler]\""

    result = compile_project(project, entrypoint, requirements)
    report = render_summary(result.final, result.skipped, changed_files=result.changed_files)
    return f"thread_id: {result.thread_id}\nexit_code: {result.exit_code}\n\n{report}"
