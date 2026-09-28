from ctd.config import EngineSettings
from ctd.persistence import SQLiteStore
from ctd.service import ProductionEngine
from ctd.structural import StructuralCase, StructuralRelation, StructuralTarget


def R(pred: str, *args):
    return StructuralRelation(pred=pred, args=args)


engine = ProductionEngine(settings=EngineSettings(auth_mode="disabled"), store=SQLiteStore(":memory:"))
incident = StructuralCase(
    id="incident:retry-storm",
    domain="distributed-systems",
    relations=(
        R("CAUSES", R("RETRIES", "client", "service"), R("INCREASES", "load", "pressure")),
        R("SATURATES", "service", "pressure"),
    ),
    types={"client": "actor", "service": "resource", "load": "load", "pressure": "state"},
    metadata={"incident": True},
    severity=5,
    check_templates={"SATURATES": "Inject load into {0} and measure headroom against {1}."},
)
target = StructuralTarget(
    name="ai-gateway",
    domain="software",
    relations=(R("CAUSES", R("REDUCES", "controller", "queue"), R("INCREASES", "demand", "target_pressure")),),
    types={"controller": "actor", "queue": "resource", "demand": "load", "target_pressure": "state"},
)

engine.ingest_structural_case(incident)
print(engine.transfer(target).model_dump_json(indent=2))
print(engine.premortem(target).model_dump_json(indent=2))
