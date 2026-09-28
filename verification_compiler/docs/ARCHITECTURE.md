# Enterprise Architecture: AI Software Compiler

Status legend: **[implemented]** is in this repository and tested. **[planned]** is a design only.

```
                                   JIRA PLATFORM                      GITHUB PULL REQUEST
                                         │ [planned]                          │ [implemented]
                              webhook: ticket assigned               pull_request_target
                                         ▼                                    ▼
┌──────────────────────────────────────────────────────────────────────────────────────────┐
│ 1. INGESTION & CONTEXT                                                                   │
│   Jira listener → issue parser (summary, AC)      .github/workflows/ai-compiler.yml      │
│   → repo context loader              [planned]    → verification_compiler/ci.py          │
│                                                   → repo_io.load_codebase  [implemented] │
│   Skips dotfiles, reserved files, binaries, symlinks. Refuses to send files that look   │
│   like secrets to model providers.                                                       │
└───────────────────────────────────────────┬──────────────────────────────────────────────┘
                                            │ requirements text + existing codebase
                                            ▼
┌──────────────────────────────────────────────────────────────────────────────────────────┐
│ 2. COMPILER ENGINE (LangGraph + PostgresSaver)                           [implemented] │
│                                                                                          │
│  req_compiler ─► verification_compiler (hidden spec, validated + hashed)                 │
│                         │                                                                │
│             new project │ existing codebase (repair mode)                                │
│                         ▼                                                                │
│                    architect ─► policy_gate ◄──────────── builder ◄──────┐               │
│                                     │                                      │ repair       │
│                                     ▼                                      │ (budget)     │
│                                  auditor ─ blocking findings ──────────────┤               │
│                                     │ (fingerprinted ledger)               │               │
│                                     ▼                                      │               │
│                              dependency_gate ─ uv lock + pip-audit ────────┤               │
│                                     ▼                                      │               │
│                              sandbox_verify ─ any failure ─────────────────┤               │
│                                     ▼                                      │               │
│                              semantic_review ─ rejection ──────────────────┘               │
│                                     ▼                                                    │
│                                  release (hash-consistency gate, manifest)               │
│  Infrastructure faults → aborted (never charged to the budget).                          │
└───────────────────────────────────────────┬──────────────────────────────────────────────┘
                                            ▼
┌──────────────────────────────────────────────────────────────────────────────────────────┐
│ 3. ISOLATED VERIFICATION (gVisor runsc, all containers)                  [implemented] │
│                                                                                          │
│  static container   network=none   /workspace:ro   ruff --isolated, pyright, semgrep     │
│  install container  network=none   /wheelhouse:ro  pip --no-index --require-hashes       │
│  ┌─────────────── internal network (no egress) ─────────────────┐                        │
│  │ SUT container     /workspace:ro /deps:ro   uvicorn <entry>   │                        │
│  │        ▲ HTTP only                                           │                        │
│  │ ORACLE container  /compiler_spec:ro /out:rw  hidden pytest   │ ◄─ no generated code   │
│  └──────────────────────────────────────────────────────────────┘                        │
│  All containers: read-only root, cap-drop ALL, no-new-privileges, UID 65534, memory/CPU/PID │
│  limits, digest-pinned images, --pull=never.                                             │
└───────────────────────────────────────────┬──────────────────────────────────────────────┘
                                            ▼ VerificationResult + evidence hashes
┌──────────────────────────────────────────────────────────────────────────────────────────┐
│ 4. PROVENANCE & DELIVERY                                                                 │
│  PR: verified patch pushed to the PR branch; manifest + lock → .verification/;          │
│      cosign keyless sign-blob; report comment on the PR              [implemented]       │
│  main: release-gate.yml → verify_gate (signature AND code-on-disk hash AND lock hash);  │
│      optional image build + cosign sign + manifest attestation       [implemented]       │
│  cluster: Kyverno (signature + manifest contents) / Gatekeeper+Ratify (signature)        │
│                                                                      [policies included] │
│  Jira: comment + transition to "In Code Review"                      [planned]           │
└──────────────────────────────────────────────────────────────────────────────────────────┘
```

## Corrections to the original proposal

| Original | Problem | Implemented instead |
|---|---|---|
| One container: `/workspace` (rw) plus `/compiler_spec` (ro), pytest imports the code | The code under test shares the process that writes the verdict and can forge it (atexit, monkeypatching, `pytest.ini`) | A separate oracle container that never mounts generated code and tests only over HTTP |
| `pull_request` trigger | Runs the PR's own workflow and compiler code, so a PR can weaken its own gate. The signing identity is also PR-controlled | `pull_request_target`: the workflow and compiler come from the base branch and the PR checkout is data only. The signing identity is `ai-compiler.yml@refs/heads/main` |
| Runner imports `compiler.build_compiler`, hardcodes `fastapi==0.111.0` and `entrypoint: main.py` | The module does not exist; the pins have known CVEs; `main.py` is not an ASGI entrypoint | `verification_compiler.ci`, with project root and entrypoint from repository variables and pins from `requirements.txt` |
| Runner sends every file to the LLMs | `.env` files and other secrets would leave the organisation | Dotfiles, binaries and reserved files are skipped, and the run aborts if a secret is detected |
| Runner rewrites every file | Can clobber files the compiler never saw | Writes only the diff and refuses to overwrite unseen files |
| `stefanzweifel/git-auto-commit-action`, `file_pattern: "*.*"` | Third-party action with write token; commits anything | Plain `git`, scoped to the project root and `.verification/`. It pushes the detached verified commit, so the push fails if the branch moved |
| Sign workflow signs a hand-written manifest | The signature attests nothing | Signs the compiler's real manifest |
| `verify_gate.py` checks only the signature | A validly signed manifest could be replayed next to different code | Also recomputes the codebase hash from disk and checks the lockfile hash |
| Gatekeeper Rego `isSuccess == false`; `responses_by_id` | Fails **open** when the field is missing; not Ratify's response shape | `not isSuccess` over `responses[_]`; provider errors are violations; init and ephemeral containers are covered |
| Kyverno policy checks the signature only | Does not check what was signed | Also checks the release-manifest attestation: `status == release_ready`, acceptance passed, runtime `runsc` |

## Signing options

1. **Keyless (default in CI).** `cosign sign-blob --bundle` with GitHub OIDC. Verify with
   `--certificate-identity https://github.com/ORG/REPO/.github/workflows/ai-compiler.yml@refs/heads/main`
   and `--certificate-oidc-issuer https://token.actions.githubusercontent.com`.
2. **Cosign key pair** (air-gapped or static PKI). Use `cosign generate-key-pair`, then
   `cosign sign-blob --key cosign.key --bundle ...` and `verify_gate --key cosign.pub`.
   Keep the private key in a secret manager or KMS (`--key awskms://...`, `hashivault://...`),
   never in a file on disk longer than the signing step.
3. **OpenSSL ECDSA** (no cosign on the runner):
   `openssl dgst -sha256 -sign key.pem -out m.sig release_manifest.json`, then
   `openssl dgst -sha256 -verify pub.pem -signature m.sig release_manifest.json`. `verify_gate`
   does not wrap this path, so the codebase and lock checks must still be run.
4. **Embedded Ed25519** (`VC_SIGNING_KEY`): the manifest carries its own signature;
   check it with `verify_gate --ed25519-public-key <hex>`.

Whichever option you choose, a signature only means something together with the codebase hash check.
`verify_gate` performs both.

## Admission control

`release-gate.yml` (with `VC_PUBLISH_IMAGE=true`) builds the image from the verified
lockfile, signs it, and attaches the manifest as an in-toto attestation of type
`https://github.com/ORG/REPO/verification-compiler/release-manifest/v1`.

* **Kyverno** (`deploy/kyverno/`, needs 1.13 or later): checks the signature and asserts manifest
  contents. This is the recommended option.
* **Gatekeeper + Ratify** (`deploy/gatekeeper/`): checks the signature only. Gatekeeper cannot
  fetch OCI artifacts, so it relies on Ratify as an ExternalData provider. You must configure
  Ratify's cosign verifier with the release-gate keyless identity.

Both policies were checked here: the Rego has OPA unit tests (including fail-closed cases), and
the Kyverno policy was parsed and exercised with Kyverno CLI 1.13.4 on an unsigned image. The
positive path needs a real signed image and Sigstore access, and has not been tested.

## Jira ingestion [planned]

Planned design, not yet implemented:

* A FastAPI receiver validates the Jira webhook secret (HMAC). It acts only on
  `jira:issue_updated` events where the assignee is the bot or the status becomes "In Progress".
  Events are made idempotent per `(issue key, updated timestamp)`.
* The normaliser turns summary, description, acceptance criteria and linked issues into requirements text.
  Jira content is untrusted input, so it goes into prompts as data, exactly like PR text.
* It creates the branch `feature/jira-KEY-auto-impl`, and it creates the PR through the GitHub API
  rather than pushing directly. The existing `ai-compiler.yml` then does all verification, so there
  is a single gate.
* After release it comments the report on the ticket and moves it to "In Code Review", including the `artifact_hash`.

Example ingestion payload:

```json
{
  "jira_key": "PROJ-8492",
  "summary": "Implement tenant-isolated rate limiting middleware in FastAPI",
  "acceptance_criteria": [
    "AC-1: sliding window per tenant_id",
    "AC-2: HTTP 429 with Retry-After on threshold breach",
    "AC-3: exclude /healthz and /ready"
  ],
  "target_repo": "org/core-auth-service",
  "base_branch": "main"
}
```

Acceptance criteria map naturally onto `AcceptanceTest.id`s. The hidden suite is black-box HTTP,
so criteria that need external state (for example Redis) require that service in the sandbox network.
That is not supported yet.

## Operations

| Concern | Setting | Default | Enforced by |
|---|---|---|---|
| Repair loop bound | `VC_MAX_REPAIR_ROUNDS` | 4 locally, 3 in CI | router → `budget_exhausted` |
| Graph step bound | `recursion_limit` | derived from the budget | LangGraph |
| Sandbox wall-clock | `SandboxConfig.*_timeout_s` | install 180s, static 300s, ready 30s, oracle 300s | container killed on timeout |
| LLM retries | `transient_retry_policy` | 3 attempts, 408/429/5xx/connection errors only | LangGraph `RetryPolicy` |
| Job wall-clock | `timeout-minutes` | 60 | GitHub Actions |
| Host isolation | `VC_REQUIRE_GVISOR` | required | `preflight()` aborts without `runsc` |

Cost per PR depends on the models and repository size. It is not bounded by the compiler
beyond the repair budget. Every LLM call receives the whole project, so keep `VC_PROJECT_ROOT`
narrow.
