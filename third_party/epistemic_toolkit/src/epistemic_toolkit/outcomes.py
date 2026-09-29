"""Public result semantics: computed / suppressed / unresolved.

This is the ONE rendering point for a published score. Both null states carry
a mandatory reason and are never serialised as a low probability:

  suppressed  no admissible evidence bears on the question  -> "not measured"
  unresolved  evidence bears on it but cancels, or the score fails the
              robustness gate                               -> "evidence inconclusive"

Consumers must branch on `policy`, never on whether `score` happens to be set.
"""
from __future__ import annotations

from enum import Enum
from typing import Any


class ScorePolicy(str, Enum):
    COMPUTED = "computed"
    SUPPRESSED = "suppressed"
    UNRESOLVED = "unresolved"


COMPUTED = ScorePolicy.COMPUTED.value
SUPPRESSED = ScorePolicy.SUPPRESSED.value
UNRESOLVED = ScorePolicy.UNRESOLVED.value
POLICIES = (COMPUTED, SUPPRESSED, UNRESOLVED)

POLICY_SEMANTICS = {
    COMPUTED: "Admissible evidence supports a robustness-tested point estimate.",
    SUPPRESSED: "No admissible evidence bears on the question; no number is reported.",
    UNRESOLVED: ("Admissible evidence bears on the question but cancels or is unstable "
                 "under the robustness gate; no number is reported."),
}

UI_LABEL = {COMPUTED: None, SUPPRESSED: "not measured", UNRESOLVED: "evidence inconclusive"}


def policy_definition() -> dict[str, str]:
    return dict(POLICY_SEMANTICS)


def public_result(policy: str, score: float | None, reason: str | None = None, *,
                  band: tuple[float, float] | None = None,
                  what_would_settle_it: str | None = None) -> dict[str, Any]:
    """Render a score without turning missing or cancelling evidence into a number."""
    try:
        state = ScorePolicy(policy)
    except ValueError as exc:
        raise ValueError(f"unknown score policy: {policy!r}") from exc
    if state is ScorePolicy.COMPUTED:
        if score is None:
            raise ValueError("computed results require a numeric score")
        return {"policy": state.value, "score": score, "band": list(band) if band else None,
                "reason": None, "label": None, "what_would_settle_it": what_would_settle_it}
    if not reason or not str(reason).strip():
        raise ValueError(f"{state.value} results require a reason")
    return {"policy": state.value, "score": None, "band": None, "reason": str(reason).strip(),
            "label": UI_LABEL[state.value], "what_would_settle_it": what_would_settle_it}
