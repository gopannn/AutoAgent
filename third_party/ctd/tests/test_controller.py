from ctd.controller import BarrierController, RuntimePolicy
from ctd.telemetry import ResolutionTelemetry


def test_controller_enforces_expansion_and_depth_budgets():
    now = [10.0]
    controller = BarrierController(clock=lambda: now[0])
    policy = RuntimePolicy(max_expansions=2, max_depth=1, deadline_ms=1000)
    telemetry = ResolutionTelemetry()
    state = controller.initialize(policy)

    assert controller.can_expand(state, policy, telemetry, depth=0)
    controller.record_expansion(state, telemetry, depth=0)
    controller.record_expansion(state, telemetry, depth=1)

    assert not controller.can_expand(state, policy, telemetry, depth=0)
    state.expansions = 0
    assert not controller.can_expand(state, policy, telemetry, depth=2)


def test_controller_enforces_deadline():
    now = [20.0]
    controller = BarrierController(clock=lambda: now[0])
    policy = RuntimePolicy(deadline_ms=50)
    telemetry = ResolutionTelemetry()
    state = controller.initialize(policy)

    now[0] = 20.051

    assert not controller.can_expand(state, policy, telemetry, depth=0)
    assert telemetry.deadline_exhausted is True


def test_controller_widens_only_after_stagnation_and_never_past_maximum():
    controller = BarrierController(clock=lambda: 0.0)
    policy = RuntimePolicy(
        initial_arousal=1,
        max_arousal=3,
        widen_step=1,
        stagnation_before_widen=2,
    )
    telemetry = ResolutionTelemetry()
    state = controller.initialize(policy)

    controller.record_progress(state, satisfied_constraints=1)
    controller.record_progress(state, satisfied_constraints=1)
    assert not controller.should_widen(state, policy)

    controller.record_progress(state, satisfied_constraints=1)
    assert controller.should_widen(state, policy)

    assert controller.widen(state, policy, telemetry) == 2
    assert controller.widen(state, policy, telemetry) == 3
    assert controller.widen(state, policy, telemetry) == 3
    assert telemetry.widening_steps == 2
