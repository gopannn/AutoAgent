"""Fail-closed tool capability boundary for the governed compiler agent runtime.

The operator enables this mode for a dedicated process. Other AutoAgent modes retain
their original behaviour; they must not be represented as governed executions.
"""
from __future__ import annotations

import os
import importlib

_APPROVED = {
    ("autoagent.tools.compiler_tool", "compile_and_verify"),
    ("autoagent.tools.ctd_tool", "resolve_with_evidence"),
    ("autoagent.tools.inner", "case_resolved"),
    ("autoagent.tools.inner", "case_not_resolved"),
}


def governed() -> bool:
    return os.environ.get("AUTOAGENT_GOVERNED_MODE") == "1"


def require_approved_tools(functions) -> None:
    if not governed():
        return
    denied = []
    for function in functions:
        key = (function.__module__, function.__name__)
        if key not in _APPROVED or getattr(importlib.import_module(key[0]), key[1], None) is not function:
            denied.append(f"{key[0]}.{key[1]}")
    if denied:
        raise PermissionError("governed mode denies agent tools: " + ", ".join(denied))


def require_legacy_shell_disabled() -> None:
    if governed():
        raise PermissionError("legacy shell execution is unavailable in governed mode")
