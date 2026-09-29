"""AST pre-mortem before expensive dependency resolution or sandbox execution.

Deterministic syntax witnesses are repairable defects. CTD projections from an
operator-supplied casebook remain checkable hypotheses and cannot authorize or
block a release without an independent check. No synthetic case is presented as
an observed incident.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

from .hashing import codebase_hash, hash_obj


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_call_name(node.value)}.{node.attr}"
    return ""


def _lock_names(tree: ast.AST) -> set[str]:
    names = set()
    threading_aliases = {"threading"}
    lock_aliases = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            threading_aliases.update(a.asname or a.name for a in node.names if a.name == "threading")
        elif isinstance(node, ast.ImportFrom) and node.module == "threading":
            lock_aliases.update(a.asname or a.name for a in node.names if a.name in {"Lock", "RLock"})
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Call):
            called = _call_name(node.value.func)
            if called in lock_aliases or any(called == f"{alias}.{kind}"
                                                  for alias in threading_aliases for kind in ("Lock", "RLock")):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names.update(t.id for t in targets if isinstance(t, ast.Name))
    return names


def analyze_ast(codebase: dict) -> tuple[list[dict], list[dict]]:
    """Return witnessed defects and a typed inventory for structural transfer."""
    defects: list[dict] = []
    inventory: list[dict] = []
    for file in codebase.get("files", []):
        path = file["path"]
        if not path.endswith(".py"):
            continue
        try:
            tree = ast.parse(file["content"], filename=path)
        except SyntaxError as exc:
            defects.append({"kind": "syntax", "path": path, "line": exc.lineno,
                            "check": "Parse the Python file before rerunning verification."})
            continue
        locks = _lock_names(tree)
        order: dict[tuple[str, str], int] = {}

        class Visitor(ast.NodeVisitor):
            def __init__(self):
                self.held: list[str] = []
                self.async_depth = 0

            def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
                prior = self.async_depth
                self.async_depth += 1
                self.generic_visit(node)
                self.async_depth = prior

            def visit_FunctionDef(self, node: ast.FunctionDef):
                prior = self.async_depth
                self.async_depth = 0
                self.generic_visit(node)
                self.async_depth = prior

            def visit_Call(self, node: ast.Call):
                if self.async_depth and _call_name(node.func) == "time.sleep":
                    defects.append({"kind": "blocking_sleep_in_async", "path": path, "line": node.lineno,
                                    "check": "Replace the blocking sleep with an awaited nonblocking delay."})
                self.generic_visit(node)

            def visit_With(self, node: ast.With):
                for item in node.items:
                    self.visit(item.context_expr)
                acquired = [_call_name(item.context_expr) for item in node.items]
                acquired = [name for name in acquired if name in locks]
                old = list(self.held)
                for name in acquired:
                    for held in self.held:
                        if held != name:
                            order[(held, name)] = node.lineno
                    self.held.append(name)
                for child in node.body:
                    self.visit(child)
                self.held = old

        Visitor().visit(tree)
        for (first, second), line in sorted(order.items()):
            if first < second and (second, first) in order:
                defects.append({"kind": "inverted_lock_order", "path": path,
                                "line": line, "other_line": order[(second, first)],
                                "check": "Use one acquisition order for both locks and test concurrent callers."})
        for (first, second), line in sorted(order.items()):
            inventory.append({"path": path, "relation": "DEPENDS", "subject": second,
                              "object": first, "line": line, "basis": "ast_observation"})
    return defects, inventory


def analyze(codebase: dict, casebook_path: Path | None = None) -> dict:
    defects, inventory = analyze_ast(codebase)
    report = {"status": "repair_required" if defects else "passed", "codebase_hash": codebase_hash(codebase),
              "defects": defects, "structural_inventory": inventory, "hypotheses": [],
              "ctd_status": "no_operator_casebook", "casebook_hash": None,
              "ctd_limitation": "AST relations alone do not establish causal mechanisms or incident provenance"}
    if casebook_path is None:
        return report

    # The casebook is operator-owned, never selected by generated code or the model.
    if casebook_path.stat().st_size > 5_000_000:
        raise ValueError("CTD casebook exceeds 5 MB")
    payload = json.loads(casebook_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not payload:
        raise ValueError("CTD casebook must be a nonempty list of sourced cases")
    from ctd.premortem import PreMortemEngine, PreMortemPolicy
    from ctd.structural import StructuralCase, StructuralRelation, StructuralTarget
    from ctd.transfer import TransferPolicy

    cases = [StructuralCase.model_validate(item) for item in payload]
    if any(not c.source_refs or not c.check_templates or not c.metadata.get("incident") for c in cases):
        raise ValueError("each case needs incident provenance, a source reference and a check template")
    relations = tuple(StructuralRelation(pred="DEPENDS", args=(item["subject"], item["object"]))
                      for item in inventory)
    types = {name: "RESOURCE" for item in inventory for name in (item["subject"], item["object"])}
    target = StructuralTarget(name=report["codebase_hash"], domain="python_service", relations=relations, types=types)
    result = PreMortemEngine().analyze(cases, target, PreMortemPolicy(
        require_check=True,
        transfer=TransferPolicy(min_depth=1, min_systematicity=0, cross_domain_only=False,
                                strict_schema=True, guard_projection=True),
    ))
    report["hypotheses"] = [f.model_dump(mode="json") for f in result.findings]
    report["ctd_status"] = ("evaluated" if result.findings else
                            "insufficient_structural_encoding" if inventory else "no_structural_targets")
    report["casebook_hash"] = hash_obj(payload)
    report["ctd_metrics"] = result.transfer_metrics
    report["ctd_uncheckable_hypotheses"] = result.uncheckable_hypotheses
    return report
