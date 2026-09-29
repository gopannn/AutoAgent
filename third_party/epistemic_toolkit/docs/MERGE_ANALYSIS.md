# Merge analysis: SSIA-transferable-toolkit 1.0.0 × epistemic-toolkit 1.0.0

**A** = `SSIA-transferable-toolkit-v1.0.0` (2026-09-20) · **B** = `epistemic-toolkit-1.0.0` (2026-09-21)
**Result** = `epistemic-toolkit 2.0.0` (this package)

Every defect listed below was reproduced by running the parent code, not inferred from reading it.

---

## 1. What the two packages are

Both are independent extractions of the same seven controls from the SSIA project, both standard-library Python ≥ 3.10, and both target the same failure: analytical systems that emit confident output they have not earned. They share the concepts and even the formula (log-odds ledger, the same four robustness tests, the same three reporting states). They differ in philosophy.

| | A — contract-first | B — API-first |
|---|---|---|
| Size | 62 files, 11 tests | 19 files, 33 tests |
| Primary interface | JSON files + CLI + 5 JSON Schemas | Python dataclasses, fluent builders |
| Documentation | one spec per component, `dist/` proof artifacts | one README, four narrative examples |
| Epistemic stance | conservative: observed ≠ proven; interval bounds | pragmatic: broad detection, rich diagnostics |
| Distinctive strength | fingerprints, Wilson bounds, resource-bounded FSM proof, declared rules | runtime, triggers, mutation builder, discrimination checks, honest bug log |

Neither is a superset of the other. A is stricter about what counts as a claim; B detects more kinds of problem.

---

## 2. Component-by-component

### 2.1 Evidence ledger
| | A | B |
|---|---|---|
| Traceability | `source.id` **and** `source.locator` required | `source` string **or** `computed_by` |
| Circularity | explicit boolean, weight 0 | same, plus kinds `arithmetic`/`definitional` forced circular |
| Neutral items | weight unconstrained | weight must be 0 |
| Policy shape rules | suppressed ⇒ no bearing evidence; unresolved ⇒ both directions | none |
| Audit | none beyond validation | one-sided, all-circular, dominant item |
| Weight cap | none | 5.0 |

**Merged:** B's construction-time API and extra rules, A's coded semantic validator and policy-shape rules, structured sources that accept both `{id, locator}` and B's `"ref#locator"` string, and loaders for both parents' JSON. **New:** independence groups (default = source id) and a `CORRELATED_SOURCE` audit, closing the limitation both READMEs state but neither addresses.

### 2.2 Robustness gate
Both implement the same four tests. Differences decided the merge:

| Issue | A | B | Merged |
|---|---|---|---|
| Side decided on | clamped probability | clamped probability | **unclamped log-odds** |
| Exact tie | explicit `tie`, fails | counted as "not supported" | explicit tie, fails |
| Perturbation seed | seed + hash(hypothesis id) | same seed for all | A's scheme |
| Quantiles | interpolated | index-truncated | interpolated |
| Fragility metric | — | `minimum_flip_weight` | kept, on every result |
| Correlated evidence | limitation | limitation | **5th test: group-out** |

Clamping for decisions matters: in A's own worked report the perturbation median and p95 are both pinned at 0.99, so the band carries no information. Group-out matters: in A's worked ledger, all four items of `HYP_INTERFACE_IMPROVES_JUDGMENT` cite the same `UX_STUDY`; the merged gate names that as a failure reason.

### 2.3 Reporting policy
A: one `public_result()` rendering point, UI wording. B: `auto_classify`, `report`, loud demotion, `what_would_settle_it`, 5–95 % band.
**Merged:** all of the above. **Fix:** a hypothesis declared `computed` with no bearing evidence is now demoted to `suppressed` with the correct reason (B let it reach the gate, which failed it for an unrelated reason; A rejected the whole ledger).

### 2.4 Derived-field detector
The largest divergence.

| | A | B |
|---|---|---|
| Detects | exact FD (1–3 column determinants), declared lookup rules | constant, duplicate, affine, FD, near-FD drift, low variance |
| Evidence levels | observed = warning, rule-proven = error | everything exact = error |
| False-positive control | excludes determinants unique in **every** row | support ≥ max(10, 0.5 n) |
| Inputs | CSV / JSON / SQLite | list of dicts |
| Complexity | O(rows × distinct keys) per pair | O(rows) per pair |

**Reproduced defects.** A on B's 400-row orders table: **29 flagged dependencies in 27.5 s**, most of them "X depends on `subtotal`" — B's own BUG-2 (near-unique determinant) recurs in A because a column with 399 distinct values in 400 rows is not "key-like". A's CSV loader also returns strings, so `subtotal_cents = 100 × subtotal` is invisible to it. B on the same table finds all seven planted defects, but its near-dependency picked a direction by alphabetical tie-break and named `region` as the drifted column when the drift was planted in `currency`; its README says the opposite of its output.

**Merged:** B's detection surface and support criterion, A's evidence levels, entropy accounting, loaders and declared rules. **New:** affine declared rules; `rule_violated` findings that list the drifted rows; symmetric drift reported as direction-ambiguous with offending rows; safe numeric coercion for CSV (leading zeros preserved); true numeric rank in `confidence_vector_rank` (B only removed exact duplicates, so a dimension that is a blend of two others passed as informative). Same table now: correct findings, well under one second.

### 2.5 Single-source state machine
| | A | B |
|---|---|---|
| Proof space | expanded (state × resource values) | state graph only |
| Termination | universal (acyclic) | existential (can reach terminal) |
| Retry budgets | modelled as bounded resources | hidden inside guard functions |
| Encodings | YAML, Mermaid, SQL seed | YAML, Mermaid, SQL enum, seed, **PG trigger, Python runtime** |
| Drift detection | SHA-256 of source in every file | substring search for transitions |
| Event coverage | — | declared events must be used |

**Reproduced defects.** B's README lists "universal termination" as proven, but the code checks only that each state *can* reach a terminal. B's own corrected payment machine contains `PENDING_RETRY → CREATED → (declined) → PENDING_RETRY`; the limit lives in a `retries_remaining` guard the prover cannot see, and B reports the machine well-formed. B's encoding check also misses an **added** edge (it only looks for expected transitions). A's hash header detects nothing unless the file is regenerated and compared.

**Merged:** A's expanded-space prover plus B's named guards, `error_type`, event coverage, trigger and runtime. Both termination properties are reported by name; universal is required unless the spec opts out, in which case the proof says `ALLOWED unbounded cycle`. `verify_encodings` regenerates and byte-compares every file. The runtime tracks resources, so a retry budget is enforced at execution as well as proven.

### 2.6 Instrument self-test
| | A | B |
|---|---|---|
| Decision | fixed threshold | permutation null at α |
| Gate | Wilson-95 bounds on FPR and TPR | point FPR ≤ α + 3·SE + 1/n; point sensitivity ≥ 0.8 |
| Diagnostics | sensitivity curve | KS null calibration, noise ladder, alternatives, exclusivity |
| Real-data lock | fingerprint + calibration pass | none |

**Reproduced defect.** B's tolerance at its example settings (α 0.01, 30 trials) is 0.098. Zero false positives in 30 trials only bounds the true rate below **0.114** (Wilson 95 %). Certifying ≤ 1 % needs **381** clean trials. B's p-value also lacks the +1 correction and can be exactly zero, and its discrimination check used one positive sample.

**Merged:** both decision modes behind one Wilson-bound gate, B's diagnostics as additional gate checks, discrimination over many samples, the +1 p-value, a `trials_needed` report line, and A's fingerprint lock (spec + statistic source code) on `measure()`.

### 2.7 Negative tests
A: JSON manifest + adapter file, untargeted checks fail the suite. B: in-memory `Suite`, `mutation_suite`, BROKEN / ERRORED statuses, coverage map, but UNTARGETED does not fail and uncovered defects are only reported.
**Merged:** one engine, both front-ends, strict by default (untargeted and uncovered both fail; `strict=False` restores B's behaviour). A validator crash becomes per-check ERRORED rather than aborting the run.

---

## 3. Defects found during the merge (in the merged code)

The same discipline both parents recorded, applied to the integration itself:

| ID | Defect | How it was caught |
|---|---|---|
| MERGE-1 | The first merged calibrator ran the noise ladder and discrimination with 50 null draws at α 0.01. The minimum attainable p was 0.0196, so the detector could never fire, the exclusivity check passed vacuously and the naive statistic in `ex4` was reported **VALIDATED**. | Reading `ex4` output (detection 0.00 at zero noise). `decide()` now refuses null counts that cannot reach α. |
| MERGE-2 | Making observed FDs warnings changed what `removable` means. B's `ex2` check `linter_finds_nothing_removable` became VACUOUS against the `add_currency` defect it claims to catch. | Component 7 flagged it. The check now reads `removable` and `candidates`. |
| TEST-1 | A composite-determinant test expected `ab ← (a, b)`, but the fixture made `{a, b, ab}` mutually determining; the linter's answer (`b ← (a, ab)`) was correct. | Test failure; fixture changed to a lossy function. |

---

## 4. Verification

| | A | B | Merged |
|---|---|---|---|
| Unit tests | 11 | 33 | **69** (all parent tests ported, adapted where semantics tightened) |
| Build gates (`run_examples.py`) | 8 | — | **12** |
| Mutation check of the test suite | — | — | **11 / 11 mutants killed** — each mutant re-introduces one parent defect |

Run everything with `bash run.sh`; run the mutation check with `python3 tests/mutation_check.py`.

---

## 5. Behavioural changes to know when migrating

- **Observed FDs are warnings.** Declare a lookup or affine rule to make them errors, or pass `observed_fd_severity="ERROR"`.
- **Same source ⇒ one group.** Several items citing one source now face the group-out test. Give genuinely independent items distinct sources or set `independence_group`.
- **Strict negative suites.** Untargeted checks and uncovered defects fail. Use `Suite(strict=False)` or `--lenient` to relax.
- **Calibration needs more trials.** A 1 % FPR claim needs ~381 clean trials; 5 % needs ~73. The report states the number.
- **Named guards need failure targets.** A guard that can fail with no `on_failure` (or `default_failure`) is a proof error.
- **Loops must be bounded or declared.** Model retry budgets as resources, or set `require_universal_termination: false` and accept the labelled reason.

## 6. Limits that remain

The log-odds model still assumes items within different groups are independent; groups make known correlations visible, not unknown ones. An observed dependency can be accidental. The FSM proof covers the declared model, not the correctness or exclusivity of guard functions, and not external calls that block. Calibration is only as relevant as its generators. Negative tests prove sensitivity to the mutations written, not completeness.
