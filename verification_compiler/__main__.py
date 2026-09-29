"""CLI: python -m verification_compiler --requirements "..." [--manifest-out release.json]

Exit codes: 0 release ready, 1 rejected or abstained, 2 infrastructure or compiler error.
"""
from __future__ import annotations

import os

# Must be set before langgraph is imported to take effect.
os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")

import argparse  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import sys  # noqa: E402
import uuid  # noqa: E402
from contextlib import nullcontext  # noqa: E402
from pathlib import Path  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    from .config import CompilerConfig
    from .dependencies import DependencyResolver
    from .graph import build_graph
    from .nodes import CompilerNodes, default_llm_factory
    from .sandbox import DockerSandbox

    parser = argparse.ArgumentParser(prog="verification_compiler")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--requirements", help="requirements text")
    src.add_argument("--requirements-file", type=Path)
    parser.add_argument("--thread-id", default=None, help="resume or name a run (checkpoint thread id)")
    parser.add_argument("--manifest-out", type=Path, default=None)
    parser.add_argument("--in-memory", action="store_true", help="use an in-memory checkpointer instead of Postgres")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    requirements = args.requirements or args.requirements_file.read_text(encoding="utf-8")
    try:
        cfg = CompilerConfig.from_env()
    except (RuntimeError, ValueError) as err:
        print(f"[CONFIG] {err}", file=sys.stderr)
        return 2

    nodes = CompilerNodes(
        cfg,
        llm_factory=default_llm_factory(cfg.models),
        sandbox=DockerSandbox(cfg.sandbox),
        resolver=DependencyResolver(cfg.sandbox),
    )
    thread_id = args.thread_id or f"BUILD-{uuid.uuid4().hex}"
    run_config = {"configurable": {"thread_id": thread_id}, "recursion_limit": cfg.recursion_limit()}

    if args.in_memory:
        from langgraph.checkpoint.memory import InMemorySaver

        saver_ctx = nullcontext(InMemorySaver())
    else:
        db_uri = os.environ.get("LANGGRAPH_POSTGRES_URI")
        if not db_uri:
            print("[CONFIG] LANGGRAPH_POSTGRES_URI is not set (or pass --in-memory)", file=sys.stderr)
            return 2
        from langgraph.checkpoint.postgres import PostgresSaver

        saver_ctx = PostgresSaver.from_conn_string(db_uri)

    try:
        with saver_ctx as saver:
            if hasattr(saver, "setup"):
                saver.setup()
            graph = build_graph(cfg, nodes, checkpointer=saver)
            final = graph.invoke({"requirements": requirements}, config=run_config)
    except Exception as err:  # noqa: BLE001 - any unhandled error is a compiler failure, never a release
        logging.exception("compiler crashed")
        print(f"\n[FATAL] Build {thread_id} crashed: {err!r}", file=sys.stderr)
        return 2

    manifest = final.get("release_manifest") or {}
    if final.get("status") == "released" and manifest.get("status") == "release_ready":
        if args.manifest_out:
            args.manifest_out.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        print(f"\n[RELEASE_READY] {manifest['build_id']} (run {thread_id})")
        return 0
    if final.get("status") == "budget_exceeded":
        print(f"\n[REJECTED] Build {thread_id}: repair budget exhausted.\n{final.get('validation_feedback', '')}")
        return 1
    if final.get("status") == "abstained":
        print(f"\n[ABSTAINED] Build {thread_id}: conflicting or ungrounded requirements.\n"
              f"{json.dumps(final.get('constraint_review', {}), indent=2)}")
        return 1
    print(f"\n[FATAL] Build {thread_id} failed: {final.get('error') or final.get('status')}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
