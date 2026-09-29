"""DISCOVER: a structural description of the service, in CTD's core vocabulary.

Two sources, both deterministic:

* the contract and requirement encoding, before any code exists;
* the Python AST of the generated code, after each change.

The extractor recognises a small set of motifs and says nothing it cannot see in the code:

  every HTTP route
      FLOWS(request_load, api_gateway)
  outbound HTTP call (httpx / requests)
      DEPENDS(api_service, upstream_api)
  retry loop, tenacity or backoff around such a call
      CAUSES(RETRIES(api_service, upstream_api), INCREASES(request_load, upstream_pressure))
  connection pool / engine / redis client
      DEPENDS(api_service, <name>_pool)
  module-level mutable container mutated inside a function
      SHARED(<name>, request_handlers), FLOWS(request_load, <name>),
      CAUSES(FLOWS(request_load, <name>), INCREASES(request_load, <name>_contention))
      and, if it only ever grows:
      CAUSES(FLOWS(request_load, api_gateway), INCREASES(request_load, <name>_entries))
  unbounded asyncio / queue.Queue
      FLOWS(request_load, <name>)

The CAUSES relations are true by construction (retries add load; requests through a shared
store add contention). They give the aligner the causal depth TRANSFER requires.
Missing timeouts on outbound calls are recorded in metadata; they are not relations because the
core vocabulary has no predicate for an absent property.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field

_HTTP_MODULES = {"httpx", "requests"}
_HTTP_VERBS = {"get", "post", "put", "patch", "delete", "request", "stream", "head", "options"}
_CLIENT_FACTORIES = {"Client", "AsyncClient", "Session"}
_POOL_FACTORIES = {
    "create_engine", "create_async_engine", "Redis", "StrictRedis", "ConnectionPool", "create_pool",
    "AsyncConnectionPool", "ConnectionPoolManager", "MongoClient", "AsyncIOMotorClient",
}
_ROUTE_DECORATORS = {"get", "post", "put", "patch", "delete", "api_route", "websocket"}
_MUTATORS_GROW = {"append", "extend", "add", "update", "setdefault", "insert", "appendleft"}
_MUTATORS_SHRINK = {"pop", "popitem", "remove", "discard", "clear", "popleft"}
_CONTAINER_CALLS = {"dict", "list", "set", "defaultdict", "OrderedDict", "deque", "Counter"}
_RETRY_DECORATORS = {"retry", "on_exception", "retry_with_backoff"}


@dataclass
class Topology:
    relations: list = field(default_factory=list)     # nested tuples: (pred, *args)
    types: dict[str, str] = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)

    def add(self, rel: tuple, **types: str) -> None:
        if rel not in self.relations:
            self.relations.append(rel)
        self.types.update(types)

    def to_target(self, name: str):
        from ..vendored import ctd_module

        structural = ctd_module("structural")

        def build(rel):
            pred, *args = rel
            return structural.StructuralRelation(
                pred=pred, args=tuple(build(a) if isinstance(a, tuple) else a for a in args))

        return structural.StructuralTarget(
            name=name, domain="software", relations=tuple(build(r) for r in self.relations),
            types=dict(self.types), metadata=dict(self.metadata))


def _base(topo: Topology) -> None:
    topo.add(("FLOWS", "request_load", "api_gateway"), request_load="LOAD", api_gateway="GATEWAY")
    topo.add(("DEPENDS", "api_service", "api_gateway"), api_service="SERVICE")


def from_contract(contract: dict, encoding: dict | None = None) -> Topology:
    """Pre-code topology: the declared HTTP surface plus what the requirements say about state and storage."""
    topo = Topology(metadata={"source": "contract", "endpoints": len(contract.get("api_endpoints", []))})
    if contract.get("api_endpoints"):
        _base(topo)
    for st in (encoding or {}).get("statements", []):
        pred, args = st["predicate"], st["args"]
        if pred == "HOLDS_IN_MEMORY" and len(args) == 2:
            store = _ident(args[1])
            _base(topo)
            topo.add(("SHARED", store, "request_handlers"), **{store: "RESOURCE", "request_handlers": "AGENT"})
            topo.add(("FLOWS", "request_load", store))
            _contention(topo, store)
        elif pred in ("PERSISTS", "DEPENDS_ON") and len(args) == 2:
            pool = f"{_ident(args[1])}_pool" if pred == "PERSISTS" else _ident(args[1])
            _base(topo)
            topo.add(("DEPENDS", "api_service", pool), **{pool: "POOL" if pred == "PERSISTS" else "RESOURCE"})
    return topo


def _contention(topo: Topology, store: str) -> None:
    """Requests flowing through a store shared by handlers raise contention on it (true by construction)."""
    contention = f"{store}_contention"
    topo.add(("CAUSES", ("FLOWS", "request_load", store), ("INCREASES", "request_load", contention)),
             **{contention: "SIGNAL"})


def from_codebase(codebase: dict) -> Topology:
    topo = Topology(metadata={"source": "codebase", "outbound_calls_without_timeout": []})
    modules = []
    for f in codebase.get("files", []):
        if f["path"].endswith(".py"):
            try:
                modules.append((f["path"], ast.parse(f["content"])))
            except SyntaxError:
                continue
    for path, tree in modules:
        _Visitor(topo, path).visit(tree)
    return topo


def _ident(text: str) -> str:
    out = "".join(c if c.isalnum() else "_" for c in text.lower()).strip("_")
    return out or "state"


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


class _Visitor(ast.NodeVisitor):
    def __init__(self, topo: Topology, path: str):
        self.topo = topo
        self.path = path
        self.clients: set[str] = set()
        self.containers: dict[str, str] = {}      # module-level name -> kind
        self.grows: set[str] = set()
        self.shrinks: set[str] = set()
        self.mutated_in_function: set[str] = set()

    # ---- module level ---------------------------------------------------
    def visit_Module(self, node: ast.Module) -> None:
        for stmt in node.body:
            if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                value = stmt.value
                targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                for t in targets:
                    if not isinstance(t, ast.Name) or value is None:
                        continue
                    if isinstance(value, (ast.Dict, ast.List, ast.Set)) or _call_name(value) in _CONTAINER_CALLS:
                        self.containers[t.id] = "container"
                    elif _call_name(value) in ("Queue", "PriorityQueue", "LifoQueue") and not _has_bound(value):
                        self.containers[t.id] = "queue"
                    elif _call_name(value) in _CLIENT_FACTORIES and _root_name(value) in _HTTP_MODULES:
                        self.clients.add(t.id)
            if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                for sub in ast.walk(stmt):
                    if isinstance(sub, ast.Call) and _call_name(sub) in _POOL_FACTORIES:
                        self._call(sub, retried=False)
        self.generic_visit(node)
        self._emit_state()

    def _emit_state(self) -> None:
        for name, kind in self.containers.items():
            ident = _ident(name)
            if kind == "queue":
                _base(self.topo)
                self.topo.add(("FLOWS", "request_load", ident), **{ident: "BUFFER"})
            elif name in self.mutated_in_function:
                _base(self.topo)
                self.topo.add(("SHARED", ident, "request_handlers"), **{ident: "RESOURCE", "request_handlers": "AGENT"})
                self.topo.add(("FLOWS", "request_load", ident))
                self._contention(ident)
                if name in self.grows and name not in self.shrinks:
                    entries = f"{ident}_entries"
                    self.topo.add(("CONTAINS", ident, entries), **{entries: "LOAD"})
                    self.topo.add(("CAUSES", ("FLOWS", "request_load", "api_gateway"),
                                   ("INCREASES", "request_load", entries)))

    # ---- functions --------------------------------------------------------
    def visit_FunctionDef(self, node) -> None:
        self._function(node)

    def visit_AsyncFunctionDef(self, node) -> None:
        self._function(node)

    def _contention(self, store: str) -> None:
        _contention(self.topo, store)

    def _function(self, node) -> None:
        decorators = {_call_name(d) for d in node.decorator_list}
        if decorators & _ROUTE_DECORATORS:
            _base(self.topo)
        retry_decorated = bool(decorators & _RETRY_DECORATORS)
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                self._call(sub, retried=retry_decorated or self._inside_retry_loop(node, sub))
                name = _call_name(sub)
                target = sub.func.value if isinstance(sub.func, ast.Attribute) else None
                if isinstance(target, ast.Name) and target.id in self.containers:
                    self.mutated_in_function.add(target.id)
                    if name in _MUTATORS_GROW:
                        self.grows.add(target.id)
                    elif name in _MUTATORS_SHRINK:
                        self.shrinks.add(target.id)
            elif isinstance(sub, (ast.Assign, ast.AugAssign, ast.Delete)):
                targets = sub.targets if isinstance(sub, (ast.Assign, ast.Delete)) else [sub.target]
                for t in targets:
                    if isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name) and t.value.id in self.containers:
                        self.mutated_in_function.add(t.value.id)
                        (self.shrinks if isinstance(sub, ast.Delete) else self.grows).add(t.value.id)

    def _inside_retry_loop(self, func, call: ast.Call) -> bool:
        for loop in ast.walk(func):
            if isinstance(loop, (ast.For, ast.While, ast.AsyncFor)):
                has_try = any(isinstance(n, ast.Try) for n in ast.walk(loop))
                if has_try and any(n is call for n in ast.walk(loop)):
                    return True
        return False

    def _call(self, call: ast.Call, retried: bool) -> None:
        name = _call_name(call)
        root = _root_name(call)
        if name in _POOL_FACTORIES:
            pool = f"{_ident(name)}_pool"
            _base(self.topo)
            self.topo.add(("DEPENDS", "api_service", pool), **{pool: "POOL"})
            return
        outbound = name in _HTTP_VERBS and (root in _HTTP_MODULES or root in self.clients)
        if not outbound:
            return
        _base(self.topo)
        self.topo.add(("DEPENDS", "api_service", "upstream_api"), upstream_api="RESOURCE")
        if not any(k.arg == "timeout" for k in call.keywords) and root in _HTTP_MODULES:
            self.topo.metadata["outbound_calls_without_timeout"].append(f"{self.path}:{call.lineno}")
        if retried:
            self.topo.add(("CAUSES", ("RETRIES", "api_service", "upstream_api"),
                           ("INCREASES", "request_load", "upstream_pressure")),
                          upstream_pressure="SIGNAL")


def _root_name(node: ast.AST) -> str:
    if isinstance(node, ast.Call):
        node = node.func
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else ""


def _has_bound(call: ast.AST) -> bool:
    if not isinstance(call, ast.Call):
        return True
    if any(k.arg == "maxsize" and not (isinstance(k.value, ast.Constant) and k.value.value in (0, None))
           for k in call.keywords):
        return True
    return bool(call.args) and not (isinstance(call.args[0], ast.Constant) and call.args[0].value in (0, None))
