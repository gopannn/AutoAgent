"""Deterministic policy checks on generated code and on the compiled verification spec.

Checks collect every violation instead of stopping at the first, so one repair
round can fix all of them.
"""
from __future__ import annotations

import ast
import re
from pathlib import PurePosixPath

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from .config import Limits

_SEGMENT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
_ENTRYPOINT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*:[A-Za-z_][A-Za-z0-9_]*$")

# Files that change how Python or the tooling behaves when they merely exist on disk.
DENIED_BASENAMES = frozenset({"sitecustomize.py", "usercustomize.py", "conftest.py", "pytest.ini"})
DENIED_SUFFIXES = (".pth",)

# Modules the compiled acceptance tests may import. Tests are black-box: project code is
# not even mounted in the oracle container, so anything else is a spec defect.
SPEC_IMPORT_ALLOWLIST = frozenset({
    "pytest", "httpx", "jwt", "os", "json", "time", "uuid", "base64", "hashlib", "hmac", "re",
    "datetime", "typing", "secrets", "string", "random", "itertools", "functools", "collections",
    "dataclasses", "math", "urllib", "concurrent", "threading", "__future__",
})


class PolicyViolation(ValueError):
    pass


def validate_repo_path(raw: str, max_length: int = 255) -> PurePosixPath:
    """Validates a repository-relative POSIX path on its raw text, before any normalisation."""
    if not raw or "\x00" in raw or "\\" in raw:
        raise PolicyViolation("invalid path characters")
    if len(raw) > max_length:
        raise PolicyViolation("path too long")
    if raw.startswith("/"):
        raise PolicyViolation("absolute paths forbidden")
    parts = raw.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise PolicyViolation("empty, '.' or '..' path segments forbidden")
    for part in parts:
        if part.startswith("."):
            raise PolicyViolation("hidden files and directories forbidden")
        if not _SEGMENT.match(part):
            raise PolicyViolation(f"segment '{part}' has characters outside [A-Za-z0-9_.-]")
    name = parts[-1].lower()
    if name in DENIED_BASENAMES or name.endswith(DENIED_SUFFIXES):
        raise PolicyViolation(f"'{parts[-1]}' is a reserved tooling/startup file")
    return PurePosixPath(raw)


def parse_pinned_dependency(dep: str) -> tuple[str, str]:
    """Returns (canonical name, exact version) or raises PolicyViolation."""
    try:
        req = Requirement(dep)
    except InvalidRequirement as err:
        raise PolicyViolation(f"unparseable requirement '{dep}': {err}") from err
    if req.url:
        raise PolicyViolation(f"'{dep}': direct URL references forbidden")
    if req.marker is not None:
        raise PolicyViolation(f"'{dep}': environment markers forbidden")
    specs = list(req.specifier)
    if len(specs) != 1 or specs[0].operator != "==" or "*" in specs[0].version:
        raise PolicyViolation(f"'{dep}': must be pinned with exactly one '==<version>' and no wildcard")
    try:
        Version(specs[0].version)
    except InvalidVersion as err:
        raise PolicyViolation(f"'{dep}': invalid version") from err
    return canonicalize_name(req.name), specs[0].version


def entrypoint_module_candidates(entrypoint: str) -> list[str]:
    module = entrypoint.split(":", 1)[0].replace(".", "/")
    return [f"{module}.py", f"{module}/__init__.py"]


def evaluate_codebase(codebase: dict, limits: Limits) -> list[str]:
    violations: list[str] = []
    files = codebase.get("files", [])

    if codebase.get("project_type") != "python":
        violations.append("only project_type 'python' (ASGI service) is supported by the verifier")
    if not files:
        violations.append("codebase contains no files")
    if len(files) > limits.max_files:
        violations.append(f"file count {len(files)} exceeds limit {limits.max_files}")

    total = 0
    seen: set[str] = set()
    for f in files:
        path = f["path"]
        try:
            validate_repo_path(path, limits.max_path_length)
        except PolicyViolation as err:
            violations.append(f"{path!r}: {err}")
            continue
        folded = path.casefold()
        if folded in seen:
            violations.append(f"{path!r}: duplicate path (case-insensitive)")
        seen.add(folded)
        size = len(f["content"].encode("utf-8"))
        if size > limits.max_file_bytes:
            violations.append(f"{path!r}: {size} bytes exceeds per-file limit {limits.max_file_bytes}")
        total += size
    if total > limits.max_repo_bytes:
        violations.append(f"repository size {total} exceeds limit {limits.max_repo_bytes}")

    # A file and a directory with the same name cannot both be materialised.
    dirs = {"/".join(p.split("/")[:i]).casefold() for p in seen for i in range(1, p.count("/") + 1)}
    for clash in sorted(seen & dirs):
        violations.append(f"{clash!r}: used both as a file and as a directory")

    entrypoint = codebase.get("entrypoint", "")
    if not _ENTRYPOINT.match(entrypoint):
        violations.append(f"entrypoint {entrypoint!r} must look like 'package.module:app'")
    elif not any(c.casefold() in seen for c in entrypoint_module_candidates(entrypoint)):
        violations.append(f"entrypoint module for {entrypoint!r} not found in files")

    deps = codebase.get("dependencies", [])
    if len(deps) > limits.max_dependencies:
        violations.append(f"{len(deps)} dependencies exceed limit {limits.max_dependencies}")
    names: set[str] = set()
    for dep in deps:
        try:
            name, _ = parse_pinned_dependency(dep)
        except PolicyViolation as err:
            violations.append(str(err))
            continue
        if name in names:
            violations.append(f"duplicate dependency '{name}'")
        names.add(name)
    if "uvicorn" not in names:
        violations.append("dependencies must pin 'uvicorn' (the service is launched with 'python -m uvicorn')")

    return violations


def evaluate_spec(spec: dict) -> list[str]:
    """Structural validation of a freshly compiled verification spec."""
    problems: list[str] = []
    tests = spec.get("acceptance_tests", [])
    invariants = spec.get("security_invariants", [])
    if not tests:
        problems.append("spec must contain at least one acceptance test")

    inv_ids = [i["id"] for i in invariants]
    if len({i.casefold() for i in inv_ids}) != len(inv_ids):
        problems.append("security invariant ids must be unique (case-insensitive)")

    test_ids = [t["id"] for t in tests]
    if len({t.casefold() for t in test_ids}) != len(test_ids):
        problems.append("acceptance test ids must be unique (case-insensitive)")

    for t in tests:
        tid = t["id"]
        unknown = set(t.get("invariant_ids", [])) - set(inv_ids)
        if unknown:
            problems.append(f"{tid}: references unknown invariants {sorted(unknown)}")
        code = t.get("executable_python_code", "")
        try:
            tree = ast.parse(code)
        except SyntaxError as err:
            problems.append(f"{tid}: syntax error at line {err.lineno}: {err.msg}")
            continue
        if not any(
            isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test")
            for n in tree.body
        ):
            problems.append(f"{tid}: defines no top-level test function")
        if "SUT_BASE_URL" not in code:
            problems.append(f"{tid}: must target os.environ['SUT_BASE_URL']")
        for node in ast.walk(tree):
            mods: list[str] = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    problems.append(f"{tid}: relative imports forbidden")
                mods = [node.module or ""]
            for mod in mods:
                if mod.split(".")[0] not in SPEC_IMPORT_ALLOWLIST:
                    problems.append(f"{tid}: import of '{mod}' not allowed (black-box tests only)")
    return problems
