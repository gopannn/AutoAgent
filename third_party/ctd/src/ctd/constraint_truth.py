from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from .models import AttributeConstraint, Node


class ConstraintTruth(StrEnum):
    SATISFIED = "SATISFIED"
    VIOLATED = "VIOLATED"
    UNKNOWN = "UNKNOWN"


class ConstraintEvaluation(BaseModel):
    truth: ConstraintTruth
    reason: str
    actual: Any = None
    expected: Any = None


def evaluate_attribute_constraint(
    constraint: AttributeConstraint,
    node: Node | None,
) -> ConstraintEvaluation:
    if node is None:
        return ConstraintEvaluation(
            truth=ConstraintTruth.UNKNOWN,
            reason=f"No bound node is available for variable {constraint.variable}.",
            expected=constraint.value,
        )
    if constraint.attribute not in node.attributes:
        return ConstraintEvaluation(
            truth=ConstraintTruth.UNKNOWN,
            reason=f"Required attribute {constraint.attribute} is missing from node {node.id}.",
            expected=constraint.value,
        )

    actual = node.attributes[constraint.attribute]
    expected = constraint.value
    try:
        if constraint.op == "eq":
            outcome = actual == expected
        elif constraint.op == "ne":
            outcome = actual != expected
        elif constraint.op == "lt":
            outcome = actual < expected
        elif constraint.op == "lte":
            outcome = actual <= expected
        elif constraint.op == "gt":
            outcome = actual > expected
        elif constraint.op == "gte":
            outcome = actual >= expected
        elif constraint.op == "in":
            outcome = actual in expected
        else:
            return ConstraintEvaluation(
                truth=ConstraintTruth.UNKNOWN,
                reason=f"Unsupported operator {constraint.op}.",
                actual=actual,
                expected=expected,
            )
    except (TypeError, ValueError):
        return ConstraintEvaluation(
            truth=ConstraintTruth.VIOLATED,
            reason=(
                f"Attribute {constraint.attribute} has incompatible value {actual!r} "
                f"for operator {constraint.op} and expected value {expected!r}."
            ),
            actual=actual,
            expected=expected,
        )

    if constraint.exclusion:
        outcome = not outcome
    return ConstraintEvaluation(
        truth=ConstraintTruth.SATISFIED if outcome else ConstraintTruth.VIOLATED,
        reason=(
            f"Attribute {constraint.attribute} satisfies {constraint.op} {expected!r}."
            if outcome
            else f"Attribute {constraint.attribute} violates {constraint.op} {expected!r}."
        ),
        actual=actual,
        expected=expected,
    )
