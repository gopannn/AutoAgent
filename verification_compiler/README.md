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

python -m verification_compiler --requirements "Build a secure multi-tenant JWT auth service" \
    --manifest-out release.json
```

Exit codes: `0` release ready, `1` rejected (repair budget exhausted), `2` infrastructure
or compiler error.

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
  6. The job fails unless the build is release-ready.
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
| secret | `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` | |

Limitations:
* `pull_request_target` workflows only run once they are on the default branch.
* Fork PRs are skipped, because their branches can't receive pushes.
* A push made with `GITHUB_TOKEN` does not trigger new workflow runs, so the auto-commit will not
  re-run other CI. Protect `main` with a required check on this workflow.

## Tests

```bash
python -m pytest verification_compiler/tests          # unit + graph tests, no Docker needed

VC_E2E_VERIFIER_IMAGE=$VC_VERIFIER_IMAGE VC_E2E_RUNTIME_IMAGE=$VC_RUNTIME_IMAGE \
  python -m pytest verification_compiler/tests/test_sandbox_docker.py   # real containers
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
