"""Stage an image context containing only the source files in a signed release.

The Docker build must consume this context, never the unchecked project directory.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .hashing import codebase_hash, sha256_hex
from .policy import validate_repo_path
from .repo_io import load_codebase
from .verify_gate import GITHUB_OIDC_ISSUER, check


def stage(manifest_dir: Path, project_root: Path, entrypoint: str, output: Path, *,
          identity: str, verify=check) -> None:
    problems = verify(manifest_dir, project_root, entrypoint, identity=identity, issuer=GITHUB_OIDC_ISSUER)
    if problems:
        raise ValueError("release verification failed: " + "; ".join(problems))
    manifest = json.loads((manifest_dir / "release_manifest.json").read_text(encoding="utf-8"))
    loaded = load_codebase(project_root, entrypoint, scan_secrets=False).codebase
    files = loaded["files"]
    lock = (manifest_dir / "requirements.lock").read_bytes()
    if (codebase_hash(loaded) != manifest["codebase_hash"] or
            sorted(f["path"] for f in files) != sorted(manifest["source_files"]) or
            sha256_hex(lock) != manifest["lockfile_hash"]):
        raise ValueError("project or lockfile changed while staging the verified image")
    output.mkdir(parents=True, exist_ok=False)
    app = output / "app"
    app.mkdir()
    for file in files:
        relative = validate_repo_path(file["path"])
        target = app / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(file["content"], encoding="utf-8")
    (output / "requirements.lock").write_bytes(lock)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest-dir", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--entrypoint", required=True)
    parser.add_argument("--identity", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        stage(args.manifest_dir, args.project_root, args.entrypoint, args.output, identity=args.identity)
    except (OSError, ValueError) as err:
        print(f"IMAGE STAGING BLOCKED: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
