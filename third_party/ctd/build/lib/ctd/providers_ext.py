from __future__ import annotations

import json
import math
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Mapping
from time import monotonic
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, build_opener

from pydantic import BaseModel, Field, ValidationError

from .graph import EvidenceGraph
from .models import AttributeConstraint, Edge, Node
from .constraint_truth import ConstraintTruth, evaluate_attribute_constraint
from .providers import EvidenceProvider, GraphProvider, ProviderCost, ProviderHealth, ProviderResult


def _sum_cost(costs: Iterable[ProviderCost]) -> ProviderCost:
    values = list(costs)
    return ProviderCost(
        provider_calls=sum(item.provider_calls for item in values),
        bytes_read=sum(item.bytes_read for item in values),
        items_examined=sum(item.items_examined for item in values),
        authorization_pruned=sum(item.authorization_pruned for item in values),
    )


class SQLProvider:
    name = "sql"

    def __init__(
        self,
        connection: Any,
        *,
        tenant_id: str | None = None,
        allowed_security_labels: set[str] | None = None,
        placeholder: str = "?",
    ) -> None:
        self.connection = connection
        self.tenant_id = tenant_id
        self.allowed_security_labels = allowed_security_labels
        self.placeholder = placeholder

    def _query_payloads(self, sql: str, params: tuple[Any, ...]) -> tuple[list[str], int]:
        cursor = self.connection.execute(sql, params) if hasattr(self.connection, "execute") else self.connection.cursor().execute(sql, params)
        rows = cursor.fetchall()
        payloads = [row.get("payload_json") if isinstance(row, Mapping) else row[0] for row in rows]
        return payloads, len(rows)

    @staticmethod
    def _node_payload(value: Any) -> Node:
        return Node.model_validate_json(value) if isinstance(value, (str, bytes)) else Node.model_validate(value)

    @staticmethod
    def _edge_payload(value: Any) -> Edge:
        return Edge.model_validate_json(value) if isinstance(value, (str, bytes)) else Edge.model_validate(value)

    def _node_allowed(self, node: Node) -> bool:
        if self.tenant_id is not None and node.attributes.get("tenant_id") not in (None, self.tenant_id):
            return False
        if self.allowed_security_labels is not None:
            label = node.attributes.get("security_label")
            if label is not None and label not in self.allowed_security_labels:
                return False
        return True

    def _edge_allowed(self, edge: Edge) -> bool:
        if self.tenant_id is not None and edge.attributes.get("tenant_id") not in (None, self.tenant_id):
            return False
        if self.allowed_security_labels is not None:
            label = edge.attributes.get("security_label")
            if label is not None and label not in self.allowed_security_labels:
                return False
        return True

    @staticmethod
    def _cost(items: list[Node] | list[Edge], examined: int, pruned: int = 0) -> ProviderCost:
        return ProviderCost(
            provider_calls=1,
            bytes_read=sum(len(item.model_dump_json()) for item in items),
            items_examined=examined,
            authorization_pruned=pruned,
        )

    def get_node(self, node_id: str) -> ProviderResult[Node]:
        p = self.placeholder
        payloads, examined = self._query_payloads(
            f"SELECT payload_json FROM ctd_nodes WHERE id={p}", (node_id,)
        )
        raw = [self._node_payload(value) for value in payloads]
        items = [node for node in raw if self._node_allowed(node)]
        return ProviderResult(items=items, cost=self._cost(items, examined, examined - len(items)), provider=self.name)

    def nodes_of_type(self, node_type: str, *, limit: int | None = None) -> ProviderResult[Node]:
        p = self.placeholder
        sql = f"SELECT payload_json FROM ctd_nodes WHERE type={p} ORDER BY id"
        params: tuple[Any, ...] = (node_type,)
        payloads, examined = self._query_payloads(sql, params)
        raw = [self._node_payload(value) for value in payloads]
        allowed = [node for node in raw if self._node_allowed(node)]
        items = allowed if limit is None else allowed[:limit]
        return ProviderResult(items=items, cost=self._cost(items, examined, examined - len(allowed)), provider=self.name)

    def prefilter_nodes(
        self,
        node_type: str,
        constraints: list[AttributeConstraint],
        *,
        limit: int | None = None,
    ) -> ProviderResult[Node]:
        supported = {"eq": "=", "ne": "!=", "lt": "<", "lte": "<=", "gt": ">", "gte": ">="}
        pushable = (
            self.placeholder == "?"
            and all(
                constraint.op in supported
                and not constraint.exclusion
                and isinstance(constraint.value, (str, int, float, bool))
                for constraint in constraints
            )
        )
        if pushable and constraints:
            clauses = [f"type={self.placeholder}"]
            params: list[Any] = [node_type]
            for constraint in constraints:
                clauses.append(f"json_extract(payload_json, {self.placeholder}) {supported[constraint.op]} {self.placeholder}")
                params.extend([f"$.attributes.{constraint.attribute}", constraint.value])
            sql = "SELECT payload_json FROM ctd_nodes WHERE " + " AND ".join(clauses) + " ORDER BY id"
            if limit is not None:
                sql += f" LIMIT {self.placeholder}"
                params.append(limit)
            payloads, examined = self._query_payloads(sql, tuple(params))
            raw = [self._node_payload(value) for value in payloads]
            allowed = [node for node in raw if self._node_allowed(node)]
            # Local verification remains authoritative even after SQL pushdown.
            items = [
                node for node in allowed
                if all(evaluate_attribute_constraint(constraint, node).truth != ConstraintTruth.VIOLATED for constraint in constraints)
            ]
            return ProviderResult(items=items, cost=self._cost(items, examined, examined - len(allowed)), provider=self.name)

        base = self.nodes_of_type(node_type, limit=None)
        matched = [
            node for node in base.items
            if all(evaluate_attribute_constraint(constraint, node).truth != ConstraintTruth.VIOLATED for constraint in constraints)
        ]
        items = matched if limit is None else matched[:limit]
        return ProviderResult(items=items, cost=ProviderCost(
            provider_calls=base.cost.provider_calls,
            bytes_read=sum(len(item.model_dump_json()) for item in items),
            items_examined=base.cost.items_examined,
            authorization_pruned=base.cost.authorization_pruned,
        ), provider=self.name)

    def outgoing(self, node_id: str, edge_type: str | None = None, *, limit: int | None = None) -> ProviderResult[Edge]:
        p = self.placeholder
        if edge_type is None:
            sql, params = f"SELECT payload_json FROM ctd_edges WHERE source={p} ORDER BY id", (node_id,)
        else:
            sql, params = f"SELECT payload_json FROM ctd_edges WHERE source={p} AND type={p} ORDER BY id", (node_id, edge_type)
        payloads, examined = self._query_payloads(sql, params)
        raw = [self._edge_payload(value) for value in payloads]
        allowed = [edge for edge in raw if self._edge_allowed(edge)]
        items = allowed if limit is None else allowed[:limit]
        return ProviderResult(items=items, cost=self._cost(items, examined, examined - len(allowed)), provider=self.name)

    def incoming(self, node_id: str, edge_type: str | None = None, *, limit: int | None = None) -> ProviderResult[Edge]:
        p = self.placeholder
        if edge_type is None:
            sql, params = f"SELECT payload_json FROM ctd_edges WHERE target={p} ORDER BY id", (node_id,)
        else:
            sql, params = f"SELECT payload_json FROM ctd_edges WHERE target={p} AND type={p} ORDER BY id", (node_id, edge_type)
        payloads, examined = self._query_payloads(sql, params)
        raw = [self._edge_payload(value) for value in payloads]
        allowed = [edge for edge in raw if self._edge_allowed(edge)]
        items = allowed if limit is None else allowed[:limit]
        return ProviderResult(items=items, cost=self._cost(items, examined, examined - len(allowed)), provider=self.name)

    def node_count(self, node_type: str) -> int:
        return len(self.nodes_of_type(node_type).items)

    def distinct_attribute_count(self, node_type: str, attribute: str) -> int:
        values = {repr(node.attributes[attribute]) for node in self.nodes_of_type(node_type).items if attribute in node.attributes}
        return len(values)

    def health(self) -> ProviderHealth:
        started = monotonic()
        try:
            cursor = self.connection.execute("SELECT 1") if hasattr(self.connection, "execute") else self.connection.cursor().execute("SELECT 1")
            if hasattr(cursor, "fetchone"):
                cursor.fetchone()
            return ProviderHealth(name=self.name, status="healthy", latency_ms=(monotonic() - started) * 1000)
        except Exception as exc:
            return ProviderHealth(name=self.name, status="unavailable", latency_ms=(monotonic() - started) * 1000, details={"error": type(exc).__name__})


class DocumentProvider(GraphProvider):
    name = "document"

    def __init__(
        self,
        paths: Iterable[str | Path],
        *,
        tenant_id: str | None = None,
        allowed_security_labels: set[str] | None = None,
    ) -> None:
        graph = EvidenceGraph()
        pending_edges: list[Edge] = []
        for raw_path in paths:
            path = Path(raw_path)
            records = self._records(path)
            for record in records:
                kind = record.pop("kind", None)
                if kind == "node":
                    graph.add_node(Node.model_validate(record))
                elif kind == "edge":
                    pending_edges.append(Edge.model_validate(record))
                else:
                    raise ValueError(f"unknown document record kind: {kind}")
        for edge in pending_edges:
            graph.add_edge(edge)
        super().__init__(graph, tenant_id=tenant_id, allowed_security_labels=allowed_security_labels)

    @staticmethod
    def _records(path: Path) -> list[dict[str, Any]]:
        if path.suffix.lower() == ".jsonl":
            records: list[dict[str, Any]] = []
            for line_no, line in enumerate(path.read_text().splitlines(), start=1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError(f"JSONL record {line_no} must be an object")
                records.append(dict(value))
            return records
        value = json.loads(path.read_text())
        if isinstance(value, list):
            return [dict(item) for item in value]
        if isinstance(value, dict) and "nodes" in value:
            return [
                *[{"kind": "node", **item} for item in value.get("nodes", [])],
                *[{"kind": "edge", **item} for item in value.get("edges", [])],
            ]
        if isinstance(value, dict):
            return [dict(value)]
        raise ValueError("JSON document must be an object or array")


class VectorRecord(BaseModel):
    id: str
    node_type: str
    vector: list[float] = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class VectorMatch(BaseModel):
    id: str
    node_type: str
    score: float
    metadata: dict[str, Any] = Field(default_factory=dict)


class VectorProvider:
    name = "vector"

    def __init__(self, records: Iterable[VectorRecord]) -> None:
        self.records = [record.model_copy(deep=True) for record in records]
        dimensions = {len(record.vector) for record in self.records}
        if len(dimensions) > 1:
            raise ValueError("all vectors must have equal dimensionality")
        self.dimension = next(iter(dimensions), None)

    @staticmethod
    def _cosine(left: list[float], right: list[float]) -> float:
        left_norm = math.sqrt(sum(item * item for item in left))
        right_norm = math.sqrt(sum(item * item for item in right))
        if left_norm == 0.0 or right_norm == 0.0:
            return 0.0
        return sum(a * b for a, b in zip(left, right, strict=True)) / (left_norm * right_norm)

    def search(self, vector: list[float], *, node_type: str | None = None, limit: int = 5) -> list[VectorMatch]:
        if self.dimension is not None and len(vector) != self.dimension:
            raise ValueError(f"expected vector dimension {self.dimension}, got {len(vector)}")
        matches = [
            VectorMatch(
                id=record.id,
                node_type=record.node_type,
                score=round(self._cosine(vector, record.vector), 12),
                metadata=record.metadata,
            )
            for record in self.records
            if node_type is None or record.node_type == node_type
        ]
        matches.sort(key=lambda item: (-item.score, item.id))
        return matches[:limit]


class RemoteAPIProvider:
    name = "remote_api"

    def __init__(
        self,
        base_url: str,
        *,
        opener: Any | None = None,
        timeout_s: float = 2.0,
        max_bytes: int = 2_000_000,
        authorization_scope: set[str] | None = None,
        allowed_security_labels: set[str] | None = None,
        tenant_id: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.opener = opener or build_opener()
        self.timeout_s = timeout_s
        self.max_bytes = max_bytes
        self.authorization_scope = authorization_scope
        self.allowed_security_labels = allowed_security_labels
        self.tenant_id = tenant_id

    def _attributes_allowed(self, attributes: dict[str, Any]) -> bool:
        if self.tenant_id is not None and attributes.get("tenant_id") not in (None, self.tenant_id):
            return False
        if self.allowed_security_labels is not None:
            label = attributes.get("security_label")
            if label is not None and label not in self.allowed_security_labels:
                return False
        return True

    def _item_allowed(self, item: Node | Edge) -> bool:
        if isinstance(item, Node):
            if self.authorization_scope is not None and item.id not in self.authorization_scope:
                return False
            return self._attributes_allowed(item.attributes)
        if self.authorization_scope is not None and (
            item.source not in self.authorization_scope or item.target not in self.authorization_scope
        ):
            return False
        return self._attributes_allowed(item.attributes)

    def _request(self, path: str, params: dict[str, Any] | None = None) -> tuple[dict[str, Any], int]:
        url = f"{self.base_url}{path}"
        if params:
            encoded = urlencode({key: value for key, value in params.items() if value is not None})
            if encoded:
                url += f"?{encoded}"
        request = Request(url, method="GET")
        try:
            with self.opener.open(request, timeout=self.timeout_s) as response:
                body = response.read(self.max_bytes + 1)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"remote provider unavailable: {type(exc).__name__}") from exc
        if len(body) > self.max_bytes:
            raise RuntimeError("remote provider response too large")
        try:
            value = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RuntimeError("invalid provider payload: JSON expected") from exc
        if not isinstance(value, dict):
            raise RuntimeError("invalid provider payload: object expected")
        return value, len(body)

    def _items(self, path: str, model: type[Node] | type[Edge], params: dict[str, Any] | None = None) -> ProviderResult:
        payload, size = self._request(path, params)
        raw_items = payload.get("items", [])
        if not isinstance(raw_items, list):
            raise RuntimeError("invalid provider payload: items must be a list")
        try:
            parsed = [model.model_validate(item) for item in raw_items]
        except ValidationError as exc:
            raise RuntimeError("invalid provider payload: model validation failed") from exc
        items = [item for item in parsed if self._item_allowed(item)]
        return ProviderResult(
            items=items,
            cost=ProviderCost(
                provider_calls=1,
                bytes_read=size,
                items_examined=len(parsed),
                authorization_pruned=len(parsed) - len(items),
            ),
            provider=self.name,
        )

    def get_node(self, node_id: str) -> ProviderResult[Node]:
        return self._items(f"/nodes/{quote(node_id, safe='')}", Node)

    def nodes_of_type(self, node_type: str, *, limit: int | None = None) -> ProviderResult[Node]:
        return self._items("/nodes", Node, {"type": node_type, "limit": limit})

    def outgoing(self, node_id: str, edge_type: str | None = None, *, limit: int | None = None) -> ProviderResult[Edge]:
        return self._items(f"/nodes/{quote(node_id, safe='')}/outgoing", Edge, {"type": edge_type, "limit": limit})

    def incoming(self, node_id: str, edge_type: str | None = None, *, limit: int | None = None) -> ProviderResult[Edge]:
        return self._items(f"/nodes/{quote(node_id, safe='')}/incoming", Edge, {"type": edge_type, "limit": limit})

    def node_count(self, node_type: str) -> int:
        if self.tenant_id is not None or self.allowed_security_labels is not None or self.authorization_scope is not None:
            return len(self.nodes_of_type(node_type).items)
        payload, _ = self._request("/stats/node-count", {"type": node_type})
        return int(payload.get("count", 0))

    def distinct_attribute_count(self, node_type: str, attribute: str) -> int:
        if self.tenant_id is not None or self.allowed_security_labels is not None or self.authorization_scope is not None:
            values = {
                repr(node.attributes[attribute])
                for node in self.nodes_of_type(node_type).items
                if attribute in node.attributes
            }
            return len(values)
        payload, _ = self._request("/stats/distinct", {"type": node_type, "attribute": attribute})
        return int(payload.get("count", 0))

    def health(self) -> ProviderHealth:
        started = monotonic()
        try:
            self._request("/health")
            return ProviderHealth(name=self.name, status="healthy", latency_ms=(monotonic() - started) * 1000)
        except RuntimeError as exc:
            return ProviderHealth(name=self.name, status="unavailable", latency_ms=(monotonic() - started) * 1000, details={"error": str(exc)[:120]})


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class Neo4jProvider:
    name = "neo4j"

    def __init__(
        self,
        *,
        driver: Any | None = None,
        uri: str | None = None,
        username: str | None = None,
        password: str | None = None,
        authorization_scope: set[str] | None = None,
        allowed_security_labels: set[str] | None = None,
        tenant_id: str | None = None,
    ) -> None:
        if driver is None:
            if not uri or username is None or password is None:
                raise ValueError("Neo4j URI, username and password are required")
            try:
                from neo4j import GraphDatabase  # type: ignore
            except ImportError as exc:
                raise RuntimeError("Neo4j support requires the 'neo4j' optional extra") from exc
            driver = GraphDatabase.driver(uri, auth=(username, password))
        self.driver = driver
        self.authorization_scope = authorization_scope
        self.allowed_security_labels = allowed_security_labels
        self.tenant_id = tenant_id

    def _attributes_allowed(self, attributes: dict[str, Any]) -> bool:
        if self.tenant_id is not None and attributes.get("tenant_id") not in (None, self.tenant_id):
            return False
        if self.allowed_security_labels is not None:
            label = attributes.get("security_label")
            if label is not None and label not in self.allowed_security_labels:
                return False
        return True

    def _node_allowed(self, node: Node) -> bool:
        if self.authorization_scope is not None and node.id not in self.authorization_scope:
            return False
        return self._attributes_allowed(node.attributes)

    def _edge_allowed(self, edge: Edge) -> bool:
        if self.authorization_scope is not None and (
            edge.source not in self.authorization_scope or edge.target not in self.authorization_scope
        ):
            return False
        return self._attributes_allowed(edge.attributes)

    def _node_result(self, parsed: list[Node], examined: int) -> ProviderResult[Node]:
        items = [node for node in parsed if self._node_allowed(node)]
        return ProviderResult(
            items=items,
            cost=ProviderCost(
                provider_calls=1,
                items_examined=examined,
                bytes_read=sum(len(item.model_dump_json()) for item in items),
                authorization_pruned=examined - len(items),
            ),
            provider=self.name,
        )

    def _edge_result(self, parsed: list[Edge], examined: int) -> ProviderResult[Edge]:
        items = [edge for edge in parsed if self._edge_allowed(edge)]
        return ProviderResult(
            items=items,
            cost=ProviderCost(
                provider_calls=1,
                items_examined=examined,
                bytes_read=sum(len(item.model_dump_json()) for item in items),
                authorization_pruned=examined - len(items),
            ),
            provider=self.name,
        )

    @staticmethod
    def _identifier(value: str) -> str:
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError(f"unsafe Neo4j identifier: {value!r}")
        return value

    @staticmethod
    def _node(value: Any, *, node_type_hint: str | None = None) -> Node:
        if isinstance(value, Node):
            return value
        data = dict(value)
        if "attributes" not in data:
            attrs = {key: item for key, item in data.items() if key not in {"id", "type"}}
            data = {
                "id": str(data.get("id")),
                "type": str(data.get("type", node_type_hint or "Entity")),
                "attributes": attrs,
            }
        elif node_type_hint is not None and data.get("type") in (None, "Entity"):
            data["type"] = node_type_hint
        return Node.model_validate(data)

    @staticmethod
    def _edge(value: Any) -> Edge:
        if isinstance(value, Edge):
            return value
        return Edge.model_validate(dict(value))

    def _run(self, query: str, params: dict[str, Any]) -> list[Any]:
        with self.driver.session() as session:
            return list(session.run(query, params))

    def get_node(self, node_id: str) -> ProviderResult[Node]:
        rows = self._run(
            "MATCH (n {id: $node_id}) RETURN n AS node, labels(n) AS labels LIMIT 1",
            {"node_id": node_id},
        )
        parsed = [
            self._node(
                row["node"],
                node_type_hint=(row.get("labels") or [None])[0] if hasattr(row, "get") else None,
            )
            for row in rows
        ]
        return self._node_result(parsed, len(rows))

    def nodes_of_type(self, node_type: str, *, limit: int | None = None) -> ProviderResult[Node]:
        label = self._identifier(node_type)
        cap = limit or 1000000
        rows = self._run(f"MATCH (n:`{label}`) RETURN n AS node ORDER BY n.id LIMIT $limit", {"limit": cap})
        parsed = [self._node(row["node"], node_type_hint=node_type) for row in rows]
        result = self._node_result(parsed, len(rows))
        if limit is not None:
            return ProviderResult(items=result.items[:limit], cost=result.cost, provider=self.name)
        return result

    def outgoing(self, node_id: str, edge_type: str | None = None, *, limit: int | None = None) -> ProviderResult[Edge]:
        relation = f":`{self._identifier(edge_type)}`" if edge_type else ""
        rows = self._run(
            f"MATCH (a {{id: $node_id}})-[r{relation}]->(b) RETURN r AS edge, a.id AS source, b.id AS target, type(r) AS edge_type, elementId(r) AS edge_id LIMIT $limit",
            {"node_id": node_id, "limit": limit or 1000000},
        )
        items: list[Edge] = []
        for row in rows:
            raw = dict(row["edge"])
            edge_id = row.get("edge_id") if hasattr(row, "get") else None
            resolved_type = row.get("edge_type") if hasattr(row, "get") else None
            raw.setdefault("id", str(raw.get("id") or edge_id or f"neo4j:{node_id}:{row['target']}"))
            raw.setdefault("source", row["source"])
            raw.setdefault("target", row["target"])
            raw.setdefault("type", edge_type or raw.get("type") or resolved_type or "RELATED")
            raw.setdefault("attributes", {})
            raw.setdefault("evidence", [])
            items.append(Edge.model_validate(raw))
        result = self._edge_result(items, len(rows))
        if limit is not None:
            return ProviderResult(items=result.items[:limit], cost=result.cost, provider=self.name)
        return result

    def incoming(self, node_id: str, edge_type: str | None = None, *, limit: int | None = None) -> ProviderResult[Edge]:
        relation = f":`{self._identifier(edge_type)}`" if edge_type else ""
        rows = self._run(
            f"MATCH (a)-[r{relation}]->(b {{id: $node_id}}) RETURN r AS edge, a.id AS source, b.id AS target, type(r) AS edge_type, elementId(r) AS edge_id LIMIT $limit",
            {"node_id": node_id, "limit": limit or 1000000},
        )
        items: list[Edge] = []
        for row in rows:
            raw = dict(row["edge"])
            edge_id = row.get("edge_id") if hasattr(row, "get") else None
            resolved_type = row.get("edge_type") if hasattr(row, "get") else None
            raw.setdefault("id", str(raw.get("id") or edge_id or f"neo4j:{row['source']}:{node_id}"))
            raw.setdefault("source", row["source"])
            raw.setdefault("target", row["target"])
            raw.setdefault("type", edge_type or raw.get("type") or resolved_type or "RELATED")
            raw.setdefault("attributes", {})
            raw.setdefault("evidence", [])
            items.append(Edge.model_validate(raw))
        result = self._edge_result(items, len(rows))
        if limit is not None:
            return ProviderResult(items=result.items[:limit], cost=result.cost, provider=self.name)
        return result

    def node_count(self, node_type: str) -> int:
        if self.tenant_id is not None or self.allowed_security_labels is not None or self.authorization_scope is not None:
            return len(self.nodes_of_type(node_type).items)
        label = self._identifier(node_type)
        rows = self._run(f"MATCH (n:`{label}`) RETURN count(n) AS count", {})
        return int(rows[0]["count"]) if rows else 0

    def distinct_attribute_count(self, node_type: str, attribute: str) -> int:
        if self.tenant_id is not None or self.allowed_security_labels is not None or self.authorization_scope is not None:
            values = {
                repr(node.attributes[attribute])
                for node in self.nodes_of_type(node_type).items
                if attribute in node.attributes
            }
            return len(values)
        label = self._identifier(node_type)
        attr = self._identifier(attribute)
        rows = self._run(f"MATCH (n:`{label}`) RETURN count(DISTINCT n.`{attr}`) AS count", {})
        return int(rows[0]["count"]) if rows else 0

    def health(self) -> ProviderHealth:
        started = monotonic()
        try:
            self._run("RETURN 1 AS ok", {})
            return ProviderHealth(name=self.name, status="healthy", latency_ms=(monotonic() - started) * 1000)
        except Exception as exc:
            return ProviderHealth(name=self.name, status="unavailable", latency_ms=(monotonic() - started) * 1000, details={"error": type(exc).__name__})


class FederatedProvider:
    name = "federated"

    def __init__(
        self,
        providers: list[EvidenceProvider],
        *,
        fail_fast: bool = False,
        parallel: bool = False,
        max_workers: int = 4,
    ) -> None:
        if not providers:
            raise ValueError("FederatedProvider requires at least one provider")
        self.providers = list(providers)
        self.fail_fast = fail_fast
        self.parallel = parallel
        self.max_workers = max_workers
        self._last_errors: dict[str, str] = {}
        self.conflicts: list[dict[str, str]] = []

    def _fanout(self, method: str, *args: Any, **kwargs: Any) -> list[tuple[int, EvidenceProvider, Any]]:
        self._last_errors = {}
        results: list[tuple[int, EvidenceProvider, Any]] = []

        def invoke(index_provider: tuple[int, EvidenceProvider]):
            index, provider = index_provider
            return index, provider, getattr(provider, method)(*args, **kwargs)

        indexed = list(enumerate(self.providers))
        if self.parallel and len(indexed) > 1:
            with ThreadPoolExecutor(max_workers=min(self.max_workers, len(indexed))) as pool:
                futures = [pool.submit(invoke, item) for item in indexed]
                for future, (index, provider) in zip(futures, indexed, strict=True):
                    try:
                        results.append(future.result())
                    except Exception as exc:
                        self._last_errors[provider.name] = type(exc).__name__
                        if self.fail_fast:
                            raise
        else:
            for item in indexed:
                index, provider = item
                try:
                    results.append(invoke(item))
                except Exception as exc:
                    self._last_errors[provider.name] = type(exc).__name__
                    if self.fail_fast:
                        raise
        results.sort(key=lambda item: item[0])
        return results

    def _merge(self, responses: list[tuple[int, EvidenceProvider, ProviderResult]], kind: str) -> ProviderResult:
        seen: dict[str, Any] = {}
        costs: list[ProviderCost] = []
        for _, provider, response in responses:
            costs.append(response.cost)
            for item in response.items:
                existing = seen.get(item.id)
                if existing is None:
                    seen[item.id] = item
                elif existing.model_dump(mode="json") != item.model_dump(mode="json"):
                    self.conflicts.append({"kind": kind, "id": item.id, "provider": provider.name})
        items = list(seen.values())
        return ProviderResult(items=items, cost=_sum_cost(costs), provider=self.name)

    def get_node(self, node_id: str) -> ProviderResult[Node]:
        return self._merge(self._fanout("get_node", node_id), "node")

    def nodes_of_type(self, node_type: str, *, limit: int | None = None) -> ProviderResult[Node]:
        merged = self._merge(self._fanout("nodes_of_type", node_type, limit=limit), "node")
        if limit is not None:
            return ProviderResult(items=merged.items[:limit], cost=merged.cost, provider=self.name)
        return merged

    def outgoing(self, node_id: str, edge_type: str | None = None, *, limit: int | None = None) -> ProviderResult[Edge]:
        merged = self._merge(self._fanout("outgoing", node_id, edge_type, limit=limit), "edge")
        if limit is not None:
            return ProviderResult(items=merged.items[:limit], cost=merged.cost, provider=self.name)
        return merged

    def incoming(self, node_id: str, edge_type: str | None = None, *, limit: int | None = None) -> ProviderResult[Edge]:
        merged = self._merge(self._fanout("incoming", node_id, edge_type, limit=limit), "edge")
        if limit is not None:
            return ProviderResult(items=merged.items[:limit], cost=merged.cost, provider=self.name)
        return merged

    def node_count(self, node_type: str) -> int:
        responses = self._fanout("node_count", node_type)
        counts = [int(value) for _, _, value in responses]
        return max(counts, default=0)

    def distinct_attribute_count(self, node_type: str, attribute: str) -> int:
        responses = self._fanout("distinct_attribute_count", node_type, attribute)
        counts = [int(value) for _, _, value in responses]
        return max(counts, default=0)

    def health(self) -> ProviderHealth:
        statuses: list[str] = []
        details: dict[str, str] = dict(self._last_errors)
        for provider in self.providers:
            if provider.name in self._last_errors:
                statuses.append("unavailable")
                continue
            health = getattr(provider, "health", None)
            if health is None:
                statuses.append("healthy")
                continue
            try:
                value = health()
                statuses.append(value.status)
                if value.status != "healthy":
                    details[provider.name] = value.status
            except Exception as exc:
                statuses.append("unavailable")
                details[provider.name] = type(exc).__name__
        if statuses and all(status == "unavailable" for status in statuses):
            status = "unavailable"
        elif any(status != "healthy" for status in statuses):
            status = "degraded"
        else:
            status = "healthy"
        return ProviderHealth(name=self.name, status=status, details=details or None)
