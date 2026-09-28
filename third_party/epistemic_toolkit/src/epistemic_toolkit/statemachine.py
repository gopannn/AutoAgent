"""Component 5 — Single-source finite state machine.

One JSON/dict definition. YAML, Mermaid, SQL seed, SQL enum, a PostgreSQL
transition trigger and a Python runtime are GENERATED from it; every encoding
carries the SHA-256 of the canonical source, and `verify_encodings` regenerates
and byte-compares, so hand edits (added OR removed edges) are detected.

Model (merged)
  states       normal | terminal; `initial_state` field (A) or kind="initial" (B)
  transitions  (from, event) unique; `to`; `on_failure`; named `guard` (B);
               resource guards `when` + integer `updates`/`failure_updates` (A);
               `error_type` (B)
  resources    finite integer counters with min/initial/max (A) — retry budgets
               live in the model, not in prose
  events       optional declared list; coverage is checked (B)

Outcome semantics
  A transition may FAIL when its guard is not "true" or when it declares an
  explicit failure target (the action itself can fail). Both branches are
  explored by the prover. A guard that can fail with nowhere to go is an error.

Proof — over the expanded (state, resource-values) graph (A), plus B's checks
  deterministic, all states reachable, every declared event used, no undeclared
  events, no reachable non-terminal dead end, resource updates stay in bounds,
  existential termination (every reachable configuration CAN reach a terminal),
  universal termination (the reachable non-terminal graph is acyclic).

  Toolkit B's README claimed "universal termination" but only checked the
  existential property. Its own payment example has an unbounded
  PENDING_RETRY -> CREATED -> PENDING_RETRY loop (the retry limit lived inside
  a guard function the prover cannot see) and was reported well-formed. Here
  that machine fails with a cycle witness; modelling the budget as a resource
  makes it pass. Set require_universal_termination=False only for machines
  whose loops are intentionally unbounded — the proof then says so explicitly.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

FSM_SCHEMA = "epistemic-toolkit/fsm/2.0.0"
OPS = {"eq": lambda x, y: x == y, "ne": lambda x, y: x != y, "gt": lambda x, y: x > y,
       "gte": lambda x, y: x >= y, "lt": lambda x, y: x < y, "lte": lambda x, y: x <= y}


class MachineError(ValueError):
    pass


@dataclass
class State:
    id: str
    kind: str = "normal"
    description: str | None = None


@dataclass
class Transition:
    source: str
    event: str
    target: str
    on_failure: str | None = None
    guard: str = "true"
    when: dict = field(default_factory=dict)
    updates: dict = field(default_factory=dict)
    failure_updates: dict = field(default_factory=dict)
    error_type: str | None = None

    @property
    def can_fail(self) -> bool:
        return self.guard != "true" or self.on_failure is not None

    def to_dict(self) -> dict:
        d = {"from": self.source, "event": self.event, "to": self.target}
        if self.on_failure is not None:
            d["on_failure"] = self.on_failure
        if self.guard != "true":
            d["guard"] = self.guard
        for k in ("when", "updates", "failure_updates"):
            if getattr(self, k):
                d[k] = getattr(self, k)
        if self.error_type:
            d["error_type"] = self.error_type
        return d


@dataclass
class Resource:
    id: str
    min: int
    initial: int
    max: int


@dataclass
class Proof:
    well_formed: bool
    source_sha256: str
    checks: dict[str, bool]
    counts: dict[str, int]
    reasons: list[str]
    unreachable_states: list[str]
    dead_ends: list[dict]
    stranded_configurations: list[str]
    invalid_resource_updates: list[dict]
    cycle_witness: list[str]
    unused_events: list[str]
    undeclared_events: list[str]
    errors: list[str]

    def text(self) -> str:
        s = [f"well_formed: {self.well_formed}  ({self.counts.get('declared_states', 0)} states, "
             f"{self.counts.get('transitions', 0)} transitions, "
             f"{self.counts.get('reachable_configurations', 0)} reachable configurations)"]
        s += [f"  - {r}" for r in self.reasons]
        return "\n".join(s)

    def to_dict(self) -> dict:
        return {"schema": "epistemic-toolkit/fsm-proof/2.0.0", **self.__dict__}


class Machine:
    def __init__(self, *, id: str, name: str, initial_state: str, states: list[State],
                 transitions: list[Transition], resources: list[Resource] | None = None,
                 events: list[str] | None = None, default_failure: str | None = None,
                 require_universal_termination: bool = True):
        self.id, self.name = id, name
        self.initial = initial_state
        self.states = {s.id: s for s in states}
        self.state_list = states
        self.transitions = transitions
        self.resources = resources or []
        self.declared_events = events
        self.default_failure = default_failure
        self.require_universal_termination = require_universal_termination
        for t in transitions:
            if t.on_failure is None and default_failure and t.guard != "true":
                t.on_failure = default_failure

    # ------------------------------------------------------------------ build
    @classmethod
    def from_dict(cls, d: dict) -> "Machine":
        raw_states = d["states"]
        inits = [s["id"] for s in raw_states if s.get("kind") == "initial"]
        initial = d.get("initial_state") or (inits[0] if len(inits) == 1 else None)
        if initial is None:
            raise MachineError("declare initial_state or exactly one state with kind='initial'")
        if d.get("initial_state") and inits and inits != [d["initial_state"]]:
            raise MachineError("initial_state disagrees with the state marked kind='initial'")
        states = [State(s["id"], "normal" if s.get("kind", "normal") == "initial" else s.get("kind", "normal"),
                        s.get("description")) for s in raw_states]
        trans = []
        for t in d["transitions"]:
            unknown = set(t) - {"from", "event", "to", "on_failure", "guard", "when", "updates",
                                "failure_updates", "error_type"}
            if unknown:
                raise MachineError(f"unknown transition fields {sorted(unknown)}")
            trans.append(Transition(t.get("from"), t.get("event"), t.get("to"), t.get("on_failure"),
                                    t.get("guard", "true"), dict(t.get("when", {})),
                                    dict(t.get("updates", {})), dict(t.get("failure_updates", {})),
                                    t.get("error_type")))
        res = [Resource(r["id"], r["min"], r["initial"], r["max"]) for r in d.get("resources", [])]
        name = d.get("name") or d.get("id")
        return cls(id=d.get("id") or name, name=name, initial_state=initial, states=states,
                   transitions=trans, resources=res, events=d.get("events"),
                   default_failure=d.get("default_failure"),
                   require_universal_termination=d.get("require_universal_termination", True))

    @classmethod
    def from_json(cls, path) -> "Machine":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def to_dict(self) -> dict:
        d = {"schema": FSM_SCHEMA, "id": self.id, "name": self.name, "initial_state": self.initial,
             "states": [{k: v for k, v in {"id": s.id, "kind": s.kind, "description": s.description}.items()
                         if v is not None} for s in self.state_list],
             "transitions": [t.to_dict() for t in self.transitions]}
        if self.resources:
            d["resources"] = [r.__dict__.copy() for r in self.resources]
        if self.declared_events is not None:
            d["events"] = list(self.declared_events)
        if not self.require_universal_termination:
            d["require_universal_termination"] = False
        return d

    def fingerprint(self) -> str:
        canon = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canon.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------- structure
    def _structural_errors(self) -> list[str]:
        e = []
        ids = [s.id for s in self.state_list]
        if len(ids) != len(set(ids)) or any(not x for x in ids):
            e.append("state ids must be non-empty and unique")
        if any(s.kind not in ("normal", "terminal") for s in self.state_list):
            e.append("state kind must be normal, terminal (or initial on input)")
        if self.initial not in self.states:
            e.append(f"initial_state {self.initial!r} is not declared")
        if self.default_failure and self.default_failure not in self.states:
            e.append(f"default_failure {self.default_failure!r} is not declared")
        rids = [r.id for r in self.resources]
        if len(rids) != len(set(rids)):
            e.append("resource ids must be unique")
        for r in self.resources:
            if not all(isinstance(v, int) and not isinstance(v, bool) for v in (r.min, r.initial, r.max)):
                e.append(f"resource {r.id} needs integer min/initial/max")
            elif not r.min <= r.initial <= r.max:
                e.append(f"resource {r.id} initial value outside bounds")
        terminals = {s.id for s in self.state_list if s.kind == "terminal"}
        if not terminals:
            e.append("at least one terminal state is required")
        for i, t in enumerate(self.transitions):
            tag = f"transition {i} ({t.source}.{t.event})"
            for f_ in ("source", "event", "target"):
                if not getattr(t, f_):
                    e.append(f"{tag} lacks {f_}")
            for ref in (t.source, t.target, t.on_failure):
                if ref is not None and ref not in self.states:
                    e.append(f"{tag} references undeclared state {ref!r}")
            if t.source in terminals:
                e.append(f"{tag} leaves terminal state {t.source}")
            if t.guard != "true" and t.on_failure is None:
                e.append(f"{tag} has guard '{t.guard}' that can fail with no on_failure target")
            used = set(t.when) | set(t.updates) | set(t.failure_updates)
            if not used <= set(rids):
                e.append(f"{tag} references unknown resources {sorted(used - set(rids))}")
            for rn, cond in t.when.items():
                if not isinstance(cond, dict) or not cond or set(cond) - set(OPS) or \
                        any(not isinstance(v, int) or isinstance(v, bool) for v in cond.values()):
                    e.append(f"{tag} has an invalid condition on {rn}")
            for fld in ("updates", "failure_updates"):
                if any(not isinstance(v, int) or isinstance(v, bool) for v in getattr(t, fld).values()):
                    e.append(f"{tag} has non-integer {fld}")
            if t.failure_updates and not t.can_fail:
                e.append(f"{tag} declares failure_updates but can never fail")
        return e

    # ------------------------------------------------------------------ proof
    def prove(self, max_configurations: int = 200_000) -> Proof:
        fp = self.fingerprint()
        errors = self._structural_errors()
        dup = sorted({f"{s}.{ev}" for (s, ev), c in Counter((t.source, t.event) for t in self.transitions).items()
                      if c > 1})
        if dup:
            errors.append(f"nondeterministic (state, event) pairs: {dup}")
        used = {t.event for t in self.transitions}
        unused = sorted(set(self.declared_events or []) - used)
        undeclared = sorted(used - set(self.declared_events)) if self.declared_events is not None else []
        empty = Proof(False, fp, {}, {"declared_states": len(self.states), "transitions": len(self.transitions)},
                      list(errors), [], [], [], [], [], unused, undeclared, errors)
        if errors:
            return empty

        kind = {s.id: s.kind for s in self.state_list}
        bounds = {r.id: (r.min, r.max) for r in self.resources}
        start_res = tuple(sorted((r.id, r.initial) for r in self.resources))
        by_state = defaultdict(list)
        for t in self.transitions:
            by_state[t.source].append(t)

        def enabled(t, res):
            return all(all(OPS[op](res[n], v) for op, v in c.items()) for n, c in t.when.items())

        def apply(res, upd):
            out = dict(res)
            for n, dlt in upd.items():
                out[n] += dlt
            if any(not bounds[n][0] <= v <= bounds[n][1] for n, v in out.items()):
                return None
            return out

        start = (self.initial, start_res)
        seen, q = {start}, deque([start])
        graph: dict = defaultdict(set)
        dead, invalid = [], []
        while q:
            if len(seen) > max_configurations:
                raise MachineError(f"configuration space exceeds {max_configurations}; tighten resources")
            cur = q.popleft()
            st, rt = cur
            if kind[st] == "terminal":
                continue
            res = dict(rt)
            en = [t for t in by_state[st] if enabled(t, res)]
            if not en:
                dead.append({"state": st, "resources": res})
            for t in en:
                branches = [("success", t.target, t.updates)]
                if t.can_fail:
                    branches.append(("failure", t.on_failure or self.default_failure, t.failure_updates))
                for outcome, dst, upd in branches:
                    nxt = apply(res, upd)
                    if nxt is None:
                        invalid.append({"state": st, "event": t.event, "outcome": outcome, "resources": res})
                        continue
                    node = (dst, tuple(sorted(nxt.items())))
                    graph[cur].add(node)
                    if node not in seen:
                        seen.add(node)
                        q.append(node)

        reachable_states = {s for s, _ in seen}
        terminal_nodes = {x for x in seen if kind[x[0]] == "terminal"}
        rev = defaultdict(set)
        for s, ts in graph.items():
            for t in ts:
                rev[t].add(s)
        can_end, q = set(terminal_nodes), deque(terminal_nodes)
        while q:
            n = q.popleft()
            for p in rev[n]:
                if p not in can_end:
                    can_end.add(p)
                    q.append(p)
        stranded = sorted((x for x in seen if x not in can_end), key=str)

        # iterative DFS cycle detection over non-terminal configurations
        cycle: list = []
        colour: dict = {}
        for root in [start]:
            stack = [(root, iter(graph[root]))]
            path = [root]
            colour[root] = 1
            while stack and not cycle:
                node, it = stack[-1]
                for nxt in it:
                    if kind[nxt[0]] == "terminal":
                        continue
                    c = colour.get(nxt, 0)
                    if c == 1:
                        cycle = path[path.index(nxt):] + [nxt]
                        break
                    if c == 0:
                        colour[nxt] = 1
                        stack.append((nxt, iter(graph[nxt])))
                        path.append(nxt)
                        break
                else:
                    colour[node] = 2
                    stack.pop()
                    path.pop()

        unreachable = sorted(set(kind) - reachable_states)
        checks = {
            "deterministic": True,
            "all_declared_states_reachable": not unreachable,
            "all_declared_events_used": not unused,
            "no_undeclared_events": not undeclared,
            "no_reachable_nonterminal_dead_ends": not dead,
            "resource_updates_stay_in_bounds": not invalid,
            "existential_termination": not stranded,
            "universal_termination": not cycle and not dead and not stranded,
        }
        required = dict(checks)
        if not self.require_universal_termination:
            required.pop("universal_termination")
        reasons = []
        if unreachable:
            reasons.append(f"unreachable states: {unreachable}")
        if unused:
            reasons.append(f"declared events with no transition: {unused}")
        if undeclared:
            reasons.append(f"events used but not declared: {undeclared}")
        if dead:
            reasons.append(f"non-terminal dead ends: {[d['state'] for d in dead]}")
        if invalid:
            reasons.append(f"resource updates leave bounds: {invalid[:3]}")
        if stranded:
            reasons.append(f"configurations that cannot reach a terminal: {stranded[:5]}")
        if cycle:
            msg = "unbounded cycle (universal termination fails): " + " -> ".join(
                f"{s}{dict(r) if r else ''}" for s, r in cycle)
            reasons.append(msg if self.require_universal_termination else "ALLOWED " + msg)
        return Proof(all(required.values()), fp, checks,
                     {"declared_states": len(kind), "transitions": len(self.transitions),
                      "reachable_configurations": len(seen), "terminal_configurations": len(terminal_nodes)},
                     reasons, unreachable, dead, [str(x) for x in stranded], invalid,
                     [str(x) for x in cycle], unused, undeclared, [])

    def paths(self, max_len: int = 12, limit: int = 50) -> list:
        """Simple success paths from the initial state (resources ignored)."""
        term = {s.id for s in self.state_list if s.kind == "terminal"}
        adj = defaultdict(list)
        for t in self.transitions:
            adj[t.source].append((t.event, t.target))
        found = []

        def walk(node, path, visited):
            if len(found) >= limit or len(path) > max_len:
                return
            if node in term:
                found.append(path)
                return
            for ev, nxt in adj[node]:
                if nxt not in visited:
                    walk(nxt, path + [(node, ev, nxt)], visited | {nxt})
        walk(self.initial, [], {self.initial})
        return found

    # --------------------------------------------------------------- emitters
    def _header(self, c: str) -> str:
        return f"{c} GENERATED from {self.id}. Do not edit. source-sha256: {self.fingerprint()}"

    def to_yaml(self) -> str:
        return self._header("#") + "\n" + "\n".join(_yaml_lines({"machine": self.to_dict()})) + "\n"

    def to_mermaid(self) -> str:
        L = [self._header("%%"), "stateDiagram-v2", f"    [*] --> {self.initial}"]
        for t in self.transitions:
            g = f" [{t.guard}]" if t.guard != "true" else ""
            w = " {" + ",".join(f"{k}:{_cond_str(v)}" for k, v in t.when.items()) + "}" if t.when else ""
            L.append(f"    {t.source} --> {t.target}: {t.event}{g}{w}")
            if t.can_fail:
                L.append(f"    {t.source} --> {t.on_failure}: {t.event} / failure")
        for s in self.state_list:
            if s.kind == "terminal":
                L.append(f"    {s.id} --> [*]")
        return "\n".join(L) + "\n"

    def to_sql_enum(self, type_name: str | None = None) -> str:
        tn = type_name or f"{_ident(self.id)}_state"
        vals = ", ".join(_sql(s.id) for s in self.state_list)
        return self._header("--") + f"\nCREATE TYPE {tn} AS ENUM ({vals});\n"

    def to_sql_seed(self) -> str:
        L = [self._header("--"), "BEGIN;",
             "CREATE TABLE IF NOT EXISTS fsm_state (machine_id text NOT NULL, state_id text NOT NULL,",
             "  kind text NOT NULL, PRIMARY KEY (machine_id, state_id));",
             "CREATE TABLE IF NOT EXISTS fsm_transition (machine_id text NOT NULL, from_state text NOT NULL,",
             "  event text NOT NULL, to_state text NOT NULL, failure_state text, guard text NOT NULL,",
             "  when_json text NOT NULL, updates_json text NOT NULL, failure_updates_json text NOT NULL,",
             "  error_type text, PRIMARY KEY (machine_id, from_state, event));",
             "INSERT INTO fsm_state (machine_id, state_id, kind) VALUES",
             ",\n".join(f"  ({_sql(self.id)}, {_sql(s.id)}, {_sql(s.kind)})" for s in self.state_list) + ";",
             "INSERT INTO fsm_transition VALUES"]
        rows = []
        for t in self.transitions:
            j = lambda v: json.dumps(v, sort_keys=True, separators=(",", ":"))
            rows.append("  (" + ", ".join(_sql(x) for x in (
                self.id, t.source, t.event, t.target, t.on_failure, t.guard, j(t.when), j(t.updates),
                j(t.failure_updates), t.error_type)) + ")")
        L += [",\n".join(rows) + ";", "COMMIT;", ""]
        return "\n".join(L)

    def to_pg_trigger(self, entity_table: str, state_column: str = "state") -> str:
        """Row-level guard: a state change must match a declared success or
        failure edge, and terminal states are immutable. The database cannot see
        the event or resource values, so this enforces the edge set only."""
        fn = f"enforce_{_ident(self.id)}_transition"
        terms = ", ".join(_sql(s.id) for s in self.state_list if s.kind == "terminal")
        return f"""{self._header('--')}
CREATE OR REPLACE FUNCTION {fn}() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.{state_column} IS DISTINCT FROM OLD.{state_column} THEN
    IF OLD.{state_column}::text IN ({terms}) THEN
      RAISE EXCEPTION '%: % is terminal and immutable', {_sql(self.id)}, OLD.{state_column};
    END IF;
    IF NOT EXISTS (
      SELECT 1 FROM fsm_transition t
       WHERE t.machine_id = {_sql(self.id)}
         AND t.from_state = OLD.{state_column}::text
         AND (t.to_state = NEW.{state_column}::text OR t.failure_state = NEW.{state_column}::text)
    ) THEN
      RAISE EXCEPTION '%: % -> % is not a declared transition', {_sql(self.id)},
        OLD.{state_column}, NEW.{state_column};
    END IF;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER {fn}_guard BEFORE UPDATE OF {state_column} ON {entity_table}
FOR EACH ROW EXECUTE FUNCTION {fn}();
"""

    def encodings(self, entity_table: str = "entity") -> dict[str, str]:
        return {"protocol.yaml": self.to_yaml(), "state-machine.mmd": self.to_mermaid(),
                "enum.sql": self.to_sql_enum(), "seed.sql": self.to_sql_seed(),
                "trigger.sql": self.to_pg_trigger(entity_table)}

    def generate(self, output_dir, entity_table: str = "entity") -> dict:
        proof = self.prove()
        if not proof.well_formed:
            raise MachineError("FSM proof failed; nothing generated:\n" + proof.text())
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        enc = self.encodings(entity_table)
        manifest = {"machine_id": self.id, "source_sha256": proof.source_sha256, "entity_table": entity_table,
                    "files": {}}
        for name, text in enc.items():
            (out / name).write_text(text, encoding="utf-8")
            manifest["files"][name] = hashlib.sha256(text.encode("utf-8")).hexdigest()
        (out / "source.json").write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")
        (out / "fsm-proof.json").write_text(json.dumps(proof.to_dict(), indent=2, default=str) + "\n",
                                            encoding="utf-8")
        (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        return {"proof": proof, "manifest": manifest, "dir": str(out)}

    def verify_encodings(self, texts: dict[str, str], entity_table: str = "entity") -> list[str]:
        """Regenerate from source and byte-compare. Detects additions, deletions,
        reorderings and stale fingerprints — B's substring check missed added edges."""
        expected = self.encodings(entity_table)
        problems = []
        for name, text in texts.items():
            if name not in expected:
                problems.append(f"{name}: not a generated encoding")
            elif text != expected[name]:
                exp, got = expected[name].splitlines(), text.splitlines()
                extra = [l for l in got if l not in exp][:3]
                missing = [l for l in exp if l not in got][:3]
                problems.append(f"{name}: differs from source (added {extra}, missing {missing})")
        return problems

    def runtime(self, guards: dict[str, Callable[[dict], bool]] | None = None) -> "Runtime":
        return Runtime(self, guards or {})


class Runtime:
    """Executable instance. Guards are callables(context) -> bool; resources are tracked."""

    def __init__(self, machine: Machine, guards: dict):
        self.m = machine
        self.guards = guards
        self.state = machine.initial
        self.resources = {r.id: r.initial for r in machine.resources}
        self._bounds = {r.id: (r.min, r.max) for r in machine.resources}
        self.history: list[dict] = []
        self._index = {(t.source, t.event): t for t in machine.transitions}
        missing = sorted({t.guard for t in machine.transitions if t.guard != "true"} - set(guards))
        self.unregistered_guards = missing

    @property
    def terminal(self) -> bool:
        return self.m.states[self.state].kind == "terminal"

    def _enabled(self, t) -> bool:
        return all(all(OPS[op](self.resources[n], v) for op, v in c.items()) for n, c in t.when.items())

    def allowed(self) -> list[str]:
        return sorted(ev for (s, ev), t in self._index.items() if s == self.state and self._enabled(t))

    def fire(self, event: str, context: dict | None = None, *, action_failed: bool = False) -> str:
        if self.terminal:
            raise MachineError(f"{self.state} is terminal")
        t = self._index.get((self.state, event))
        if t is None:
            raise MachineError(f"event '{event}' not declared in {self.state}; allowed: {self.allowed()}")
        if not self._enabled(t):
            raise MachineError(f"event '{event}' not enabled in {self.state} with resources {self.resources}")
        if t.guard == "true":
            ok = True
        else:
            g = self.guards.get(t.guard)
            if g is None:
                raise MachineError(f"guard '{t.guard}' is not registered; registered: {sorted(self.guards)}")
            ok = bool(g(context or {}))
        if action_failed:
            if t.on_failure is None:
                raise MachineError(f"{t.source}.{event} declares no failure target")
            ok = False
        dest, upd = (t.target, t.updates) if ok else (t.on_failure, t.failure_updates)
        new = dict(self.resources)
        for n, d in upd.items():
            new[n] += d
            lo, hi = self._bounds[n]
            if not lo <= new[n] <= hi:
                raise MachineError(f"resource {n} would leave bounds [{lo}, {hi}]")
        self.history.append({"from": self.state, "event": event, "to": dest, "guard": t.guard,
                             "guard_passed": ok, "error_type": None if ok else t.error_type,
                             "resources": dict(new)})
        self.state, self.resources = dest, new
        return dest


# ---------------------------------------------------------------- helpers
def _sql(v) -> str:
    return "NULL" if v is None else "'" + str(v).replace("'", "''") + "'"


def _ident(s: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in s).lower()


def _cond_str(c: dict) -> str:
    return "&".join(f"{op}{v}" for op, v in c.items())


def _yaml_scalar(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return json.dumps(str(v), ensure_ascii=False)


def _yaml_lines(v, ind: int = 0) -> list[str]:
    pad = " " * ind
    if isinstance(v, dict):
        if not v:
            return [pad + "{}"]
        out = []
        for k, x in v.items():
            if isinstance(x, (dict, list)) and x:
                out.append(f"{pad}{k}:")
                out += _yaml_lines(x, ind + 2)
            else:
                out.append(f"{pad}{k}: {_yaml_scalar(x) if not isinstance(x, (dict, list)) else ('{}' if isinstance(x, dict) else '[]')}")
        return out
    if isinstance(v, list):
        out = []
        for x in v:
            if isinstance(x, dict) and x:
                sub = _yaml_lines(x, ind + 2)
                out.append(pad + "- " + sub[0].lstrip())
                out += sub[1:]
            else:
                out.append(f"{pad}- {_yaml_scalar(x)}")
        return out
    return [pad + _yaml_scalar(v)]


def prove(spec: dict) -> dict:
    """Toolkit-A compatible helper."""
    return Machine.from_dict(spec).prove().to_dict()


def generate(spec: dict, output_dir) -> dict:
    r = Machine.from_dict(spec).generate(output_dir)
    return {"proof": r["proof"].to_dict(), "manifest": r["manifest"], "dir": r["dir"]}
