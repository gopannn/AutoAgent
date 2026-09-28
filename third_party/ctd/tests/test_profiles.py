from ctd.models import ResolutionResult, ResolutionState
from ctd.profiles import QueryProfileManager
from ctd.repositories import InMemoryQueryProfileRepository


def test_profile_records_successful_plan_as_advisory_prior():
    manager = QueryProfileManager(InMemoryQueryProfileRepository())
    result = ResolutionResult(
        state=ResolutionState.RESOLVED,
        constraint_coverage=1.0,
        telemetry={
            "planner_operations": [
                {"constraint_id": "a:code"},
                {"constraint_id": "r:produces"},
            ],
            "edges_traversed": 4,
            "elapsed_ms": 12.0,
            "provider_calls": 3,
            "arousal_history": [1, 2],
        },
    )

    manager.observe("supplier", result)
    profile = manager.get("supplier")

    assert profile["successful_runs"] == 1
    assert profile["preferred_order"] == ["a:code", "r:produces"]
    assert profile["mean_expansions"] == 4.0


def test_profile_tracks_failures_without_mutating_policy():
    manager = QueryProfileManager(InMemoryQueryProfileRepository())
    result = ResolutionResult(
        state=ResolutionState.BUDGET_EXHAUSTED,
        constraint_coverage=0.5,
        telemetry={"edges_traversed": 10, "elapsed_ms": 20.0},
    )

    manager.observe("supplier", result)
    profile = manager.get("supplier")

    assert profile["successful_runs"] == 0
    assert profile["failure_counts"]["BUDGET_EXHAUSTED"] == 1
