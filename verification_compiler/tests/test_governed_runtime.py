"""The legacy coding path must not be a write capability in governed mode."""
import importlib.util
from pathlib import Path

import pytest


source = Path(__file__).resolve().parents[2] / "autoagent" / "governance.py"
spec = importlib.util.spec_from_file_location("governance_under_test", source)
governance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(governance)


def execute_command():
    pass


def test_governed_agent_rejects_legacy_tools(monkeypatch):
    monkeypatch.setenv("AUTOAGENT_GOVERNED_MODE", "1")
    with pytest.raises(PermissionError, match="execute_command"):
        governance.require_approved_tools([execute_command])
    with pytest.raises(PermissionError, match="legacy shell"):
        governance.require_legacy_shell_disabled()


def test_legacy_mode_is_explicitly_ungoverned(monkeypatch):
    monkeypatch.delenv("AUTOAGENT_GOVERNED_MODE", raising=False)
    governance.require_approved_tools([execute_command])
