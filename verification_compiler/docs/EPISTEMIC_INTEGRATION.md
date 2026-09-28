# Decision-making and reasoning integration

This page covers what was analysed, what was integrated and why, and what was deliberately left out.
Three packages were reviewed:

| Package | What it is | Verdict |
|---|---|---|
| **epistemic-toolkit 2.0.0** | Seven dependency-free controls against "confidence that was not earned": evidence ledger, robustness gate, reporting policy, derived-field linter, proven single-source state machines, calibrated instruments, strict negative tests | **Integrated** into the compiler's own guarantees. Vendored at `third_party/epistemic_toolkit` |
| **CTD Unified Intelligence Engine 0.5.1** | Constrained evidence-resolution engine: structured queries over an evidence graph that end RESOLVED or abstain with typed gaps; tenant-aware authorization; structural hypothesis plane | **Integrated** as an AutoAgent tool, not in the release path. Vendored at `third_party/ctd` |
| **SSIA 2.0.0** | A conceptual protocol model of the *Lesser Key of Solomon* (eligibility guards, guarded session FSM, provenance labels) plus a 72-entry textual corpus | **Principles only.** The corpus has no role in a code compiler. The toolkit is SSIA's transferable engineering core |

Provenance, checksums and the defects found in the archives are listed in [`third_party/VENDORED.md`](../../third_party/VENDORED.md).

## Verification of the inputs

| Package | Own test suite | Other checks |
|---|---|---|
| epistemic-toolkit | 69/69 tests; 11/11 mutants killed; 12 example build gates | — |
| CTD | 260/260 tests (after declaring `openpyxl`) | Tenant isolation probed on both builds; checksums of all `src/` and `tests/` files match |
| SSIA | 4/4 bundled validators pass | The toolkit's prover finds its declared protocol **not well formed**, see below |

### Defects found

**CTD:**
- The recommended wheel is not built from the shipped source. It lacks `ctd/oracles.py`, and 7 modules differ. Install from source.
- `openpyxl` is used but undeclared.
- 3 top-level files were edited after their checksums were generated.
- The README still states Python 3.13+.

**SSIA:** `validate.py` passes, but the prover shows that the protocol in `ssia.yaml` violates SSIA's own invariant 8:
- ERROR and ABORTED are unreachable, because no declared transition enters them.
- `SESSION_ACTIVE ⇄ OPERATION_PENDING` is an unbounded loop, so universal termination fails.
- `examples/example-system.json` declares a different lifecycle again, one that skips CONTACT_PENDING.

## What was integrated into the compiler

### 1. Proven single-source lifecycle (toolkit §5)

[`protocol/compiler-lifecycle.json`](../protocol/compiler-lifecycle.json) declares the compiler as a state machine. The repair
budget is a bounded resource. Tests in `tests/test_lifecycle.py` enforce four properties:

* **Proof:** for every allowed budget (1–20), the machine is deterministic, every state is reachable, there are no
  dead ends, updates stay in bounds, and termination is *universal*. Every path ends in RELEASED, BUDGET_EXHAUSTED or ABORTED.
* **Conformance:** `graph.py` wires exactly the declared edges. A mutation check confirmed that one extra edge fails the test.
* **Runtime replay:** real graph runs (release, audit repair, exhausted budget, semantic rejection, invalid spec,
  repair mode) replay step by step on the toolkit's runtime, budget included.
* **Published diagram:** [`protocol/compiler-lifecycle.mmd`](../protocol/compiler-lifecycle.mmd) is generated from the spec and byte-compared.

Found and fixed along the way:
* `graph.py` gave every node the full set of repair, budget and abort edges, including routes its node can never take.
  One of them (`verification_compiler → builder`) would have run the builder before any codebase existed.
* With `max_repair_rounds = 0` the builder is unreachable, a configuration outside the proven space. The config now enforces `1 ≤ rounds ≤ 20`.

### 2. Calibrated secret scanner (toolkit §6)

The secret scanner decides what may be sent to model providers, so both error rates matter. It was measured
on labelled, realistic auth-service lines:

| | False-positive rate | True-positive rate |
|---|---|---|
| Before | 7.3% (Wilson-95 upper bound **9.7%**) | 89% |
| After | 0/762 held-out (upper bound ≤ 1%) | 600/600 held-out (lower bound ≥ 97%) |

The false positives (`token_type = "access_token"`, `password_reset_path = "forgot-password"`) would have aborted CI on ordinary JWT
services. The misses were the dict form `{"password": "..."}`.

The fix exempts descriptive names (`*_type`, `*_path`, `*_url`, ...) and accepts a closing quote before `:`.
A broader "identifier-like value" exemption was tried and rejected, because it hid `JWT_SECRET = "super-secret-key"`.
`tests/test_detector_calibration.py` certifies both rates on seeds held out from tuning. The number of clean trials comes
from the toolkit's `trials_needed` (381 certify 1%).

### 3. Strict negative tests for release evidence (toolkit §7)

`tests/test_negative_release_evidence.py` runs a strict suite over the release gate's three checks:
- evidence passes,
- no release blockers,
- the manifest matches the code and the lockfile.

It uses 10 defect mutations: failed, zero-case or missing acceptance results; emptied expected ids; a failed or missing
static check; a reopened finding; an unverified linked test; code changed after verification; and a swapped lockfile.

Every check must catch what it claims to catch and every defect must be caught. Reintroducing the original "empty report
passes" bug makes the suite report `evidence_passes` as **VACUOUS**.

### 4. Evidence basis of findings (toolkit §1/§3, SSIA provenance labels)

Closing a finding now records *why* it was closed:
- `verified_by_tests`: its linked acceptance tests passed.
- `model_assertion_only`: the auditor said so.
- `open`.

The manifest carries `evidence_basis` per finding. The PR report states the two closure kinds separately and never folds
"the model said so" into "closed".

`VC_REQUIRE_TEST_EVIDENCE_FOR=critical,high` blocks release when findings of those severities were closed on the auditor's word
alone. It is off by default, because the auditor can link only tests that the hidden spec contains.

## What was integrated into AutoAgent

**Tool `resolve_with_evidence(query_json, graph_path)`** (`autoagent/tools/ctd_tool.py`): runs CTD's resolver on an
evidence-graph snapshot. Only RESOLVED is an answer. Every other state is returned as an abstention with typed gaps and next actions.

* **Authorization is operator-owned.** Tenant, labels, scopes and source classes come from `CTD_TOOL_*` environment
  variables, and a query that tries to set them is refused. Tenant mode is always `strict`, so untagged evidence is invisible
  to a tenant-scoped query (tested).
* The model may only **tighten** evidence thresholds and **shrink** search budgets.
* Graph files are confined to `CTD_TOOL_ALLOWED_ROOT` and capped at 50 MB.

## What was deliberately not integrated

| Not integrated | Reason |
|---|---|
| CTD in the release decision | Its measured incident-ranking quality (hit@1 0.15, MRR 0.25 on 20 incidents) doesn't justify steering releases. The deterministic gates are simpler and already fail closed |
| Toolkit robustness gate / numeric release confidence | The compiler publishes no confidence number. Adding one would manufacture exactly the unearned confidence the toolkit warns against |
| Toolkit derived-field linter | No tabular data in the compiler |
| SSIA corpus (72 entries, correspondences) | Not relevant to software verification |

## Review of the stated advantages

| Claim | Assessment |
|---|---|
| "Runs entirely within your infrastructure; code doesn't pass through vendors" | **Not true as configured.** Requirements and code are sent to OpenAI and Anthropic. Only the sandbox, Postgres and CI are yours. The secret-scan gate limits what can leak; it doesn't make the setup self-hosted. Self-hosted models would be needed for that claim |
| "100% parity between local and CI" | Parity holds for the images (digest-pinned) and the sandbox (gVisor, same flags). GitHub-hosted runners differ in kernel and gVisor platform, and model outputs are not deterministic |
| "Resumes mid-loop after crashes via PostgresSaver" | Only while the database survives. In CI, Postgres is a service container that dies with the job, so a crashed CI run starts from scratch. Durable resumption needs an external database |
| "Zero-touch remediation" | Patches are pushed only when every gate passes. They still need human review, which the design assumes |
| "Catch failures locally before CI" | True. `smoke_gvisor.sh` runs the same sandbox locally |
| "Customizable guardrails" | True, and now safer to change: the lifecycle proof, calibration and negative suites fail if a change weakens them |
