"""Component 7 — Negative tests: every check must fail on the broken artifact.

Two front-ends, one engine:
  Suite             in-memory checks, good/bad artifacts, mutation_suite   (B)
  run_manifest      JSON manifest + local validator adapter file           (A)

Statuses
  MEANINGFUL   passes every good artifact, fails every bad one it targets
  VACUOUS      passes a bad artifact it claims to catch
  BROKEN       fails (or crashes on) a good artifact
  ERRORED      "catches" a targeted defect only by raising
  UNTARGETED   never shown to catch anything

Suite verdict (merged, strict by default):
  fails on VACUOUS / BROKEN / ERRORED                                     (both)
  fails on UNTARGETED — new rules cannot escape negative coverage         (A; B passed these)
  fails on an UNCOVERED defect — a bad artifact no meaningful check catches (NEW; B only reported it)
  fails on manifest errors (unknown targets, missing results)             (A)
"""
from __future__ import annotations

import copy
import importlib.util
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass
class CheckOutcome:
    name: str
    status: str
    passed_on_good: dict[str, bool]
    passed_on_bad: dict[str, bool]
    targets: list[str]
    errors: dict[str, str] = field(default_factory=dict)
    description: str | None = None


@dataclass
class SuiteResult:
    outcomes: list[CheckOutcome]
    coverage: dict[str, list[str]]
    strict: bool = True
    manifest_errors: list[str] = field(default_factory=list)

    @property
    def meaningful(self):
        return [o for o in self.outcomes if o.status == "MEANINGFUL"]

    @property
    def uncovered(self) -> list[str]:
        return sorted(b for b, c in self.coverage.items() if not c)

    @property
    def ok(self) -> bool:
        bad = {"VACUOUS", "BROKEN", "ERRORED"} | ({"UNTARGETED"} if self.strict else set())
        if any(o.status in bad for o in self.outcomes) or self.manifest_errors:
            return False
        return not (self.strict and self.uncovered)

    def text(self) -> str:
        L = [f"Negative-test suite: {len(self.meaningful)}/{len(self.outcomes)} checks meaningful "
             f"({'strict' if self.strict else 'lenient'})"]
        for o in self.outcomes:
            L.append(f"  [{o.status:10s}] {o.name}")
            if o.status == "VACUOUS":
                L.append(f"      passes bad artifact(s) {[b for b in o.targets if o.passed_on_bad.get(b)]} it should catch")
            elif o.status == "BROKEN":
                L.append(f"      fails good artifact(s) {[g for g, p in o.passed_on_good.items() if not p]}")
            elif o.status == "ERRORED":
                L.append("      'catches' a targeted defect only by crashing; return False, don't raise")
            elif o.status == "UNTARGETED":
                L.append("      no bad artifact targets this check")
            for art, err in o.errors.items():
                L.append(f"      error on {art}: {err}")
        for b in self.uncovered:
            L.append(f"  [UNCOVERED ] defect '{b}' is caught by no meaningful check")
        for e in self.manifest_errors:
            L.append(f"  [MANIFEST  ] {e}")
        L.append(f"  suite OK: {self.ok}")
        return "\n".join(L)

    def to_dict(self) -> dict:
        counts = {s: sum(o.status == s for o in self.outcomes)
                  for s in ("MEANINGFUL", "VACUOUS", "BROKEN", "ERRORED", "UNTARGETED")}
        return {"schema": "epistemic-toolkit/negative-test-report/2.0.0",
                "summary": {"checks": len(self.outcomes), **{k.lower(): v for k, v in counts.items()},
                            "uncovered_defects": self.uncovered, "passes": self.ok, "strict": self.strict},
                "checks": [o.__dict__ for o in self.outcomes], "coverage": self.coverage,
                "manifest_errors": self.manifest_errors}


class Suite:
    def __init__(self, strict: bool = True):
        self.strict = strict
        self._checks: dict[str, tuple[Callable[[Any], bool], list[str], str | None]] = {}
        self._good: dict[str, Any] = {}
        self._bad: dict[str, Any] = {}

    def check(self, name: str, fn: Callable[[Any], bool], catches: list[str] | None = None,
              description: str | None = None) -> "Suite":
        self._checks[name] = (fn, list(catches or []), description)
        return self

    def good(self, name: str, artifact: Any) -> "Suite":
        self._good[name] = artifact
        return self

    def bad(self, name: str, artifact: Any) -> "Suite":
        self._bad[name] = artifact
        return self

    def mutation_suite(self, base: Any, mutations: dict[str, Callable[[Any], Any]]) -> "Suite":
        """One bad artifact per mutation, from a deep copy of `base`. Mutations may
        mutate in place or return a replacement; a list of Nones (a list
        comprehension over in-place updates) is treated as in-place (B's BUG-6)."""
        for name, mut in mutations.items():
            art = copy.deepcopy(base)
            res = mut(art)
            in_place = res is None or (isinstance(res, list) and res and all(x is None for x in res))
            self._bad[name] = art if in_place else res
        return self

    @staticmethod
    def _safe(fn, art):
        try:
            return bool(fn(art)), None
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    def run(self) -> SuiteResult:
        if not self._good:
            raise ValueError("a negative suite needs at least one known-good artifact (positive control)")
        outcomes = []
        for name, (fn, targets, desc) in self._checks.items():
            unknown = [t for t in targets if t not in self._bad]
            if unknown:
                raise ValueError(f"check {name} targets unknown bad artifacts {unknown}")
            errs, g, b = {}, {}, {}
            for gname, art in self._good.items():
                g[gname], e = self._safe(fn, art)
                if e:
                    errs[gname] = e
            for bname, art in self._bad.items():
                b[bname], e = self._safe(fn, art)
                if e:
                    errs[bname] = e
            if any(x in errs for x in self._good) or not all(g.values()):
                status = "BROKEN"
            elif any(t in errs for t in targets):
                status = "ERRORED"
            elif not targets:
                status = "UNTARGETED"
            elif any(b[t] for t in targets):
                status = "VACUOUS"
            else:
                status = "MEANINGFUL"
            outcomes.append(CheckOutcome(name, status, g, b, targets, errs, desc))
        coverage = {bn: [] for bn in self._bad}
        for o in outcomes:
            if o.status != "MEANINGFUL":
                continue
            for bn, passed in o.passed_on_bad.items():
                if not passed and bn not in o.errors:
                    coverage[bn].append(o.name)
        return SuiteResult(outcomes, coverage, self.strict)

    def coverage(self) -> dict[str, list[str]]:
        return self.run().coverage


# ------------------------------------------------------------ manifest (A)
def load_validator(reference: str, base_dir) -> Callable[[Any], dict]:
    """Load `relative/path.py:function` relative to the manifest directory."""
    if ":" not in reference:
        raise ValueError("validator reference must be path.py:function")
    path_text, fn_name = reference.rsplit(":", 1)
    path = (Path(base_dir) / path_text).resolve()
    if not path.is_file():
        raise ValueError(f"validator module not found: {path}")
    spec = importlib.util.spec_from_file_location(f"et_adapter_{abs(hash(path))}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fn = getattr(mod, fn_name, None)
    if not callable(fn):
        raise ValueError(f"validator function not found: {fn_name}")
    return fn


def _normalise(results: dict) -> dict[str, dict]:
    out = {}
    for cid, v in results.items():
        if isinstance(v, bool):
            out[cid] = {"passes": v, "detail": ""}
        elif isinstance(v, dict) and isinstance(v.get("passes"), bool):
            out[cid] = {"passes": v["passes"], "detail": str(v.get("detail", ""))}
        else:
            raise ValueError(f"validator result {cid!r} must be bool or {{passes, detail}}")
    return out


def run_manifest(manifest: dict, base_dir, *, strict: bool = True) -> dict:
    root = Path(base_dir)
    validator = load_validator(manifest["validator"], root)
    load = lambda rel: json.loads((root / rel).read_text(encoding="utf-8"))
    goods = manifest.get("positive_artifacts") or [manifest["positive_artifact"]]
    good_arts = {g: load(g) for g in goods}
    bad_cases = manifest["broken_artifacts"]
    bad_arts = {c["id"]: load(c["artifact"]) for c in bad_cases}

    cache: dict[int, dict] = {}

    def results(art):
        k = id(art)
        if k not in cache:
            cache[k] = _normalise(validator(art))
        return cache[k]

    first = results(next(iter(good_arts.values())))
    check_ids = list(first)
    manifest_errors = []
    targets: dict[str, list[str]] = {c: [] for c in check_ids}
    for case in bad_cases:
        for cid in case["must_fail"]:
            if cid not in targets:
                manifest_errors.append(f"{case['id']} targets unknown check {cid!r}")
            else:
                targets[cid].append(case["id"])

    def make(cid):
        def fn(art):
            r = results(art)
            if cid not in r:
                raise KeyError(f"validator omitted result {cid!r}")
            return r[cid]["passes"]
        return fn

    s = Suite(strict=strict)
    for cid in check_ids:
        s.check(cid, make(cid), catches=targets[cid], description=first[cid]["detail"])
    for g, a in good_arts.items():
        s.good(g, a)
    for bid, a in bad_arts.items():
        s.bad(bid, a)
    res = s.run()
    res.manifest_errors = manifest_errors
    out = res.to_dict()
    out["suite_id"] = manifest.get("suite_id")
    out["broken_cases"] = [{"id": c["id"], "artifact": c["artifact"],
                            "checks": [{"check": cid,
                                        "status": "CAUGHT" if not next(o for o in res.outcomes if o.name == cid)
                                        .passed_on_bad.get(c["id"], True) else "VACUOUS"}
                                       for cid in c["must_fail"] if cid in targets]}
                           for c in bad_cases]
    return out


def run_negative_suite(manifest: dict, base_dir) -> dict:
    """Toolkit-A compatible name."""
    return run_manifest(manifest, base_dir)
