from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

from .authz import AuthorizationFilter
from .graph import EvidenceGraph
from .models import AttributeConstraint, Edge, Node
from .constraint_truth import ConstraintTruth, evaluate_attribute_constraint

T = TypeVar("T")


@dataclass(frozen=True)
class ProviderHealth:
    name: str
    status: str
    latency_ms: float = 0.0
    details: dict[str, str | int | float | bool] | None = None


@dataclass(frozen=True)
class ProviderCost:
    provider_calls: int = 0
    bytes_read: int = 0
    items_examined: int = 0
    authorization_pruned: int = 0


@dataclass(frozen=True)
class ProviderResult(Generic[T]):
    items: list[T]
    cost: ProviderCost
    provider: str


class EvidenceProvider(Protocol):
    name: str

    def get_node(self, node_id: str) -> ProviderResult[Node]: ...
    def nodes_of_type(self, node_type: str, *, limit: int | None = None) -> ProviderResult[Node]: ...
    def prefilter_nodes(self, node_type: str, constraints: list[AttributeConstraint], *, limit: int | None = None) -> ProviderResult[Node]: ...
    def outgoing(self, node_id: str, edge_type: str | None = None, *, limit: int | None = None) -> ProviderResult[Edge]: ...
    def incoming(self, node_id: str, edge_type: str | None = None, *, limit: int | None = None) -> ProviderResult[Edge]: ...
    def node_count(self, node_type: str) -> int: ...
    def distinct_attribute_count(self, node_type: str, attribute: str) -> int: ...
    def health(self) -> ProviderHealth: ...


class GraphProvider:
    """Evidence-provider adapter around the sparse in-memory graph.

    Authorization is intentionally enforced here rather than in the resolver: an
    unauthorized entity is indistinguishable from a missing entity to callers.
    """

    name = "graph"

    def __init__(
        self,
        graph: EvidenceGraph,
        *,
        authorization_scope: set[str] | None = None,
        allowed_security_labels: set[str] | None = None,
        tenant_id: str | None = None,
    ) -> None:
        self.graph = graph
        self.authorization_scope = authorization_scope
        self.allowed_security_labels = allowed_security_labels
        self.tenant_id = tenant_id
        # One decision point for the whole system. This class previously
        # carried its own copy of the policy, which had drifted from the two
        # other copies: it admitted a record with no tenant tag to every
        # tenant, while ctd.unified_close withheld the same record. See
        # ctd.authz for the resolved policy and why tenant fails closed while
        # label does not.
        self._authz = AuthorizationFilter(
            tenant_id=tenant_id,
            allowed_security_labels=(
                None if allowed_security_labels is None
                else frozenset(allowed_security_labels)),
            authorization_scope=None,   # node-id scoping is applied separately
        )

    def _label_allowed(self, attributes: dict) -> bool:
        return self._authz.label_allowed((attributes or {}).get("security_label"))

    def _tenant_allowed(self, attributes: dict) -> bool:
        return self._authz.tenant_allowed((attributes or {}).get("tenant_id"))

    def _authorized(self, node_id: str) -> bool:
        if self.authorization_scope is not None and node_id not in self.authorization_scope:
            return False
        node = self.graph.get_node(node_id)
        return node is None or (self._label_allowed(node.attributes) and self._tenant_allowed(node.attributes))

    def _edge_authorized(self, edge: Edge) -> bool:
        return (
            self._authorized(edge.source)
            and self._authorized(edge.target)
            and self._label_allowed(edge.attributes)
            and self._tenant_allowed(edge.attributes)
        )

    @staticmethod
    def _bytes(items: list[Node] | list[Edge]) -> int:
        return sum(len(item.model_dump_json()) for item in items)

    def _result(self, items: list[T], *, examined: int, pruned: int = 0) -> ProviderResult[T]:
        return ProviderResult(
            items=items,
            cost=ProviderCost(
                provider_calls=1,
                bytes_read=self._bytes(items),  # type: ignore[arg-type]
                items_examined=examined,
                authorization_pruned=pruned,
            ),
            provider=self.name,
        )

    def get_node(self, node_id: str) -> ProviderResult[Node]:
        node = self.graph.get_node(node_id)
        if node is not None and not self._authorized(node_id):
            return self._result([], examined=1, pruned=1)
        return self._result([node] if node is not None else [], examined=int(node is not None))

    def nodes_of_type(self, node_type: str, *, limit: int | None = None) -> ProviderResult[Node]:
        raw = self.graph.nodes_of_type(node_type)
        authorized = [node for node in raw if self._authorized(node.id)]
        items = authorized if limit is None else authorized[:limit]
        return self._result(items, examined=len(raw), pruned=len(raw) - len(authorized))

    def prefilter_nodes(
        self,
        node_type: str,
        constraints: list[AttributeConstraint],
        *,
        limit: int | None = None,
    ) -> ProviderResult[Node]:
        raw = self.graph.nodes_of_type(node_type)
        authorized = [node for node in raw if self._authorized(node.id)]
        matched = [
            node
            for node in authorized
            if all(
                evaluate_attribute_constraint(constraint, node).truth != ConstraintTruth.VIOLATED
                for constraint in constraints
            )
        ]
        items = matched if limit is None else matched[:limit]
        return self._result(items, examined=len(raw), pruned=len(raw) - len(authorized))

    def outgoing(
        self,
        node_id: str,
        edge_type: str | None = None,
        *,
        limit: int | None = None,
    ) -> ProviderResult[Edge]:
        if not self._authorized(node_id):
            return self._result([], examined=1, pruned=1)
        raw = self.graph.outgoing(node_id, edge_type)
        authorized = [edge for edge in raw if self._edge_authorized(edge)]
        items = authorized if limit is None else authorized[:limit]
        return self._result(items, examined=len(raw), pruned=len(raw) - len(authorized))

    def incoming(
        self,
        node_id: str,
        edge_type: str | None = None,
        *,
        limit: int | None = None,
    ) -> ProviderResult[Edge]:
        if not self._authorized(node_id):
            return self._result([], examined=1, pruned=1)
        raw = self.graph.incoming(node_id, edge_type)
        authorized = [edge for edge in raw if self._edge_authorized(edge)]
        items = authorized if limit is None else authorized[:limit]
        return self._result(items, examined=len(raw), pruned=len(raw) - len(authorized))

    def node_count(self, node_type: str) -> int:
        return len([node for node in self.graph.nodes_of_type(node_type) if self._authorized(node.id)])

    def health(self) -> ProviderHealth:
        return ProviderHealth(name=self.name, status="healthy", details={"backend": "memory_graph"})

    def distinct_attribute_count(self, node_type: str, attribute: str) -> int:
        values = {
            repr(node.attributes.get(attribute))
            for node in self.graph.nodes_of_type(node_type)
            if self._authorized(node.id) and attribute in node.attributes
        }
        return len(values)

    def snapshot(self) -> dict:
        """Return only evidence visible through this provider's Focus policy.

        Replay persistence must never serialize the raw backing graph when the
        provider has tenant, security-label, or explicit authorization filters.
        """
        raw = self.graph.snapshot()
        nodes = []
        visible_ids: set[str] = set()
        for raw_node in raw.get("nodes", []):
            node = Node.model_validate(raw_node)
            if self._authorized(node.id):
                nodes.append(node.model_dump(mode="json"))
                visible_ids.add(node.id)
        edges = []
        for raw_edge in raw.get("edges", []):
            edge = Edge.model_validate(raw_edge)
            if (
                edge.source in visible_ids
                and edge.target in visible_ids
                and self._edge_authorized(edge)
            ):
                edges.append(edge.model_dump(mode="json"))
        return {"nodes": nodes, "edges": edges}


class MemoryProvider(GraphProvider):
    """Deterministic fixture provider; currently graph-backed by design."""

    name = "memory"

    @classmethod
    def from_graph(cls, graph: EvidenceGraph) -> "MemoryProvider":
        return cls(graph)
