"""Decision gate: CTD resolution per requirement and epistemic justification."""
from datetime import datetime, timezone

import pytest

pytest.importorskip("ctd")

from verification_compiler.reasoning.decision import decide, resolve  # noqa: E402
from verification_compiler.schemas import AcceptanceResult  # noqa: E402

from . import fakes  # noqa: E402

NOW = datetime(2026, 9, 29, tzinfo=timezone.utc)
STRONG = {"not_found": False, "server_error": False, "empty_ok": False}
WEAK = {"not_found": False, "server_error": False, "empty_ok": True}
APPROVED = {"is_valid": True, "feedback": "ok", "unmet_requirements": []}


def obs(i, claim, ct=(1.0, 1.0), kind="acceptance_test", source=None):
    return {"id": f"{kind}:{i}", "kind": kind, "claim": claim, "discriminating": ct == (1.0, 1.0),
            "source": source or f"oracle:{i}", "confidence_trust": ct}


@pytest.mark.parametrize("label,observations,state", [
    ("passing test", [obs("a", True)], "RESOLVED"),
    ("failing test only (was fail-open)", [obs("a", False)], "UNRESOLVABLE"),
    ("pass and fail (was hidden by a filtered edge)", [obs("a", True), obs("b", False)], "CONTRADICTED"),
    ("pass and model objection", [obs("a", True), obs("f", False, (0.9, 0.75), "audit_finding", "llm:auditor")],
     "CONTRADICTED"),
    ("non-discriminating only", [obs("a", True, (0.3, 1.0))], "PARTIAL"),
    ("no evidence", [], "UNRESOLVABLE"),
])
def test_ctd_resolution_states(label, observations, state):
    assert resolve("FR1", observations, NOW)["state"] == state, label


def setup(passed=True, calibration=STRONG):
    spec = fakes.spec()
    spec["acceptance_tests"][0].update(requirement_ids=["FR1"], invariant_ids=["INV_auth"])
    result = fakes.passing_result(fakes.codebase(), spec, {"sha256": "x"})
    if not passed:
        result.acceptance = [AcceptanceResult(id="AT_health", passed=False, cases=1, failures=["x"])]
    return spec, {"AT_health": calibration}, result


def test_release_needs_resolution_and_robust_justification():
    spec, cal, result = setup()
    d = decide(fakes.CONTRACT, spec, cal, result, {}, {"high"}, APPROVED, NOW)
    assert (d["ctd_outcome"], d["epistemic_verdict"], d["release"]) == ("RESOLVED", "JUSTIFIED", True)
    fr1 = d["requirements"]["FR1"]
    assert fr1["resolution"]["evidence_ids"] == ["ev:test:AT_health"]
    assert fr1["justification"]["band"] and fr1["justification"]["verdict"] == "ROBUST"


def test_single_observation_without_corroboration_is_not_justified():
    spec, cal, result = setup()
    d = decide(fakes.CONTRACT, spec, cal, result, {}, {"high"}, None, NOW)
    assert d["ctd_outcome"] == "RESOLVED" and d["epistemic_verdict"] == "UNJUSTIFIED" and not d["release"]
    assert d["requirements"]["FR1"]["justification"]["what_would_settle_it"]


def test_non_discriminating_evidence_cannot_release():
    spec, cal, result = setup(calibration=WEAK)
    d = decide(fakes.CONTRACT, spec, cal, result, {}, {"high"}, APPROVED, NOW)
    assert d["ctd_outcome"] == "PARTIAL" and not d["release"]


def test_model_objections_contradict_but_never_support():
    spec, cal, result = setup()
    unmet = {"is_valid": True, "unmet_requirements": ["FR1: health returns the wrong body"]}
    assert decide(fakes.CONTRACT, spec, cal, result, {}, {"high"}, unmet, NOW)["ctd_outcome"] == "CONTRADICTED"
    finding = {"FND-1": {"fingerprint": "FND-1", "severity": "high", "closed": False,
                         "invariant_ids": ["INV_auth"], "acceptance_test_ids": []}}
    d = decide(fakes.CONTRACT, spec, cal, result, finding, {"high"}, APPROVED, NOW)
    assert d["requirements"]["INV_auth"]["resolution"]["state"] == "CONTRADICTED" and not d["release"]


def test_failing_test_blocks_release():
    spec, cal, result = setup(passed=False)
    d = decide(fakes.CONTRACT, spec, cal, result, {}, {"high"}, APPROVED, NOW)
    assert d["ctd_outcome"] == "UNRESOLVABLE" and not d["release"]
