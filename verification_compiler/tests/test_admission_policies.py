"""Admission policy checks. The Rego tests need `opa` on PATH and are skipped otherwise."""
import shutil
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"


def test_policies_are_well_formed():
    kyverno = yaml.safe_load((DEPLOY / "kyverno" / "require-verified-release.yaml").read_text())
    rule = kyverno["spec"]["rules"][0]["verifyImages"][0]
    assert rule["failureAction"] == "Enforce" and rule["required"] and rule["mutateDigest"]
    conditions = rule["attestations"][0]["conditions"][0]["all"]
    assert {"key": "{{ status }}", "operator": "Equals", "value": "release_ready"} in conditions

    template, constraint = yaml.safe_load_all((DEPLOY / "gatekeeper" / "ratify-verification.yaml").read_text())
    assert constraint["kind"] == template["spec"]["crd"]["spec"]["names"]["kind"]
    assert constraint["spec"]["enforcementAction"] == "deny"


@pytest.mark.skipif(shutil.which("opa") is None, reason="opa not installed")
def test_gatekeeper_rego_fails_closed(tmp_path):
    template = next(yaml.safe_load_all((DEPLOY / "gatekeeper" / "ratify-verification.yaml").read_text()))
    (tmp_path / "policy.rego").write_text(template["spec"]["targets"][0]["rego"])
    shutil.copy(Path(__file__).parent / "rego" / "ratify_verification_test.rego", tmp_path)
    proc = subprocess.run(["opa", "test", "-v", str(tmp_path)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
