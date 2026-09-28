"""Facade.

Deliberately a component, not a platform. It sits beside whatever storage you
already run: you ingest records into it, you query it, you throw it away. It
does not ask any existing system to be rebuilt around it, which is the
difference between something that gets adopted and something that does not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from .close import run_close
from .encoding import Validation, summary, validate
from .premortem import PremortemRound, run_premortem
from .query import (
    Budget, Closure, Outcome, Oracle, RelaxationPolicy, SlotSpec, Stats, Tier,
)
from .schema import CORE_SCHEMA, Schema
from .store import Record, TopoStore
from .transfer import Knobs, Round, TargetPattern, run_transfer

__all__ = ["Telemetry", "TopologicalEngine"]

_OUTCOME_FIELD = {
    Outcome.CLOSED: "closed",
    Outcome.PARTIAL: "partial",
    Outcome.REQUEST: "requests",
    Outcome.ABSTAIN: "abstains",
}


@dataclass
class Telemetry:
    """Per-mode running totals. Reported, never asserted."""
    closes: int = 0
    closed: int = 0
    partial: int = 0
    requests: int = 0
    abstains: int = 0
    deadline_hits: int = 0
    model_calls: int = 0
    model_tokens: int = 0
    records_read: int = 0
    index_probes: int = 0
    ms: float = 0.0
    rejected_on_ingest: int = 0

    def record(self, c: Closure) -> None:
        self.closes += 1
        self.model_calls += c.model_calls
        self.model_tokens += c.model_tokens
        self.records_read += c.reads
        self.index_probes += c.probes
        self.ms += c.ms
        if c.deadline_hit:
            self.deadline_hits += 1
        name = _OUTCOME_FIELD[c.outcome]
        setattr(self, name, getattr(self, name) + 1)

    def __str__(self) -> str:
        return (
            f"CLOSE runs            {self.closes}\n"
            f"  closed / partial    {self.closed} / {self.partial}\n"
            f"  request / abstain   {self.requests} / {self.abstains}\n"
            f"  deadline hits       {self.deadline_hits}\n"
            f"model calls           {self.model_calls}\n"
            f"model tokens          {self.model_tokens}\n"
            f"records materialised  {self.records_read}\n"
            f"index probes          {self.index_probes}\n"
            f"total latency         {self.ms:.1f}ms\n"
            f"rejected at ingest    {self.rejected_on_ingest}"
        )


class TopologicalEngine:
    def __init__(self, strict: bool = True,
                 schema: Schema = CORE_SCHEMA) -> None:
        self.store = TopoStore()
        self.stats = Stats()
        self.telemetry = Telemetry()
        self.schema = schema
        self.strict = strict          # reject records failing validation
        self.rejections: list[Validation] = []
        self.validations: dict[str, Validation] = {}

    # ---- layer 1 ------------------------------------------------------

    def enricher(self, attr: str, fn: Callable[[Record], Any]) -> None:
        """Write-time derivation. Everything the cheap tier filters on has to
        be materialised here, or every query degrades to a full scan."""
        self.store.enricher(attr, fn)

    def index(self, *attrs: str) -> None:
        for a in attrs:
            self.store.declare_index(a)

    def ingest(self, rec: Record) -> Validation:
        """Validation gate. A malformed encoding fails silently downstream, so
        it is cheaper to refuse it here than to debug it later.

        Note the gate only applies to records carrying relational structure:
        an attributes-only record is legitimately invisible to TRANSFER and is
        not an error.
        """
        v = validate(rec, self.schema)
        self.validations[rec.id] = v
        if self.strict and not v.ok and rec.rels:
            self.rejections.append(v)
            self.telemetry.rejected_on_ingest += 1
            return v
        self.store.ingest(rec)
        return v

    def ingest_all(self, recs: Iterable[Record]) -> list[Validation]:
        return [self.ingest(r) for r in recs]

    def delete(self, record_id: str) -> bool:
        return self.store.delete(record_id)

    # ---- layer 2/3 ----------------------------------------------------

    def close(self, spec: SlotSpec, oracle: Oracle | None = None,
              budget: Budget | None = None,
              relax: RelaxationPolicy | None = None) -> Closure:
        c = run_close(self.store, spec, oracle, budget, self.stats, relax)
        self.telemetry.record(c)
        return c

    def transfer(self, target: TargetPattern,
                 knobs: Knobs | None = None) -> list[Round]:
        return run_transfer(self.store, target, knobs, self.schema)

    def premortem(self, design: TargetPattern, knobs: Knobs | None = None,
                  enrich_top: int = 3,
                  enrich_requires_check: bool = False
                  ) -> list[PremortemRound]:
        return run_premortem(self.store, design, knobs, enrich_top,
                             self.schema, enrich_requires_check)

    # ---- introspection ------------------------------------------------

    def explain_plan(self, spec: SlotSpec) -> str:
        """Dry run of the planner: what order, at what tier, on what estimate.

        Costs nothing and answers the question that decides whether a query
        belongs in the cascade at all — is anything cheap actually selective
        here? A spec that is all model-tier constraints pays the cascade's
        overhead for no filtering and is better served by a plain scan.
        """
        indexed = [c for c in spec.constraints if c.tier is Tier.INDEX]
        residual = [c for c in spec.constraints if c.tier is not Tier.INDEX]
        lines = [f"plan for: {spec.query}"]
        for c in indexed:
            try:
                card = TopoStore.count(c.bitmap(self.store))
                est = f"{card} record(s) match"
            except Exception as exc:                    # unindexed attribute
                est = f"UNRESOLVABLE: {exc}"
            lines.append(f"  1 index     {c.name:<28} {est}")
        for c in self.stats.order(residual):
            lines.append(
                f"  2 {c.tier.label:<9} {c.name:<28} "
                f"est. selectivity {self.stats.selectivity(c.name):.2f}, "
                f"{c.tier.cost:,} units/record")
        if not indexed:
            lines.append("  WARNING: no index-resolvable constraint. The "
                         "cascade has nothing cheap to filter with and will "
                         "cost more than a plain scan.")
        return "\n".join(lines)

    def health(self) -> str:
        rels = [r for r in self.store if r.rels]
        dupes = self.store.duplicates()
        out = (f"{len(self.store)} records "
               f"({len(rels)} with relational structure), "
               f"{len(self.store.indexed())} indexed attributes, "
               f"{len(self.rejections)} rejected at ingest")
        if dupes:
            groups = "; ".join(", ".join(v) for v in dupes.values())
            out += (f"\nWARNING: {len(dupes)} near-duplicate group(s) share a "
                    f"structure fingerprint ({groups}). Convergence counts "
                    f"across these are not independent evidence.")
        return out

    def validation_summary(self) -> str:
        return summary(list(self.validations.values()))

    def shard_plan(self, attr: str) -> dict[Any, list[str]]:
        return self.store.partition_by(attr)

    # ---- persistence --------------------------------------------------

    def save(self, path: str) -> None:
        self.store.save(path)

    @classmethod
    def load(cls, path: str, strict: bool = True,
             schema: Schema = CORE_SCHEMA) -> "TopologicalEngine":
        eng = cls(strict=strict, schema=schema)
        eng.store = TopoStore.load(path)
        return eng
