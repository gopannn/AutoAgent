from pathlib import Path
import runpy

from topo.evaluate import guard_probe, run_ablations


def _incident_library():
    path = Path(__file__).parents[1] / "legacy" / "intelligence_engine_v5_reference" / "library.py"
    namespace = runpy.run_path(str(path))
    return namespace["INCIDENTS"]


def test_unified_intelligence_release_gate_beats_random_and_emits_no_schema_nonsense():
    incidents = _incident_library()
    arms = {arm.name: arm for arm in run_ablations(incidents)}
    assert {"full", "frequency control", "random control"} <= set(arms)
    assert arms["full"].violation_rate == 0.0
    assert arms["full"].mrr > arms["random control"].mrr

    guards = {row.arm: row for row in guard_probe(incidents)}
    assert guards["both guards on"].schema_violations == 0
    # The probe must still be capable of detecting the exact class of error the
    # guards are meant to stop; otherwise a zero violation rate proves little.
    assert guards["both guards off"].schema_violations > 0
