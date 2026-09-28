from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Iterator

from pydantic import BaseModel, Field

from .graph import EvidenceGraph
from .models import Edge, Evidence, Node
from .structural import StructuralCase
from .structural_repository import HypothesisRunRecord


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def _dump(value: Any) -> str:
    if isinstance(value, BaseModel):
        return value.model_dump_json(exclude_none=False)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _load(value: str | bytes | None) -> Any:
    if value is None:
        return None
    if not isinstance(value, (str, bytes)):
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    return json.loads(value)


def _validate_model_payload(model: type[BaseModel], value: Any) -> BaseModel:
    if isinstance(value, (str, bytes)):
        return model.model_validate_json(value)
    return model.model_validate(value)


class AdvisorStat(BaseModel):
    query_class: str
    operation_id: str
    successes: int = Field(default=0, ge=0)
    failures: int = Field(default=0, ge=0)
    reward_sum: float = 0.0
    updated_at: datetime | None = None

    @property
    def observations(self) -> int:
        return self.successes + self.failures

    @property
    def mean_reward(self) -> float:
        return self.reward_sum / self.observations if self.observations else 0.0


class _ExecutionAdapter:
    def __init__(self, store: "SQLiteStore") -> None:
        self.store = store

    def save(self, execution_id: str, record: dict[str, Any]) -> None:
        self.store.save_execution(execution_id, record)

    def get(self, execution_id: str) -> dict[str, Any] | None:
        return self.store.get_execution(execution_id)

    def list(self) -> list[dict[str, Any]]:
        return self.store.list_executions()


class _ProfileAdapter:
    def __init__(self, store: "SQLiteStore") -> None:
        self.store = store

    def save(self, query_class: str, record: dict[str, Any]) -> None:
        self.store.save_profile(query_class, record)

    def get(self, query_class: str) -> dict[str, Any] | None:
        return self.store.get_profile(query_class)

    def list(self) -> list[dict[str, Any]]:
        return self.store.list_profiles()


class _AdvisorAdapter:
    def __init__(self, store: "SQLiteStore") -> None:
        self.store = store

    def save_stat(self, query_class: str, operation_id: str, record: dict[str, Any]) -> None:
        stat = AdvisorStat.model_validate({"query_class": query_class, "operation_id": operation_id, **record})
        self.store.save_advisor_stat(stat)

    def get_stat(self, query_class: str, operation_id: str) -> dict[str, Any] | None:
        stat = self.store.get_advisor_stat(query_class, operation_id)
        return stat.model_dump(mode="json") if stat else None

    def list_stats(self, query_class: str | None = None) -> list[dict[str, Any]]:
        return [stat.model_dump(mode="json") for stat in self.store.list_advisor_stats(query_class)]




class _StructuralCaseAdapter:
    def __init__(self, store: Any) -> None:
        self.store = store

    def save(self, case: StructuralCase) -> None:
        self.store.save_structural_case(case)

    def get(self, case_id: str) -> StructuralCase | None:
        return self.store.get_structural_case(case_id)

    def list(self, *, tenant_id: str | None = None, allowed_security_labels: set[str] | None = None) -> list[StructuralCase]:
        return self.store.list_structural_cases(tenant_id=tenant_id, allowed_security_labels=allowed_security_labels)


class _HypothesisRunAdapter:
    def __init__(self, store: Any) -> None:
        self.store = store

    def save(self, run: HypothesisRunRecord) -> None:
        self.store.save_hypothesis_run(run)

    def get(self, run_id: str) -> HypothesisRunRecord | None:
        return self.store.get_hypothesis_run(run_id)

    def list(self, *, tenant_id: str | None = None) -> list[HypothesisRunRecord]:
        return self.store.list_hypothesis_runs(tenant_id=tenant_id)


class SQLiteStore:
    SCHEMA_VERSION = "4"

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self._lock = RLock()
        self._transaction_depth = 0
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._initialize_schema()
        self.executions = _ExecutionAdapter(self)
        self.profiles = _ProfileAdapter(self)
        self.advisor = _AdvisorAdapter(self)
        self.structural_cases = _StructuralCaseAdapter(self)
        self.hypothesis_runs = _HypothesisRunAdapter(self)

    def _initialize_schema(self) -> None:
        ddl = [
            """CREATE TABLE IF NOT EXISTS ctd_nodes (
                id TEXT PRIMARY KEY, type TEXT NOT NULL, tenant_id TEXT,
                payload_json TEXT NOT NULL, updated_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS ctd_edges (
                id TEXT PRIMARY KEY, source TEXT NOT NULL, target TEXT NOT NULL,
                type TEXT NOT NULL, tenant_id TEXT, payload_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""",
            """CREATE INDEX IF NOT EXISTS idx_ctd_nodes_type ON ctd_nodes(type)""",
            """CREATE INDEX IF NOT EXISTS idx_ctd_edges_source_type ON ctd_edges(source, type)""",
            """CREATE INDEX IF NOT EXISTS idx_ctd_edges_target_type ON ctd_edges(target, type)""",
            """CREATE TABLE IF NOT EXISTS ctd_evidence (
                id TEXT PRIMARY KEY, payload_json TEXT NOT NULL, updated_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS ctd_claims (
                claim_key TEXT NOT NULL, sequence INTEGER NOT NULL,
                payload_json TEXT NOT NULL, PRIMARY KEY(claim_key, sequence)
            )""",
            """CREATE TABLE IF NOT EXISTS ctd_executions (
                execution_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL, created_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS ctd_profiles (
                query_class TEXT PRIMARY KEY, payload_json TEXT NOT NULL, updated_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS ctd_advisor (
                query_class TEXT NOT NULL, operation_id TEXT NOT NULL,
                successes INTEGER NOT NULL, failures INTEGER NOT NULL,
                reward_sum REAL NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(query_class, operation_id)
            )""",
            """CREATE TABLE IF NOT EXISTS ctd_structural_cases (
                id TEXT PRIMARY KEY, tenant_id TEXT, security_label TEXT,
                payload_json TEXT NOT NULL, updated_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS ctd_hypothesis_runs (
                run_id TEXT PRIMARY KEY, tenant_id TEXT, security_label TEXT,
                payload_json TEXT NOT NULL, created_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS ctd_meta (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            )""",
        ]
        with self._lock:
            for statement in ddl:
                self._conn.execute(statement)
            self._conn.execute(
                "INSERT INTO ctd_meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("schema_version", self.SCHEMA_VERSION),
            )
            self._conn.commit()

    @contextmanager
    def transaction(self) -> Iterator["SQLiteStore"]:
        with self._lock:
            outer = self._transaction_depth == 0
            if outer:
                self._conn.execute("BEGIN")
            self._transaction_depth += 1
            try:
                yield self
            except Exception:
                self._transaction_depth -= 1
                if outer:
                    self._conn.rollback()
                raise
            else:
                self._transaction_depth -= 1
                if outer:
                    self._conn.commit()

    def _write(self, sql: str, params: tuple[Any, ...]) -> sqlite3.Cursor:
        with self._lock:
            cursor = self._conn.execute(sql, params)
            if self._transaction_depth == 0:
                self._conn.commit()
            return cursor

    def _one(self, sql: str, params: tuple[Any, ...]) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def _all(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def save_node(self, node: Node) -> None:
        self._write(
            "INSERT INTO ctd_nodes(id,type,tenant_id,payload_json,updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET type=excluded.type, tenant_id=excluded.tenant_id, "
            "payload_json=excluded.payload_json, updated_at=excluded.updated_at",
            (node.id, node.type, node.attributes.get("tenant_id"), node.model_dump_json(), _now_iso()),
        )

    def get_node(self, node_id: str) -> Node | None:
        row = self._one("SELECT payload_json FROM ctd_nodes WHERE id=?", (node_id,))
        return Node.model_validate_json(row["payload_json"]) if row else None

    def list_nodes(self, node_type: str | None = None) -> list[Node]:
        rows = (
            self._all("SELECT payload_json FROM ctd_nodes WHERE type=? ORDER BY id", (node_type,))
            if node_type is not None
            else self._all("SELECT payload_json FROM ctd_nodes ORDER BY id")
        )
        return [Node.model_validate_json(row["payload_json"]) for row in rows]

    def save_edge(self, edge: Edge) -> None:
        self._write(
            "INSERT INTO ctd_edges(id,source,target,type,tenant_id,payload_json,updated_at) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET source=excluded.source,target=excluded.target,type=excluded.type,"
            "tenant_id=excluded.tenant_id,payload_json=excluded.payload_json,updated_at=excluded.updated_at",
            (
                edge.id,
                edge.source,
                edge.target,
                edge.type,
                edge.attributes.get("tenant_id"),
                edge.model_dump_json(),
                _now_iso(),
            ),
        )
        for evidence in edge.evidence:
            self.save_evidence(evidence)

    def get_edge(self, edge_id: str) -> Edge | None:
        row = self._one("SELECT payload_json FROM ctd_edges WHERE id=?", (edge_id,))
        return Edge.model_validate_json(row["payload_json"]) if row else None

    def list_edges(self) -> list[Edge]:
        return [Edge.model_validate_json(row["payload_json"]) for row in self._all("SELECT payload_json FROM ctd_edges ORDER BY id")]

    def save_evidence(self, evidence: Evidence) -> None:
        self._write(
            "INSERT INTO ctd_evidence(id,payload_json,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET payload_json=excluded.payload_json,updated_at=excluded.updated_at",
            (evidence.id, evidence.model_dump_json(), _now_iso()),
        )

    def get_evidence(self, evidence_id: str) -> Evidence | None:
        row = self._one("SELECT payload_json FROM ctd_evidence WHERE id=?", (evidence_id,))
        return Evidence.model_validate_json(row["payload_json"]) if row else None

    def save_claim(self, claim_key: str, record: dict[str, Any]) -> None:
        row = self._one(
            "SELECT COALESCE(MAX(sequence),0)+1 AS next_sequence FROM ctd_claims WHERE claim_key=?",
            (claim_key,),
        )
        sequence = int(row["next_sequence"]) if row else 1
        self._write(
            "INSERT INTO ctd_claims(claim_key,sequence,payload_json) VALUES(?,?,?)",
            (claim_key, sequence, _dump(record)),
        )

    def get_claims(self, claim_key: str) -> list[dict[str, Any]]:
        return [
            _load(row["payload_json"])
            for row in self._all(
                "SELECT payload_json FROM ctd_claims WHERE claim_key=? ORDER BY sequence", (claim_key,)
            )
        ]

    def save_execution(self, execution_id: str, record: dict[str, Any]) -> None:
        try:
            self._write(
                "INSERT INTO ctd_executions(execution_id,payload_json,created_at) VALUES(?,?,?)",
                (execution_id, _dump(record), _now_iso()),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"execution already exists: {execution_id}") from exc

    def get_execution(self, execution_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT payload_json FROM ctd_executions WHERE execution_id=?", (execution_id,))
        return _load(row["payload_json"]) if row else None

    def list_executions(self) -> list[dict[str, Any]]:
        return [_load(row["payload_json"]) for row in self._all("SELECT payload_json FROM ctd_executions ORDER BY created_at, execution_id")]

    def save_profile(self, query_class: str, record: dict[str, Any]) -> None:
        self._write(
            "INSERT INTO ctd_profiles(query_class,payload_json,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(query_class) DO UPDATE SET payload_json=excluded.payload_json,updated_at=excluded.updated_at",
            (query_class, _dump(record), _now_iso()),
        )

    def get_profile(self, query_class: str) -> dict[str, Any] | None:
        row = self._one("SELECT payload_json FROM ctd_profiles WHERE query_class=?", (query_class,))
        return _load(row["payload_json"]) if row else None

    def list_profiles(self) -> list[dict[str, Any]]:
        return [_load(row["payload_json"]) for row in self._all("SELECT payload_json FROM ctd_profiles ORDER BY query_class")]

    def save_advisor_stat(self, stat: AdvisorStat) -> None:
        updated = stat.model_copy(update={"updated_at": stat.updated_at or datetime.now(tz=UTC)})
        self._write(
            "INSERT INTO ctd_advisor(query_class,operation_id,successes,failures,reward_sum,updated_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(query_class,operation_id) DO UPDATE SET successes=excluded.successes,failures=excluded.failures,"
            "reward_sum=excluded.reward_sum,updated_at=excluded.updated_at",
            (
                updated.query_class,
                updated.operation_id,
                updated.successes,
                updated.failures,
                updated.reward_sum,
                updated.updated_at.isoformat(),
            ),
        )

    def get_advisor_stat(self, query_class: str, operation_id: str) -> AdvisorStat | None:
        row = self._one(
            "SELECT query_class,operation_id,successes,failures,reward_sum,updated_at FROM ctd_advisor "
            "WHERE query_class=? AND operation_id=?",
            (query_class, operation_id),
        )
        if not row:
            return None
        return AdvisorStat(
            query_class=row["query_class"],
            operation_id=row["operation_id"],
            successes=row["successes"],
            failures=row["failures"],
            reward_sum=row["reward_sum"],
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def list_advisor_stats(self, query_class: str | None = None) -> list[AdvisorStat]:
        rows = (
            self._all(
                "SELECT query_class,operation_id,successes,failures,reward_sum,updated_at FROM ctd_advisor WHERE query_class=? ORDER BY operation_id",
                (query_class,),
            )
            if query_class is not None
            else self._all(
                "SELECT query_class,operation_id,successes,failures,reward_sum,updated_at FROM ctd_advisor ORDER BY query_class,operation_id"
            )
        )
        return [
            AdvisorStat(
                query_class=row["query_class"],
                operation_id=row["operation_id"],
                successes=row["successes"],
                failures=row["failures"],
                reward_sum=row["reward_sum"],
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )
            for row in rows
        ]

    def save_structural_case(self, case: StructuralCase) -> None:
        self._write(
            "INSERT INTO ctd_structural_cases(id,tenant_id,security_label,payload_json,updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET tenant_id=excluded.tenant_id,security_label=excluded.security_label,payload_json=excluded.payload_json,updated_at=excluded.updated_at",
            (case.id, case.tenant_id, case.security_label, case.model_dump_json(), _now_iso()),
        )

    def get_structural_case(self, case_id: str) -> StructuralCase | None:
        row = self._one("SELECT payload_json FROM ctd_structural_cases WHERE id=?", (case_id,))
        return StructuralCase.model_validate_json(row["payload_json"]) if row else None

    def list_structural_cases(self, *, tenant_id: str | None = None, allowed_security_labels: set[str] | None = None) -> list[StructuralCase]:
        rows = self._all("SELECT payload_json FROM ctd_structural_cases ORDER BY id")
        cases = [StructuralCase.model_validate_json(row["payload_json"]) for row in rows]
        return [case for case in cases if (tenant_id is None or case.tenant_id in (None, tenant_id)) and (allowed_security_labels is None or case.security_label is None or case.security_label in allowed_security_labels)]

    def save_hypothesis_run(self, run: HypothesisRunRecord) -> None:
        self._write(
            "INSERT INTO ctd_hypothesis_runs(run_id,tenant_id,security_label,payload_json,created_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(run_id) DO UPDATE SET tenant_id=excluded.tenant_id,security_label=excluded.security_label,payload_json=excluded.payload_json,created_at=excluded.created_at",
            (run.run_id, run.tenant_id, run.security_label, run.model_dump_json(), run.created_at.isoformat()),
        )

    def get_hypothesis_run(self, run_id: str) -> HypothesisRunRecord | None:
        row = self._one("SELECT payload_json FROM ctd_hypothesis_runs WHERE run_id=?", (run_id,))
        return HypothesisRunRecord.model_validate_json(row["payload_json"]) if row else None

    def list_hypothesis_runs(self, *, tenant_id: str | None = None) -> list[HypothesisRunRecord]:
        rows = self._all("SELECT payload_json FROM ctd_hypothesis_runs ORDER BY created_at,run_id")
        runs = [HypothesisRunRecord.model_validate_json(row["payload_json"]) for row in rows]
        return [run for run in runs if tenant_id is None or run.tenant_id in (None, tenant_id)]

    def load_graph(self) -> EvidenceGraph:
        graph = EvidenceGraph()
        for node in self.list_nodes():
            graph.add_node(node)
        for edge in self.list_edges():
            if graph.get_node(edge.source) is not None and graph.get_node(edge.target) is not None:
                graph.add_edge(edge)
        return graph

    def ping(self) -> bool:
        try:
            return self._one("SELECT 1 AS ok", ()) is not None
        except sqlite3.Error:
            return False

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class PostgresStore:
    """PostgreSQL persistence adapter with the SQLite logical contract.

    All SQL identifiers are fixed by this module and all external values are
    passed as DB-API parameters. The optional psycopg dependency is imported
    only when this backend is actually configured.
    """

    SCHEMA_VERSION = "4"

    def __init__(
        self,
        dsn: str,
        *,
        connection_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.dsn = dsn
        self._lock = RLock()
        self._transaction_depth = 0
        if connection_factory is None:
            try:
                import psycopg  # type: ignore
                from psycopg.rows import dict_row  # type: ignore
            except ImportError as exc:
                raise RuntimeError("PostgreSQL support requires the 'postgres' optional extra") from exc
            connection_factory = lambda: psycopg.connect(dsn, row_factory=dict_row)
        self._conn = connection_factory()
        self._initialize_schema()
        self.executions = _ExecutionAdapter(self)  # type: ignore[arg-type]
        self.profiles = _ProfileAdapter(self)  # type: ignore[arg-type]
        self.advisor = _AdvisorAdapter(self)  # type: ignore[arg-type]
        self.structural_cases = _StructuralCaseAdapter(self)
        self.hypothesis_runs = _HypothesisRunAdapter(self)

    @staticmethod
    def _row_value(row: Any, key: str, index: int = 0) -> Any:
        if row is None:
            return None
        if isinstance(row, dict):
            return row.get(key)
        try:
            return row[key]
        except (TypeError, KeyError, IndexError):
            return row[index]

    def _initialize_schema(self) -> None:
        statements = [
            "CREATE TABLE IF NOT EXISTS ctd_nodes (id TEXT PRIMARY KEY, type TEXT NOT NULL, tenant_id TEXT, payload_json JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL)",
            "CREATE INDEX IF NOT EXISTS idx_ctd_nodes_type ON ctd_nodes(type)",
            "CREATE TABLE IF NOT EXISTS ctd_edges (id TEXT PRIMARY KEY, source TEXT NOT NULL, target TEXT NOT NULL, type TEXT NOT NULL, tenant_id TEXT, payload_json JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL)",
            "CREATE INDEX IF NOT EXISTS idx_ctd_edges_source_type ON ctd_edges(source, type)",
            "CREATE INDEX IF NOT EXISTS idx_ctd_edges_target_type ON ctd_edges(target, type)",
            "CREATE TABLE IF NOT EXISTS ctd_evidence (id TEXT PRIMARY KEY, payload_json JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL)",
            "CREATE TABLE IF NOT EXISTS ctd_claims (claim_key TEXT NOT NULL, sequence INTEGER NOT NULL, payload_json JSONB NOT NULL, PRIMARY KEY(claim_key, sequence))",
            "CREATE TABLE IF NOT EXISTS ctd_executions (execution_id TEXT PRIMARY KEY, payload_json JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL)",
            "CREATE TABLE IF NOT EXISTS ctd_profiles (query_class TEXT PRIMARY KEY, payload_json JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL)",
            "CREATE TABLE IF NOT EXISTS ctd_advisor (query_class TEXT NOT NULL, operation_id TEXT NOT NULL, successes INTEGER NOT NULL, failures INTEGER NOT NULL, reward_sum DOUBLE PRECISION NOT NULL, updated_at TIMESTAMPTZ NOT NULL, PRIMARY KEY(query_class, operation_id))",
            "CREATE TABLE IF NOT EXISTS ctd_structural_cases (id TEXT PRIMARY KEY, tenant_id TEXT, security_label TEXT, payload_json JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL)",
            "CREATE TABLE IF NOT EXISTS ctd_hypothesis_runs (run_id TEXT PRIMARY KEY, tenant_id TEXT, security_label TEXT, payload_json JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL)",
            "CREATE TABLE IF NOT EXISTS ctd_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        ]
        try:
            with self._lock, self._conn.cursor() as cursor:
                for statement in statements:
                    cursor.execute(statement)
                cursor.execute(
                    "INSERT INTO ctd_meta(key,value) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
                    ("schema_version", self.SCHEMA_VERSION),
                )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    @contextmanager
    def transaction(self) -> Iterator["PostgresStore"]:
        with self._lock:
            outer = self._transaction_depth == 0
            self._transaction_depth += 1
            try:
                yield self
            except Exception:
                self._transaction_depth -= 1
                if outer:
                    self._conn.rollback()
                raise
            else:
                self._transaction_depth -= 1
                if outer:
                    self._conn.commit()

    def _write(self, sql: str, params: tuple[Any, ...]) -> None:
        try:
            with self._lock, self._conn.cursor() as cursor:
                cursor.execute(sql, params)
            if self._transaction_depth == 0:
                self._conn.commit()
        except Exception:
            if self._transaction_depth == 0:
                self._conn.rollback()
            raise

    def _one(self, sql: str, params: tuple[Any, ...]) -> Any | None:
        try:
            with self._lock, self._conn.cursor() as cursor:
                cursor.execute(sql, params)
                return cursor.fetchone()
        except Exception:
            if self._transaction_depth == 0:
                self._conn.rollback()
            raise

    def _all(self, sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
        try:
            with self._lock, self._conn.cursor() as cursor:
                cursor.execute(sql, params)
                return list(cursor.fetchall())
        except Exception:
            if self._transaction_depth == 0:
                self._conn.rollback()
            raise

    def save_node(self, node: Node) -> None:
        self._write(
            "INSERT INTO ctd_nodes(id,type,tenant_id,payload_json,updated_at) VALUES(%s,%s,%s,CAST(%s AS JSONB),%s) "
            "ON CONFLICT(id) DO UPDATE SET type=EXCLUDED.type,tenant_id=EXCLUDED.tenant_id,"
            "payload_json=EXCLUDED.payload_json,updated_at=EXCLUDED.updated_at",
            (node.id, node.type, node.attributes.get("tenant_id"), node.model_dump_json(), datetime.now(tz=UTC)),
        )

    def get_node(self, node_id: str) -> Node | None:
        row = self._one("SELECT payload_json FROM ctd_nodes WHERE id=%s", (node_id,))
        payload = self._row_value(row, "payload_json")
        return _validate_model_payload(Node, payload) if payload is not None else None  # type: ignore[return-value]

    def list_nodes(self, node_type: str | None = None) -> list[Node]:
        rows = (
            self._all("SELECT payload_json FROM ctd_nodes WHERE type=%s ORDER BY id", (node_type,))
            if node_type is not None
            else self._all("SELECT payload_json FROM ctd_nodes ORDER BY id")
        )
        return [_validate_model_payload(Node, self._row_value(row, "payload_json")) for row in rows]  # type: ignore[misc]

    def save_edge(self, edge: Edge) -> None:
        self._write(
            "INSERT INTO ctd_edges(id,source,target,type,tenant_id,payload_json,updated_at) VALUES(%s,%s,%s,%s,%s,CAST(%s AS JSONB),%s) "
            "ON CONFLICT(id) DO UPDATE SET source=EXCLUDED.source,target=EXCLUDED.target,type=EXCLUDED.type,"
            "tenant_id=EXCLUDED.tenant_id,payload_json=EXCLUDED.payload_json,updated_at=EXCLUDED.updated_at",
            (edge.id, edge.source, edge.target, edge.type, edge.attributes.get("tenant_id"), edge.model_dump_json(), datetime.now(tz=UTC)),
        )
        for evidence in edge.evidence:
            self.save_evidence(evidence)

    def get_edge(self, edge_id: str) -> Edge | None:
        row = self._one("SELECT payload_json FROM ctd_edges WHERE id=%s", (edge_id,))
        payload = self._row_value(row, "payload_json")
        return _validate_model_payload(Edge, payload) if payload is not None else None  # type: ignore[return-value]

    def list_edges(self) -> list[Edge]:
        return [
            _validate_model_payload(Edge, self._row_value(row, "payload_json"))
            for row in self._all("SELECT payload_json FROM ctd_edges ORDER BY id")
        ]  # type: ignore[misc]

    def save_evidence(self, evidence: Evidence) -> None:
        self._write(
            "INSERT INTO ctd_evidence(id,payload_json,updated_at) VALUES(%s,CAST(%s AS JSONB),%s) "
            "ON CONFLICT(id) DO UPDATE SET payload_json=EXCLUDED.payload_json,updated_at=EXCLUDED.updated_at",
            (evidence.id, evidence.model_dump_json(), datetime.now(tz=UTC)),
        )

    def get_evidence(self, evidence_id: str) -> Evidence | None:
        row = self._one("SELECT payload_json FROM ctd_evidence WHERE id=%s", (evidence_id,))
        payload = self._row_value(row, "payload_json")
        return _validate_model_payload(Evidence, payload) if payload is not None else None  # type: ignore[return-value]

    def save_claim(self, claim_key: str, record: dict[str, Any]) -> None:
        with self.transaction():
            row = self._one(
                "SELECT COALESCE(MAX(sequence),0)+1 AS next_sequence FROM ctd_claims WHERE claim_key=%s",
                (claim_key,),
            )
            sequence = int(self._row_value(row, "next_sequence") or 1)
            self._write(
                "INSERT INTO ctd_claims(claim_key,sequence,payload_json) VALUES(%s,%s,CAST(%s AS JSONB))",
                (claim_key, sequence, _dump(record)),
            )

    def get_claims(self, claim_key: str) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT payload_json FROM ctd_claims WHERE claim_key=%s ORDER BY sequence",
            (claim_key,),
        )
        return [_load(self._row_value(row, "payload_json")) for row in rows]

    def save_execution(self, execution_id: str, record: dict[str, Any]) -> None:
        if self.get_execution(execution_id) is not None:
            raise ValueError(f"execution already exists: {execution_id}")
        try:
            self._write(
                "INSERT INTO ctd_executions(execution_id,payload_json,created_at) VALUES(%s,CAST(%s AS JSONB),%s)",
                (execution_id, _dump(record), datetime.now(tz=UTC)),
            )
        except Exception:
            if self._transaction_depth == 0:
                self._conn.rollback()
            raise

    def get_execution(self, execution_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT payload_json FROM ctd_executions WHERE execution_id=%s", (execution_id,))
        payload = self._row_value(row, "payload_json")
        return _load(payload) if payload is not None else None

    def list_executions(self) -> list[dict[str, Any]]:
        return [
            _load(self._row_value(row, "payload_json"))
            for row in self._all("SELECT payload_json FROM ctd_executions ORDER BY created_at,execution_id")
        ]

    def save_profile(self, query_class: str, record: dict[str, Any]) -> None:
        self._write(
            "INSERT INTO ctd_profiles(query_class,payload_json,updated_at) VALUES(%s,CAST(%s AS JSONB),%s) "
            "ON CONFLICT(query_class) DO UPDATE SET payload_json=EXCLUDED.payload_json,updated_at=EXCLUDED.updated_at",
            (query_class, _dump(record), datetime.now(tz=UTC)),
        )

    def get_profile(self, query_class: str) -> dict[str, Any] | None:
        row = self._one("SELECT payload_json FROM ctd_profiles WHERE query_class=%s", (query_class,))
        payload = self._row_value(row, "payload_json")
        return _load(payload) if payload is not None else None

    def list_profiles(self) -> list[dict[str, Any]]:
        return [
            _load(self._row_value(row, "payload_json"))
            for row in self._all("SELECT payload_json FROM ctd_profiles ORDER BY query_class")
        ]

    def save_advisor_stat(self, stat: AdvisorStat) -> None:
        updated = stat.updated_at or datetime.now(tz=UTC)
        self._write(
            "INSERT INTO ctd_advisor(query_class,operation_id,successes,failures,reward_sum,updated_at) VALUES(%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT(query_class,operation_id) DO UPDATE SET successes=EXCLUDED.successes,failures=EXCLUDED.failures,reward_sum=EXCLUDED.reward_sum,updated_at=EXCLUDED.updated_at",
            (stat.query_class, stat.operation_id, stat.successes, stat.failures, stat.reward_sum, updated),
        )

    def get_advisor_stat(self, query_class: str, operation_id: str) -> AdvisorStat | None:
        row = self._one(
            "SELECT query_class,operation_id,successes,failures,reward_sum,updated_at FROM ctd_advisor WHERE query_class=%s AND operation_id=%s",
            (query_class, operation_id),
        )
        if row is None:
            return None
        if isinstance(row, dict):
            payload = dict(row)
        else:
            payload = {
                "query_class": row[0], "operation_id": row[1], "successes": row[2],
                "failures": row[3], "reward_sum": row[4], "updated_at": row[5],
            }
        return AdvisorStat.model_validate(payload)

    def list_advisor_stats(self, query_class: str | None = None) -> list[AdvisorStat]:
        if query_class is None:
            rows = self._all(
                "SELECT query_class,operation_id,successes,failures,reward_sum,updated_at FROM ctd_advisor ORDER BY query_class,operation_id"
            )
        else:
            rows = self._all(
                "SELECT query_class,operation_id,successes,failures,reward_sum,updated_at FROM ctd_advisor WHERE query_class=%s ORDER BY operation_id",
                (query_class,),
            )
        result: list[AdvisorStat] = []
        for row in rows:
            if isinstance(row, dict):
                payload = dict(row)
            else:
                payload = {
                    "query_class": row[0], "operation_id": row[1], "successes": row[2],
                    "failures": row[3], "reward_sum": row[4], "updated_at": row[5],
                }
            result.append(AdvisorStat.model_validate(payload))
        return result

    def save_structural_case(self, case: StructuralCase) -> None:
        self._write(
            "INSERT INTO ctd_structural_cases(id,tenant_id,security_label,payload_json,updated_at) VALUES(%s,%s,%s,CAST(%s AS JSONB),%s) ON CONFLICT(id) DO UPDATE SET tenant_id=EXCLUDED.tenant_id,security_label=EXCLUDED.security_label,payload_json=EXCLUDED.payload_json,updated_at=EXCLUDED.updated_at",
            (case.id, case.tenant_id, case.security_label, case.model_dump_json(), datetime.now(tz=UTC)),
        )

    def get_structural_case(self, case_id: str) -> StructuralCase | None:
        row = self._one("SELECT payload_json FROM ctd_structural_cases WHERE id=%s", (case_id,))
        payload = self._row_value(row, "payload_json")
        return _validate_model_payload(StructuralCase, payload) if payload is not None else None

    def list_structural_cases(self, *, tenant_id: str | None = None, allowed_security_labels: set[str] | None = None) -> list[StructuralCase]:
        rows = self._all("SELECT payload_json FROM ctd_structural_cases ORDER BY id")
        cases = [_validate_model_payload(StructuralCase, self._row_value(row, "payload_json")) for row in rows]
        return [case for case in cases if (tenant_id is None or case.tenant_id in (None, tenant_id)) and (allowed_security_labels is None or case.security_label is None or case.security_label in allowed_security_labels)]

    def save_hypothesis_run(self, run: HypothesisRunRecord) -> None:
        self._write(
            "INSERT INTO ctd_hypothesis_runs(run_id,tenant_id,security_label,payload_json,created_at) VALUES(%s,%s,%s,CAST(%s AS JSONB),%s) ON CONFLICT(run_id) DO UPDATE SET tenant_id=EXCLUDED.tenant_id,security_label=EXCLUDED.security_label,payload_json=EXCLUDED.payload_json,created_at=EXCLUDED.created_at",
            (run.run_id, run.tenant_id, run.security_label, run.model_dump_json(), run.created_at),
        )

    def get_hypothesis_run(self, run_id: str) -> HypothesisRunRecord | None:
        row = self._one("SELECT payload_json FROM ctd_hypothesis_runs WHERE run_id=%s", (run_id,))
        payload = self._row_value(row, "payload_json")
        return _validate_model_payload(HypothesisRunRecord, payload) if payload is not None else None

    def list_hypothesis_runs(self, *, tenant_id: str | None = None) -> list[HypothesisRunRecord]:
        rows = self._all("SELECT payload_json FROM ctd_hypothesis_runs ORDER BY created_at,run_id")
        runs = [_validate_model_payload(HypothesisRunRecord, self._row_value(row, "payload_json")) for row in rows]
        return [run for run in runs if tenant_id is None or run.tenant_id in (None, tenant_id)]

    def load_graph(self) -> EvidenceGraph:
        graph = EvidenceGraph()
        for node in self.list_nodes():
            graph.add_node(node)
        for edge in self.list_edges():
            graph.add_edge(edge)
        return graph

    def ping(self) -> bool:
        try:
            with self._lock, self._conn.cursor() as cursor:
                cursor.execute("SELECT 1")
                if hasattr(cursor, "fetchone"):
                    cursor.fetchone()
            return True
        except Exception:
            return False

    def close(self) -> None:
        self._conn.close()
