"""Graph nodes. LLM access, the sandbox and the resolver are injected so every node is testable."""
from __future__ import annotations

import logging
from typing import Any, Callable, Protocol

from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel

from . import prompts
from .config import COMPILER_VERSION, CompilerConfig, ModelConfig
from .dependencies import DependencyFailure
from .errors import InfrastructureError
from .hashing import codebase_hash, hash_obj, sha256_hex
from .ledger import blocking_open_findings, closure_basis, open_findings, reconcile, release_blockers
from .policy import evaluate_codebase, evaluate_spec
from .reasoning import decision, discovery, governance, topology
from .reasoning import requirements as reqgate
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

    def calibrate_spec(self, spec: dict) -> dict[str, dict[str, bool]]: ...


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


def abstain(reason: str) -> dict:
    """Terminal refusal that is not a fault: contradictory requirements or unjustified release."""
    log.warning("abstaining: %s", reason)
    return {"status": "abstained", "abstain_reason": reason}


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

    # ------------------------------------------------------------ reasoning layers (before code)

    def requirement_gate(self, state: SystemState) -> dict:
        log.info("[1b] requirement gate: encoding and contradiction check")
        llm = self.llm("requirement_encoder", reqgate.RequirementEncoding)
        try:
            description = reqgate.describe_schema()
        except InfrastructureError as err:
            return abort(str(err))
        feedback = ""
        check = None
        for _ in range(self.cfg.spec_compile_attempts):
            encoding = llm.invoke(prompts.requirement_encoding(
                state["requirements"], state["requirement_contract"], description, feedback))
            try:
                check = reqgate.check(state["requirements"], encoding)
            except InfrastructureError as err:
                return abort(str(err))
            if not check.malformed:
                update = {"requirement_encoding": encoding.model_dump(), "requirement_check": check.to_dict()}
                if check.contradictory:
                    pairs = "; ".join(" vs ".join(f'"{e}"' for e in c["excerpts"]) for c in check.contradictions)
                    return {**update, **abstain(f"requirements contradict each other: {pairs}")}
                return {**update, "status": "running"}
            feedback = "\n".join(f"- {p}" for p in check.problems)
        return abort("requirement encoding failed validation: " + "; ".join(check.problems if check else []))

    def discovery(self, state: SystemState) -> dict:
        log.info("[1c] structural discovery (pre-code premortem)")
        try:
            risks = discovery.premortem(
                topology.from_contract(state["requirement_contract"], state.get("requirement_encoding")), "service")
        except InfrastructureError as err:
            return abort(str(err))
        return {"structural_risks": {"pre_code": risks, "code": []}, "status": "running"}

    def verification_compiler(self, state: SystemState) -> dict:
        log.info("[2] compiling hidden verification spec")
        llm = self.llm("verification_compiler", VerificationSpec)
        contract = state["requirement_contract"]
        risks = state.get("structural_risks", {}).get("pre_code", [])
        feedback = ""
        problems: list[str] = []
        for _ in range(self.cfg.spec_compile_attempts):
            spec = llm.invoke(prompts.verification_spec(contract, feedback, risks)).model_dump()
            problems = evaluate_spec(spec) + governance.coverage_problems(spec, contract) + [
                p for t in spec["acceptance_tests"] for p in governance.lint_assertions(t["id"], t["executable_python_code"])
            ]
            if not problems:
                try:
                    calibration = self.sandbox.calibrate_spec(spec)
                except InfrastructureError as err:
                    return abort(str(err))
                problems = governance.evidence_problems(spec, contract, calibration)
                if not problems:
                    return {"verification_spec": spec, "verification_spec_hash": hash_obj(spec),
                            "spec_calibration": calibration}
            feedback = "\n".join(f"- {p}" for p in problems)
        return abort("verification spec failed validation: " + "; ".join(problems))

    def architect(self, state: SystemState) -> dict:
        log.info("[3] generating initial implementation")
        risks = state.get("structural_risks", {}).get("pre_code", [])
        codebase = self.llm("architect", Codebase).invoke(prompts.architect(state["requirement_contract"], risks))
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
        try:
            code_risks = discovery.premortem(topology.from_codebase(state["codebase"]), "service")
        except InfrastructureError as err:
            return abort(str(err))
        risks = _merge_risks(state.get("structural_risks", {}).get("pre_code", []), code_risks)
        report = self.llm("adversarial_auditor", AuditReport).invoke(prompts.audit(
            spec["security_invariants"], state["requirement_contract"], state["codebase"], open_findings(ledger), risks,
        ))
        known = {r["id"] for r in risks}
        verdicts = {v.risk_id: {**v.model_dump(), "audit_round": audit_round, "evidence_basis": "model_assertion_only"}
                    for v in report.risk_verdicts if v.risk_id in known}
        verdicts.update({rid: {"risk_id": rid, "verdict": "unreviewed", "justification": "", "audit_round": audit_round,
                               "evidence_basis": "none"} for rid in known - set(verdicts)})
        ledger = reconcile(
            ledger, report,
            audit_round=audit_round,
            codebase_hash=current_hash,
            known_test_ids={t["id"] for t in spec["acceptance_tests"]},
            known_invariant_ids={i["id"] for i in spec["security_invariants"]},
        )
        update = {"findings_ledger": ledger, "audit_round": audit_round, "audited_codebase_hash": current_hash,
                  "structural_risks": {**state.get("structural_risks", {}), "code": code_risks},
                  "risk_verdicts": verdicts}
        blocking = blocking_open_findings(ledger, self.cfg.blocking_severities)
        if blocking:
            lines = [f"- {f['fingerprint']} [{f['severity']}] {f['category']}: {f['description']}" for f in blocking]
            return {**update, **failure("audit", "Blocking audit findings:\n" + "\n".join(lines))}
        return {**update, "status": "running"}

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

    def decision_gate(self, state: SystemState) -> dict:
        log.info("[9] decision gate: CTD resolution and epistemic justification")
        try:
            decided = decision.decide(
                state["requirement_contract"], state["verification_spec"], state.get("spec_calibration", {}),
                VerificationResult(**state["verification_result"]), state.get("findings_ledger", {}),
                set(self.cfg.blocking_severities), state.get("semantic_validation"),
            )
        except InfrastructureError as err:
            return abort(str(err))
        except Exception as err:  # noqa: BLE001 - an engine failure is a fault, never a release
            return abort(f"decision engines failed: {err!r}")
        if not decided["release"]:
            lacking = [f"{rid}: {r['resolution']['state']}/{r['justification']['verdict']}"
                       for rid, r in decided["requirements"].items()
                       if r["resolution"]["state"] != "RESOLVED" or not r["justification"]["justified"]]
            return {"decision": decided, **abstain(
                f"evidence does not justify release ({decided['ctd_outcome']}, {decided['epistemic_verdict']}): "
                + "; ".join(lacking))}
        return {"decision": decided, "status": "running"}

    # ------------------------------------------------------------ terminal nodes

    def release(self, state: SystemState, config: RunnableConfig) -> dict:
        log.info("[9] release gate")
        codebase = state["codebase"]
        current = codebase_hash(codebase)
        spec = state["verification_spec"]
        result = VerificationResult(**state["verification_result"])
        ledger = state.get("findings_ledger", {})

        problems = []
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
        decided = state.get("decision") or {}
        if not decided.get("release"):
            problems.append("decision gate did not justify this release")
        if problems:
            return abort("release invariants violated: " + "; ".join(problems))

        contract_hash = hash_obj(state["requirement_contract"])
        artifact_hash = hash_obj({
            "compiler_version": COMPILER_VERSION,
            "runtime": result.runtime,
            "images": result.images,
            "toolchain_versions": result.toolchain_versions,
            "requirement_contract": contract_hash,
            "verification_spec": state["verification_spec_hash"],
            "codebase": current,
            "lockfile": result.lockfile_hash,
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
            decision={k: decided[k] for k in ("ctd_outcome", "epistemic_verdict", "requirements", "decided_at")},
            governance={
                "requirement_check": state.get("requirement_check"),
                "requirement_statements": (state.get("requirement_encoding") or {}).get("statements", []),
                "structural_risks": state.get("structural_risks", {}),
                "risk_verdicts": state.get("risk_verdicts", {}),
                "failure_library_sha256": discovery.library_fingerprint(),
                "discriminating_tests": sorted(governance.discriminating_tests(state.get("spec_calibration", {}))),
                "spec_calibration": state.get("spec_calibration", {}),
            },
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

    def abstained(self, state: SystemState) -> dict:
        return {"status": "abstained"}

    def aborted(self, state: SystemState) -> dict:
        return {"status": "execution_failed"}


def _merge_risks(*groups: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}
    for group in groups:
        for risk in group:
            merged.setdefault(risk["id"], risk)
    return sorted(merged.values(), key=lambda r: (-(r.get("severity") or 0), r["id"]))
