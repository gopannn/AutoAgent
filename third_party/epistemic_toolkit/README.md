# epistemic-toolkit 2.0.0

Seven domain-independent controls that stop careful-looking analytical systems
from publishing confidence they have not earned. Standard library only,
Python 3.10+.

This release merges **SSIA-transferable-toolkit 1.0.0** (contract-first: JSON
schemas, CLI, fingerprints, Wilson bounds, resource-bounded FSM proofs) with
**epistemic-toolkit 1.0.0** (API-first: broad detection, runtime, triggers,
mutation builder, discrimination diagnostics). See `docs/MERGE_ANALYSIS.md`
for the full comparison, the defects reproduced in each parent, and migration
notes.

```bash
bash run.sh                        # 12 build gates + 69 tests
python3 tests/mutation_check.py    # 11/11 mutants must be killed
```

| # | Module | Prevents | Merged from |
|---|---|---|---|
| 1 | `ledger` | Confidence asserted beside a one-line reason | B's API + A's contract, **+ independence groups** |
| 2 | `robustness` | A score that is really a statement about its weights | both; **unclamped decisions, explicit ties, group-out test** |
| 3 | `outcomes` / `policy` | Untestable or cancelling results shown as low numbers | A's rendering point + B's auto-classify and bands |
| 4 | `derived` | Stored columns carrying zero information; silent drift | B's detectors + A's evidence levels and rules |
| 5 | `statemachine` | Spec, diagram and database describing different lifecycles | A's prover + B's runtime and trigger |
| 6 | `instrument` | A detector with an unmeasured false-positive rate | both modes, **one Wilson-bound gate**, fingerprint lock |
| 7 | `regression` | Tests that also pass on the broken artifact | both front-ends, **strict by default** |

---

## 1. Evidence ledger

```python
from epistemic_toolkit import Evidence as E, Hypothesis as H, Ledger

led = Ledger()
led.add(H("H_CONFIG_PUSH", "The 14:02 config push caused the degradation.", prior=0.3)
    .add(E("E1", "Error rate rose 88s after the push.", "temporal", "supports", 1.6,
           source="deploy-log#4411"))
    .add(E("E2", "Rollback restored latency in 3 minutes.", "experimental", "supports", 2.4,
           source={"id": "incident-channel", "locator": "14:47"})))
print(led.breakdown("H_CONFIG_PUSH").explain())
led.validate()   # coded semantic errors
led.audit()      # one-sided, all-circular, dominant item, correlated source, missing locator
```

`logit(p) = logit(prior) + Σ direction × weight` over non-circular items.
Weight guide: 0.5 weak, 1.0 moderate, 2.0 strong, 3.0 very strong, cap 5.0.

Enforced at construction: a source or `computed_by` is required; `circular`
forces weight 0; kinds `arithmetic`, `definitional`, `derived_identity` are
always circular; neutral items weigh 0; evidence ids are unique across the
ledger. Items sharing a source share an **independence group** unless you set
`independence_group` explicitly.

Loads both parents' JSON formats (`Ledger.from_json`) and writes v2.

## 2. Robustness gate

```python
from epistemic_toolkit import gate, GateConfig
g = gate(led, "H_CONFIG_PUSH", GateConfig(runs=5000))
g.verdict, g.failures, g.band, g.minimum_flip_weight
```

ROBUST only if the side of the threshold survives: leave-one-out,
lognormal weight perturbation (≥ 95 % same side), uniform-weight ablation,
adversarial prior, and **group-out** (drop every item from one source, when a
source contributes two or more items). Sides are decided on unclamped
log-odds; an exact tie is its own state and fails.

## 3. Reporting policy

```python
from epistemic_toolkit import auto_classify, report, report_table
auto_classify(led)
print(report_table(report(led)))   # the only sanctioned way to publish
```

| Policy | Meaning | Publishes |
|---|---|---|
| `computed` | evidence bears on it and the gate passes | number + 5–95 % band |
| `suppressed` | no admissible evidence bears on it ("not measured") | null + reason |
| `unresolved` | evidence cancels or the gate fails ("evidence inconclusive") | null + reason |

`report()` re-gates every declared `computed` and demotes loudly. Set
`what_would_settle_it` to turn "we don't know" into a work item.

## 4. Derived-field linter

```python
from epistemic_toolkit import lint, load_records
rows = load_records("orders.csv")                 # CSV / JSON / SQLite
rep = lint(rows, key="order_id", config={"declared_rules": [
    {"id": "R_VAT", "determinants": ["region"], "dependent": "vat_rate",
     "lookup": {"EU": 0.2, "UK": 0.2, "US": 0.0}},
    {"id": "R_CENTS", "type": "affine", "determinants": ["subtotal"],
     "dependent": "subtotal_cents", "slope": 100}]})
print(rep.text()); rep.removable; rep.candidates
```

| Evidence level | Findings | Default severity |
|---|---|---|
| `rule_proven` | declared rule reproduces every value — or `rule_violated`, listing drifted rows | ERROR |
| `structural` | constant, duplicate, exact affine | ERROR |
| `observed` | zero-violation FD (1- or 2-column determinant) with adequate support | WARNING |
| `near` | FD violated in a few rows: probable drift, rows listed | WARNING |

A dependency counts only if ≥ max(10, 0.5 n) rows could have violated it.
`confidence_vector_rank()` reports the true numeric rank of a
multi-dimensional score.

## 5. Single-source state machine

```python
from epistemic_toolkit import Machine
m = Machine.from_json("payment.json")
print(m.prove().text())
m.generate("out/", entity_table="payment")   # refuses unless proven
m.verify_encodings({...})                    # regenerate and byte-compare
rt = m.runtime(guards={"card_valid": lambda ctx: ctx["ok"]})
rt.fire("authorize", {"ok": False})
```

Named guards, `on_failure`, `error_type`, declared events, and **bounded
integer resources** (`when` conditions, `updates`, `failure_updates`). The
proof expands (state × resource values) and checks determinism,
reachability, event coverage, dead ends, bounds, **existential** and
**universal** termination. Model retry budgets as resources; set
`require_universal_termination: false` only for loops that are meant to be
unbounded — the proof then labels them. Outputs: YAML, Mermaid, SQL enum,
SQL seed, PostgreSQL trigger, manifest with per-file SHA-256.

## 6. Instrument self-test

```python
from epistemic_toolkit import Instrument
inst = Instrument(statistic, positive, negative, null,
                  alternatives=["T_A", "T_B"], true_alternative="T_B", spec={"version": 1})
cal = inst.calibrate(trials=400, max_false_positive_rate=0.05, min_true_positive_rate=0.8)
print(cal.text())
inst.measure(real_sample, cal)   # refused if calibration failed or the instrument changed
```

Gate: Wilson-95 upper bound of FPR ≤ limit and lower bound of TPR ≥ minimum.
Additional checks: null not anti-conservative, noise degrades the
measurement, true alternative ranked first, non-zero margin, exclusive
attribution — measured over many positives. The report states how many clean
trials the FPR claim needs (≈ 73 for 5 %, 381 for 1 %). Threshold-mode and the
built-in JSON `mean_shift` instrument are also supported.

## 7. Negative tests

```python
from epistemic_toolkit import Suite
s = Suite()   # strict: untargeted checks and uncovered defects fail
s.check("ids_unique", lambda rows: len({r["id"] for r in rows}) == len(rows), catches=["dup_id"])
s.good("current", rows)
s.mutation_suite(rows, {"dup_id": lambda rows: rows.append(dict(rows[0]))})
print(s.run().text())
```

Statuses: MEANINGFUL, VACUOUS, BROKEN, ERRORED (caught only by crashing),
UNTARGETED. Or drive it from a JSON manifest with a local validator adapter
(`run_manifest`, CLI `negative`).

---

## CLI

```bash
export PYTHONPATH="$PWD/src"
python3 -m epistemic_toolkit ledger examples/data/evidence-ledger.json
python3 -m epistemic_toolkit robustness examples/data/evidence-ledger.json --config examples/data/robustness-config.json
python3 -m epistemic_toolkit report examples/data/evidence-ledger.json
python3 -m epistemic_toolkit derived examples/data/derived-fields.csv --config examples/data/derived-fields-config.json --fail-on error
python3 -m epistemic_toolkit fsm examples/data/fsm-review.json out/fsm
python3 -m epistemic_toolkit verify-encodings out/fsm
python3 -m epistemic_toolkit calibrate examples/data/instrument-calibration.json --output out/cal.json
python3 -m epistemic_toolkit measure examples/data/instrument.json out/cal.json examples/data/real-sample.json
python3 -m epistemic_toolkit negative examples/data/negative-suite.json
```

Exit codes are nonzero when a gate fails, so every command works in CI.

## Layout

```text
src/epistemic_toolkit/   implementation + CLI
schemas/                 JSON Schema 2020-12 contracts (v2; accept v1 files)
examples/data/           JSON/CSV inputs, negative fixtures, validator adapter
examples/scripts/        ex1 incident attribution · ex2 orders schema
                         ex3 payment lifecycle (v1 → v2 → v3) · ex4 duplicate detector
dist/                    artifacts rebuilt by run_examples.py
docs/MERGE_ANALYSIS.md   parent comparison, reproduced defects, migration
tests/                   69 tests + mutation_check.py
```

## Limits

Items in different independence groups are still summed as independent;
groups expose known correlations, not unknown ones. An observed dependency
can be accidental. The FSM proof covers the declared model, not guard
correctness or blocking external calls. Calibration is only as relevant as
its generators. Negative tests prove sensitivity to the mutations written.
