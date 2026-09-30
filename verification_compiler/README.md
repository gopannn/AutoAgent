# Verification Compiler

A LangGraph pipeline that turns natural-language requirements into a Python HTTP
service. It publishes a release manifest only when the evidence for that exact
codebase has been produced by processes the generated code cannot influence.

```
req_compiler → verification_compiler → architect → policy_gate → auditor → dependency_gate
                                                       ▲             │              │
                                                       │             ▼              ▼
                                                    builder ◄─── (repair) ◄─ sandbox_verify → semantic_review → release
```

Every builder patch goes back through the policy gate and a fresh audit. The release
gate checks that the audited, verified and reviewed codebase hashes all equal the
released one.

## Trust model

| Component | Sees generated code | Can influence the verdict |
|---|---|---|
| Static container (ruff, pyright, semgrep) | read-only, never executed | no: tools ignore workspace config (`ruff --isolated`, compiler-owned `pyrightconfig.json`) |
| SUT container (`uvicorn <entrypoint>`) | read-only, executed | only through its HTTP responses |
| Oracle container (hidden pytest suite) | **never mounted** | no: it alone writes `/out/junit.xml` |
| Redis container (only for stateful contracts) | no | only through what the SUT stores; the SUT's ACL user cannot flush, reconfigure or shut it down |
| Host | parsed only (`ast`, secret regexes) | no |

All containers run with `--runtime=runsc` (gVisor; required unless `VC_REQUIRE_GVISOR=0`)
and use `--read-only`, `--cap-drop=ALL`, `no-new-privileges`, UID 65534, memory, CPU and
PID limits, and `--pull=never` on digest-pinned images. The SUT and the oracle share an
`--internal` network with no egress. Nothing ever falls back to running on the host.

## Failure semantics

* **Fail closed.** A missing report, missing exit status, zero collected tests, a skipped
  test, a collection error, or an acceptance id with no test cases is a failure.
  `VerificationResult.passed` requires every check to pass and every expected
  acceptance id to have at least one passing case.
* **Repairable** failures (`validation_failed`: policy, audit, dependencies, verification,
  semantic review) go to the builder. Each builder round consumes one unit of
  `max_repair_rounds`.
* **Infrastructure** failures (`execution_failed`: no Docker or gVisor, missing image,
  broken tool or ruleset, resolver outage, invalid spec) abort immediately and never
  consume the repair budget.

## Audit ledger

Findings are keyed by a fingerprint the compiler computes from category, files and
invariants, never by an id the LLM chooses. A finding closes only when a later audit of
changed code explicitly marks it `resolved`; omitting it keeps it open. Release also
requires that every acceptance test linked to a closed finding passed. Findings with
`low` severity are recorded in the manifest as accepted risk.

## Hidden spec

The verification spec is compiled once, validated (it must parse, define tests, target
`SUT_BASE_URL`, and import only allow-listed modules), and hashed. The hash is checked
before verification and again at release. The builder sees only failing test ids, their
descriptions and the first line of each failure message; it never sees test source or
tracebacks.

## Dependencies

Dependencies must be exact `==` pins with no markers or URLs. `uv pip compile
--generate-hashes` produces a real hash lock, `pip-audit` scans it, and binary-only wheels
are downloaded on the host into a cache keyed by the lockfile hash. They are then installed
with `--no-index --require-hashes` inside a network-less container.

## Setup

```bash
pip install -r verification_compiler/requirements.txt

docker build -f verification_compiler/docker/verifier.Dockerfile -t vc-verifier:1 verification_compiler/docker
export VC_VERIFIER_IMAGE=$(docker image inspect --format '{{.Id}}' vc-verifier:1)
export VC_RUNTIME_IMAGE=python:3.12-slim@sha256:<digest>
export LANGGRAPH_POSTGRES_URI=postgresql://...
export OPENAI_API_KEY=... ANTHROPIC_API_KEY=...
# optional: VC_SIGNING_KEY=/path/ed25519.pem  VC_MAX_REPAIR_ROUNDS=4  VC_MODEL_BUILDER=...
# optional, for stateful contracts: VC_REDIS_IMAGE=redis:7.4-alpine@sha256:<digest>  VC_STATEFUL_REPLICAS=2

python -m verification_compiler --requirements "Build a secure multi-tenant JWT auth service" \
    --manifest-out release.json
```

Exit codes: `0` release ready, `1` rejected (repair budget exhausted), `2` infrastructure
or compiler error.

## Governed layers (Discover → Reason → Build → Verify → Decide → Explain)

The CTD resolver and structural kernel and epistemic-toolkit run inside the pipeline. The design, findings and
limits are in [docs/GOVERNED_ARCHITECTURE.md](docs/GOVERNED_ARCHITECTURE.md).

| Stage | What happens | Outcome on failure |
|---|---|---|
| Requirement gate | Requirements are encoded into a declared schema with verbatim quotes; CTD's kernel checks the encoding and finds antonym contradictions | **ABSTAINED** before any code |
| Discovery | Contract/code topology → CTD `premortem_v5` against a validated failure-case library | Hypotheses only; the auditor must confirm or refute each one |
| Spec governance | Coverage, assertion lint, and null-service calibration (404 / 500 / empty 200) | The spec is recompiled; every requirement needs a discriminating test |
| Decision gate | Per-requirement CTD resolution (one claim edge per observation) plus the toolkit's robustness gate | **ABSTAINED** unless every requirement is RESOLVED and JUSTIFIED |

The manifest carries `decision` and `governance`. `verify_gate` and the Kyverno policy require
`RESOLVED`/`JUSTIFIED`. Exit code 1 covers both "rejected" and "abstained".

## Reasoning guarantees

The compiler's own decisions are checked with the vendored
[epistemic-toolkit](../third_party/epistemic_toolkit). The analysis and results are in
[docs/EPISTEMIC_INTEGRATION.md](docs/EPISTEMIC_INTEGRATION.md).

* **Proven lifecycle.** [`protocol/compiler-lifecycle.json`](protocol/compiler-lifecycle.json) is the single source for
  the graph. It is proven deterministic and universally terminating for every repair budget from 1 to 20. `graph.py` must wire exactly its
  edges, and real runs replay on it step by step.
* **Calibrated secret scanner.** The false-positive rate is ≤ 1% and the true-positive rate is ≥ 97%, as Wilson-95 bounds on held-out seeds.
* **Strict negative suite.** Every release-evidence check must catch the defects it claims, and every defect class must be caught.
* **Evidence basis.** Findings are labelled `verified_by_tests` or `model_assertion_only`. Set
  `VC_REQUIRE_TEST_EVIDENCE_FOR=critical` to refuse releases where a critical finding was closed on the auditor's word alone.

## Local gVisor sandbox

```bash
sudo verification_compiler/scripts/install_gvisor.sh     # checksum-verified gVisor bundle + `runsc install`
verification_compiler/scripts/smoke_gvisor.sh            # builds images, runs the real-container suite with gVisor required
```

gVisor publishes release bundles (`gvisor.tar.bz2` + `.sha512`). The per-binary
`.../release/latest/<arch>/runsc` URL used by older instructions now returns 404.

Under gVisor, container names do not resolve: gVisor's network stack bypasses the iptables rules
that Docker's embedded DNS depends on. The sandbox therefore passes the service's IP address to
the oracle instead of a hostname.

## AutoAgent integration

`pip install -e ".[compiler]"` installs the compiler as an optional extra of AutoAgent.

* **Tool `compile_and_verify(requirements, project_path, entrypoint="")`**
  (`autoagent/tools/compiler_tool.py`). Runs `verification_compiler.api.compile_project` and
  returns the markdown report. Files change only when the build is release-ready. `project_path`
  must stay inside `VC_ALLOWED_ROOT` (default: the current directory). Heavy dependencies are
  imported only when the tool runs.
* **Agent `Software Compiler Agent`** (`autoagent/agents/compiler_agent.py`, `get_compiler_agent`).
  It may change code only through `compile_and_verify`. It retries at most once after a
  rejection and never retries infrastructure errors.

* **Tool `resolve_with_evidence(query_json, graph_path)`** (`autoagent/tools/ctd_tool.py`). Answers structured
  questions over an evidence graph with the vendored CTD resolver (`pip install ./third_party/ctd`). Only RESOLVED is an
  answer; other states come back as abstentions with typed gaps. Tenant, labels and scopes are set by the operator
  (`CTD_TOOL_TENANT_ID`, ...), never by the model.

```python
from verification_compiler.api import compile_project
result = compile_project(Path("services/api"), "app.main:app", "Add /v1/tenants/{id}/limits ...")
print(result.status, result.changed_files, result.manifest.get("artifact_hash"))
```

## GitHub Actions

Two workflows ship with the compiler. The full design is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

* **`.github/workflows/ai-compiler.yml`** (`pull_request_target`, plus a `/autoagent compile` comment
  from an owner, member or collaborator). Runs the compiler in *repair mode* on the PR's service
  directory. Its steps:
  1. The workflow and compiler come from the base branch; the PR checkout is data only.
  2. The job installs gVisor on the runner with `scripts/install_gvisor.sh`. Set the `GVISOR_RELEASE`
     variable to pin a release.
  3. It runs the compiler on the PR.
  4. On `release_ready`, it pushes the verified patch and `.verification/{release_manifest.json,
     requirements.lock}` back to the PR branch and signs the manifest keylessly with cosign.
  5. It posts or updates a report comment on the PR.
  6. On PRs opened by the Jira bridge, it comments the result on the ticket and, on release, moves
     the ticket to review (`verification_compiler.jira.feedback`).
  7. The job fails unless the build is release-ready.
* **`.github/workflows/release-gate.yml`** (push to `main`). Runs `verification_compiler.verify_gate`:
  the signature must verify, **and** the code and lockfile on `main` must hash to what was verified.
  Optionally it also builds, signs and attests a service image for admission control
  (`deploy/kyverno`, `deploy/gatekeeper`).

Repository configuration:

| Kind | Name | Example |
|---|---|---|
| variable | `VC_PROJECT_ROOT` (enables the workflows) | `services/api` |
| variable | `VC_ENTRYPOINT` | `app.main:app` |
| variable | `VC_MAX_REPAIR_ROUNDS` (optional) | `3` |
| variable | `VC_PUBLISH_IMAGE` (optional) | `true` |
| variable | `GVISOR_RELEASE` (optional) | `20260921.0` |
| variable | `VC_REDIS_IMAGE` (optional, stateful contracts; pinned by digest at run time) | `redis:7.4-alpine` |
| secret | `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` | |
| variable | `JIRA_BASE_URL`, `JIRA_REVIEW_STATUS` (optional, Jira feedback) | `https://org.atlassian.net`, `In Code Review` |
| secret | `JIRA_EMAIL`, `JIRA_API_TOKEN` (optional, Jira feedback) | |

Limitations:
* `pull_request_target` workflows only run once they are on the default branch.
* Fork PRs are skipped, because their branches can't receive pushes.
* A push made with `GITHUB_TOKEN` does not trigger new workflow runs, so the auto-commit will not
  re-run other CI. Protect `main` with a required check on this workflow.

## Jira bridge

`verification_compiler/jira/` turns Jira tickets into pull requests; `ai-compiler.yml` then verifies
them like any other PR and reports back to the ticket. The bridge authenticates every webhook
(HMAC `X-Hub-Signature`, or a token for Data Center), acts only when an issue in a mapped project is
assigned to the bot or moved to a trigger status, processes each delivery once, and never runs a model.

```bash
pip install -r verification_compiler/jira/requirements.txt
export JIRA_WEBHOOK_SECRET=... JIRA_BOT_ACCOUNT_ID=... GITHUB_TOKEN=...
export JIRA_PROJECT_REPOS='{"ABC": {"repo": "org/service", "base_branch": "main"}}'
uvicorn verification_compiler.jira.app:create_app --factory --port 8080
# or: docker build -f verification_compiler/deploy/jira-bridge.Dockerfile -t jira-bridge .
```

In Jira, add a webhook to `https://<host>/webhooks/jira` for *Issue created* and *Issue updated*
with the same secret. Settings, idempotency and failure handling are in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#jira-ingestion-implemented).

## Stateful services (Redis)

A contract declares `backing_services: ["redis"]` when requirements need state that is shared or
survives a process: rate limits, sessions, counters, locks, idempotency keys. Such a contract is
only accepted when `VC_REDIS_IMAGE` (digest-pinned) is set. Otherwise the run stops before any model
writes code. For every verification and calibration run the sandbox then:

1. starts a fresh Redis (gVisor, read-only root, no persistence, 128 MB, default user disabled) on the
   run's internal network;
2. gives the service a restricted ACL user through `REDIS_URL`. The user has application commands
   only: no `FLUSHALL`/`FLUSHDB`, `CONFIG`, `SHUTDOWN`, `DEBUG`, `ACL` or replication;
3. runs the service as `VC_STATEFUL_REPLICAS` (default 2) containers sharing that Redis, and gives the
   oracle `SUT_REPLICA_URLS`;
4. empties Redis before every test function (a pytest plugin in the oracle that holds the only admin
   credentials). A failed reset is an infrastructure error, never the service's fault.

The spec governance requires at least one test that uses `SUT_REPLICA_URLS` (write through one
replica, read through another) and forbids tests from reaching the store directly. As a result, a
service that keeps state in process memory fails twice. Its replicas disagree, and its state
survives the per-test reset and leaks into the next test. The Redis image and server version are
recorded in the evidence and the release manifest.

## Tests

```bash
python -m pytest verification_compiler/tests          # unit + graph tests, no Docker needed

VC_E2E_VERIFIER_IMAGE=$VC_VERIFIER_IMAGE VC_E2E_RUNTIME_IMAGE=$VC_RUNTIME_IMAGE \
  python -m pytest verification_compiler/tests/test_sandbox_docker.py   # real containers

VC_E2E_VERIFIER_IMAGE=... VC_E2E_RUNTIME_IMAGE=... VC_E2E_REDIS_IMAGE=$VC_REDIS_IMAGE \
  python -m pytest verification_compiler/tests/test_sandbox_redis.py    # stateful topology
```

`tests/test_admission_policies.py` also runs the Gatekeeper Rego unit tests when `opa`
is installed. The Docker suite covers these cases: a correct service passes; wrong behaviour fails; a service that
tries to overwrite reports or the spec cannot; a service that fails to start fails
closed; and `eval` is blocked by ruff and semgrep.

## Scope and known limits

* Only Python ASGI services are supported. Other `project_type`s are rejected by policy
  instead of being silently run through Python tooling.
* The LLM gates (auditor, semantic reviewer) can only block a release. Untrusted code
  reaches them inside a tag whose name carries a random nonce, but prompt injection can
  still reduce their recall. The deterministic gates are the enforcement boundary.
* The bundled semgrep ruleset is deliberately small. Vendor additional registry rules
  into `docker/semgrep-rules/` for production use.
* Wheel downloads assume `x86_64` manylinux; adjust `SandboxConfig.wheel_platform` for other
  architectures.
