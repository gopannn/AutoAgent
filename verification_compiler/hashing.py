"""Canonical hashing. Every identity in the manifest is derived through these helpers."""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def hash_obj(obj: Any) -> str:
    return sha256_hex(canonical_json(obj))


def codebase_hash(codebase: dict) -> str:
    return hash_obj({
        "project_type": codebase.get("project_type"),
        "entrypoint": codebase.get("entrypoint"),
        "dependencies": sorted(codebase.get("dependencies", [])),
        "files": sorted(
            ({"path": f["path"], "sha256": sha256_hex(f["content"])} for f in codebase.get("files", [])),
            key=lambda f: f["path"],
        ),
    })


def release_artifact_hash(fields: dict) -> str:
    """Identity of the inputs and toolchain the verifier actually used."""
    return hash_obj({
        "compiler_version": fields["compiler_version"],
        "runtime": fields["runtime"],
        "images": fields["images"],
        "toolchain_versions": fields["toolchain_versions"],
        "requirement_contract": fields["requirement_contract_hash"],
        "constraint_review": hash_obj(fields["constraint_review"]),
        "premortem_review": hash_obj(fields["premortem_review"]),
        "verification_spec": fields["verification_spec_hash"],
        "declared_coverage": hash_obj(fields["declared_coverage"]),
        "codebase": fields["codebase_hash"],
        "lockfile": fields["lockfile_hash"],
    })
