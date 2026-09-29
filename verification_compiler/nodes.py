"""Graph nodes. LLM access, the sandbox and the resolver are injected so every node is testable."""
from __future__ import annotations

import logging
from typing import Any, Callable, Protocol

from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel

from . import prompts
from .config import COMPILER_VERSION, CompilerConfig, ModelConfig
from .constraints import review_constraints
from .dependencies import DependencyFailure
from .errors import InfrastructureError
from .hashing import codebase_hash, hash_obj, release_artifact_hash, sha256_hex
from .ledger import blocking_open_findings, closure_basis, open_findings, reconcile, release_blockers
from .policy import evaluate_codebase, evaluate_spec
from .premortem import analyze as analyze_premortem
from .schemas import (
    AuditReport,
    CheckResult,
    CodePatch,
    Codebase,
    ReleaseManifest,
    RequirementContract,
    SemanticReview,
    VerificationResult,
    VerificationSpec,
)
from .signing import sign_manifest
from .state import SystemState

log = logging.getLogger("verification_compiler")

MAX_FEEDBACK_CHARS = 12_000


class StructuredLLM(Protocol):
    def invoke(self, messages: list[tuple[str, str]]) -> Any: ...


LLMFactory = Callable[[str, type[BaseModel]], StructuredLLM]


class Sandbox(Protocol):
    def verify(self, codebase: dict, spec: dict, lockfile: dict) -> VerificationResult: ...


class Resolver(Protocol):
    def resolve(self, dependencies: list[str]) -> dict: ...


def default_llm_factory(models: ModelConfig) -> LLMFactory:
    def make(role: str, schema: type[BaseModel]) -> StructuredLLM:
        model = getattr(models, role)
        if model.startswith("claude"):
            from langchain_anthropic import ChatAnthropic

            llm = ChatAnthropic(model=model, max_tokens=models.anthropic_max_tokens)
        else:
            from langchain_openai import ChatOpenAI

            llm = ChatOpenAI(model=model)
        return llm.with_structured_output(schema)

    return make


def failure(source: str, feedback: str) -> dict:
    """A repairable gate failure. The budget is charged when the builder runs, not here."""
    return {
        "status": "validation_failed",
        "feedback_source": source,
        "validation_feedback": feedback[:MAX_FEEDBACK_CHARS],
    }


def abort(error: str) -> dict:
    log.error("aborting: %s", error)
    return {"status": "execution_failed", "error": error}


def verification_feedback(result: VerificationResult, spec: dict) -> str:
    """Failure summary for the builder: check output and failing test ids, never hidden test source."""
    described = {t["id"]: t["description"] for t in spec["acceptance_tests"]}
    parts = [
        f"[{c.name}] exit={c.exit_code}\n{c.detail}".rstrip()
        for c in result.checks.values() if not c.passed and c.name != "acceptance_tests"
    ]
    for a in result.acceptance:
        if not a.passed:
            parts.append(f"[acceptance {a.id}] {described.get(a.id, '')}\n  " + "\n  ".join(a.failures[:5]))
    if not result.checks.get("service_startup", CheckResult(name="", passed=True)).passed:
        parts.append(f"[service log]\n{result.logs.get('service', '')[-2000:]}")
    return "\n\n".join(parts)


class CompilerNodes:
    def __init__(
        self,
        cfg: CompilerConfig,
        llm_factory: LLMFactory,
        sandbox: Sandbox,
        resolver: Resolver,
    ):
        self.cfg = cfg
        self.llm = llm_factory
        self.sandbox = sandbox
        self.resolver = resolver

    # ------------------------------------------------------------ compilation

    def req_compiler(self, state: SystemState) -> dict:
        log.info("[1] compiling requirement contract")
        contract = self.llm("architect", RequirementContract).invoke(prompts.requirement_contract(state["requirements"]))
        return {
            "requirement_contract": contract.model_dump(),
            "iteration": 0,
            "audit_round": 0,
            "findings_ledger": {},
            "status": "running",
        }

    def verification_compiler(self, state: SystemState) -> dict:
        log.info("[2] compiling hidden verification spec")
        llm = self.llm("verification_compiler", VerificationSpec)
        feedback = ""
        problems: list[str] = []
        for _ in range(self.cfg.spec_compile_attempts):
            spec = llm.invoke(prompts.verification_spec(state["requirement_contract"], feedback)).model_dump()
            problems = evaluate_spec(spec)
            if not problems:
                return {"verification_spec": spec, "verification_spec_hash": hash_obj(spec)}
            feedback = "\n".join(f"- {p}" for p in problems)
        return abort("verification spec failed validation: " + "; ".join(problems))

    def constraint_gate(self, state: SystemState) -> dict:
        review = review_constraints(state["requirements"], state["requirement_contract"])
        if review["status"] == "abstained":
            return {"constraint_review": review, "status": "abstained"}
        return {"constraint_review": review, "status": "running"}

    def architect(self, state: SystemState) -> dict:
        log.info("[3] generating initial implementation")
        codebase = self.llm("architect", Codebase).invoke(prompts.architect(state["requirement_contract"]))
        return {"codebase": codebase.model_dump(), "status": "running"}

    # ------------------------------------------------------------ gates & repair

    def policy_gate(self, state: SystemState) -> dict:
        log.info("    policy gate")
        violations = evaluate_codebase(state["codebase"], self.cfg.limits)
        if violations:
            return failure("policy", "Policy violations:\n" + "\n".join(f"- {v}" for v in violations))
        return {"status": "running", "validation_feedback": ""}

    def auditor(self, state: SystemState) -> dict:
        audit_round = state.get("audit_round", 0) + 1
        log.info("[4] adversarial audit (round %d)", audit_round)
        spec = state["verification_spec"]
        ledger = state.get("findings_ledger", {})
        current_hash = codebase_hash(state["codebase"])
        report = self.llm("adversarial_auditor", AuditReport).invoke(prompts.audit(
            spec["security_invariants"], state["requirement_contract"], state["codebase"], open_findings(ledger),
        ))
        ledger = reconcile(
            ledger, report,
            audit_round=audit_round,
            codebase_hash=current_hash,
            known_test_ids={t["id"] for t in spec["acceptance_tests"]},
            known_invariant_ids={i["id"] for i in spec["security_invariants"]},
        )
        update = {"findings_ledger": ledger, "audit_round": audit_round, "audited_codebase_hash": current_hash}
        blocking = blocking_open_findings(ledger, self.cfg.blocking_severities)
        if blocking:
            lines = [f"- {f['fingerprint']} [{f['severity']}] {f['category']}: {f['description']}" for f in blocking]
            return {**update, **failure("audit", "Blocking audit findings:\n" + "\n".join(lines))}
        return {**update, "status": "running"}

    def premortem(self, state: SystemState) -> dict:
        log.info("    AST and CTD pre-mortem")
        try:
            review = analyze_premortem(state["codebase"], self.cfg.premortem_casebook_path)
        except (OSError, ValueError, ImportError) as err:
            return abort(f"pre-mortem engine unavailable or casebook invalid: {err}")
        if review["defects"]:
            feedback = "Pre-mortem checks:\n" + "\n".join(
                f"- {f['path']}:{f['line']} {f['kind']}: {f['check']}" for f in review["defects"])
            return {"premortem_review": review, **failure("premortem", feedback)}
        return {"premortem_review": review, "status": "running"}

    def builder(self, state: SystemState) -> dict:
        iteration = state.get("iteration", 0) + 1
        log.info("[5] repair round %d (%s)", iteration, state.get("feedback_source"))
        codebase = state["codebase"]
        findings = blocking_open_findings(state.get("findings_ledger", {}), self.cfg.blocking_severities)
        patch = self.llm("repository_builder", CodePatch).invoke(prompts.build_repair(
            state["requirement_contract"], codebase, findings,
            state.get("feedback_source", ""), state.get("validation_feedback", ""),
        ))
        files = {f["path"]: f["content"] for f in codebase["files"]}
        for path in patch.deleted_files:
            files.pop(path, None)
        for f in patch.modified_files:
            files[f.path] = f.content
        updated = {**codebase, "files": [{"path": p, "content": c} for p, c in sorted(files.items())]}
        if patch.dependencies is not None:
            updated["dependencies"] = patch.dependencies
        return {"codebase": updated, "iteration": iteration, "status": "running", "validation_feedback": ""}

    def dependency_gate(self, state: SystemState) -> dict:
        log.info("[6] resolving and auditing dependencies")
        try:
            lockfile = self.resolver.resolve(state["codebase"]["dependencies"])
        except DependencyFailure as err:
            return failure("dependencies", str(err))
        except InfrastructureError as err:
            return abort(str(err))
        audit = CheckResult(**lockfile["audit"])
        if not audit.passed:
            return {"lockfile": lockfile, **failure("dependencies", "Vulnerable dependencies:\n" + audit.detail)}
        return {"lockfile": lockfile, "status": "running"}

    def sandbox_verify(self, state: SystemState) -> dict:
        log.info("[7] sandboxed verification")
        spec = state["verification_spec"]
        if hash_obj(spec) != state.get("verification_spec_hash"):
            return abort("verification spec changed after compilation")
        try:
            result = self.sandbox.verify(state["codebase"], spec, state["lockfile"])
        except InfrastructureError as err:
            return abort(str(err))
        update = {"verification_result": result.model_dump()}
        if not result.passed:
            return {**update, **failure("verification", verification_feedback(result, spec))}
        return {**update, "verified_codebase_hash": result.codebase_hash, "status": "running"}

    def semantic_review(self, state: SystemState) -> dict:
        log.info("[8] semantic review")
        result = VerificationResult(**state["verification_result"])
        summary = [{"id": a.id, "passed": a.passed, "cases": a.cases} for a in result.acceptance]
        review = self.llm("semantic_reviewer", SemanticReview).invoke(
            prompts.semantic_review(state["requirement_contract"], state["codebase"], summary)
        )
        update = {"semantic_validation": review.model_dump()}
        if not review.is_valid:
            unmet = "\n".join(f"- {u}" for u in review.unmet_requirements)
            return {**update, **failure("semantic_review", f"{review.feedback}\n{unmet}".strip())}
        return {**update, "reviewed_codebase_hash": codebase_hash(state["codebase"]), "status": "running"}

    # ------------------------------------------------------------ terminal nodes

    def release(self, state: SystemState, config: RunnableConfig) -> dict:
        log.info("[9] release gate")
        codebase = state["codebase"]
        current = codebase_hash(codebase)
        spec = state["verification_spec"]
        result = VerificationResult(**state["verification_result"])
        ledger = state.get("findings_ledger", {})

        problems = []
        constraint_review = state.get("constraint_review") or {}
        if (constraint_review.get("status") != "consistent_with_encoded_constraints"
                or constraint_review.get("requirements_hash") != sha256_hex(state["requirements"])
                or constraint_review.get("contract_hash") != hash_obj(state["requirement_contract"])):
            problems.append("requirements or constraint review changed after the upfront gate")
        premortem_review = state.get("premortem_review") or {}
        if premortem_review.get("status") != "passed" or premortem_review.get("codebase_hash") != current:
            problems.append("pre-mortem review is absent, failed or stale")
        if not result.passed:
            problems.append("verification evidence is not passing")
        stages = {
            "audited": state.get("audited_codebase_hash"),
            "verified": state.get("verified_codebase_hash"),
            "reviewed": state.get("reviewed_codebase_hash"),
            "evidence": result.codebase_hash,
        }
        stale = [k for k, v in stages.items() if v != current]
        if stale:
            problems.append(f"codebase changed after stages {stale}")
        if result.lockfile_hash != state["lockfile"]["sha256"]:
            problems.append("lockfile differs from the verified one")
        if not (hash_obj(spec) == state.get("verification_spec_hash") == result.spec_hash):
            problems.append("verification spec differs from the one tests ran against")
        problems += release_blockers(ledger, result, self.cfg.blocking_severities, self.cfg.require_test_evidence_for)
        if problems:
            return abort("release invariants violated: " + "; ".join(problems))

        contract_hash = hash_obj(state["requirement_contract"])
        artifact_hash = release_artifact_hash({
            "compiler_version": COMPILER_VERSION, "runtime": result.runtime,
            "images": result.images, "toolchain_versions": result.toolchain_versions,
            "requirement_contract_hash": contract_hash, "constraint_review": constraint_review,
            "premortem_review": premortem_review, "verification_spec_hash": state["verification_spec_hash"],
            "codebase_hash": current, "lockfile_hash": result.lockfile_hash,
        })
        summary = [
            {**{k: f[k] for k in ("fingerprint", "severity", "category", "closed", "opened_round", "closed_round",
                                  "acceptance_test_ids")},
             "evidence_basis": closure_basis(f, result)}
            for f in ledger.values()
        ]
        manifest = ReleaseManifest(
            build_id=f"BLD-{artifact_hash[:24]}",
            run_id=str(((config or {}).get("configurable") or {}).get("thread_id", "")),
            status="release_ready",
            requirement_hash=sha256_hex(state["requirements"]),
            requirement_contract_hash=contract_hash,
            constraint_review=constraint_review,
            premortem_review=premortem_review,
            verification_spec_hash=state["verification_spec_hash"],
            codebase_hash=current,
            lockfile_hash=result.lockfile_hash,
            artifact_hash=artifact_hash,
            compiler_version=COMPILER_VERSION,
            runtime=result.runtime,
            images=result.images,
            toolchain_versions=result.toolchain_versions,
            models_used={k: v for k, v in self.cfg.models.model_dump().items() if isinstance(v, str)},
            source_files=sorted(f["path"] for f in codebase["files"]),
            locked_packages=state["lockfile"]["packages"],
            findings=summary,
            accepted_findings=[s for s in summary if not s["closed"]],
            verification_evidence=result,
        ).model_dump()
        try:
            manifest["signature"] = sign_manifest(manifest, self.cfg.signing_key_path)
        except InfrastructureError as err:
            return abort(str(err))
        return {"release_manifest": manifest, "status": "released"}

    def budget_exhausted(self, state: SystemState) -> dict:
        log.error("repair budget of %d rounds exhausted", self.cfg.max_repair_rounds)
        return {
            "status": "budget_exceeded",
            "validation_feedback": state.get("validation_feedback") or "repair budget exhausted",
        }

    def aborted(self, state: SystemState) -> dict:
        return {"status": "execution_failed"}

    def abstained(self, state: SystemState) -> dict:
        return {"status": "abstained"}
