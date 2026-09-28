# Requirement and pre-mortem gates

## Requirement constraints

`RequirementContract.constraints` contains `key`, `operator` (`eq` or `neq`),
`value`, and an exact `source_quote` from the original input. The gate checks
each quote byte for byte, then finds two kinds of contradiction for the same
single-valued key: two different equalities, or an equality and inequality
with the same value. An explicit whole-line `must use X` / `must not use X`
pair is also detected without relying on model extraction.

Conflicts and claims with absent source quotes terminate in `ABSTAINED` before
the acceptance-test compiler or architect runs. The report includes the
source quotes; the repair budget is not charged. This is a proof about the
**encoded** constraints only. Missing extraction, ambiguous scope, and more
complex logical conflicts remain unresolved; `consistent_with_encoded_constraints`
does not mean that all requirements are satisfiable.

The requirement and contract hashes in `constraint_review` are checked again at
release. The review hash is included in `artifact_hash` and the signed manifest.

## AST pre-mortem

`premortem.py` parses each Python source file before the auditor, dependency
resolver, or sandbox. It records the path, line, and deterministic check for
blocking `time.sleep` in async code and inverse nested acquisition of locks
created with `threading.Lock` / `RLock`. A witnessed defect goes to the builder
and every patch is rechecked. This is a bounded static analysis, not a proof
that the service has no concurrency bugs.

To run CTD's `PreMortemEngine`, set `VC_PREMORTEM_CASEBOOK` to an operator-owned
JSON file containing a nonempty list of `ctd.structural.StructuralCase`
objects. Each requires `metadata.incident=true`, at least one `source_refs`
entry, and a `check_templates` mapping. Install the audited source tree with
`pip install ./third_party/ctd`. In GitHub Actions, set the repository variable
to a filename in **trusted base** `verification_compiler/casebooks/`; the
workflow refuses other paths.

CTD consumes AST-observed `DEPENDS` relations from lock ordering. Those shallow
relations do not assert a causal mechanism, and CTD's strict schema can reject
them as insufficient for projection. The report says
`insufficient_structural_encoding`, `no_structural_targets`, or
`no_operator_casebook` in those situations. Any projected output is stored as
`HYPOTHESIS` with source lineage and a check. It is not a release blocker or
evidence that an incident occurred; the auditor and verifier must establish a
concrete defect before one can affect release.

The current branch intentionally contains no invented incident casebook. A
production casebook needs independently sourced incidents, versioned
normalisation, tenant policy, checks for each projected relation, and a
representative evaluation set. Its content hash and resulting review are
bound into the release artifact when supplied.
