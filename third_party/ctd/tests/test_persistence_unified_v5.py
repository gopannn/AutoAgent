from ctd.config import EngineSettings
from ctd.premortem import PreMortemPolicy
from ctd.service import ProductionEngine
from ctd.structural import StructuralCase, StructuralRelation, StructuralTarget
from ctd.transfer import TransferPolicy


def R(pred, *args):
    return StructuralRelation(pred=pred, args=args)


def strict_incident():
    return StructuralCase(
        id="incident:retry",
        domain="distributed",
        metadata={"incident": True},
        severity=5,
        check_templates={"SATURATES": "Load-test {0}."},
        relations=(
            R("DEPENDS", "caller", "backend"),
            R("CAUSES", R("EXCESS", "load"), R("QUEUEING", "backend")),
            R("INCREASES", R("QUEUEING", "backend"), "latency"),
            R("SENSES", "caller", "latency"),
            R("CAUSES", R("SENSES", "caller", "latency"), R("RETRIES", "caller", "backend")),
            R("CAUSES", R("RETRIES", "caller", "backend"), R("AMPLIFIES", "load", "backend")),
            R("CAUSES", R("AMPLIFIES", "load", "backend"), R("SATURATES", "backend")),
        ),
        types={"caller": "agent", "backend": "resource", "load": "load", "latency": "signal"},
    )


def strict_target():
    return StructuralTarget(
        name="gateway",
        domain="software",
        relations=(
            R("DEPENDS", "controller", "gateway"),
            R("CAUSES", R("EXCESS", "requests"), R("QUEUEING", "gateway")),
            R("INCREASES", R("QUEUEING", "gateway"), "target-latency"),
            R("SENSES", "controller", "target-latency"),
        ),
        types={"controller": "agent", "gateway": "resource", "requests": "load", "target-latency": "signal"},
    )


def test_v5_structural_library_and_hypothesis_runs_survive_sqlite_restart(tmp_path):
    path = tmp_path / "unified-v5.db"
    settings = EngineSettings(auth_mode="disabled", persistence_backend="sqlite", sqlite_path=str(path))
    first = ProductionEngine(settings=settings)
    assert first.ingest_structural_case_v5(strict_incident()).ok
    result = first.transfer_v5(strict_target(), policy=TransferPolicy(min_depth=2))
    assert result.hypotheses

    second = ProductionEngine(settings=settings)
    assert [case.id for case in second.list_structural_cases()] == ["incident:retry"]
    replayed = second.transfer_v5(strict_target(), policy=TransferPolicy(min_depth=2))
    assert replayed.hypotheses
    metrics = second.intelligence_metrics()
    assert metrics["transfer"]["runs"] >= 2

    premortem = second.premortem_v5(strict_target(), policy=PreMortemPolicy())
    assert premortem.findings and all(finding.check for finding in premortem.findings)
