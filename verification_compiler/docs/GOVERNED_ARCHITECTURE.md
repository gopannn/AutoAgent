# Governed architecture: Discover → Reason → Build → Verify → Decide → Explain

This plan integrates CTD 0.5.1 (resolver + structural kernel) and epistemic-toolkit 2.0 into the compiler.
It is based on what the engines actually do, established by reading their code and probing them. It is not
based on the capability descriptions alone. Each layer states what it guarantees and what it doesn't.

## Findings that shaped the plan

| Proposal | What the engines actually do | Consequence |
|---|---|---|
| "CTD compiles requirement text into a constraint graph" | CTD's compiler parses a *controlled* language over a fixed ontology. Free Jira text needs an encoding step | An LLM encodes the requirements into a declared requirement schema. Every statement must quote its source text verbatim, and CTD's kernel validates the encoding deterministically |
| "CTD detects contradictory requirements upfront" | The kernel flags a predicate and its declared antonym over the same arguments. It detects only what the encoding states | Real contradictions are caught deterministically and explained with the quoted text. This does **not** prove a specification consistent |
| "CLOSE analyses the AST for structural gaps" | CLOSE is a budgeted constraint-satisfaction query over stored records. Gap-finding is TRANSFER/PREMORTEM projecting relations from analogous cases. The engine has no AST extractor and no software failure library | Build both: a deterministic code/contract topology extractor and a curated, validated failure-case library |
| "PREMORTEM predicts failures" | Projections are `HYPOTHESIS` by CTD's own invariant, each with a check. The measured incident-ranking quality is modest (hit@1 0.15) | Risks steer the spec, the architect and the auditor. The auditor must confirm or refute each one. They are **never** release evidence |
| "CTD final resolution: RESOLVED / PARTIAL / CONTRADICTED / ABSTAIN" | Probed: conflicting claims are only compared when every observation is its own edge from the requirement. A query that filters to passing tests resolves despite a failing one (fail-open) | Per-requirement evidence graph with one edge per observation. Verified: pass → RESOLVED; pass + fail → CONTRADICTED; weak only → PARTIAL; none → UNRESOLVABLE |
| "Calibrated confidence S ≥ 0.85" | A score built from chosen weights isn't calibrated. The toolkit's own rule is to publish a number only when the conclusion is robust | The epistemic verdict is the robustness gate per requirement (ROBUST = JUSTIFIED). A band is published only when justified, and there is no 0.85 threshold |
| "Anti-tautology: tests must stress real logic" | The acceptance tests are compiler-owned black-box HTTP tests | Two checks: a static assertion lint, and **null-service calibration**. Every test runs against stub services (404-all, 500-all, 200-empty); a test that passes against a stub doesn't discriminate. Each requirement needs a discriminating test before any code is written |
| Proposed state schema (`tests_failed == 0`, single `/workspace:rw` container, `iteration < 3`) | Reintroduces the "empty report passes" and forgeable-verdict defects fixed earlier | Not adopted. The hardened schemas and two-container sandbox stay |
| The wheel `constrained_topological_engine-0.5.1` | Byte-identical to the vendored source (all 66 modules) | Recorded as the verified install artifact |

## Lifecycle (single source: `protocol/compiler-lifecycle.json`, re-proven)

```
REQ_COMPILER → REQUIREMENT_GATE ──contradiction──► ABSTAINED
                     │ consistent
                     ▼
                 DISCOVERY (premortem on contract topology)
                     ▼
          VERIFICATION_COMPILER (lint + null-service calibration + coverage)
                     ▼
     ARCHITECT / POLICY_GATE / AUDITOR (+code premortem) / BUILDER / DEPENDENCY_GATE / SANDBOX_VERIFY / SEMANTIC_REVIEW
                     ▼
               DECISION_GATE ──not justified──► ABSTAINED
                     │ every requirement RESOLVED and JUSTIFIED
                     ▼
                  RELEASE → RELEASED
```

The new terminal state is ABSTAINED. It means "the specification is contradictory" or "the evidence doesn't justify release".
It exits with code 1, is distinct from budget exhaustion, and is never charged to the builder.

## Status: implemented

| Layer | Module | Verified by |
|---|---|---|
| Requirement gate | `reasoning/requirements.py`, node `requirement_gate` | `tests/test_graph.py`: a contradiction abstains before any code; invented quotes are re-encoded, then abort |
| Discovery | `reasoning/topology.py`, `reasoning/discovery.py`, `knowledge/failure_library.json` | `tests/test_discovery.py`: every case passes the strict gate; projections are grounded and each carries a check |
| Spec governance | `reasoning/governance.py`, `DockerSandbox.calibrate_spec`, `harness/stub_server.py` | `tests/test_governance.py`; under gVisor the vacuous and 404-only tests are identified |
| Decision gate | `reasoning/decision.py`, node `decision_gate` | `tests/test_decision.py`: all six CTD states, the justification rules, and two fail-open regressions |
| Explain | manifest `decision` / `governance`, PR report tables, `verify_gate`, Kyverno | `tests/test_ci.py`, `tests/test_admission_policies.py` |
| Assurance | re-proven lifecycle (17 states, 33 transitions), abstain-path replays, 14-defect strict negative suite | `tests/test_lifecycle.py`, `tests/test_negative_release_evidence.py` |

### Defects found while building it

* **CTD fail-open (2).** A query that filtered to passing tests resolved despite a failing one. A lone failing
  observation also resolved, because the query bound any observation regardless of its claim. The fix gives each
  observation its own claim edge and binds only a supporting one. Both cases are regression tests.
* **Pyright never checked generated code.** The sandbox config used an absolute `include`, which pyright silently
  ignores, so it analysed `/harness` instead. The paths are now relative, with a gVisor regression test that a type
  error in generated code fails the gate.
* The kernel validator's alignment checks (SHALLOW, ORPHAN) are not requirement defects. The gate acts only on
  UNTYPED, ARITY, SIGNATURE, OFF-VOCAB and CONTRADICTION.
* TRANSFER projects only from alignments with causal depth ≥ 2. The extractor therefore emits relations that are
  true by construction (retries add load; requests through shared state add contention) instead of flat facts.

### Known limits

* Requirement coverage (`requirement_ids` / `invariant_ids`) is declared by the spec's author model and is not
  checked semantically. Calibration proves a test discriminates; it does not prove the test is about the requirement it claims.
* The failure library has 7 cases. Discovery is only as good as the library and the extractor's motifs.

## Work items (original plan)

1. **Requirement gate.** A requirement schema (kernel `Schema` with antonym pairs), an LLM encoding with verbatim excerpts,
   kernel validation with bounded re-encoding, and ABSTAIN on contradiction.
2. **Discovery.** A contract/AST topology extractor in CTD's core vocabulary, a curated failure-case library validated strictly
   in tests, `premortem_v5` on the contract and then on the code each audit round, and auditor verdicts per risk.
3. **Spec governance.** `requirement_ids` on acceptance tests, a coverage check, a static assertion lint, and sandbox
   null-service calibration. A requirement without a discriminating test fails spec compilation.
4. **Decision gate.** A per-requirement CTD evidence graph plus the toolkit ledger and robustness gate, and an overall
   `ctd_outcome` and `epistemic_verdict`.
5. **Explain.** A manifest `decision` block (encoding, hypotheses and verdicts, per-requirement state, evidence and
   verdict), a PR report table, and `verify_gate` and Kyverno conditions on the new fields.
6. **Assurance.** Re-prove the lifecycle, add runtime replay for the abstain paths, extend the negative suite with decision
   defects, and run end-to-end under gVisor.

## Guarantees and limits

* The deterministic gates remain authoritative. The decision gate can only *withhold* a release; it can never grant one the gates refused.
* Contradiction detection covers what the encoding states. Excerpts stop invented statements, but not missed ones.
* Structural risks are hypotheses. Their usefulness is bounded by the failure library and by the extractor's heuristics.
* "Justified" means no single observation or single model decides a requirement. It is not a probability that the code is correct.
