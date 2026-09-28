"""Layer 1 — the record store.

Records carry two parallel descriptions of the same thing:

    attrs   flat, indexed, cheap to filter       -> drives CLOSE
    rels    relational structure, expensive      -> drives TRANSFER

Both modes read the same store. That is the point of one engine rather than
two, and it is unchanged from v4.

What changed, and why
---------------------

**Index construction is no longer quadratic.** v4 did `table[k] |= 1 << i` per
record per key, which allocates an i-bit integer each time: ingesting n records
costs O(n^2/64) word operations, and materialising a result set cost another
O(n^2) because `bitmap >> i` re-allocated on every loop iteration. Postings now
accumulate as plain lists and are converted to bitmaps lazily and in one pass
(see `bitset.py`). Measured effect is in `bench/bench_scale.py`.

**An unindexed attribute is now an error, not an empty result.** v4's
`bitmap()` returned 0 for an attribute with no index, which is indistinguishable
from "nothing matches" — and with `negate=True` it returned *everything*, so a
typo in an attribute name silently inverted a filter. Both now raise
`UnindexedAttribute`.

**Records are identified by id.** v4 used dataclass field-wise equality, so
`record in list` deep-compared every attribute dict, and two structurally
identical records compared equal. Identity is now the id.

**Deletion exists.** Tombstones keep row positions stable — which is what makes
the bitmap index safe to hold across mutation — while removing the record from
every future result set.

**Relations are validated on construction.** `R("CAUSES", "x")` and
`R("FLOWS", ["a"])` were both accepted by v4 and failed later, somewhere else.

Still deliberately absent: a dense adjacency matrix. It is O(n^2) memory and
cannot be maintained at ingest rate. Set membership over an inverted index is
what actually makes "mask out the irrelevant region" a free operation.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from . import bitset

__all__ = [
    "Rel", "R", "walk", "Record", "TopoStore",
    "UnindexedAttribute", "MalformedRelation",
]

MAX_RELATION_ORDER = 16
"""Nesting depth beyond which a relation is refused.

Not a capacity limit — real causal encodings are 2 to 4 deep. It is a guard
against a malformed or adversarial input driving the recursive walkers
(`walk`, `gloss`, `order`, `_substitute`) into a stack overflow, which is an
interpreter-level crash rather than a handled error.
"""

FORMAT_VERSION = 5


class UnindexedAttribute(KeyError):
    """Raised when a query pushes down a predicate on an unindexed attribute.

    v4 returned an empty bitmap here, which reads downstream as 'no match' and
    is the single easiest way to get a confidently wrong empty answer.
    """


class MalformedRelation(TypeError):
    """Raised when a relation's arguments are not entities or relations."""


# --------------------------------------------------------------------------
# Relations
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Rel:
    """A relation over entities (str) or other relations (Rel).

    Nesting is what carries causal structure. A store of flat facts supports
    CLOSE but cannot support TRANSFER, because there is nothing higher-order
    to align on.
    """
    pred: str
    args: tuple[Any, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.pred, str) or not self.pred:
            raise MalformedRelation("predicate must be a non-empty string")
        if not self.args:
            raise MalformedRelation(f"{self.pred} has no arguments")
        for i, a in enumerate(self.args):
            if not isinstance(a, (str, Rel)):
                raise MalformedRelation(
                    f"{self.pred} argument {i} is {type(a).__name__}; "
                    f"arguments must be entity names or nested relations")
            if isinstance(a, str) and not a:
                raise MalformedRelation(
                    f"{self.pred} argument {i} is an empty entity name")
        # Order is computed once at construction and cached. It is read on
        # every compatibility test, every score, and every sort key in the
        # aligner; recomputing it recursively each time made the hot loop
        # quadratic in nesting depth for no reason.
        sub = [a.order for a in self.args if isinstance(a, Rel)]
        order = 1 + (max(sub) if sub else 0)
        if order > MAX_RELATION_ORDER:
            raise MalformedRelation(
                f"{self.pred} nests {order} deep; the limit is "
                f"{MAX_RELATION_ORDER}. Real causal encodings are 2-4 deep, so "
                f"this is a malformed input, and the recursive walkers would "
                f"overflow the stack on it.")
        object.__setattr__(self, "_order", order)

    @property
    def order(self) -> int:
        return self._order

    @property
    def arity(self) -> int:
        return len(self.args)

    def entities(self) -> set[str]:
        out: set[str] = set()
        for a in self.args:
            if isinstance(a, Rel):
                out |= a.entities()
            else:
                out.add(a)
        return out

    def signature_key(self) -> str:
        """Structure-only fingerprint: predicates and nesting, entities erased.

        Two relations with the same signature_key describe the same shape over
        different subject matter. Used for near-duplicate detection at ingest.
        """
        parts = [a.signature_key() if isinstance(a, Rel) else "_"
                 for a in self.args]
        return f"{self.pred}({','.join(parts)})"

    def __str__(self) -> str:
        return f"{self.pred}({', '.join(str(a) for a in self.args)})"


def R(pred: str, *args: Any) -> Rel:
    return Rel(pred, tuple(args))


def walk(rels: Iterable[Rel]) -> list[Rel]:
    """Every relation including nested sub-relations, de-duplicated, in a
    deterministic order (outermost first, arguments left to right)."""
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

@dataclass(eq=False)
class Record:
    id: str
    domain: str
    attrs: dict[str, Any] = field(default_factory=dict)
    rels: tuple[Rel, ...] = ()
    types: dict[str, str] = field(default_factory=dict)
    text: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("record id must be non-empty")
        if isinstance(self.rels, list):
            self.rels = tuple(self.rels)

    # Identity is the id. Field-wise equality made `rec in candidates` a deep
    # comparison of every attribute dict and equated distinct records that
    # happened to carry the same payload.
    def __eq__(self, other: object) -> bool:
        return isinstance(other, Record) and other.id == self.id

    def __hash__(self) -> int:
        return hash(self.id)

    def all_rels(self) -> list[Rel]:
        return walk(self.rels)

    def predicates(self) -> set[str]:
        return {r.pred for r in self.all_rels()}

    def entities(self) -> set[str]:
        return {a for r in self.all_rels() for a in r.args
                if isinstance(a, str)}

    def depth(self) -> int:
        return max((r.order for r in self.all_rels()), default=0)

    def structure_fingerprint(self) -> str:
        """Canonical structure-only fingerprint of the whole record.

        Entity names are erased, so two incidents that are the same story told
        about different nouns produce the same fingerprint. That matters
        because convergence — 'two independent domains projected the same
        inference' — is the engine's main evidence signal, and it is trivially
        inflated by near-duplicate records filed under different domains.
        """
        return "|".join(sorted(r.signature_key() for r in self.all_rels()))


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------

class TopoStore:
    """Bitmap-indexed record store with write-time enrichment.

    Row positions are stable for the lifetime of the store: ingest appends,
    deletion tombstones. That is what lets a bitmap built at time t stay valid
    at time t+1 without a global rebuild.
    """

    def __init__(self) -> None:
        self.records: list[Record] = []
        self.pos: dict[str, int] = {}
        # Liveness is held as a set of tombstoned row positions and only
        # materialised into a bitmap on demand. v5.0 did `self._live |= 1 << i`
        # on every ingest, which allocates an i-bit integer per record and put
        # an O(n^2/64) term straight back into the write path that the postings
        # rewrite had just removed from it.
        self._dead: set[int] = set()
        self._live_cache: tuple[int, int, int] = (-1, -1, 0)   # (n, ndead, bm)
        self._postings: dict[str, dict[Any, list[int]]] = {}
        # key -> (version, bitmap). Bumping one integer per attribute per
        # ingest is cheaper than popping every touched key out of a dict, and
        # it invalidates exactly as precisely: a cached bitmap is stale iff the
        # attribute has been written since it was built.
        self._bmcache: dict[str, dict[Any, tuple[int, int]]] = {}
        self._ver: dict[str, int] = {}
        self._enrichers: list[tuple[str, Callable[[Record], Any]]] = []
        self._lock = threading.RLock()
        # telemetry
        self.probes = 0          # index lookups (cheap)
        self.reads = 0           # records materialised (expensive)
        self.cache_hits = 0

    # ---- ingestion ----------------------------------------------------

    def enricher(self, attr: str, fn: Callable[[Record], Any]) -> None:
        """Register a write-time derivation. Applied to every record ingested
        afterwards and retroactively to what is already stored.

        Everything the cheap tier filters on has to be materialised here. If
        nothing is extracted at write time, every query degrades to a full scan
        plus model calls, and no amount of planner cleverness recovers it.
        """
        with self._lock:
            self._enrichers.append((attr, fn))
            changed = False
            for rec in self.records:
                if attr not in rec.attrs:
                    rec.attrs[attr] = fn(rec)
                    changed = True
            if changed:
                for a in list(self._postings):
                    self._rebuild(a)

    def declare_index(self, attr: str) -> None:
        with self._lock:
            self._rebuild(attr)

    def ingest(self, rec: Record) -> Record:
        with self._lock:
            if rec.id in self.pos:
                raise ValueError(f"duplicate record id: {rec.id}")
            for attr, fn in self._enrichers:
                rec.attrs.setdefault(attr, fn(rec))
            i = len(self.records)
            self.records.append(rec)
            self.pos[rec.id] = i
            for attr, table in self._postings.items():
                keys = self._keys(rec, attr)
                if not keys:
                    continue
                for k in keys:
                    table.setdefault(k, []).append(i)
                self._ver[attr] += 1
            return rec

    def ingest_all(self, recs: Iterable[Record]) -> None:
        for r in recs:
            self.ingest(r)

    def delete(self, record_id: str) -> bool:
        """Tombstone a record. Row positions stay stable so live bitmaps stay
        valid; the row simply stops appearing in `all_bits`."""
        with self._lock:
            i = self.pos.get(record_id)
            if i is None or i in self._dead:
                return False
            self._dead.add(i)
            return True

    def get(self, record_id: str) -> Record | None:
        i = self.pos.get(record_id)
        if i is None or i in self._dead:
            return None
        return self.records[i]

    # ---- indices ------------------------------------------------------

    @staticmethod
    def _keys(rec: Record, attr: str) -> list[Any]:
        """Attributes first, then first-class record fields.

        Without the field fallback, indexing `domain` builds a silently empty
        index and every query using it returns nothing — a failure that looks
        exactly like 'no match'.
        """
        v = rec.attrs.get(attr, getattr(rec, attr, None))
        if v is None:
            return []
        if isinstance(v, (list, tuple, set, frozenset)):
            return [k for k in v if k is not None]
        if isinstance(v, dict):
            return list(v)
        return [v]

    def _rebuild(self, attr: str) -> None:
        table: dict[Any, list[int]] = {}
        for i, rec in enumerate(self.records):
            for k in self._keys(rec, attr):
                table.setdefault(k, []).append(i)
        self._postings[attr] = table
        self._bmcache[attr] = {}
        self._ver[attr] = self._ver.get(attr, 0) + 1
        self._ver.setdefault(attr, 0)

    def indexed(self) -> set[str]:
        return set(self._postings)

    def bitmap(self, attr: str, key: Any) -> int:
        """Live rows carrying `key` under `attr`.

        Raises rather than returning an empty bitmap when the attribute has no
        index: an empty result and a missing index are not the same fact, and
        conflating them is how a filter silently stops filtering.
        """
        table = self._postings.get(attr)
        if table is None:
            raise UnindexedAttribute(
                f"attribute '{attr}' is not indexed; call declare_index("
                f"'{attr}') before pushing a predicate down to it. "
                f"Indexed: {sorted(self._postings) or 'none'}")
        self.probes += 1
        cache = self._bmcache.setdefault(attr, {})
        ver = self._ver.get(attr, 0)
        hit = cache.get(key)
        if hit is not None and hit[0] == ver:
            self.cache_hits += 1
            bm = hit[1]
        else:
            rows = table.get(key)
            bm = (bitset.from_indices(rows, len(self.records)) if rows else 0)
            cache[key] = (ver, bm)
        return bm & self.all_bits

    @property
    def all_bits(self) -> int:
        """Bitmap of live rows, built lazily and cached.

        Recomputed only when a record has been added or tombstoned since the
        last call, which makes the common case (many queries between writes) a
        dictionary-free integer comparison.
        """
        n, ndead = len(self.records), len(self._dead)
        cn, cd, bm = self._live_cache
        if cn == n and cd == ndead:
            return bm
        bm = bitset.universe(n)
        for i in self._dead:
            bm &= ~(1 << i)
        self._live_cache = (n, ndead, bm)
        return bm

    @staticmethod
    def count(bitmap: int) -> int:
        return bitset.count(bitmap)

    def materialise(self, bitmap: int) -> list[Record]:
        """Read the records a bitmap selects. This is the expensive operation
        the whole cheap tier exists to avoid, so it is counted."""
        out = [self.records[i] for i in bitset.indices(bitmap & self.all_bits)]
        self.reads += len(out)
        return out

    def bits_for(self, recs: Sequence[Record]) -> int:
        return bitset.from_indices(
            (self.pos[r.id] for r in recs), len(self.records))

    # ---- partitioning -------------------------------------------------

    def partition_by(self, attr: str) -> dict[Any, list[str]]:
        """Shard plan.

        Partition on the highest-selectivity queried attribute, not on graph
        density. Clustering by edge weight optimises for query locality, but
        constraint queries are usually cross-cutting, so density clustering
        makes them span every shard. Returns the plan only; placement is the
        caller's problem.
        """
        out: dict[Any, list[str]] = {}
        for i, rec in enumerate(self.records):
            if i in self._dead:
                continue
            for k in self._keys(rec, attr):
                out.setdefault(k, []).append(rec.id)
        return out

    def duplicates(self) -> dict[str, list[str]]:
        """Records sharing a structure fingerprint, grouped.

        Near-duplicates inflate `convergence`, which is the engine's strongest
        evidence signal. Surfacing them is cheap; discovering them after acting
        on a false convergence is not.
        """
        groups: dict[str, list[str]] = {}
        for i, rec in enumerate(self.records):
            if i in self._dead or not rec.rels:
                continue
            groups.setdefault(rec.structure_fingerprint(), []).append(rec.id)
        return {k: v for k, v in groups.items() if len(v) > 1}

    # ---- persistence --------------------------------------------------

    @staticmethod
    def _rel_to_json(r: Rel) -> dict[str, Any]:
        return {"p": r.pred,
                "a": [TopoStore._rel_to_json(a) if isinstance(a, Rel) else a
                      for a in r.args]}

    @staticmethod
    def _rel_from_json(d: Any) -> Any:
        if isinstance(d, str):
            return d
        return Rel(d["p"], tuple(TopoStore._rel_from_json(a) for a in d["a"]))

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": FORMAT_VERSION,
            "indexed": sorted(self._postings),
            "records": [
                {"id": r.id, "domain": r.domain,
                 "attrs": r.attrs, "types": r.types, "text": r.text,
                 "rels": [self._rel_to_json(x) for x in r.rels],
                 "live": i not in self._dead}
                for i, r in enumerate(self.records)
            ],
        }

    @staticmethod
    def _refuse(obj: Any):
        """No silent stringification.

        `json.dump(..., default=str)` turns an un-serialisable index key into
        its repr, so a store that round-trips through disk comes back with
        different index keys than it went in with and every query on that
        attribute silently stops matching. Refusing is the only safe default.
        """
        raise TypeError(
            f"cannot serialise {type(obj).__name__} ({obj!r}). Store only "
            f"JSON-native values in attrs: silently stringifying this would "
            f"change the index key and break every query using it.")

    def save(self, path: str) -> None:
        payload = json.dumps(self.to_dict(), indent=1, default=self._refuse)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(payload)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TopoStore":
        fmt = data.get("format")
        if fmt != FORMAT_VERSION:
            raise ValueError(
                f"store format {fmt} cannot be read by version "
                f"{FORMAT_VERSION}; migrate explicitly rather than guessing")
        st = cls()
        for row in data["records"]:
            rec = Record(
                id=row["id"], domain=row["domain"],
                attrs=dict(row.get("attrs", {})),
                types=dict(row.get("types", {})),
                text=row.get("text", ""),
                rels=tuple(cls._rel_from_json(x) for x in row.get("rels", [])),
            )
            st.ingest(rec)
            if not row.get("live", True):
                st.delete(rec.id)
        for attr in data.get("indexed", []):
            st.declare_index(attr)
        return st

    @classmethod
    def load(cls, path: str) -> "TopoStore":
        with open(path, encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))

    # ---- misc ---------------------------------------------------------

    def reset_counters(self) -> None:
        self.probes = 0
        self.reads = 0
        self.cache_hits = 0

    def __len__(self) -> int:
        return len(self.records) - len(self._dead)

    def __iter__(self) -> Iterator[Record]:
        """Iterates live records only."""
        dead = self._dead
        for i, rec in enumerate(self.records):
            if i not in dead:
                yield rec
