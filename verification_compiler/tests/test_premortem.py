"""Witnessed AST risks block early; CTD predictions never masquerade as proof."""
from __future__ import annotations

import json

import pytest

from verification_compiler.premortem import analyze, analyze_ast

from . import fakes
from .test_graph import run, script, roles


def test_ast_detects_blocking_async_and_inverted_lock_order():
    source = """\
import threading
import time
first = threading.Lock()
second = threading.Lock()

def one():
    with first:
        with second:
            pass

def two():
    with second:
        with first:
            pass

async def work():
    time.sleep(1)
"""
    defects, inventory = analyze_ast(fakes.codebase(app=source))
    assert {d["kind"] for d in defects} == {"blocking_sleep_in_async", "inverted_lock_order"}
    assert all(item["basis"] == "ast_observation" for item in inventory)


def test_premortem_failure_repairs_before_auditor_or_sandbox():
    source = fakes.APP + "\nimport time\nasync def bad():\n    time.sleep(1)\n"
    bad = fakes.codebase(app=source)
    fixed = {"modified_files": [{"path": "service/main.py", "content": fakes.APP}]}
    final, llms, sandbox = run(script(architect=[fakes.CONTRACT, bad], repository_builder=[fixed]))
    assert final["status"] == "released"
    assert final["iteration"] == 1
    assert final["release_manifest"]["premortem_review"]["status"] == "passed"
    assert roles(llms).count("adversarial_auditor") == 1
    assert sandbox.calls == 1


def test_unsourced_casebook_is_refused(tmp_path):
    p = tmp_path / "cases.json"
    p.write_text(json.dumps([{"id": "invented", "domain": "software", "relations": [],
                              "metadata": {"incident": True}, "check_templates": {"FAILS": "check"}}]))
    with pytest.raises(ValueError, match="incident provenance"):
        analyze(fakes.codebase(), p)


def test_no_casebook_is_reported_as_no_ctd_evaluation():
    report = analyze(fakes.codebase())
    assert report["status"] == "passed"
    assert report["ctd_status"] == "no_operator_casebook"
    assert report["hypotheses"] == []


def test_release_refuses_stale_premortem_review():
    from verification_compiler.nodes import CompilerNodes

    final, _, _ = run(script())
    final["premortem_review"]["codebase_hash"] = "0" * 64
    cfg = fakes.config()
    node = CompilerNodes(cfg, fakes.FakeLLMs(script()), fakes.FakeSandbox(), fakes.FakeResolver())
    result = node.release(final, {"configurable": {"thread_id": "test"}})
    assert result["status"] == "execution_failed"
    assert "pre-mortem review" in result["error"]
