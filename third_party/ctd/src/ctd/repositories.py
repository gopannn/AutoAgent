from __future__ import annotations

from copy import deepcopy
from typing import Any, Protocol

from .models import Edge, Evidence, Node


class NodeRepository(Protocol):
    def save(self, node: Node) -> None: ...
    def get(self, node_id: str) -> Node | None: ...
    def list_by_type(self, node_type: str) -> list[Node]: ...


class EdgeRepository(Protocol):
    def save(self, edge: Edge) -> None: ...
    def get(self, edge_id: str) -> Edge | None: ...
    def outgoing(self, node_id: str) -> list[Edge]: ...
    def incoming(self, node_id: str) -> list[Edge]: ...


class ClaimRepository(Protocol):
    def save(self, claim_key: str, record: dict[str, Any]) -> None: ...
    def get_by_key(self, claim_key: str) -> list[dict[str, Any]]: ...


class EvidenceRepository(Protocol):
    def save(self, evidence: Evidence) -> None: ...
    def get(self, evidence_id: str) -> Evidence | None: ...


class ExecutionRepository(Protocol):
    def save(self, execution_id: str, record: dict[str, Any]) -> None: ...
    def get(self, execution_id: str) -> dict[str, Any] | None: ...
    def list(self) -> list[dict[str, Any]]: ...


class QueryProfileRepository(Protocol):
    def save(self, query_class: str, record: dict[str, Any]) -> None: ...
    def get(self, query_class: str) -> dict[str, Any] | None: ...
    def list(self) -> list[dict[str, Any]]: ...


class InMemoryNodeRepository:
    def __init__(self) -> None:
        self._records: dict[str, Node] = {}

    def save(self, node: Node) -> None:
        self._records[node.id] = node.model_copy(deep=True)

    def get(self, node_id: str) -> Node | None:
        node = self._records.get(node_id)
        return node.model_copy(deep=True) if node is not None else None

    def list_by_type(self, node_type: str) -> list[Node]:
        return [node.model_copy(deep=True) for node in self._records.values() if node.type == node_type]


class InMemoryEdgeRepository:
    def __init__(self) -> None:
        self._records: dict[str, Edge] = {}

    def save(self, edge: Edge) -> None:
        self._records[edge.id] = edge.model_copy(deep=True)

    def get(self, edge_id: str) -> Edge | None:
        edge = self._records.get(edge_id)
        return edge.model_copy(deep=True) if edge is not None else None

    def outgoing(self, node_id: str) -> list[Edge]:
        return [edge.model_copy(deep=True) for edge in self._records.values() if edge.source == node_id]

    def incoming(self, node_id: str) -> list[Edge]:
        return [edge.model_copy(deep=True) for edge in self._records.values() if edge.target == node_id]


class InMemoryClaimRepository:
    def __init__(self) -> None:
        self._records: dict[str, list[dict[str, Any]]] = {}

    def save(self, claim_key: str, record: dict[str, Any]) -> None:
        self._records.setdefault(claim_key, []).append(deepcopy(record))

    def get_by_key(self, claim_key: str) -> list[dict[str, Any]]:
        return deepcopy(self._records.get(claim_key, []))


class InMemoryEvidenceRepository:
    def __init__(self) -> None:
        self._records: dict[str, Evidence] = {}

    def save(self, evidence: Evidence) -> None:
        self._records[evidence.id] = evidence.model_copy(deep=True)

    def get(self, evidence_id: str) -> Evidence | None:
        evidence = self._records.get(evidence_id)
        return evidence.model_copy(deep=True) if evidence is not None else None


class InMemoryExecutionRepository:
    def __init__(self) -> None:
        self._records: dict[str, dict[str, Any]] = {}

    def save(self, execution_id: str, record: dict[str, Any]) -> None:
        self._records[execution_id] = deepcopy(record)

    def get(self, execution_id: str) -> dict[str, Any] | None:
        record = self._records.get(execution_id)
        return deepcopy(record) if record is not None else None

    def list(self) -> list[dict[str, Any]]:
        return [deepcopy(record) for _, record in sorted(self._records.items())]


class InMemoryQueryProfileRepository:
    def __init__(self) -> None:
        self._records: dict[str, dict[str, Any]] = {}

    def save(self, query_class: str, record: dict[str, Any]) -> None:
        self._records[query_class] = deepcopy(record)

    def get(self, query_class: str) -> dict[str, Any] | None:
        record = self._records.get(query_class)
        return deepcopy(record) if record is not None else None

    def list(self) -> list[dict[str, Any]]:
        return [deepcopy(record) for _, record in sorted(self._records.items())]


class AdvisorRepository(Protocol):
    def save_stat(self, query_class: str, operation_id: str, record: dict[str, Any]) -> None: ...
    def get_stat(self, query_class: str, operation_id: str) -> dict[str, Any] | None: ...
    def list_stats(self, query_class: str | None = None) -> list[dict[str, Any]]: ...


class InMemoryAdvisorRepository:
    def __init__(self) -> None:
        self._records: dict[tuple[str, str], dict[str, Any]] = {}

    def save_stat(self, query_class: str, operation_id: str, record: dict[str, Any]) -> None:
        self._records[(query_class, operation_id)] = deepcopy(record)

    def get_stat(self, query_class: str, operation_id: str) -> dict[str, Any] | None:
        record = self._records.get((query_class, operation_id))
        return deepcopy(record) if record is not None else None

    def list_stats(self, query_class: str | None = None) -> list[dict[str, Any]]:
        items = []
        for (record_query_class, operation_id), record in sorted(self._records.items()):
            if query_class is not None and record_query_class != query_class:
                continue
            items.append({"query_class": record_query_class, "operation_id": operation_id, **deepcopy(record)})
        return items
