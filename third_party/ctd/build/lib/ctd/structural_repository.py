from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from .authz import AuthorizationFilter
from .structural import StructuralCase


class HypothesisRunRecord(BaseModel):
    run_id: str
    tenant_id: str | None = None
    security_label: str | None = None
    mode: Literal["transfer", "premortem"]
    target_name: str
    result: dict = Field(default_factory=dict)
    created_at: datetime


class StructuralCaseRepository(Protocol):
    def save(self, case: StructuralCase) -> None: ...
    def get(self, case_id: str) -> StructuralCase | None: ...
    def list(self, *, tenant_id: str | None = None, allowed_security_labels: set[str] | None = None) -> list[StructuralCase]: ...


class HypothesisRunRepository(Protocol):
    def save(self, run: HypothesisRunRecord) -> None: ...
    def get(self, run_id: str) -> HypothesisRunRecord | None: ...
    def list(self, *, tenant_id: str | None = None) -> list[HypothesisRunRecord]: ...


class InMemoryStructuralCaseRepository:
    def __init__(self) -> None:
        self._records: dict[str, StructuralCase] = {}

    def save(self, case: StructuralCase) -> None:
        self._records[case.id] = case.model_copy(deep=True)

    def get(self, case_id: str) -> StructuralCase | None:
        value = self._records.get(case_id)
        return value.model_copy(deep=True) if value is not None else None

    def list(self, *, tenant_id: str | None = None, allowed_security_labels: set[str] | None = None) -> list[StructuralCase]:
        # Delegated to ctd.authz so this repository cannot drift from the
        # provider and the close operator. It previously admitted an untagged
        # case to every tenant.
        authz = AuthorizationFilter(
            tenant_id=tenant_id,
            allowed_security_labels=(
                None if allowed_security_labels is None
                else frozenset(allowed_security_labels)),
        )
        values = [case.model_copy(deep=True)
                  for case in self._records.values()
                  if authz.allows_object(case)]
        return sorted(values, key=lambda item: item.id)


class InMemoryHypothesisRunRepository:
    def __init__(self) -> None:
        self._records: dict[str, HypothesisRunRecord] = {}

    def save(self, run: HypothesisRunRecord) -> None:
        self._records[run.run_id] = run.model_copy(deep=True)

    def get(self, run_id: str) -> HypothesisRunRecord | None:
        value = self._records.get(run_id)
        return value.model_copy(deep=True) if value is not None else None

    def list(self, *, tenant_id: str | None = None) -> list[HypothesisRunRecord]:
        values = [run.model_copy(deep=True) for run in self._records.values() if tenant_id is None or run.tenant_id in (None, tenant_id)]
        return sorted(values, key=lambda item: (item.created_at, item.run_id))
