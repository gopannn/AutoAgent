from __future__ import annotations

from collections import defaultdict

from .models import Edge, Node


class EvidenceGraph:
    """Sparse in-memory property graph with evidence-preserving edges."""

    def __init__(self) -> None:
        self._nodes: dict[str, Node] = {}
        self._outgoing: dict[str, list[Edge]] = defaultdict(list)
        self._incoming: dict[str, list[Edge]] = defaultdict(list)

    def add_node(self, node: Node) -> None:
        self._nodes[node.id] = node

    def add_edge(self, edge: Edge) -> None:
        if edge.source not in self._nodes or edge.target not in self._nodes:
            raise ValueError("edge endpoints must exist before adding an edge")
        self._outgoing[edge.source].append(edge)
        self._incoming[edge.target].append(edge)

    def get_node(self, node_id: str) -> Node | None:
        return self._nodes.get(node_id)

    def nodes_of_type(self, node_type: str) -> list[Node]:
        return [node for node in self._nodes.values() if node.type == node_type]

    def outgoing(self, node_id: str, edge_type: str | None = None) -> list[Edge]:
        edges = list(self._outgoing.get(node_id, []))
        if edge_type is None:
            return edges
        return [edge for edge in edges if edge.type == edge_type]

    def incoming(self, node_id: str, edge_type: str | None = None) -> list[Edge]:
        edges = list(self._incoming.get(node_id, []))
        if edge_type is None:
            return edges
        return [edge for edge in edges if edge.type == edge_type]

    def snapshot(self) -> dict[str, list[dict]]:
        edges = [edge for group in self._outgoing.values() for edge in group]
        return {
            "nodes": [node.model_dump(mode="json") for node in self._nodes.values()],
            "edges": [edge.model_dump(mode="json") for edge in edges],
        }
