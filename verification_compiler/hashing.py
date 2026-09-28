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
