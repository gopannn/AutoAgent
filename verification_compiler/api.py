"""Library entrypoint shared by CI and the AutoAgent tool.

    result = compile_project(Path("services/api"), "app.main:app", "Add tenant rate limiting")

Loads the project (skipping files the compiler must not see or touch), runs the
compiler in repair mode, and, only if the build is release-ready, writes the
verified diff back plus `.verification/{release_manifest.json,requirements.lock}`.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .repo_io import ProjectError, load_codebase, write_back

MANIFEST_DIR = ".verification"
Invoke = Callable[[str, dict, str], dict]

log = logging.getLogger("verification_compiler")


@dataclass
class CompileResult:
    thread_id: str
    status: str                     # released | budget_exceeded | abstained | execution_failed
    final: dict
    changed_files: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    manifest_updated: bool = False

    @property
    def released(self) -> bool:
        return self.status == "released"

    @property
    def manifest(self) -> dict:
        return self.final.get("release_manifest") or {}

    @property
    def exit_code(self) -> int:
        return 0 if self.released else 1 if self.status in {"budget_exceeded", "abstained"} else 2


def default_invoke(requirements: str, codebase: dict, thread_id: str) -> dict:
    """Runs the real graph: configuration from the environment, Postgres if configured, else in memory."""
    os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
    from .config import CompilerConfig
    from .dependencies import DependencyResolver
    from .graph import build_graph
    from .nodes import CompilerNodes, default_llm_factory
    from .sandbox import DockerSandbox

    cfg = CompilerConfig.from_env()
    nodes = CompilerNodes(cfg, default_llm_factory(cfg.models), DockerSandbox(cfg.sandbox), DependencyResolver(cfg.sandbox))
    run_config = {"configurable": {"thread_id": thread_id}, "recursion_limit": cfg.recursion_limit()}
    inputs = {"requirements": requirements, "codebase": codebase}
    db_uri = os.environ.get("LANGGRAPH_POSTGRES_URI")
    if db_uri:
        from langgraph.checkpoint.postgres import PostgresSaver

        with PostgresSaver.from_conn_string(db_uri) as saver:
            saver.setup()
            return build_graph(cfg, nodes, checkpointer=saver).invoke(inputs, config=run_config)
    from langgraph.checkpoint.memory import InMemorySaver

    return build_graph(cfg, nodes, checkpointer=InMemorySaver()).invoke(inputs, config=run_config)


def compile_project(
    project: Path,
    entrypoint: str,
    requirements: str,
    *,
    manifest_root: Path | None = None,
    thread_id: str | None = None,
    invoke: Invoke = default_invoke,
) -> CompileResult:
    """Never raises for compiler outcomes; failures are reported in the result."""
    thread_id = thread_id or f"VC-{uuid.uuid4().hex[:12]}"
    manifest_root = manifest_root or project
    try:
        loaded = load_codebase(project, entrypoint)
    except ProjectError as err:
        return CompileResult(thread_id, "execution_failed", {"status": "execution_failed", "error": str(err)})

    try:
        final = invoke(requirements, loaded.codebase, thread_id)
    except Exception as err:  # noqa: BLE001 - a crash is a failed gate, never a release
        log.exception("compiler crashed")
        final = {"status": "execution_failed", "error": f"compiler crashed: {err!r}"}

    manifest = final.get("release_manifest") or {}
    released = final.get("status") == "released" and manifest.get("status") == "release_ready"
    result = CompileResult(thread_id, "released" if released else final.get("status") or "execution_failed",
                           final, skipped=loaded.skipped)
    if released:
        try:
            result.changed_files = write_back(project, loaded.codebase, final["codebase"])
        except ProjectError as err:
            result.final = {**final, "status": "execution_failed", "error": str(err)}
            result.status = "execution_failed"
            return result
        result.manifest_updated = store_manifest(manifest_root / MANIFEST_DIR, manifest, final["lockfile"]["text"])
    return result


def store_manifest(directory: Path, manifest: dict, lock_text: str) -> bool:
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
