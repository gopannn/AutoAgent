from ctd.controller import BarrierController, RuntimePolicy
from ctd.models import FailureReason
from ctd.telemetry import ResolutionTelemetry


def test_search_stops_before_using_validation_reserve():
    controller = BarrierController(clock=lambda: 0.0)
    policy = RuntimePolicy(max_expansions=10, validation_reserve_ratio=0.2)
    telemetry = ResolutionTelemetry()
    state = controller.initialize(policy)

    for _ in range(8):
        controller.record_expansion(state, telemetry, depth=1)

    assert controller.can_search(state, policy, telemetry, depth=1) is False
    assert controller.can_validate(state, policy, telemetry) is True
    assert telemetry.validation_reserve_entered is True


def test_provider_call_and_byte_budgets_are_hard_bounds():
    controller = BarrierController(clock=lambda: 0.0)
    policy = RuntimePolicy(max_provider_calls=1, max_bytes_read=100)
    telemetry = ResolutionTelemetry()
    state = controller.initialize(policy)

    assert controller.can_call_provider(state, policy, telemetry)
    controller.record_provider_call(state, telemetry, bytes_read=60)

    assert controller.can_call_provider(state, policy, telemetry) is False
    assert FailureReason.PROVIDER_CALL_BUDGET_EXHAUSTED in controller.failure_reasons(telemetry)

    controller.record_bytes(state, telemetry, 50)
    assert state.bytes_read == 110
    assert FailureReason.VALIDATION_RESERVE_EXHAUSTED not in controller.failure_reasons(telemetry)


def test_candidate_budget_is_enforced_and_runtime_snapshot_is_explainable():
    controller = BarrierController(clock=lambda: 10.0)
    policy = RuntimePolicy(max_candidates=2)
    telemetry = ResolutionTelemetry()
    state = controller.initialize(policy)

    assert controller.can_create_candidate(state, policy, telemetry)
    controller.record_candidate(state, telemetry)
    controller.record_candidate(state, telemetry)
    assert not controller.can_create_candidate(state, policy, telemetry)

    snapshot = controller.snapshot(state, policy)
    assert snapshot["candidates"] == 2
    assert snapshot["arousal"] == policy.initial_arousal
