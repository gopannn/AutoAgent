"""Property, fuzz, determinism and concurrency tests.

The suite in `test_topo.py` pins known behaviours and known defects. This one
attacks the invariants with generated input, because the defects that actually
reach production are the ones nobody thought to write a case for. The
identity-vs-equality bug in v4's unifier is the example: it needed a record
where the same sub-relation is written twice, which no hand-written test in
either package happened to contain.

Every generator is seeded, so a failure here is reproducible from its output.

    python3 tests/test_robustness.py
"""

from __future__ import annotations

import os
import random
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from topo import (                                              # noqa: E402
    CORE_SCHEMA, Knobs, MalformedRelation, R, Record, TargetPattern,
    TopoStore, align, align_k_best, run_premortem, run_transfer, validate,
)
from topo.store import MAX_RELATION_ORDER                        # noqa: E402
from topo.transfer import project, subsumed_relations            # noqa: E402

ROLES = ("AGENT", "RESOURCE", "LOAD", "SIGNAL")
TRIALS = 200


# --------------------------------------------------------------------------
# Generator
# --------------------------------------------------------------------------

def gen_entities(rng: random.Random, prefix: str) -> dict[str, str]:
    """One entity per role, so every signature has something to bind."""
    return {f"{prefix}-{role.lower()}": role for role in ROLES}


def _pick_entity(rng, types, allowed):
    cands = [e for e, r in types.items()
             if allowed is None or any(CORE_SCHEMA.role_satisfies(r, a)
                                       for a in allowed)]
    return rng.choice(cands) if cands else None


def gen_relation(rng: random.Random, types: dict[str, str],
                 depth: int = 0) -> object | None:
    """A random relation that is well-formed by construction.

    Built from the schema rather than from a fixed template, so the generated
    structure exercises argument kinds and role constraints the hand-written
    library never uses.
    """
    preds = sorted(CORE_SCHEMA.vocabulary)
    rng.shuffle(preds)
    for pred in preds:
        sig = CORE_SCHEMA.signature(pred)
        if sig is None:
            continue
        args = []
        ok = True
        for spec in sig.args:
            if spec.rel_ok and (depth < 2 or not spec.entity_ok):
                sub = gen_relation(rng, types, depth + 1)
                if sub is None:
                    ok = False
                    break
                args.append(sub)
            elif spec.entity_ok:
                e = _pick_entity(rng, types, spec.roles)
                if e is None:
                    ok = False
                    break
                args.append(e)
            else:
                ok = False
                break
        if ok:
            return R(pred, *args)
    return None


def gen_record(rng: random.Random, rid: str, domain: str) -> Record:
    types = gen_entities(rng, "e")
    rels = []
    for _ in range(rng.randint(3, 8)):
        r = gen_relation(rng, types)
        if r is not None:
            rels.append(r)
    return Record(id=rid, domain=domain,
                  attrs={"incident": True, "severity": rng.randint(1, 5)},
                  types=types, rels=tuple(rels),
                  text=f"generated record {rid}")


def gen_target(rng: random.Random) -> TargetPattern:
    types = gen_entities(rng, "t")
    rels = [r for r in (gen_relation(rng, types)
                        for _ in range(rng.randint(2, 5))) if r is not None]
    return TargetPattern(name="generated", domain="generated-domain",
                         rels=tuple(rels), types=types)


# --------------------------------------------------------------------------
# Structural invariants
# --------------------------------------------------------------------------

def test_generated_records_are_schema_clean():
    """The generator must not be able to produce a SIGNATURE or ARITY error.

    If it can, either the generator or the schema is wrong, and every property
    below it is testing the wrong thing.
    """
    rng = random.Random(1)
    for i in range(TRIALS):
        v = validate(gen_record(rng, f"g{i}", "d"))
        bad = {f.code for f in v.errors} & {"SIGNATURE", "ARITY", "UNTYPED"}
        assert not bad, f"generator produced {bad} on trial {i}"


def test_mappings_are_injective():
    """A mapping must be a bijection on the parts it binds. Two source entities
    sharing one target entity would let the projection collapse distinct roles
    into one and emit a relation the source never asserted."""
    rng = random.Random(2)
    for i in range(TRIALS):
        m = align(gen_record(rng, f"s{i}", "a"), gen_target(rng))
        assert len(set(m.ent_map.values())) == len(m.ent_map), f"trial {i}"
        assert len(set(m.rel_map.values())) == len(m.rel_map), f"trial {i}"


def test_projection_never_invents_an_entity():
    """Every entity in a projected relation must already exist in the target.
    The analogy licenses a rearrangement of the target's own vocabulary and
    nothing else."""
    rng = random.Random(3)
    for i in range(TRIALS):
        src, tgt = gen_record(rng, f"s{i}", "a"), gen_target(rng)
        m = align(src, tgt)
        admitted, _ = project(m, tgt)
        known = tgt.entities() | set(m.ent_map.values())
        for r in admitted:
            assert r.entities() <= known, f"trial {i}: {r}"


def test_guarded_projection_never_violates_the_schema():
    rng = random.Random(4)
    for i in range(TRIALS):
        src, tgt = gen_record(rng, f"s{i}", "a"), gen_target(rng)
        m = align(src, tgt, Knobs(use_types=False))   # deliberately unguarded
        admitted, _ = project(m, tgt, Knobs(guard_projection=True))
        for r in admitted:
            assert not CORE_SCHEMA.check_args(r.pred, r.args, tgt.types), \
                f"trial {i}: guarded projection emitted {r}"


def test_projected_relations_are_never_already_in_the_target():
    rng = random.Random(5)
    for i in range(TRIALS):
        src, tgt = gen_record(rng, f"s{i}", "a"), gen_target(rng)
        admitted, _ = project(align(src, tgt), tgt)
        assert set(admitted).isdisjoint(set(tgt.all_rels())), f"trial {i}"


def test_returned_mappings_stay_maximal_under_fuzz():
    rng = random.Random(6)
    for i in range(60):
        ms = align_k_best(gen_record(rng, f"s{i}", "a"), gen_target(rng), k=6)
        keys = [frozenset(m.rel_map.items()) for m in ms]
        for a in range(len(keys)):
            for b in range(len(keys)):
                assert a == b or not (keys[a] < keys[b]), f"trial {i}"


def test_subsumption_set_is_exact():
    """Exactly the relations nested inside another emitted relation — no more,
    no fewer. Over-marking silently buries real findings at the bottom of the
    checklist, which is worse than not demoting at all."""
    rng = random.Random(7)
    for i in range(60):
        src, tgt = gen_record(rng, f"s{i}", "a"), gen_target(rng)
        admitted, _ = project(align(src, tgt), tgt)
        got = subsumed_relations(admitted)
        expected = {r for r in admitted
                    if any(r in _nested(other) for other in admitted
                           if other != r)}
        assert got == expected, f"trial {i}: {got ^ expected}"


def _nested(rel):
    out = []
    for a in rel.args:
        if not isinstance(a, str):
            out.append(a)
            out.extend(_nested(a))
    return out


# --------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------

def _fingerprint(rounds) -> str:
    return "|".join(f"{rr.index}:{p.rel}:{p.priority:.6f}"
                    for rr in rounds for p in rr.predictions)


def test_premortem_is_deterministic_across_runs():
    """Same input, same output, byte for byte.

    Sets are iterated in several places (predicates, entities, domains) and
    Python randomises string hashing per process, so a ranking that leaks set
    order would be stable within a run and unstable across deployments — the
    worst possible failure mode, because it never reproduces locally.
    """
    rng = random.Random(8)
    recs = [gen_record(rng, f"r{i}", f"d{i}") for i in range(6)]
    tgt = gen_target(rng)
    st = TopoStore()
    st.ingest_all(recs)
    first = _fingerprint(run_premortem(st, tgt, Knobs(rounds=2)))
    for _ in range(4):
        st2 = TopoStore()
        st2.ingest_all([Record(id=r.id, domain=r.domain, attrs=dict(r.attrs),
                               types=dict(r.types), rels=r.rels, text=r.text)
                        for r in recs])
        assert _fingerprint(run_premortem(st2, tgt, Knobs(rounds=2))) == first


def test_transfer_is_deterministic_under_input_reordering():
    """Ingest order must not change the ranking. It changes row positions, and
    row positions must never reach the scorer."""
    rng = random.Random(9)
    recs = [gen_record(rng, f"r{i}", f"d{i}") for i in range(6)]
    tgt = gen_target(rng)

    def run(order):
        st = TopoStore()
        st.ingest_all([Record(id=r.id, domain=r.domain, attrs=dict(r.attrs),
                              types=dict(r.types), rels=r.rels, text=r.text)
                       for r in order])
        return [str(i.rel) for rr in run_transfer(st, tgt, Knobs(rounds=1))
                for i in rr.inferences]

    a = run(recs)
    b = run(list(reversed(recs)))
    assert a == b, "ranking depends on ingest order"


def test_output_is_identical_across_process_hash_seeds():
    """The strongest determinism check available, and the only one that catches
    the failure mode that matters.

    Python randomises string hashing per process, so set iteration order is
    stable within a run and different between runs. A ranking that leaks set
    order therefore reproduces perfectly on the developer's machine and ranks
    differently in production, intermittently, with no way to bisect it. Every
    in-process determinism test above would pass while that was happening.

    This runs the whole demo in fresh interpreters under different seeds and
    compares the output with timings stripped.
    """
    import re
    import subprocess

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    demo = os.path.join(root, "demo.py")
    if not os.path.exists(demo):
        return
    timing = re.compile(r"[0-9]+\.[0-9]+\s*ms|[0-9]+\.[0-9]+m")

    outputs = []
    for seed in ("0", "1", "524287"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        proc = subprocess.run([sys.executable, demo], cwd=root, env=env,
                              capture_output=True, text=True, timeout=300)
        assert proc.returncode == 0, proc.stderr[-800:]
        outputs.append(timing.sub("", proc.stdout))

    for i, out in enumerate(outputs[1:], 1):
        assert out == outputs[0], (
            f"output under PYTHONHASHSEED differs at seed index {i}; "
            f"set-iteration order is leaking into the result")


# --------------------------------------------------------------------------
# Bounds and refusals
# --------------------------------------------------------------------------

def test_deep_nesting_is_refused_not_crashed():
    rel = R("EXCESS", "l")
    for _ in range(MAX_RELATION_ORDER - 1):
        rel = R("CAUSES", rel, R("EXCESS", "l"))
    try:
        R("CAUSES", rel, R("EXCESS", "l"))
    except MalformedRelation:
        return
    raise AssertionError("expected the nesting guard to fire")


def test_alignment_cap_bounds_a_pathological_case():
    """A wide record must not be able to make alignment unbounded."""
    types = {f"e{i}": ROLES[i % 4] for i in range(8)}
    rels = tuple(
        R("CAUSES", R("EXCESS", "e2"), R("QUEUEING", "e1"))
        if i % 2 else R("SENSES", "e0", "e3")
        for i in range(2)
    ) + tuple(R("DEPENDS", f"e{i}", "e1") for i in range(0, 8, 4))
    big = Record(id="wide", domain="a", types=types, rels=rels,
                 attrs={"incident": True})
    tgt = TargetPattern(name="t", domain="b",
                        types={"a": "AGENT", "r": "RESOURCE",
                               "l": "LOAD", "s": "SIGNAL"},
                        rels=(R("DEPENDS", "a", "r"),
                              R("CAUSES", R("EXCESS", "l"),
                                R("QUEUEING", "r")),
                              R("SENSES", "a", "s")))
    capped = align(big, tgt, Knobs(max_hypotheses=2))
    full = align(big, tgt, Knobs(max_hypotheses=600))
    assert capped.systematicity <= full.systematicity
    assert len(capped.rel_map) <= len(full.rel_map)


def test_persistence_refuses_to_stringify_silently():
    st = TopoStore()
    st.ingest(Record(id="x", domain="d", attrs={"when": object()}))
    with tempfile.TemporaryDirectory() as tmp:
        try:
            st.save(os.path.join(tmp, "s.json"))
        except TypeError:
            return
    raise AssertionError("expected save to refuse an un-serialisable value")


def test_persistence_preserves_index_key_types():
    st = TopoStore()
    st.ingest(Record(id="a", domain="d", attrs={"severity": 5,
                                                "tags": ["x", "y"]}))
    st.declare_index("severity")
    st.declare_index("tags")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "s.json")
        st.save(path)
        back = TopoStore.load(path)
    assert TopoStore.count(back.bitmap("severity", 5)) == 1, \
        "integer index key did not survive the round trip"
    assert TopoStore.count(back.bitmap("tags", "x")) == 1


# --------------------------------------------------------------------------
# Store fuzz and concurrency
# --------------------------------------------------------------------------

def test_store_survives_random_ingest_delete_sequences():
    rng = random.Random(10)
    st = TopoStore()
    st.declare_index("domain")
    live: set[str] = set()
    for i in range(400):
        if live and rng.random() < 0.3:
            victim = rng.choice(sorted(live))
            assert st.delete(victim)
            live.discard(victim)
        else:
            rid = f"r{i}"
            st.ingest(Record(id=rid, domain=f"d{i % 5}"))
            live.add(rid)
        assert len(st) == len(live)
        assert TopoStore.count(st.all_bits) == len(live)
    assert {r.id for r in st} == live
    assert {r.id for r in st.materialise(st.all_bits)} == live
    total = sum(len(v) for v in st.partition_by("domain").values())
    assert total == len(live)


def test_concurrent_ingest_loses_nothing():
    """Row positions are assigned under a lock and never reused, which is the
    property the bitmap index depends on. This checks it holds under
    contention rather than assuming the GIL is doing it."""
    st = TopoStore()
    st.declare_index("domain")
    errors: list[BaseException] = []

    def worker(w: int) -> None:
        try:
            for i in range(120):
                st.ingest(Record(id=f"w{w}-{i}", domain=f"d{i % 4}"))
        except BaseException as exc:                  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert len(st) == 6 * 120
    assert len(set(st.pos.values())) == 6 * 120, "row positions were reused"
    assert sorted(st.pos.values()) == list(range(6 * 120))
    assert TopoStore.count(st.bitmap("domain", "d0")) == 6 * 30


def test_concurrent_reads_during_writes_stay_consistent():
    st = TopoStore()
    st.declare_index("domain")
    for i in range(200):
        st.ingest(Record(id=f"seed{i}", domain="d0"))
    stop = threading.Event()
    bad: list[str] = []

    def reader() -> None:
        while not stop.is_set():
            bm = st.bitmap("domain", "d0")
            n = TopoStore.count(bm)
            recs = st.materialise(bm)
            if len(recs) != n:
                bad.append(f"count {n} != materialised {len(recs)}")

    t = threading.Thread(target=reader)
    t.start()
    for i in range(300):
        st.ingest(Record(id=f"new{i}", domain="d0"))
    stop.set()
    t.join()
    assert not bad, bad[:3]


# --------------------------------------------------------------------------

def _run_all() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  pass  {name}")
        except Exception as exc:                      # noqa: BLE001
            failed += 1
            print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
