"""Layer 1 — Topological store.

Holds records that carry two parallel descriptions of the same thing:

    attrs   flat, indexed, cheap to filter      -> drives CLOSE
    rels    relational structure, expensive     -> drives TRANSFER

Both modes read the same store. That is the point of one engine rather
than two.

Design notes worth keeping honest:

  * There is no dense adjacency matrix. An adjacency matrix over n nodes is
    O(n^2) memory and cannot be maintained at ingest rate. Set membership
    is held as bitmaps over an inverted index, which is what actually makes
    "mask out the irrelevant region" a free operation.

  * Enrichment happens at WRITE time. The cheap tier can only filter on
    attributes somebody materialised during ingestion. This is the single
    precondition the whole architecture rests on: if nothing was extracted
    at write time, every query degrades to a full scan plus model calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator, Sequence


# --------------------------------------------------------------------------
# Relations
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Rel:
    """A relation over entities (str) or other relations (Rel).

    Nesting is what carries causal structure. A store of flat facts supports
    CLOSE but cannot support TRANSFER, because there is no higher-order
    structure to align on.
    """
    pred: str
    args: tuple[Any, ...]

    @property
    def order(self) -> int:
        sub = [a.order for a in self.args if isinstance(a, Rel)]
        return 1 + (max(sub) if sub else 0)

    def __str__(self) -> str:
        return f"{self.pred}({', '.join(str(a) for a in self.args)})"


def R(pred: str, *args: Any) -> Rel:
    return Rel(pred, tuple(args))


def walk(rels: Iterable[Rel]) -> list[Rel]:
    """Every relation including nested sub-relations, de-duplicated."""
    out: list[Rel] = []
    seen: set[Rel] = set()

    def rec(r: Rel) -> None:
        if r not in seen:
            seen.add(r)
            out.append(r)
        for a in r.args:
            if isinstance(a, Rel):
                rec(a)

    for r in rels:
        rec(r)
    return out


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------

@dataclass
class Record:
    id: str
    domain: str
    attrs: dict[str, Any] = field(default_factory=dict)
    rels: tuple[Rel, ...] = ()
    types: dict[str, str] = field(default_factory=dict)
    text: str = ""

    def all_rels(self) -> list[Rel]:
        return walk(self.rels)

    def predicates(self) -> set[str]:
        return {r.pred for r in self.all_rels()}

    def entities(self) -> set[str]:
        return {a for r in self.all_rels() for a in r.args
                if isinstance(a, str)}


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------

class TopoStore:
    """Bitmap-indexed record store with write-time enrichment."""

    def __init__(self) -> None:
        self.records: list[Record] = []
        self.pos: dict[str, int] = {}
        self._idx: dict[str, dict[Any, int]] = {}
        self._enrichers: list[tuple[str, Callable[[Record], Any]]] = []
        # telemetry
        self.probes = 0          # index lookups (cheap)
        self.reads = 0           # records materialised (expensive)

    # ---- ingestion ----------------------------------------------------

    def enricher(self, attr: str, fn: Callable[[Record], Any]) -> None:
        """Register a write-time derivation. Applied to every record
        ingested afterwards, and retroactively to what is already stored."""
        self._enrichers.append((attr, fn))
        for rec in self.records:
            rec.attrs.setdefault(attr, fn(rec))
        for a in list(self._idx):
            self._rebuild(a)

    def declare_index(self, attr: str) -> None:
        self._rebuild(attr)

    def ingest(self, rec: Record) -> Record:
        for attr, fn in self._enrichers:
            rec.attrs.setdefault(attr, fn(rec))
        i = len(self.records)
        self.records.append(rec)
        self.pos[rec.id] = i
        for attr, table in self._idx.items():
            for k in self._keys(rec, attr):
                table[k] = table.get(k, 0) | (1 << i)
        return rec

    def ingest_all(self, recs: Iterable[Record]) -> None:
        for r in recs:
            self.ingest(r)

    # ---- indices ------------------------------------------------------

    @staticmethod
    def _keys(rec: Record, attr: str) -> list[Any]:
        """Attributes first, then record fields.

        Without the fallback, indexing a first-class field like `domain`
        builds a silently empty index and every query using it returns
        nothing -- a failure that looks exactly like "no match".
        """
        v = rec.attrs.get(attr, getattr(rec, attr, None))
        if v is None:
            return []
        if isinstance(v, (list, tuple, set, frozenset)):
            return list(v)
        return [v]

    def _rebuild(self, attr: str) -> None:
        table: dict[Any, int] = {}
        for i, rec in enumerate(self.records):
            for k in self._keys(rec, attr):
                table[k] = table.get(k, 0) | (1 << i)
        self._idx[attr] = table

    def indexed(self) -> set[str]:
        return set(self._idx)

    def bitmap(self, attr: str, key: Any) -> int:
        self.probes += 1
        return self._idx.get(attr, {}).get(key, 0)

    @property
    def all_bits(self) -> int:
        return (1 << len(self.records)) - 1

    @staticmethod
    def count(bitmap: int) -> int:
        return bin(bitmap).count("1")

    def materialise(self, bitmap: int) -> list[Record]:
        out = [r for i, r in enumerate(self.records) if bitmap >> i & 1]
        self.reads += len(out)
        return out

    def bits_for(self, recs: Sequence[Record]) -> int:
        acc = 0
        for r in recs:
            acc |= 1 << self.pos[r.id]
        return acc

    # ---- partitioning -------------------------------------------------

    def partition_by(self, attr: str) -> dict[Any, list[str]]:
        """Shard plan.

        Partition on the highest-selectivity queried attribute, not on graph
        density. Clustering by edge weight optimises for query locality, but
        constraint queries are usually cross-cutting, so density clustering
        makes them span every shard. This returns the plan only; placement
        is the caller's problem.
        """
        out: dict[Any, list[str]] = {}
        for rec in self.records:
            for k in self._keys(rec, attr):
                out.setdefault(k, []).append(rec.id)
        return out

    def reset_counters(self) -> None:
        self.probes = 0
        self.reads = 0

    def __len__(self) -> int:
        return len(self.records)

    def __iter__(self) -> Iterator[Record]:
        return iter(self.records)
