"""Load an on-disk project into a codebase and write verified changes back.

Used by CI (pull request repair) and by the deployment gate, which must hash a
project exactly as the compiler saw it. Files the compiler must not see or
touch (dotfiles, reserved tooling files, binaries, oversized files, symlinks)
are skipped and left on disk untouched.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .config import Limits
from .policy import PolicyViolation, validate_repo_path
from .static_checks import secret_scan

REQUIREMENTS_FILE = "requirements.txt"
EXCLUDED_DIRS = frozenset({
    ".git", "__pycache__", "node_modules", "venv", ".venv", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".tox", "dist", "build",
})


class ProjectError(ValueError):
    """The project cannot be handed to the compiler as-is."""


@dataclass
class LoadedProject:
    codebase: dict
    skipped: list[tuple[str, str]] = field(default_factory=list)   # (path, reason)


def read_requirements(root: Path) -> list[str]:
    path = root / REQUIREMENTS_FILE
    if not path.is_file():
        return []
    deps = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("-"):
            raise ProjectError(f"{REQUIREMENTS_FILE}: option lines are not supported ({line!r}); use exact pins only")
        deps.append(line)
    return deps


def load_codebase(root: Path, entrypoint: str, limits: Limits | None = None, *, scan_secrets: bool = True) -> LoadedProject:
    limits = limits or Limits()
    root = root.resolve()
    if not root.is_dir():
        raise ProjectError(f"project root {root} does not exist")
    files: list[dict] = []
    skipped: list[tuple[str, str]] = []

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED_DIRS)
        for name in sorted(filenames):
            path = Path(dirpath, name)
            rel = path.relative_to(root).as_posix()
            if rel == REQUIREMENTS_FILE:
                continue                                     # represented by `dependencies`
            if path.is_symlink():
                skipped.append((rel, "symlink"))
                continue
            try:
                validate_repo_path(rel, limits.max_path_length)
            except PolicyViolation as err:
                skipped.append((rel, str(err)))
                continue
            if path.stat().st_size > limits.max_file_bytes:
                skipped.append((rel, "exceeds per-file size limit"))
                continue
            try:
                content = path.read_bytes().decode("utf-8")
            except UnicodeDecodeError:
                skipped.append((rel, "not UTF-8 text"))
                continue
            files.append({"path": rel, "content": content})

    if scan_secrets:
        scan = secret_scan(files)
        if not scan.passed:
            raise ProjectError(
                "refusing to send files that may contain secrets to model providers:\n" + scan.detail
            )

    codebase = {
        "files": files,
        "entrypoint": entrypoint,
        "project_type": "python",
        "dependencies": read_requirements(root),
    }
    return LoadedProject(codebase=codebase, skipped=skipped)


def write_back(root: Path, original: dict, final: dict) -> list[str]:
    """Applies the difference between two codebases to disk. Returns changed paths (relative to root)."""
    root = root.resolve()
    before = {f["path"]: f["content"] for f in original["files"]}
    after = {f["path"]: f["content"] for f in final["files"]}
    changed: list[str] = []

    for rel, content in sorted(after.items()):
        if before.get(rel) == content:
            continue
        target = _safe_target(root, rel)
        if rel not in before and (target.exists() or target.is_symlink()):
            # A file the compiler never saw (binary, oversized, reserved). Never overwrite it blind.
            raise ProjectError(f"refusing to overwrite {rel}: it was not part of the verified codebase")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        changed.append(rel)

    for rel in sorted(set(before) - set(after)):
        target = _safe_target(root, rel)
        if target.is_file() and not target.is_symlink():
            target.unlink()
            changed.append(rel)

    if sorted(original.get("dependencies", [])) != sorted(final.get("dependencies", [])):
        (root / REQUIREMENTS_FILE).write_text("\n".join(sorted(final["dependencies"])) + "\n", encoding="utf-8")
        changed.append(REQUIREMENTS_FILE)
    return changed


def _safe_target(root: Path, rel: str) -> Path:
    validate_repo_path(rel)
    target = root / rel
    if not target.parent.resolve().is_relative_to(root) or target.is_symlink():
        raise ProjectError(f"refusing to write through a symlink or outside the project: {rel}")
    return target
