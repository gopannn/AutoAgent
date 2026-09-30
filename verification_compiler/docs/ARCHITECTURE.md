# Enterprise Architecture: AI Software Compiler

Status legend: **[implemented]** is in this repository and tested. **[planned]** is a design only.

```
                                   JIRA PLATFORM                      GITHUB PULL REQUEST
                                         │ [implemented]                      │ [implemented]
                              webhook: ticket assigned               pull_request_target
                                         ▼                                    ▼
┌──────────────────────────────────────────────────────────────────────────────────────────┐
│ 1. INGESTION & CONTEXT                                                                   │
│   Jira listener → issue parser (summary, AC)      .github/workflows/ai-compiler.yml      │
│   → PR via verification_compiler/jira  [impl.]    → verification_compiler/ci.py          │
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
│  Jira: comment + transition to "In Code Review" (jira/feedback.py)   [implemented]       │
└──────────────────────────────────────────────────────────────────────────────────────────┘
```

## Corrections to the original proposal

| Original | Problem | Implemented instead |
|---|---|---|
| One container: `/workspace` (rw) plus `/compiler_spec` (ro), pytest imports the code | The code under test shares the process that writes the verdict and can forge it (atexit, monkeypatching, `pytest.ini`) | A separate oracle container that never mounts generated code and tests only over HTTP |
| `issue_comment` trigger `contains(body, '/autoagent compile')` | Any commenter can trigger a run that has write permissions and API secrets. `github.head_ref` is empty for comment events, so it compiles the default branch instead of the PR | Only owners, members and collaborators; the PR head is resolved through the API; concurrency is set per job so unrelated comments can't cancel a run |
| gVisor install from `.../release/latest/<arch>/runsc` | That URL returns 404 (releases are now `gvisor.tar.bz2` bundles) | `scripts/install_gvisor.sh`: checksum-verified bundle, optional pinned release |
| Local sandbox test: one container, `/workspace` rw, spec imports `app` | Same forgeable design; `python:3.12-slim` has no pytest, so the test cannot pass | `scripts/smoke_gvisor.sh` runs the real two-container suite with gVisor required |
| `CompilerTool` imports `compiler.build_compiler`, sends all files, writes all files back | Same issues as the CI runner | `autoagent/tools/compiler_tool.py` uses `verification_compiler.api` and is confined to `VC_ALLOWED_ROOT` |
| `AutoCompilerAgent` plain class | Not an AutoAgent agent; can't be used by AutoAgent's orchestration | `@register_agent` agent whose only write path is `compile_and_verify` |
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

## Jira ingestion [implemented]

`verification_compiler/jira/` is a small, separate service. It turns a ticket into a pull request and
does nothing else: it never runs a model, never pushes code and never executes ticket content.

```
Jira ──webhook──► POST /webhooks/jira ─► authenticate ─► should_act? ─► claim (idempotent) ─► 202
                                                                             │ background
                     GitHub ◄── branch feature/jira-KEY-auto-impl + .verification/requests/KEY.md
                            ◄── PR "KEY: summary", body = requirements text + <!-- jira-bridge:KEY -->
ai-compiler.yml (unchanged gate) ─► verify, repair, sign ─► jira/feedback.py ─► comment (+ transition)
```

* **Authentication** (`service.verify_signature`): Jira Cloud signs the raw body as
  `X-Hub-Signature: sha256=<hex>`; compared in constant time. Jira Data Center cannot sign, so
  `JIRA_WEBHOOK_AUTH=token` compares a `?token=` query parameter instead. Bodies over
  `max_body_bytes` get 413 before authentication work; unsigned or forged requests get 401.
* **Filtering** (`service.should_act`): only `jira:issue_created`/`jira:issue_updated`, a well-formed
  key, a project mapped in `JIRA_PROJECT_REPOS`, assigned to `JIRA_BOT_ACCOUNT_ID`, and either the bot
  was just assigned or the status just moved into `JIRA_TRIGGER_STATUSES`. Everything else is 200
  `ignored` so Jira does not retry it.
* **Idempotency**: `(issue key, changelog id)` (falling back to the event timestamp) is claimed in
  SQLite before work starts; redeliveries get 200 `duplicate`. If a PR for the branch is already
  open the job is recorded as `skipped`. Failures are recorded as `failed` with the reason, and
  visible at `GET /jobs/{KEY}` (bearer: the webhook secret). The ticket is left untouched on failure.
* **Normalisation** (`normalize.py`): ADF or wiki text → plain text; acceptance criteria come from
  `JIRA_ACCEPTANCE_CRITERIA_FIELD` when set, otherwise from an "Acceptance Criteria" section;
  linked issues are listed as context only. Jira content is untrusted and reaches the models only
  as PR text, exactly like any other pull request.
* **Single gate**: the PR is opened with the bridge's token, so `ai-compiler.yml` runs on it like
  any PR (`pull_request_target`, base-branch compiler, PR as data). Nothing about the release
  decision lives in the bridge.
* **Feedback** (`feedback.py`, a step in `ai-compiler.yml`): on branches matching
  `feature/jira-KEY-auto-impl` it comments the headline, PR link, run link, `artifact_hash` and the
  CTD/epistemic decision; on release it also moves the issue to `JIRA_REVIEW_STATUS` (default
  "In Code Review"). Rejections and abstentions only comment. Missing Jira credentials or an
  unreachable Jira are a warning, never a failed build: the release gate is the next step.

| Setting | Required | Meaning |
|---|---|---|
| `JIRA_WEBHOOK_SECRET` | yes | HMAC secret (≥16 chars); also the bearer for `/jobs` |
| `JIRA_BOT_ACCOUNT_ID` | yes | accountId of the automation user |
| `JIRA_PROJECT_REPOS` | yes | JSON: `{"ABC": {"repo": "org/svc", "base_branch": "main"}}` |
| `GITHUB_TOKEN` | yes | contents + pull-requests write on the mapped repositories only |
| `JIRA_WEBHOOK_AUTH` | no | `hmac` (default) or `token` |
| `JIRA_SIGNATURE_HEADER` | no | default `X-Hub-Signature` |
| `JIRA_TRIGGER_STATUSES` | no | comma-separated, default `In Progress` |
| `JIRA_ACCEPTANCE_CRITERIA_FIELD` | no | custom field id, e.g. `customfield_10100` |
| `GITHUB_API_URL` | no | GitHub Enterprise API base |
| `JIRA_BRIDGE_STATE` | no | SQLite path (the image uses the `/state` volume) |

Workflow side: `vars.JIRA_BASE_URL`, optional `vars.JIRA_REVIEW_STATUS`, and the secrets
`JIRA_EMAIL` / `JIRA_API_TOKEN`.

Run it with `verification_compiler/deploy/jira-bridge.Dockerfile` (non-root, fails closed on missing
settings), behind TLS. A PR opened with the default `GITHUB_TOKEN` of another workflow does not
trigger workflows, so give the bridge a GitHub App or fine-grained token. One replica: the
idempotency store is a local SQLite file.

Example normalised payload (`RequirementPayload`):

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

Acceptance criteria map naturally onto `AcceptanceTest.id`s. The hidden suite is black-box HTTP.
Criteria that need shared state (the rate-limiting example above) are handled by the Redis backing
service [implemented]. The architect declares `backing_services: ["redis"]`, and the sandbox runs the
service as several replicas behind a per-run Redis that is reset before every test. The governance
check requires a cross-replica test, so an in-process counter cannot pass. See "Stateful services"
in the README. Other stores (Postgres, Kafka, ...) are not supported yet.

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
