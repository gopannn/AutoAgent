"""Component 1 — Evidence ledger.

A hypothesis score is COMPUTED from typed, directed, weighted, traceable
evidence by a formula that can be checked by hand:

    logit(p) = logit(prior) + SUM(direction * weight)   over admissible evidence

Merged from both parents:
  * Python API with construction-time enforcement          (toolkit B)
  * JSON contract + coded semantic validator               (toolkit A)
  * Structured sources {id, locator}; string sources accepted  (A + B)
  * `computed_by` as an alternative trace                   (B)
  * always-circular kinds, neutral-weight-zero, weight cap  (B)
  * declared-policy shape rules (suppressed/unresolved)     (A)
  * NEW: independence groups. Items default to the group of their source id,
    so three rows citing one study are visible as one source. The audit flags
    them and the robustness gate drops whole groups (see robustness.py).
  * NEW: scores are carried unclamped in log-odds; clamping is display-only.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from .outcomes import COMPUTED, POLICIES, SUPPRESSED, UNRESOLVED

LEDGER_SCHEMA = "epistemic-toolkit/evidence-ledger/2.0.0"
LEGACY_A_SCHEMA = "epistemic-toolkit/evidence-ledger/1.0.0"

DIRECTIONS = {"supports": 1, "undermines": -1, "neutral": 0}
ALWAYS_CIRCULAR_KINDS = {"arithmetic", "definitional", "derived_identity"}
DEFAULT_CLAMP = (0.01, 0.99)
MAX_WEIGHT = 5.0
MIN_STATEMENT = 10


class LedgerError(ValueError):
    """A ledger entry violates a structural rule."""


def logit(p: float) -> float:
    if not isinstance(p, (int, float)) or isinstance(p, bool) or not 0.0 < p < 1.0:
        raise LedgerError(f"probability must be strictly between 0 and 1, got {p!r}")
    return math.log(p / (1.0 - p))


def sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    z = math.exp(x)
    return z / (1.0 + z)


@dataclass(frozen=True)
class Source:
    id: str
    locator: str | None = None

    @classmethod
    def coerce(cls, value: Any) -> "Source | None":
        if value is None or isinstance(value, Source):
            return value
        if isinstance(value, str):
            ref, _, loc = value.partition("#")
            return cls(ref.strip() or value.strip(), loc.strip() or None)
        if isinstance(value, dict):
            return cls(str(value.get("id", "")).strip(),
                       (str(value["locator"]).strip() if value.get("locator") else None))
        raise LedgerError(f"unsupported source value {value!r}")

    def to_dict(self) -> dict:
        return {"id": self.id, "locator": self.locator}


@dataclass
class Evidence:
    """One typed, directed, weighted, traceable item. Weight is log-odds:
    0.5 weak (50%->62%), 1.0 moderate (->73%), 2.0 strong (->88%),
    3.0 very strong (->95%). Cap 5.0."""
    id: str
    statement: str
    kind: str
    direction: str
    weight: float
    source: Any = None
    computed_by: str | None = None
    circular: bool = False
    rationale: str | None = None
    independence_group: str | None = None
    notes: str | None = None

    def __post_init__(self):
        self.source = Source.coerce(self.source)
        if not self.id or not str(self.id).strip():
            raise LedgerError("evidence id is required")
        if not isinstance(self.statement, str) or len(self.statement.strip()) < MIN_STATEMENT:
            raise LedgerError(f"{self.id}: statement must be a real sentence")
        if not isinstance(self.kind, str) or not self.kind.strip():
            raise LedgerError(f"{self.id}: typed evidence requires a non-empty kind")
        if self.direction not in DIRECTIONS:
            raise LedgerError(f"{self.id}: direction must be one of {sorted(DIRECTIONS)}")
        if (not isinstance(self.weight, (int, float)) or isinstance(self.weight, bool)
                or not 0.0 <= self.weight <= MAX_WEIGHT):
            raise LedgerError(f"{self.id}: weight must be a number in [0, {MAX_WEIGHT}]")
        if not isinstance(self.circular, bool):
            raise LedgerError(f"{self.id}: circular must be an explicit boolean")
        if not ((self.source and self.source.id) or self.computed_by):
            raise LedgerError(f"{self.id}: evidence must be traceable — give a source "
                              f"or computed_by. Untraceable evidence cannot be audited.")
        if self.kind in ALWAYS_CIRCULAR_KINDS and not self.circular:
            raise LedgerError(f"{self.id}: kind '{self.kind}' is always circular: an identity "
                              f"of the construction cannot be evidence for it. Set circular=True, weight=0.")
        if self.circular and self.weight != 0.0:
            raise LedgerError(f"{self.id}: circular evidence must carry weight 0")
        if self.direction == "neutral" and self.weight != 0.0:
            raise LedgerError(f"{self.id}: neutral evidence must carry weight 0")

    @property
    def admissible(self) -> bool:
        return not self.circular

    @property
    def bearing(self) -> bool:
        return self.admissible and self.direction != "neutral" and self.weight > 0

    @property
    def group(self) -> str:
        if self.independence_group:
            return self.independence_group
        if self.source and self.source.id:
            return f"source:{self.source.id}"
        return f"computed:{self.computed_by}"

    def to_dict(self) -> dict:
        return {"id": self.id, "statement": self.statement, "kind": self.kind,
                "direction": self.direction, "weight": self.weight,
                "source": self.source.to_dict() if self.source else None,
                "computed_by": self.computed_by, "circular": self.circular,
                "rationale": self.rationale, "independence_group": self.independence_group,
                "notes": self.notes}


@dataclass
class Hypothesis:
    id: str
    statement: str
    prior: float = 0.5
    evidence: list[Evidence] = field(default_factory=list)
    score_policy: str = COMPUTED
    policy_reason: str | None = None
    what_would_settle_it: str | None = None
    tags: list[str] = field(default_factory=list)

    def __post_init__(self):
        logit(self.prior)
        if not isinstance(self.statement, str) or len(self.statement.strip()) < MIN_STATEMENT:
            raise LedgerError(f"{self.id}: statement must be a real sentence")
        if self.score_policy not in POLICIES:
            raise LedgerError(f"{self.id}: score_policy must be one of {POLICIES}")

    def add(self, ev: Evidence) -> "Hypothesis":
        if any(e.id == ev.id for e in self.evidence):
            raise LedgerError(f"{self.id}: duplicate evidence id {ev.id}")
        self.evidence.append(ev)
        return self

    @property
    def admissible(self) -> list[Evidence]:
        return [e for e in self.evidence if e.admissible]

    @property
    def inadmissible(self) -> list[Evidence]:
        return [e for e in self.evidence if not e.admissible]

    @property
    def bearing(self) -> list[Evidence]:
        return [e for e in self.evidence if e.bearing]

    def to_dict(self) -> dict:
        return {"id": self.id, "statement": self.statement, "prior": self.prior,
                "score_policy": self.score_policy, "policy_reason": self.policy_reason,
                "what_would_settle_it": self.what_would_settle_it, "tags": list(self.tags),
                "evidence": [e.to_dict() for e in self.evidence]}


@dataclass
class ScoreBreakdown:
    hypothesis_id: str
    prior: float
    prior_logit: float
    contributions: list[dict]
    total_logit: float
    raw_probability: float
    clamped_probability: float
    admissible_count: int
    inadmissible_ids: list[str]

    def explain(self) -> str:
        lines = [self.hypothesis_id, f"  prior {self.prior:.3f}  -> logit {self.prior_logit:+.3f}"]
        for c in self.contributions:
            mark = f"  [{c['exclusion']}: 0]" if c["exclusion"] else ""
            lines.append(f"  {c['evidence_id']:16s} {c['direction']:10s} {c['log_odds_delta']:+.2f}{mark}")
        lines.append(f"  total logit {self.total_logit:+.3f} -> p = {self.clamped_probability:.3f}"
                     + ("  (clamped for display)" if self.clamped_probability != self.raw_probability else ""))
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class Ledger:
    """A set of hypotheses scored by an explicit, reproducible rule."""

    def __init__(self, hypotheses: Iterable[Hypothesis] = (), *, ledger_id: str | None = None,
                 title: str | None = None, clamp: tuple[float, float] = DEFAULT_CLAMP):
        self.ledger_id = ledger_id
        self.title = title
        self.clamp = tuple(clamp)
        self.hypotheses: dict[str, Hypothesis] = {}
        for h in hypotheses:
            self.add(h)

    def add(self, h: Hypothesis) -> "Ledger":
        if h.id in self.hypotheses:
            raise LedgerError(f"duplicate hypothesis id {h.id}")
        known = {e.id for x in self.hypotheses.values() for e in x.evidence}
        clash = known & {e.id for e in h.evidence}
        if clash:
            raise LedgerError(f"evidence ids must be unique across the ledger: {sorted(clash)}")
        self.hypotheses[h.id] = h
        return self

    def __getitem__(self, hid: str) -> Hypothesis:
        return self.hypotheses[hid]

    def __iter__(self):
        return iter(self.hypotheses.values())

    # ---------------------------------------------------------------- scoring
    def breakdown(self, hid: str, *, weight_fn: Callable[[Evidence], float] | None = None,
                  prior_override: float | None = None,
                  exclude: set[str] | None = None) -> ScoreBreakdown:
        h = self.hypotheses[hid]
        prior = h.prior if prior_override is None else prior_override
        x0 = logit(prior)
        x = x0
        contributions = []
        for ev in h.evidence:
            if exclude and ev.id in exclude:
                continue
            if ev.circular:
                delta, used = 0.0, 0.0
            else:
                used = weight_fn(ev) if weight_fn else ev.weight
                delta = DIRECTIONS[ev.direction] * used
            x += delta
            contributions.append({"evidence_id": ev.id, "direction": ev.direction,
                                  "admissible": ev.admissible, "weight_used": round(used, 6),
                                  "log_odds_delta": round(delta, 6),
                                  "exclusion": "circular" if ev.circular else None,
                                  "group": ev.group})
        raw = sigmoid(x)
        lo, hi = self.clamp
        return ScoreBreakdown(hid, prior, round(x0, 6), contributions, x, raw,
                              max(lo, min(hi, raw)), len(h.admissible),
                              [e.id for e in h.inadmissible])

    def logit_score(self, hid: str, **kw) -> float:
        """Unclamped log-odds. Decisions and robustness tests use this."""
        return self.breakdown(hid, **kw).total_logit

    def score(self, hid: str, **kw) -> float:
        """Clamped probability, IGNORING policy. Publish via policy.report()."""
        return self.breakdown(hid, **kw).clamped_probability

    # ------------------------------------------------------------------ audit
    def validate(self, *, require_locator: bool = False) -> list[dict]:
        """Semantic checks that span records (codes compatible with toolkit A)."""
        issues = []
        if not self.hypotheses:
            issues.append(_issue("LEDGER_EMPTY", "$", "at least one hypothesis is required"))
        for h in self.hypotheses.values():
            hp = f"$.hypotheses[{h.id}]"
            dirs = {e.direction for e in h.bearing}
            if require_locator:
                for e in h.evidence:
                    if e.source and not e.source.locator and not e.computed_by:
                        issues.append(_issue("TRACEABILITY", f"{hp}.{e.id}.source",
                                             "source.locator required in strict mode"))
            if h.score_policy == SUPPRESSED:
                if h.bearing:
                    issues.append(_issue("SUPPRESSED_HAS_EVIDENCE", hp,
                                         "suppressed means no admissible evidence bears on the question"))
                if not h.policy_reason:
                    issues.append(_issue("POLICY_REASON", hp, "suppressed requires a reason"))
            if h.score_policy == UNRESOLVED:
                if not {"supports", "undermines"} <= dirs:
                    issues.append(_issue("UNRESOLVED_NOT_CONTESTED", hp,
                                         "declared unresolved requires admissible support AND undermining "
                                         "evidence (gate demotion is recorded separately)"))
                if not h.policy_reason:
                    issues.append(_issue("POLICY_REASON", hp, "unresolved requires a reason"))
        return issues

    def audit(self) -> list[dict]:
        """Advisory problems that construction-time checks cannot see."""
        out = []
        for h in self.hypotheses.values():
            if not h.evidence:
                out.append(_advice(h.id, "NO_EVIDENCE", "no evidence at all"))
            elif not h.admissible:
                out.append(_advice(h.id, "ALL_CIRCULAR", "all evidence is circular; the score is the prior"))
            dirs = {e.direction for e in h.bearing}
            if dirs == {"supports"} and len(h.bearing) >= 3:
                out.append(_advice(h.id, "ONE_SIDED", "only supporting evidence was recorded; "
                                                       "was disconfirming evidence looked for?"))
            heavy = [e.id for e in h.bearing if e.weight >= 3.0]
            if len(heavy) == 1 and len(h.bearing) > 1:
                out.append(_advice(h.id, "DOMINANT_ITEM", f"single very-strong item {heavy[0]} may dominate"))
            groups: dict[str, list[Evidence]] = {}
            for e in h.bearing:
                groups.setdefault(e.group, []).append(e)
            for g, items in groups.items():
                if len(items) > 1:
                    net = sum(DIRECTIONS[e.direction] * e.weight for e in items)
                    out.append(_advice(h.id, "CORRELATED_SOURCE",
                                       f"{len(items)} items share independence group '{g}' "
                                       f"({[e.id for e in items]}, net {net:+.2f} log-odds). They are "
                                       f"summed as independent; merge them or accept the group-out gate test."))
            no_loc = [e.id for e in h.evidence if e.source and not e.source.locator and not e.computed_by]
            if no_loc:
                out.append(_advice(h.id, "NO_LOCATOR", f"{len(no_loc)} item(s) cite a source without a "
                                                        f"locator {no_loc}; add one so the item can be recovered"))
        return out

    # --------------------------------------------------------------------- io
    def to_dict(self) -> dict:
        return {"schema": LEDGER_SCHEMA, "ledger_id": self.ledger_id, "title": self.title,
                "aggregation": {"method": "log_odds", "clamp": list(self.clamp),
                                "formula": "logit(p) = logit(prior) + SUM(direction * weight) "
                                           "over non-circular evidence; clamp is display-only"},
                "hypotheses": [h.to_dict() for h in self.hypotheses.values()]}

    def to_json(self, path: str | Path | None = None, indent: int = 2) -> str:
        text = json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)
        if path:
            Path(path).write_text(text + "\n", encoding="utf-8")
        return text

    @classmethod
    def from_dict(cls, d: dict) -> "Ledger":
        """Accepts v2, toolkit-A v1 (`type`, source object) and toolkit-B (`kind`, source string)."""
        clamp = tuple(d.get("aggregation", {}).get("clamp", DEFAULT_CLAMP))
        led = cls(ledger_id=d.get("ledger_id"), title=d.get("title"), clamp=clamp)
        for hd in d.get("hypotheses", []):
            evs = [Evidence(**_normalise_evidence(e)) for e in hd.get("evidence", [])]
            h = Hypothesis(id=hd["id"], statement=hd["statement"], prior=hd.get("prior", 0.5),
                           score_policy=hd.get("score_policy", COMPUTED),
                           policy_reason=hd.get("policy_reason"),
                           what_would_settle_it=hd.get("what_would_settle_it"),
                           tags=list(hd.get("tags", [])))
            for e in evs:
                h.add(e)
            led.add(h)
        return led

    @classmethod
    def from_json(cls, path: str | Path) -> "Ledger":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


_EVIDENCE_FIELDS = {"id", "statement", "kind", "direction", "weight", "source", "computed_by",
                    "circular", "rationale", "independence_group", "notes"}


def _normalise_evidence(e: dict) -> dict:
    e = dict(e)
    if "type" in e and "kind" not in e:
        e["kind"] = e.pop("type")
    unknown = set(e) - _EVIDENCE_FIELDS
    if unknown:
        raise LedgerError(f"{e.get('id')}: unknown evidence fields {sorted(unknown)}")
    return e


def _issue(code: str, path: str, message: str) -> dict:
    return {"code": code, "path": path, "message": message}


def _advice(hid: str, code: str, message: str) -> dict:
    return {"hypothesis": hid, "code": code, "issue": message}


def load_ledger(source: str | Path | dict | Ledger) -> Ledger:
    if isinstance(source, Ledger):
        return source
    if isinstance(source, dict):
        return Ledger.from_dict(source)
    return Ledger.from_json(source)
