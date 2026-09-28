from __future__ import annotations

from typing import Any

from .models import ResolutionResult, ResolutionState
from .repositories import QueryProfileRepository


class QueryProfileManager:
    """Maintains advisory execution priors without changing safety policy."""

    def __init__(self, repository: QueryProfileRepository) -> None:
        self.repository = repository

    def get(self, query_class: str) -> dict[str, Any]:
        return self.repository.get(query_class) or {
            "query_class": query_class,
            "total_runs": 0,
            "successful_runs": 0,
            "mean_expansions": 0.0,
            "mean_elapsed_ms": 0.0,
            "mean_provider_calls": 0.0,
            "preferred_order": [],
            "useful_providers": {},
            "failure_counts": {},
            "min_arousal": None,
            "max_arousal": None,
        }

    def observe(self, query_class: str, result: ResolutionResult) -> dict[str, Any]:
        profile = self.get(query_class)
        previous_runs = profile["total_runs"]
        total_runs = previous_runs + 1
        telemetry = result.telemetry or {}
        expansions = float(telemetry.get("edges_traversed", 0))
        elapsed = float(telemetry.get("elapsed_ms", 0.0))
        provider_calls = float(telemetry.get("provider_calls", 0))

        profile["total_runs"] = total_runs
        profile["successful_runs"] += int(result.state == ResolutionState.RESOLVED)
        profile["mean_expansions"] = self._running_mean(
            float(profile["mean_expansions"]), previous_runs, expansions
        )
        profile["mean_elapsed_ms"] = self._running_mean(
            float(profile["mean_elapsed_ms"]), previous_runs, elapsed
        )
        profile["mean_provider_calls"] = self._running_mean(
            float(profile["mean_provider_calls"]), previous_runs, provider_calls
        )

        operations = telemetry.get("planner_operations") or []
        if result.state == ResolutionState.RESOLVED and operations:
            profile["preferred_order"] = [
                operation["constraint_id"]
                for operation in operations
                if "constraint_id" in operation
            ]

        providers = dict(profile.get("useful_providers") or {})
        for event in telemetry.get("events", []):
            if event.get("event") == "provider_called":
                name = str(event.get("provider", "unknown"))
                providers[name] = providers.get(name, 0) + 1
        profile["useful_providers"] = providers

        if result.state != ResolutionState.RESOLVED:
            failures = dict(profile.get("failure_counts") or {})
            failures[result.state.value] = failures.get(result.state.value, 0) + 1
            profile["failure_counts"] = failures

        arousal = telemetry.get("arousal_history") or []
        if arousal:
            run_min = min(arousal)
            run_max = max(arousal)
            profile["min_arousal"] = (
                run_min if profile.get("min_arousal") is None else min(profile["min_arousal"], run_min)
            )
            profile["max_arousal"] = (
                run_max if profile.get("max_arousal") is None else max(profile["max_arousal"], run_max)
            )

        self.repository.save(query_class, profile)
        return profile

    @staticmethod
    def _running_mean(previous_mean: float, previous_count: int, value: float) -> float:
        return round(((previous_mean * previous_count) + value) / (previous_count + 1), 6)
