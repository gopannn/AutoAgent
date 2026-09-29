from ctd.auth import Principal
from ctd.config import EngineSettings
from ctd.persistence import SQLiteStore
from ctd.service import ProductionEngine
from ctd.structural import StructuralCase, StructuralRelation, StructuralTarget
from ctd.transfer import TransferPolicy


def R(pred, *args):
    return StructuralRelation(pred=pred, args=args)


def principal(tenant):
    return Principal(subject=f"u-{tenant}", tenant_id=tenant, roles={"admin"}, scopes={"*"}, security_labels={"*"})


def case(case_id, tenant, domain="distributed", incident=False):
    return StructuralCase(
        id=case_id,
        tenant_id=tenant,
        security_label="INTERNAL",
        domain=domain,
        relations=(R("CAUSES", R("RETRIES", "client", "service"), R("INCREASES", "load", "pressure")), R("SATURATES", "service", "pressure")),
        types={"client": "actor", "service": "resource", "load": "load", "pressure": "state"},
        severity=5 if incident else None,
        metadata={"incident": incident},
        check_templates={"SATURATES": "Load-test {0} against {1}."} if incident else {},
    )


def target(tenant="t1"):
    return StructuralTarget(
        name="gateway",
        tenant_id=tenant,
        domain="software",
        relations=(R("CAUSES", R("REDUCES", "controller", "queue"), R("INCREASES", "demand", "target_pressure")),),
        types={"controller": "actor", "queue": "resource", "demand": "load", "target_pressure": "state"},
    )


def test_production_engine_ingests_filters_transfers_and_persists_runs():
    store = SQLiteStore(":memory:")
    engine = ProductionEngine(settings=EngineSettings(auth_mode="disabled"), store=store)
    p1, p2 = principal("t1"), principal("t2")
    engine.ingest_structural_case(case("c1", "t1"), principal=p1)
    engine.ingest_structural_case(case("c2", "t2"), principal=p2)

    visible = engine.list_structural_cases(principal=p1)
    result = engine.transfer(target(), policy=TransferPolicy(min_depth=2), principal=p1)

    assert [item.id for item in visible] == ["c1"]
    assert result.hypotheses
    assert all("c2" not in h.source_cases for h in result.hypotheses)
    assert store.hypothesis_runs.list(tenant_id="t1")


def test_production_engine_premortem_and_intelligence_metrics():
    store = SQLiteStore(":memory:")
    engine = ProductionEngine(settings=EngineSettings(auth_mode="disabled"), store=store)
    p = principal("t1")
    engine.ingest_structural_case(case("incident", "t1", incident=True), principal=p)

    result = engine.premortem(target(), principal=p)
    metrics = engine.intelligence_metrics(principal=p)

    assert result.findings and result.findings[0].check
    assert metrics["encoding"]["accepted"] >= 1
    assert metrics["premortem"]["runs"] >= 1
