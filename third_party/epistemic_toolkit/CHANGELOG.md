# Changelog

## 2.0.0 — 2026-09-21

Merge of SSIA-transferable-toolkit 1.0.0 (A) and epistemic-toolkit 1.0.0 (B).
Full rationale and reproduced defects: `docs/MERGE_ANALYSIS.md`.

### Added
- Independence groups on evidence; `CORRELATED_SOURCE` audit; fifth gate test `group_out`.
- Derived linter: affine declared rules, `rule_violated` findings with drifted row keys,
  direction-ambiguous drift, composite (2-column) determinants, true numeric rank.
- FSM: named guards + bounded resources in one model; existential and universal termination
  reported separately; `verify_encodings` byte-compare; resource-aware runtime; manifest hashes.
- Instrument: single Wilson-bound gate for null-referenced and threshold modes;
  `trials_needed`; discrimination over many samples; fingerprint lock on `measure()`.
- Regression: strict mode (untargeted checks and uncovered defects fail); manifest and
  in-memory front-ends share one engine.
- CLI commands `report` and `verify-encodings`; `tests/mutation_check.py`.

### Changed
- Robustness decisions use unclamped log-odds; exact ties are an explicit failing state.
- Observed functional dependencies are WARNING until a declared rule proves them.
- Declared `computed` with no bearing evidence is reported as `suppressed`.
- p-values use the (1 + k) / (1 + N) correction; null counts that cannot reach alpha are refused.

### Fixed (defects reproduced in the parents)
- A: quadratic FD grouping (27.5 s → <1 s on 400×14); near-unique determinant false positives;
  CSV numbers read as strings; clamped perturbation bands.
- B: existential termination presented as universal; encoding check blind to added edges;
  FPR tolerance ~10× alpha at 30 trials; zero p-values; alphabetical drift direction;
  untargeted checks passing; ties counted as a side.

### Merge defects caught and fixed
- MERGE-1: reduced null draws made the detector unable to fire, so exclusivity passed vacuously.
- MERGE-2: new severity semantics made an example check vacuous (caught by component 7).
