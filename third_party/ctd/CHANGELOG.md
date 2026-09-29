# Changelog

## 0.5.1

### Fixed

**Cross-tenant disclosure (`ctd.authz`).** The same authorization question had
three implementations returning different answers. `GraphProvider` and
`InMemoryStructuralCaseRepository` treated a record with no `tenant_id` as
visible to every tenant; `unified_close` withheld it. Duplicated policy logic
diverges — that is not a risk, it is a schedule. One implementation now, in
`ctd.authz`, with tenant failing closed.

The label asymmetry is deliberate and now stated once: an absent security label
means *unclassified*, which is a positive statement, whereas an absent tenant
tag means *unattributed*, which is not. Reversing the label default would make
every label-scoped query return nothing until a full back-fill completed, which
in practice means the scoping gets switched off.

**Nested projection guard (`ctd.kernel.transfer`).** `project()` validated only
the root predicate against the schema. `CAUSES(INCOMPLETE(cash),
QUEUEING(stage))` therefore passed — `CAUSES` takes two relations and nothing
looked inside — and the engine asserted that cash was missing required
information. Grammatical, guarded, false. Found by a deployment rather than by
the benchmark, because the benchmark corpus never produced a container whose
argument was inadmissible.

**Structured gaps on ABSTAIN (`ctd.unified_close`).** An engine whose contract
is "say what would settle it" must do so for every non-answer. The three
abstention reasons need opposite actions — more evidence cannot resolve an
underspecified query — so they are now typed rather than concatenated into one
English string the caller has to parse.

**`requires-python`** lowered from `>=3.13` to `>=3.12`, the verified floor.

**Brittle version assertion** in `test_api_v5` replaced with a comparison
against `ctd.__version__`.

**Uniqueness asserted without evidence (`ctd.kernel.close`).** With one
candidate satisfying every constraint and another left undecided,
`expect_unique=True` returned CLOSED. That is a claim about the world the
engine had not established: the undecided candidate might satisfy the
specification too, and nothing in the result revealed it existed. Now ABSTAIN
with a `__uniqueness__` gap. A CLOSED result also carries `undecided_ids`, so
an answer set is never silently mistaken for an exhaustive one.

### Added

**`ctd.oracles` and the `ask` constraint kind.** The MODEL tier — the
1,000,000-cost tier the entire cascade exists to avoid spending — was
unreachable through the unified API, which made the cost argument
unfalsifiable: you cannot show that filtering saved model calls when zero is
the only number the public surface can produce. `NullOracle` is the default and
decides nothing, so an unbound model constraint abstains rather than passing. A
real judge is bound in process, never named over the wire.

Measured, on a 40-record corpus with an identical judge and an identical
answer: 1 model call with a selective index predicate pushed down, 40 without.


`ctd.adapters` — the three-plane ERP deployment (record / resolution /
hypothesis), its incident libraries, and 16 tests including the plane-boundary
invariant that an advisory evidence class can never satisfy a commercial gate.

### Evaluation

**Corpus 11 → 20 incidents, 20 domains.** At eleven, every guard ablation
scored identically and v5.0 had to report that as a corpus limitation rather
than a result. At twenty they separate: removing kinship costs 68% of MRR,
removing subsumption demotion takes hit@1 to zero. Two components that could
not previously be shown to earn their place now demonstrably do.

**Perturbation study (`run_perturbations`).** The standing criticism — that the
corpus shares a vocabulary by construction — was an unfalsifiable caveat. It no
longer is. Re-wording 20 of 127 source relations to family siblings costs 75%
of MRR under strict equality and **nothing** under kin-credited scoring
(kin@10 = 0.45 either way): the strict metric was refusing to credit exactly
the substitution kinship exists to make. Renaming every entity is bit-for-bit
invariant, which is the load-bearing structural claim.

Both arms failed vacuously on their first run — one changed nothing and
reported a perfect score, the other changed the names the answer was written in
and reported a total collapse. Both now carry anti-vacuity tests, because an
arm that does not perturb is worse than no arm: it produces a number people
quote.

### Compatibility

`UnifiedCloseResult.gap` is retained and is now the first entry of the new
`gaps` list. No other public shape changed.

A deployment that relied on untagged records being visible under a
tenant-scoped policy has a migration path rather than a breakage:

```python
from ctd.authz import audit_tenant_tags
print(audit_tenant_tags(my_records))     # size the exposure first
```

`RuntimePolicy(tenant_mode="legacy")` restores pre-5.1 behaviour and warns on
every untagged record it admits. The warning is the point — a silent legacy
mode never gets switched off, because nobody is ever reminded it is on. Legacy
mode relaxes only the untagged case; a foreign tenant is still refused.
