"""Component 4 — Derived-field detector (schema linter).

Rule: a stored field must not be recomputable from other stored fields.

Merged detection surface
  constant, duplicate, exact affine derivation, low-variance measure  (toolkit B)
  single and composite (2-column) functional dependencies            (A had composites)
  near-dependency (drift between two sources of truth)                (B)
  declared exact rules — lookup or affine — that PROVE a derivation   (A, + affine)
  declared rules the data VIOLATES (drift against the spec)           (NEW)
  entropy / conditional-entropy / redundant-bit accounting            (A)
  CSV / JSON / SQLite loaders with safe numeric coercion              (A, + coercion)

Evidence levels (A's epistemics, B's coverage)
  rule_proven   a declared rule reproduces every stored value          -> ERROR
  structural    constant, byte-duplicate, exact affine                 -> ERROR
  observed      zero-violation FD with adequate support                -> WARNING by default
  near          FD violated in a few rows (probable drift)             -> WARNING
An observed FD in finite data can be accidental, so it is a candidate until a
declared rule proves it. Pass `observed_fd_severity="ERROR"` for B's behaviour.

Fixes over both parents
  * O(rows) grouping. A's pair metric was O(rows x distinct keys): 27.5 s on a
    400-row, 14-column table. This version does the same table in well under 1 s.
  * B's support criterion replaces A's `key_like` test. A's test only excluded
    determinants that were unique in EVERY row, so a 399-distinct-of-400 price
    column made every other column look dependent (B's BUG-2 reproduced in A:
    29 findings on B's orders example, most spurious).
  * Near-dependencies with symmetric violation counts are reported as
    direction-ambiguous instead of picking a direction by alphabetical
    tie-break (B named the wrong column in its own ex2), and the violating
    rows are listed so the drift can actually be inspected.
"""
from __future__ import annotations

import csv
import json
import math
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from itertools import combinations, permutations
from pathlib import Path
from typing import Any, Iterable

_INT = re.compile(r"-?(0|[1-9]\d*)")
_FLOAT = re.compile(r"-?(0|[1-9]\d*)\.\d+([eE][-+]?\d+)?|-?[1-9]\d*[eE][-+]?\d+")


def _coerce(v: Any) -> Any:
    if not isinstance(v, str):
        return v
    s = v.strip()
    if _INT.fullmatch(s):
        return int(s)
    if _FLOAT.fullmatch(s):
        return float(s)
    return v


def load_records(path: str | Path, *, table: str | None = None, coerce_numeric: bool = True) -> list[dict]:
    src = Path(path)
    suffix = src.suffix.lower()
    if suffix == ".csv":
        with src.open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        return [{k: _coerce(v) for k, v in r.items()} for r in rows] if coerce_numeric else rows
    if suffix == ".json":
        payload = json.loads(src.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            if table and isinstance(payload.get(table), list):
                return payload[table]
            lists = [v for v in payload.values() if isinstance(v, list)]
            if len(lists) == 1:
                return lists[0]
        raise ValueError("JSON source must be an array or name an array with table=")
    if suffix in {".sqlite", ".sqlite3", ".db"}:
        if not table or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
            raise ValueError("SQLite input requires a safe table name")
        with sqlite3.connect(src) as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute(f'SELECT * FROM "{table}"')]
    raise ValueError(f"unsupported input format: {suffix}")


def _hashable(v):
    if isinstance(v, (list, tuple)):
        return tuple(_hashable(x) for x in v)
    if isinstance(v, dict):
        return tuple(sorted((k, _hashable(x)) for k, x in v.items()))
    if isinstance(v, set):
        return tuple(sorted(_hashable(x) for x in v))
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def _canon(v) -> str:
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v)


def _h(values: Iterable) -> float:
    c = Counter(values)
    n = sum(c.values())
    return -sum(k / n * math.log2(k / n) for k in c.values()) if n else 0.0


def _numeric(col: list):
    if col and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in col):
        return col
    return None


def _affine(xs, ys, tol=1e-7):
    pairs = {}
    for x, y in zip(xs, ys):
        pairs.setdefault(x, y)
    if len(pairs) < 2:
        return None
    (x0, y0), (x1, y1) = sorted(pairs.items())[0], sorted(pairs.items())[-1]
    a = (y1 - y0) / (x1 - x0)
    b = y0 - a * x0
    if all(abs(a * x + b - y) <= tol * max(1.0, abs(y)) for x, y in zip(xs, ys)):
        return a, b
    return None


@dataclass
class Finding:
    kind: str
    column: str
    determined_by: list[str] | None
    evidence_level: str
    severity: str
    violations: int
    rows: int
    support: int | None
    redundant_bits: float | None
    detail: str
    violating_keys: list = field(default_factory=list)
    allowed_to_store: bool = False
    rule_id: str | None = None

    def line(self) -> str:
        src = f" <- {' + '.join(self.determined_by)}" if self.determined_by else ""
        return f"[{self.severity:7s}] {self.kind:21s} {self.column}{src}  ({self.detail})"


@dataclass
class LintReport:
    rows: int
    columns: list[str]
    entropy: dict[str, float]
    findings: list[Finding] = field(default_factory=list)
    rule_checks: list[dict] = field(default_factory=list)
    observations: list[dict] = field(default_factory=list)

    @property
    def removable(self) -> list[str]:
        return sorted({f.column for f in self.findings
                       if f.evidence_level in ("structural", "rule_proven") and not f.allowed_to_store})

    @property
    def candidates(self) -> list[str]:
        return sorted({f.column for f in self.findings
                       if f.evidence_level == "observed" and not f.allowed_to_store} - set(self.removable))

    def passes(self, fail_on: str = "ERROR") -> bool:
        levels = {"ERROR": {"ERROR"}, "WARNING": {"ERROR", "WARNING"}}[fail_on.upper()]
        return not any(f.severity in levels for f in self.findings)

    def text(self) -> str:
        out = [f"Derived-field lint: {self.rows} rows, {len(self.columns)} columns",
               f"  removable (proven, 0 extra bits): {self.removable or 'none'}",
               f"  candidates (observed only, declare a rule to prove): {self.candidates or 'none'}"]
        for sev in ("ERROR", "WARNING", "INFO"):
            out += ["  " + f.line() for f in self.findings if f.severity == sev]
        return "\n".join(out)

    def to_dict(self) -> dict:
        return {"schema": "epistemic-toolkit/derived-field-audit/2.0.0", "rows": self.rows,
                "columns": self.columns, "entropy_bits": {k: round(v, 6) for k, v in self.entropy.items()},
                "summary": {"findings": len(self.findings), "removable": self.removable,
                            "candidates": self.candidates,
                            "errors": sum(f.severity == "ERROR" for f in self.findings),
                            "warnings": sum(f.severity == "WARNING" for f in self.findings),
                            "passes": self.passes()},
                "findings": [f.__dict__ for f in self.findings],
                "declared_rule_checks": self.rule_checks, "observations": self.observations}


class _Table:
    def __init__(self, rows: list[dict], cols: list[str]):
        self.n = len(rows)
        self.cols = cols
        self.vals = {c: [_hashable(r.get(c)) for r in rows] for c in cols}
        self.raw = {c: [r.get(c) for r in rows] for c in cols}

    def key(self, dets: tuple[str, ...]) -> list:
        if len(dets) == 1:
            return self.vals[dets[0]]
        return list(zip(*(self.vals[d] for d in dets)))

    def support(self, dets) -> int:
        return sum(g - 1 for g in Counter(self.key(dets)).values() if g > 1)

    def fd(self, dets, b) -> tuple[int, int, float, list]:
        """(violations, distinct keys, H(b|dets), violating determinant values)."""
        groups: dict[Any, Counter] = defaultdict(Counter)
        for k, v in zip(self.key(dets), self.vals[b]):
            groups[k][v] += 1
        viol, cond, bad = 0, 0.0, []
        for k, c in groups.items():
            tot = sum(c.values())
            if len(c) > 1:
                viol += tot - max(c.values())
                bad.append(k)
            cond += tot / self.n * _h(c.elements())
        return viol, len(groups), cond, bad


def lint(rows: list[dict], key: str | None = None, ignore: Iterable[str] = (), *,
         config: dict | None = None, near_tolerance: float = 0.05, min_rows: int = 5,
         cv_floor: float = 0.01, min_support_fraction: float = 0.5, min_support_rows: int = 10,
         max_determinant_size: int | None = None, observed_fd_severity: str = "WARNING") -> LintReport:
    """Lint a table (list of dicts). `config` accepts toolkit-A keys:
    declared_rules, allow_stored, candidate_pairs, max_determinant_size."""
    cfg = dict(config or {})
    rows = list(rows)
    max_det = int(max_determinant_size or cfg.get("max_determinant_size", 1))
    if max_det not in (1, 2):
        raise ValueError("max_determinant_size must be 1 or 2")
    cols = sorted({k for r in rows for k in r} - set(ignore))
    n = len(rows)
    T = _Table(rows, cols)
    ent = {c: _h(T.vals[c]) for c in cols}
    rep = LintReport(rows=n, columns=cols, entropy=ent)
    allowed = {tuple(a) if isinstance(a, (list, tuple)) else (a,) for a in cfg.get("allow_stored", [])}
    allowed_cols = {a[-1] for a in allowed}
    need = max(min_support_rows, int(min_support_fraction * n))
    flagged: set[str] = set()

    def add(f: Finding):
        if f.column in allowed_cols:
            f.allowed_to_store, f.severity = True, "INFO"
            f.detail += "; explicitly allowed to store (cache contract required)"
        rep.findings.append(f)

    if n >= min_rows:
        for c in cols:
            if ent[c] == 0.0:
                add(Finding("constant", c, None, "structural", "ERROR", 0, n, None, 0.0,
                            "single value across all rows"))
                flagged.add(c)

        sig: dict[tuple, str] = {}
        for c in cols:
            if c in flagged:
                continue
            s = tuple(T.vals[c])
            if s in sig:
                add(Finding("duplicate", c, [sig[s]], "structural", "ERROR", 0, n, None, round(ent[c], 6),
                            "identical value in every row"))
                flagged.add(c)
            else:
                sig[s] = c

        live = [c for c in cols if c not in flagged and c != key]
        nums = {c: _numeric(T.raw[c]) for c in live}
        for a, b in permutations(live, 2):
            if a in flagged or b in flagged or not nums[a] or not nums[b] or len(set(nums[b])) < 2:
                continue
            fit = _affine(nums[a], nums[b])
            if fit and not (abs(fit[0] - 1) < 1e-12 and abs(fit[1]) < 1e-12):
                slope = fit[0]
                icpt = 0.0 if abs(fit[1]) <= 1e-9 * max(1.0, abs(slope)) else fit[1]
                add(Finding("arithmetic_derivation", b, [a], "structural", "ERROR", 0, n, None,
                            round(ent[b], 6), f"{b} = {slope:.10g}*{a} + {icpt:.8g} exactly"))
                flagged.add(b)

        live = [c for c in cols if c not in flagged and c != key]
        sup1 = {c: T.support((c,)) for c in live}
        for a, b in permutations(live, 2):
            if a in flagged or b in flagged or ent[b] == 0.0 or sup1[a] < need:
                continue
            viol, k, cond, _ = T.fd((a,), b)
            if viol == 0:
                add(Finding("functional_dependency", b, [a], "observed", observed_fd_severity, 0, n,
                            sup1[a], round(ent[b], 6),
                            f"total function over {k} {a}-values, tested on {sup1[a]} rows; "
                            f"H({b}|{a}) = 0, {ent[b]:.2f} redundant bits per row"))
                flagged.add(b)

        if max_det == 2:
            live = [c for c in cols if c not in flagged and c != key]
            if len(live) > 40:
                raise ValueError("composite scan limited to 40 columns; pass candidate_pairs instead")
            for a1, a2 in combinations(live, 2):
                if a1 in flagged or a2 in flagged:
                    continue
                s = T.support((a1, a2))
                if s < need:
                    continue
                for b in live:
                    if b in (a1, a2) or b in flagged or ent[b] == 0.0:
                        continue
                    viol, k, _, _ = T.fd((a1, a2), b)
                    if viol == 0:
                        add(Finding("functional_dependency", b, [a1, a2], "observed", observed_fd_severity,
                                    0, n, s, round(ent[b], 6),
                                    f"total function of ({a1}, {a2}) over {k} value pairs, tested on {s} rows"))
                        flagged.add(b)

        live = [c for c in cols if c not in flagged and c != key]
        for a, b in combinations(live, 2):
            if ent[a] == 0.0 or ent[b] == 0.0:
                continue
            cands = []
            if sup1.get(a, T.support((a,))) >= need:
                v, _, cond, bad = T.fd((a,), b)
                cands.append((v, a, b, cond, bad))
            if sup1.get(b, T.support((b,))) >= need:
                v, _, cond, bad = T.fd((b,), a)
                cands.append((v, b, a, cond, bad))
            ok = [c for c in cands if 0 < c[0] <= near_tolerance * n]
            if not ok:
                continue
            ok.sort(key=lambda c: c[0])
            best = ok[0]
            ambiguous = len(ok) == 2 and ok[0][0] == ok[1][0]
            v, src, dst, cond, bad = best
            det_vals = T.vals[src]
            groups: dict[Any, Counter] = defaultdict(Counter)
            for x, y in zip(det_vals, T.vals[dst]):
                groups[x][y] += 1
            majority = {x: c.most_common(1)[0][0] for x, c in groups.items()}
            ids = T.vals[key] if key else list(range(n))
            offenders = [ids[i] for i, (x, y) in enumerate(zip(det_vals, T.vals[dst])) if y != majority[x]]
            if ambiguous:
                col, det = f"{a}|{b}", None
                detail = (f"{a} and {b} agree except in {v}/{n} rows; direction undetermined "
                          f"(equal violations both ways) — probable drift between two sources of truth")
            else:
                col, det = dst, [src]
                detail = f"{src} -> {dst} holds except in {v}/{n} rows: probable drift; inspect listed rows"
            add(Finding("near_dependency", col, det, "near", "WARNING", v, n, None,
                        round(ent[dst] - cond, 6), detail, violating_keys=offenders[:20]))

        for c in cols:
            if c in flagged:
                continue
            xs = _numeric(T.raw[c])
            if xs and len(set(xs)) > 1:
                m = sum(xs) / len(xs)
                sd = (sum((x - m) ** 2 for x in xs) / len(xs)) ** 0.5
                if m and abs(sd / m) < cv_floor:
                    add(Finding("low_variance", c, None, "observed", "INFO", 0, n, None, round(ent[c], 6),
                                f"coefficient of variation {sd / m:.4f}; a measure that barely varies"))

    for rule in cfg.get("declared_rules", []):
        rep.rule_checks.append(_check_rule(rows, rule, key))
    for chk in rep.rule_checks:
        det, dep = chk["determinants"], chk["dependent"]
        existing = next((f for f in rep.findings if f.column == dep and f.determined_by == det), None)
        if chk["rule_is_total_and_exact"]:
            if existing:
                existing.evidence_level, existing.rule_id = "rule_proven", chk["rule_id"]
                existing.severity = "INFO" if existing.allowed_to_store else "ERROR"
                existing.detail += f"; PROVEN by declared rule {chk['rule_id']}"
            else:
                add(Finding("rule_derivation", dep, det, "rule_proven", "ERROR", 0, n, None,
                            round(ent.get(dep, 0.0), 6), f"declared rule {chk['rule_id']} reproduces every "
                            f"stored value", rule_id=chk["rule_id"]))
        else:
            add(Finding("rule_violated", dep, det, "rule_proven", "ERROR", len(chk["failures"]), n, None, None,
                        f"declared rule {chk['rule_id']} disagrees with {len(chk['failures'])} stored rows: "
                        f"the stored column has drifted from its specification",
                        violating_keys=[f["row_key"] for f in chk["failures"][:20]], rule_id=chk["rule_id"]))

    for pair in cfg.get("candidate_pairs", []):
        det, dep = tuple(pair["determinants"]), pair["dependent"]
        missing = [c for c in (*det, dep) if c not in cols]
        if missing:
            raise ValueError(f"unknown field(s) in candidate pair: {missing}")
        v, k, cond, bad = T.fd(det, dep)
        rep.observations.append({"determinants": list(det), "dependent": dep, "rows_examined": n,
                                 "distinct_determinant_values": k, "support": T.support(det),
                                 "violation_count": v, "is_total_function": n > 0 and v == 0,
                                 "dependent_entropy_bits": round(ent[dep], 6),
                                 "conditional_entropy_bits": round(cond, 6),
                                 "redundant_bits_if_stored": round(max(0.0, ent[dep] - cond), 6),
                                 "violating_determinant_values": [list(b) if isinstance(b, tuple) else b
                                                                  for b in bad[:20]]})
    return rep


def _check_rule(rows: list[dict], rule: dict, key: str | None) -> dict:
    det, dep = list(rule["determinants"]), rule["dependent"]
    rtype = rule.get("type", "lookup")
    failures = []
    if rtype == "lookup":
        lookup = {str(k): v for k, v in rule["lookup"].items()}
        for i, r in enumerate(rows):
            lk = " | ".join(_canon(r.get(d)) for d in det)
            rk = r.get(key, i) if key else i
            if lk not in lookup:
                failures.append({"row_key": rk, "key": lk, "problem": "lookup key missing"})
            elif _canon(r.get(dep)) != _canon(lookup[lk]):
                failures.append({"row_key": rk, "key": lk, "problem": "stored value differs from rule",
                                 "stored": r.get(dep), "derived": lookup[lk]})
    elif rtype == "affine":
        if len(det) != 1:
            raise ValueError(f"{rule['id']}: affine rules take one determinant")
        a, b = float(rule["slope"]), float(rule.get("intercept", 0.0))
        tol = float(rule.get("tolerance", 1e-9))
        for i, r in enumerate(rows):
            x, y = r.get(det[0]), r.get(dep)
            rk = r.get(key, i) if key else i
            try:
                ok = abs(a * float(x) + b - float(y)) <= tol * max(1.0, abs(float(y)))
            except (TypeError, ValueError):
                ok = False
            if not ok:
                failures.append({"row_key": rk, "problem": "stored value differs from rule",
                                 "stored": y, "derived": None if x is None else a * float(x) + b})
    else:
        raise ValueError(f"{rule.get('id')}: unknown rule type {rtype!r}")
    return {"rule_id": rule["id"], "rule_type": rtype, "determinants": det, "dependent": dep,
            "rows_examined": len(rows), "failures": failures, "rule_is_total_and_exact": not failures}


def audit_records(records: list[dict], config: dict | None = None, **kw) -> dict:
    """Toolkit-A compatible entry point returning a JSON-ready dict."""
    return lint(records, config=config, **kw).to_dict()


def confidence_vector_rank(rows: list[dict], dims: list[str], tol: float = 1e-9) -> dict:
    """How many dimensions of a multi-dimensional score carry information?
    Reports constants, duplicates, and TRUE numeric rank (NEW: B only removed
    exact duplicates, so a dimension that is the average of two others passed
    as informative)."""
    const = [d for d in dims if len({r[d] for r in rows}) == 1]
    dup = [(a, b) for i, a in enumerate(dims) for b in dims[i + 1:]
           if all(abs(r[a] - r[b]) <= tol for r in rows)]
    varying = [d for d in dims if d not in const]
    basis: list[list[float]] = []
    informative, dependent = [], []
    for d in varying:
        xs = [float(r[d]) for r in rows]
        m = sum(xs) / len(xs)
        v = [x - m for x in xs]
        for b in basis:
            proj = sum(p * q for p, q in zip(v, b))
            v = [p - proj * q for p, q in zip(v, b)]
        norm = math.sqrt(sum(p * p for p in v))
        scale = math.sqrt(sum((x - m) ** 2 for x in xs)) or 1.0
        if norm / scale > 1e-7:
            basis.append([p / norm for p in v])
            informative.append(d)
        else:
            dependent.append(d)
    return {"dimensions": len(dims), "constant": const, "duplicates": dup,
            "linearly_dependent": dependent, "effective_rank": len(informative),
            "informative": informative}
