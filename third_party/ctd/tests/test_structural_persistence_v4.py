from datetime import datetime, timezone

from ctd.persistence import SQLiteStore
from ctd.structural import StructuralCase, StructuralRelation
from ctd.structural_repository import HypothesisRunRecord, InMemoryStructuralCaseRepository


def case(case_id="c1", tenant="t1", label="internal"):
    return StructuralCase(
        id=case_id,
        domain="ops",
        relations=(StructuralRelation(pred="CAUSES", args=(StructuralRelation(pred="INCREASES", args=("a", "b")), "c")),),
        types={"a": "actor", "b": "state", "c": "outcome"},
        tenant_id=tenant,
        security_label=label,
    )


def test_in_memory_structural_repository_filters_tenant_and_security():
    repo = InMemoryStructuralCaseRepository()
    repo.save(case("a", "t1", "internal"))
    repo.save(case("b", "t2", "internal"))
    repo.save(case("c", "t1", "secret"))

    visible = repo.list(tenant_id="t1", allowed_security_labels={"internal"})

    assert [item.id for item in visible] == ["a"]


def test_sqlite_structural_cases_and_hypothesis_runs_survive_restart(tmp_path):
    path = tmp_path / "ctd-v4.db"
    store = SQLiteStore(path)
    store.structural_cases.save(case())
    run = HypothesisRunRecord(
        run_id="run1",
        tenant_id="t1",
        mode="transfer",
        target_name="gateway",
        result={"hypotheses": 2},
        created_at=datetime(2026, 9, 12, tzinfo=timezone.utc),
    )
    store.hypothesis_runs.save(run)
    store.close()

    reopened = SQLiteStore(path)
    loaded = reopened.structural_cases.get("c1")
    runs = reopened.hypothesis_runs.list(tenant_id="t1")

    assert loaded is not None and loaded.id == "c1"
    assert len(runs) == 1 and runs[0].run_id == "run1"
    assert reopened.structural_cases.list(tenant_id="wrong") == []
    reopened.close()
