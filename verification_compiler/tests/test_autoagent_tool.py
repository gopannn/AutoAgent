"""The AutoAgent tool wrapper. Skipped unless AutoAgent's own dependencies are installed."""
import pytest

try:
    from autoagent.tools import compiler_tool
    from autoagent.util import function_to_json
except Exception as err:  # noqa: BLE001 - AutoAgent pulls in many optional dependencies at import
    pytest.skip(f"autoagent not importable: {err}", allow_module_level=True)

from verification_compiler import api

from . import fakes
from .test_ci import ENTRY, make_project, released_final


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv(compiler_tool.ALLOWED_ROOT_ENV, str(tmp_path))
    make_project(tmp_path)
    return tmp_path


def test_schema_marks_entrypoint_optional():
    params = function_to_json(compiler_tool.compile_and_verify)["function"]["parameters"]
    assert set(params["required"]) == {"requirements", "project_path"}


def test_refuses_paths_outside_allowed_root(workspace):
    assert "must stay inside" in compiler_tool.compile_and_verify("x", "../..", ENTRY)
    assert "not a directory" in compiler_tool.compile_and_verify("x", "missing", ENTRY)


def test_requires_entrypoint(workspace, monkeypatch):
    monkeypatch.delenv(compiler_tool.ENTRYPOINT_ENV, raising=False)
    assert "entrypoint is required" in compiler_tool.compile_and_verify("x", "svc")


def test_release_applies_patch_and_reports(workspace, monkeypatch):
    def invoke(requirements, codebase, thread_id):
        patched = {**codebase, "files": [dict(f) for f in codebase["files"]]}
        patched["files"][1]["content"] += "\n# verified change\n"
        return released_final(patched)

    real = api.compile_project
    monkeypatch.setattr(api, "compile_project", lambda *a, **kw: real(*a, **kw, invoke=invoke))
    out = compiler_tool.compile_and_verify("Add header", "svc", ENTRY)
    assert "exit_code: 0" in out and "RELEASE_READY" in out and "service/main.py" in out
    assert (workspace / "svc" / "service" / "main.py").read_text().endswith("# verified change\n")
    assert (workspace / "svc" / ".verification" / "release_manifest.json").exists()


def test_rejection_leaves_files_untouched(workspace, monkeypatch):
    real = api.compile_project
    monkeypatch.setattr(api, "compile_project", lambda *a, **kw: real(
        *a, **kw, invoke=lambda *x: {"status": "budget_exceeded", "validation_feedback": "semgrep: eval"}))
    out = compiler_tool.compile_and_verify("x", "svc", ENTRY)
    assert "exit_code: 1" in out and "REJECTED" in out
    assert (workspace / "svc" / "service" / "main.py").read_text() == fakes.APP
