# Topological engine, v5

Two modes over one store, built as a component that sits beside existing
storage rather than replacing it.

```
python3 demo.py                     end to end
python3 tests/test_topo.py          40 behaviour and regression tests
python3 tests/test_robustness.py    17 property, fuzz, determinism, concurrency
python3 bench/bench_scale.py        v4 store vs v5 store, same workload
```

No dependencies. Python 3.10+.

```
CLOSE      returns the match      total satisfaction, fail-closed
TRANSFER   returns the leftover   partial alignment, hypotheses
PREMORTEM  TRANSFER pointed at incident history
```

Both modes are the same operation: *find the complement of a partial
description*. CLOSE demands every constraint be satisfied and hands back the
record. TRANSFER accepts partial alignment and hands back the part that did
**not** align, rewritten in the target's vocabulary. That is why one engine
rather than two.

---

## What v5 is

v4 was a good architecture with three holes in it, and this release closes
those three rather than adding a fourth mode.

| Hole in v4 | v5 |
|---|---|
| The encoder was checked against a flat `set[str]` of predicate names. No arity check, no argument-role check. | `schema.py`: predicate signatures, a role lattice, antonym pairs. Three of the four malformed records in the demo library were **accepted without complaint by v4** — verified by running v4's own validator. |
| The aligner committed the deepest hypothesis and never revisited it. Acknowledged in a v4 comment and left. | Bounded beam search with a maximal-mapping filter. Greedy is retained as `Knobs(aligner="greedy")` so the difference is measurable rather than asserted. |
| Accuracy was never measured. v4's own README said so. | `evaluate.py`: a held-out-tail protocol with ablations, two controls, and a label-free guard probe. Results below, including the ones that are unflattering. |

Plus a genuine correctness bug inherited from v4, found by the evaluation
harness and described under *Defects fixed*.

---

## Architecture

```
Layer 0   bitset.py      bitmap primitives; linear build and iteration
          schema.py      predicate signatures, role lattice, families, antonyms
Layer 1   store.py       records, relations, write-time enrichment, indices,
                         tombstones, persistence, shard planning
Layer 2   query.py       constraints, tiers, cost-based planner, budget
          close.py       relaxation ladder, data requests
          transfer.py    MAC/FAC, beam alignment, guarded projection
          premortem.py   failure projection with checks and assumption tracking
Layer 3   engine.py      facade, ingest gate, plan explanation, telemetry
          encoding.py    encoding validator  <- still the actual bottleneck
          evaluate.py    held-out evaluation, ablations, controls, guard probe
          interop.py     CTD v4 exchange
```

### Cost tiers

| Tier | Cost | Operation |
|---|---|---|
| INDEX | 1 | bitmap intersection, **zero records read** |
| SCAN | 100 | attribute test on a materialised record |
| SEMANTIC | 1,000 | embedding or lexical scoring |
| MODEL | 1,000,000 | an LLM call |

Constraints are ordered by learned selectivity ÷ tier cost, so anything
decidable from an index runs before anything needing a model.

### Control knobs, as runtime parameters

Most systems hardcode all four at build time.

| Knob | Implemented as |
|---|---|
| objective | the closure predicate / systematicity + convergence score |
| budget | `Budget` — calls, cost units, **and a wall-clock deadline** |
| threshold | `Verdict.UNKNOWN` — defer upward, never assume |
| restriction | index intersection (CLOSE), `reach` (TRANSFER) |

`Budget.max_ms` is new and is the honest form of the "urgency" bound. Real
systems are bounded by latency; an abstract cost ceiling is only a proxy for
it.

---

## Measured results

Everything in this section was produced by running the code in this repository.
Reproduce with `python3 demo.py` and `python3 bench/bench_scale.py`.

### Held-out tail: does structural transfer reconstruct a causal chain?

For each of 11 incidents: withhold its causal tail, restate the premise as a
design in a domain that appears nowhere in the library, delete the incident
from the library, run the pre-mortem, and ask whether the tail comes back. The
tail is ground truth — it is what actually happened in that incident, and it
was withheld.

```
arm                      n  hit@1  hit@3  hit@5 hit@10  rec@10  cov@10 deep@10   dRec    MRR   viol  cands
----------------------------------------------------------------------------------------------------------
full                    11   0.18   0.18   0.27   0.36    0.11    0.22    0.36   0.14   0.23   0.00     29
greedy aligner          11   0.18   0.18   0.27   0.36    0.11    0.22    0.36   0.14   0.23   0.00     27
no entity types         11   0.18   0.18   0.27   0.36    0.11    0.22    0.36   0.14   0.23   0.00     29
no kinship              11   0.18   0.18   0.27   0.36    0.11    0.22    0.36   0.14   0.23   0.00     27
no projection guard     11   0.18   0.18   0.27   0.36    0.11    0.22    0.36   0.14   0.23   0.00     29
single round            11   0.18   0.18   0.27   0.36    0.11    0.22    0.36   0.14   0.23   0.00     22
flat ranking            11   0.09   0.18   0.55   0.64    0.22    0.22    0.27   0.09   0.23   0.00     27
frequency control       11   0.00   0.55   0.64   0.64    0.16    0.16    0.00   0.00   0.21   0.00     79
random control          11   0.00   0.00   0.00   0.09    0.03    0.19    0.09   0.03   0.05   0.00     79
```


**`deep@10` is the column that separates this from guessing.** The frequency control — no
alignment, no mapping, no structure, just "predict the most common predicates
filled with role-compatible entities" — beats the engine at hit@3 and matches
it at hit@10. It does that by guessing order-1 relations like
`SENSES(agent, signal)`, which appear in almost every tail and are exactly what
predicate frequency is good at.

It recovers **zero** order-2 causal relations. The engine recovers them in 27%
of held-out incidents. Reconstructing causal structure is the only thing here
that structural transfer does and frequency cannot, and a metric that scores
`SENSES(caller, latency)` and
`CAUSES(AMPLIFIES(load, backend), SATURATES(backend))` as one hit each is
measuring the wrong thing.

**Subsumption demotion, and a metric that was lying.** `flat ranking` is v5.0's
behaviour: rank purely by score. Demoting a prediction already contained inside
a higher-ranked one doubles `hit@1` (0.09 → 0.18) and lifts `deep@10` from 0.27
to 0.36, while `rec@10` appears to halve.

It does not halve. `cov@10` — recall that credits containment — is **identical
at 0.22 for both arms**. Confirming `CAUSES(AMPLIFIES(x, y), SATURATES(y))`
confirms `SATURATES(y)`; plain recall was counting one finding twice and paying
the ranking to restate itself. Demotion is on by default because it wins on
every measure that is not an artefact, and both arms stay in the table so the
trade-off is visible rather than argued.

**The honest negative result:** the four guard ablations are numerically
identical. That is not evidence the guards are inert. It is evidence the corpus
cannot test them: all eleven incidents are encoded with the same four roles in
the same arrangement, so the aligner is never offered an incompatible binding
to refuse. Rather than report that as a null finding, v5 measures the guards
directly.

### Guard probe: what the guards refuse, with no gold label

An emitted relation either violates its own predicate signature under the
target's declared roles, or it does not. No label is involved. The probe feeds
in a target whose entity granularity is deliberately wrong — `vehicles` typed
as a resource rather than an agent, which is the documented dominant failure
mode that once made this engine emit "cars sensing their own journey time".

```
configuration             emitted  nonsense  refused
----------------------------------------------------
both guards on                 13         0        0
type guard off                 21         0        9
projection guard off           13         0        0
both guards off                26         5        0
```

Either guard alone suffices. With the type guard off, the projection guard
catches 9 bad projections that would otherwise ship. With both off, five
nonsense relations reach the output. Defence in depth, demonstrated rather than
claimed.

### Store: the quadratic terms are gone

Same workload, same process, three indexed attributes, one of them
multi-valued. `x` is v4 time ÷ v5 time.

```
      n    ingest v4  ingest v5      x    20 queries v4       v5      x    5 materialise v4       v5      x
-----------------------------------------------------------------------------------------------------------
   1000         2.5m       2.8m   0.9x             0.0m     0.1m   0.5x                0.3m     0.1m   4.4x
   4000        12.2m      12.6m   1.0x             0.1m     0.1m   1.0x                2.1m     0.3m   7.7x
  16000        58.9m      57.0m   1.0x             0.4m     0.4m   1.0x               21.8m     1.3m  17.4x
  32000       169.9m     106.2m   1.6x             0.8m     0.7m   1.1x               68.7m     2.6m  26.2x
```

Both stores returned identical result-set cardinalities at every size, so this
measures the access pattern and nothing else.

Materialisation is the real result: the multiplier grows with n (4.4× → 26.2×),
which is what a quadratic term being removed looks like. v4 wrote
`[r for i, r in enumerate(records) if bitmap >> i & 1]`, and `bitmap >> i`
re-allocates the entire big integer on every iteration.

**Ingest.** v5.0 shipped with a second quadratic term still in the write path:
`self._live |= 1 << i` on every ingest allocates an i-bit integer, which is
exactly the cost the postings rewrite had just removed. Liveness is now a set
of tombstoned positions materialised into a bitmap only on demand, and cache
invalidation is a per-attribute version counter rather than a per-key dict
eviction.

Measured over three repeats, v5 ingest is **~7% slower below 4k records and
~1.3–1.6× faster from 16k up**. The crossover is around 8–10k. That residual
small-n cost is real and is the price of the postings indirection; if your
store is a few thousand records that never grow, v4's ingest path was fine.

### CLOSE: probes halved

The demo's first CLOSE ran 8 index probes under v4 and runs 4 under v5, for
identical results. v4 called `constraint.bitmap(store)` once inside the sort
key that ordered constraints by cardinality and again inside the intersection
loop, so every probe was performed — and counted — exactly twice.

---

## Defects fixed

Each has a regression test in `tests/test_topo.py` naming it.

**Identity instead of equality in unification.** v4's one-to-one consistency
check was `rel.get(b, t) is not t`. `Rel` is a frozen dataclass, so
`R("QUEUEING", "backend")` written in two different top-level relations
constructs two objects that are equal and not identical. Any relation sharing a
sub-relation with an already-committed one therefore failed to unify —
silently, with a truncated mapping as the only symptom.

On this corpus that cost roughly half of every mapping and, downstream, the
entire sensing-and-response arm of every projection: the engine could not bind
the signal entity, so `SENSES`, `RETRIES` and everything causally after them
were unprojectable. Fixing it moved held-out `rec@10` from 0.08 to 0.22 and
`hit@5` from 0.18 to 0.55. The evaluation harness found this; nothing else
would have.

**An unindexed attribute returned an empty bitmap.** Indistinguishable from "no
match" downstream — and with `negate=True` it returned the entire universe,
silently inverting a filter into a no-op. Now raises `UnindexedAttribute`.

**Record equality was field-wise.** `rec in candidates` deep-compared every
attribute dict, and two structurally identical records compared equal. Identity
is now the id.

**Relaxation only tried prefixes.** Declaring `("a", "b")` droppable with
`max_drops=1` could never try dropping `"b"` alone. Now enumerates combinations
in increasing size, deterministically, stopping at the first closure.

**The cost ceiling was checked after the spend.** A SEMANTIC stage over 10,000
survivors blew the ceiling by 10,000 evaluations and then reported it.
Pre-flight now, per stage, plus a wall-clock deadline.

**`lexical` scanned every attribute value.** Including, on this corpus, the
stored check-procedure text — so a query term could match on unrelated
metadata. Searchable fields are now explicit.

**Malformed relations were constructible.** `R("CAUSES", "x")` and
`R("FLOWS", ["a"])` were both accepted and failed later, elsewhere.

**A second quadratic term survived in the write path.** v5.0 removed the
O(n²/64) index build and then put `self._live |= 1 << i` in `ingest`, which
allocates an i-bit integer per record. Liveness is now a tombstone set
materialised on demand.

**`save` stringified silently.** `json.dump(..., default=str)` turns an
un-serialisable index key into its repr, so a store that round-trips through
disk comes back with different index keys than it went in with and every query
on that attribute quietly stops matching. It now refuses.

**Relation nesting was unbounded**, so a malformed input could drive the
recursive walkers into a stack overflow — an interpreter-level crash rather
than a handled error. Capped at 16, which is four times any real encoding.

**`Rel.order` was recomputed recursively on every access**, and it is read on
every compatibility test, every score and every sort key in the aligner. Cached
at construction.

**Alignment was unbounded.** Candidate pairs are |source relations| ×
|target relations|, so a 60-relation case against a 40-relation target is 2,400
pairs × beam width × a dict copy each. `Knobs.max_hypotheses` caps it after the
deepest-first sort, so what gets dropped is the shallowest.

**Role compatibility was exact string equality** (in both v4 and the CTD
package). Two sibling refinements of the same parent — a connection `POOL` and
an API `GATEWAY`, both resources — refused to bind, which forbids exactly the
cross-domain transfer the engine exists to perform. Compatibility is now a
least-common-ancestor test over the role lattice; disjoint ancestries are still
refused.

---

## Robustness

`tests/test_topo.py` pins known behaviours and known defects. `tests/test_robustness.py`
attacks the invariants with generated input, because the defects that reach
production are the ones nobody wrote a case for — the identity-vs-equality bug
is the example, since catching it needed a record where the same sub-relation is
written twice, which no hand-written test in either package happened to contain.

Relations are generated *from the schema*, not from a template, so the fuzzer
exercises argument kinds and role constraints the hand-written library never
uses. A guard test asserts the generator cannot itself emit a SIGNATURE or
ARITY error; if it could, every property below it would be testing the wrong
thing.

**Invariants held under 200 generated trials each**

- mappings are injective in both directions — two source entities can never
  share one target entity, which would let projection collapse distinct roles
  and emit a relation the source never asserted
- projection never invents an entity absent from the target
- guarded projection never emits a relation violating its own signature, even
  when alignment is deliberately run unguarded
- projected relations are never already present in the target
- returned mappings stay maximal
- the subsumption set is *exactly* the nested relations, no more and no fewer —
  over-marking would silently bury real findings at the bottom of the checklist

**Determinism**

Three levels, because the one that matters is the one that never reproduces
locally. In-process repetition; ingest-order invariance (row positions must
never reach the scorer); and **identical output across processes under varying
`PYTHONHASHSEED`**. Python randomises string hashing per process, so a ranking
that leaks set-iteration order is stable on the developer's machine and
different in production, intermittently, with nothing to bisect. Both
in-process tests pass while that is happening. Verified at seeds 0, 1 and
524287 with timings stripped.

**Concurrency**

Row positions are assigned under a lock and never reused, which is the property
the whole bitmap index depends on. Tested under contention (6 threads × 120
ingests) rather than assumed from the GIL, plus a reader thread checking that
`count(bitmap)` and `len(materialise(bitmap))` never disagree while writes are
in flight.

**Bounds and refusals**

Nesting depth, alignment hypotheses, wall-clock deadline, model calls, semantic
calls, cost units. Every one refuses before the spend rather than reporting the
overrun afterwards.

---

## The encoding is still the bottleneck

Both modes have the same bottleneck wearing two hats: CLOSE depends on spec
extraction, TRANSFER on relational encoding. A bad encoding fails *silently* —
it either produces structured nonsense or sits in the library never matching
anything. Improving the aligner buys almost nothing; `encoding.py` is where the
leverage is.

v4 had six checks. v5 has eleven.

| Code | Catches | New |
|---|---|---|
| UNTYPED | entities that can bind to any kind and project nonsense | |
| BARREN | relations referencing unanchored entities — can never project | |
| SHALLOW | no higher-order structure; surface matching only | |
| OFF-VOCAB | predicates nothing else can align with | |
| ORPHAN | entities appearing in one relation only | |
| **ARITY** | `CAUSES` with one argument — accepted and mis-glossed by v4 | ● |
| **SIGNATURE** | `SENSES(broker, collector)`: a resource observing an agent | ● |
| **CONTRADICTION** | a predicate and its antonym over identical arguments | ● |
| **CYCLE** | a loop in the causal graph, so direction is unrecoverable | ● |
| **SELF-CAUSE** | `CAUSES(P, P)` | ● |
| ROUNDTRIP | *encoded correctly, encodes the wrong thing* | |

Round-trip rendering remains the only check that catches the last category, and
nothing automates it:

```
- caller depends on backend
- there is too much load, which causes work piles up at backend
- work piles up at backend drives up latency
- caller observes latency
- caller observes latency, which causes caller retries against backend
- caller retries against backend, which causes load is amplified at backend
- load is amplified at backend, which causes backend runs out of headroom
```

A human reads that against the source postmortem and confirms it still says the
same thing.

### Two deliberate non-decisions in the schema

Structural predicates (`DEPENDS`, `SHARED`, `FLOWS`, `CONTAINS`) are in **no**
family. They are the anchors the rest of a mapping hangs off, and letting
`DEPENDS` kin-match `SHARED` would loosen every downstream binding at once.

`CAUSES` is in **no** family and specifically is not kin to `PRECEDES`.
Temporal sequence is not causation, and a system that silently aligns the two
projects correlations as mechanisms.

---

## Assumption tracking

v4 ran multiple rounds, enriching the design with its own top predictions
between rounds, and printed round 2 in the same format and with the same
authority as round 1. But a round-2 prediction derived from a mapping that used
a round-1 *prediction* holds only if that earlier, unverified guess holds. A
reviewer working down the checklist could not see that, and would spend real
engineering time checking a prediction whose premise had already been refuted
two lines above.

Each prediction now records which enrichments its mapping actually rested on —
read off the mapping, not inferred from the round number — and is discounted
for them:

```
1. SHARED(gateway, agent-service)  [CONDITIONAL]
   priority=  87.5  structural= 35.0  convergence=3  severity=5  confidence x0.50
   precedent: caching, database, finance (cache-stampede, pool-exhaustion, bank-run)
   HOLDS ONLY IF: SATURATES(gateway)
   CHECK: Enumerate every consumer of gateway. Confirm whether any one
          consumer can starve the others.
```

Check the premises first. If one fails, everything below it collapses and there
is no point measuring it.

---

## Convergence is protected against duplicates

Convergence — two structurally independent domains projecting the same
inference — is the strongest signal this engine emits, and it is trivially
inflated by near-duplicate records filed under different domain labels. v5
counts it as the minimum of distinct domains, distinct structure fingerprints,
and distinct declared independence groups. The ingest gate reports duplicate
groups; `test_convergence_is_not_inflated_by_duplicate_cases` pins it.

---

## Interop with the CTD v4 package

The `ctd` package (`constrained-topological-intelligence-engine-v4`) is the
production-shaped sibling of this one: evidence graph and resolver,
three-valued constraint truth, auth/IAM, durable stores, provider federation,
tracing, async bounded execution, HTTP API. The overlap between the two is
almost exactly the transfer engine.

`interop.py` is the seam. Conversion is via plain dictionaries matching CTD's
`StructuralCase` and `StructuralTarget` JSON shapes — deliberately not via
`import ctd`, which would drag a pydantic and FastAPI dependency tree into a
package whose value proposition is having none.

**Adopted from CTD**, because these were better:

- *family-aware MAC scoring*. Plain Jaccard over exact predicate names scores a
  case at zero when its overlap with the target is entirely kin, so the cheap
  filter drops it before the aligner ever sees it. The kin discount belongs in
  retrieval as well as in scoring.
- *several mappings per case*, not only the best one.
- *explicit `independence_group`*, which records what a human knows to be
  causally independent rather than what happens to look different. Honoured
  directly, and combined with the automatic fingerprint.
- *a looseness penalty*: a mapping held together entirely by kinship is a weaker
  analogy than one held together by shared predicates, and ranking has to say
  so or kinship becomes a free pass.
- *the plane separation*, as an enforced invariant. Every payload leaves
  `interop.py` as `state=HYPOTHESIS`. There is no code path in this package that
  emits a resolved claim; promotion to truth stays with the evidence resolver,
  which is the only component holding evidence to do it with.

**Contributed back**, because CTD's encoding gate checks none of these: arity,
argument kind, entity role against a signature, antonym contradiction, causal
cycles. A CTD-accepted case can be structurally incoherent in five distinct
ways. `interop.import_cases` is where they get caught, before anything reaches
the aligner. CTD's `_type_ok` is also exact string equality, so it inherits the
sibling-role problem described above.

---

## Honest accounting

**Real.** Model calls, records materialised, index probes, latency, abstention
behaviour. These follow from filtering and are independent of how good the
model or retriever is. Severity on each prediction is observed data from the
incident record.

**Real.** The held-out-tail numbers, on this corpus, and the store benchmark.
The tail is withheld ground truth; the controls run the identical protocol
through the identical metric.

**Real.** The guard probe. An emitted relation either violates its own
predicate signature or it does not.

**Proxy.** Systematicity and convergence measure whether a transfer is
structurally well-founded. They do not measure whether the failure will occur.

**Not measured.** Real-world predictive accuracy. Eleven hand-encoded incidents
sharing a vocabulary by construction is a mechanism test, not an
incident-prediction benchmark. `hit@1 = 0.09` is a weak number and should be
read as one.

**Simplifications.** Alignment is a bounded beam, not full SME with competing
global mappings scored to exhaustion. Planner statistics are calibrated offline
from logged outcomes, deliberately not a runtime reinforcement learner: at query
time there is no reward signal, policy inference would cost more than the
savings, and the distribution is non-stationary. This is ordinary cost-based
query planning.

**Not built, on purpose.** A storage-layer replacement. Components get adopted
into systems; platforms ask systems to rebuild around them.

---

## The experiment to run next

A real postmortem corpus, encoded by someone who is not the person who wrote
the engine, with a real model doing spec extraction. Four numbers:

1. **deep hit@k** on held-out tails — the metric that separates this from
   frequency guessing
2. **model tokens** per query against a dense-retrieve-then-verify baseline
3. **latency** p50 / p95 under `Budget.max_ms`
4. **correct-abstention rate** — how often it refuses when it should

The claim to falsify: *equal or better deep hit@k at ≤10% of the baseline's
model tokens.*

If it loses, the informative question is which of two things broke.
**Spec extraction** — the natural-language query became the wrong predicate.
This is the binding constraint on the whole design and where research effort
should go; instrument it separately or you will not be able to tell the failure
modes apart. **Constraint expressiveness** — the question could not be
expressed as predicates at all. That tells you the domain boundary.

---

## Prior art

- **MAC/FAC** (Forbus, Gentner & Law 1995) — cheap content filter then
  expensive structural alignment. The two-stage cascade, published.
- **SME** (Falkenhainer, Forbus & Gentner 1989) — the alignment algorithm.
- **Structure-mapping theory** (Gentner 1983) — analogy matches relations, not
  attributes; surface similarity actively hurts.
- **TRIZ** — principles abstracted from patent corpora for cross-domain
  transfer. Worth reading for its documented failure modes.
- **Magic sets** (Datalog) — pushing query constraints into the search.
- **Cost-based query optimisation** — every production DBMS.

## Files

```
topo/bitset.py      bitmap primitives
topo/schema.py      signatures, role lattice, families, antonyms
topo/store.py       records, relations, indices, tombstones, persistence
topo/query.py       constraints, tiers, planner, budget, outcomes
topo/close.py       CLOSE executor
topo/transfer.py    MAC/FAC, beam alignment, guarded projection
topo/premortem.py   failure projection with checks and assumptions
topo/encoding.py    validator + round-trip gloss
topo/evaluate.py    held-out evaluation, ablations, controls, guard probe
topo/interop.py     CTD v4 exchange
topo/engine.py      facade, ingest gate, plan explanation, telemetry
library.py               11 incidents + 4 malformed encodings
demo.py                  end to end
tests/test_topo.py       40 behaviour and regression tests
tests/test_robustness.py 17 property, fuzz, determinism, concurrency tests
bench/bench_scale.py     v4 store vs v5 store
```
