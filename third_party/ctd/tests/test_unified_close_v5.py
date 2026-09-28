from ctd.controller import RuntimePolicy
from ctd.unified_close import CloseConstraintSpec, CloseRecordModel, UnifiedCloseRequest, UnifiedCloseOperator


def test_indexed_close_is_tenant_filtered_and_returns_structured_gap():
    records = [
        CloseRecordModel(id="a", domain="supplier", attrs={"tenant_id": "t1", "status": "active", "region": "south"}),
        CloseRecordModel(id="b", domain="supplier", attrs={"tenant_id": "t2", "status": "active", "region": "north"}),
    ]
    request = UnifiedCloseRequest(
        query="active north supplier",
        records=records,
        constraints=[
            CloseConstraintSpec(name="active", kind="has", attr="status", values=["active"]),
            CloseConstraintSpec(name="north", kind="has", attr="region", values=["north"]),
        ],
        policy=RuntimePolicy(tenant_id="t1"),
    )
    result = UnifiedCloseOperator().run(request)
    assert result.outcome == "REQUEST"
    assert result.gap is not None
    assert result.gap.constraint_id == "north"
    assert result.gap.truth == "UNKNOWN"
    assert result.telemetry["authorized_records"] == 1


def test_close_relaxation_is_explicitly_partial_not_closed():
    records = [CloseRecordModel(id="a", domain="supplier", attrs={"tenant_id": "t1", "status": "active", "region": "south"})]
    request = UnifiedCloseRequest(
        query="active north supplier",
        records=records,
        constraints=[
            CloseConstraintSpec(name="active", kind="has", attr="status", values=["active"]),
            CloseConstraintSpec(name="north", kind="has", attr="region", values=["north"]),
        ],
        droppable=["north"],
        max_drops=1,
        policy=RuntimePolicy(tenant_id="t1"),
    )
    result = UnifiedCloseOperator().run(request)
    assert result.outcome == "PARTIAL"
    assert result.ids == ["a"]
    assert result.dropped == ["north"]
