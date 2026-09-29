"""DECIDE and EXPLAIN: per-requirement resolution with CTD, justification with epistemic-toolkit.

For every requirement (FR1..n from the contract, plus the spec's security invariants):

1. CTD resolution over an evidence graph. Every observation is its own VERIFIED_BY edge from the
   requirement, carrying a claim (`<rid>.satisfied` = True/False). CTD therefore sees all of them
   at once (a single filtered edge would hide a failing observation):
     * a discriminating acceptance test         confidence 1.0, trust 1.0, own independence group
     * a non-discriminating acceptance test     confidence 0.3: below threshold, weak on its own
     * an open blocking finding on the requirement / a reviewer saying it is unmet
                                                claim False, trust 0.75 (model assertion)
   Model judgements can only add conflicts here, never support, so a model alone cannot RESOLVE.
   Outcomes: RESOLVED, CONTRADICTED (conflicting claims), PARTIAL (only weak evidence), UNRESOLVABLE.

2. Justification with the toolkit's evidence ledger and robustness gate. Discriminating passing tests
   count as experimental support; the semantic reviewer's approval counts as testimony from a separate
   source; failing tests and model objections refute. A requirement is JUSTIFIED only if the gate is
   ROBUST: no single observation or single source (including "the models") decides it, and an
   adversarial prior does not flip it. A band is published only when justified.

Release requires every requirement RESOLVED and JUSTIFIED. The decision gate can only withhold a
release that the deterministic gates allowed; it can never grant one they refused.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from ..ledger import blocking_open_findings
from ..schemas import VerificationResult
from ..vendored import ctd_module, toolkit
from . import governance

DISCRIMINATING = (1.0, 1.0)
NON_DISCRIMINATING = (0.3, 1.0)
MODEL_OBJECTION = (0.9, 0.75)
_GATE_RUNS = 2000


def _mentions(text: str, rid: str, requirement: str) -> bool:
    norm = lambda s: re.sub(r"\s+", " ", s).strip().casefold()  # noqa: E731
    return bool(re.search(rf"\b{re.escape(rid)}\b", text)) or norm(requirement) in norm(text)


def observations(rid: str, requirement: str, spec: dict, calibration: dict, result: VerificationResult,
                 ledger: dict, blocking: set[str], review: dict | None) -> list[dict]:
    strong = governance.discriminating_tests(calibration)
    by_id = {a.id: a for a in result.acceptance}
    obs = []
    for test in spec["acceptance_tests"]:
        if rid not in governance.claims(test):
            continue
        outcome = by_id.get(test["id"])
        passed = bool(outcome and outcome.passed and outcome.cases > 0)
        disc = test["id"] in strong
        obs.append({"id": f"test:{test['id']}", "kind": "acceptance_test", "claim": passed,
                    "discriminating": disc, "source": f"oracle:{test['id']}",
                    "confidence_trust": DISCRIMINATING if disc else NON_DISCRIMINATING})
    test_ids = {t["id"] for t in spec["acceptance_tests"] if rid in governance.claims(t)}
    for finding in blocking_open_findings(ledger, blocking):
        if rid in finding.get("invariant_ids", []) or test_ids & set(finding.get("acceptance_test_ids", [])):
            obs.append({"id": f"finding:{finding['fingerprint']}", "kind": "audit_finding", "claim": False,
                        "discriminating": False, "source": "llm:auditor", "confidence_trust": MODEL_OBJECTION})
    if review:
        unmet = any(_mentions(u, rid, requirement) for u in review.get("unmet_requirements", []))
        if unmet or not review.get("is_valid", False):
            if unmet:
                obs.append({"id": f"review:{rid}", "kind": "semantic_review", "claim": False,
                            "discriminating": False, "source": "llm:reviewer", "confidence_trust": MODEL_OBJECTION})
    return obs


def resolve(rid: str, obs: list[dict], now: datetime) -> dict:
    """CTD resolution of `<rid>.satisfied` over one edge per observation."""
    graph_mod, models, controller, resolver = (ctd_module(m) for m in ("graph", "models", "controller", "resolver"))
    graph = graph_mod.EvidenceGraph()
    req_node = f"req:{rid}"
    graph.add_node(models.Node(id=req_node, type="Requirement", attributes={"rid": rid}))
    key = f"{rid}.satisfied"
    for o in obs:
        conf, trust = o["confidence_trust"]
        graph.add_node(models.Node(id=f"obs:{o['id']}", type="Observation", attributes={"kind": o["kind"], "supports": o["claim"]}))
        graph.add_edge(models.Edge(
            id=f"edge:{o['id']}", source=req_node, target=f"obs:{o['id']}", type="VERIFIED_BY",
            attributes={"claim_key": key, "claim_value": o["claim"]},
            evidence=[models.Evidence(
                id=f"ev:{o['id']}", source_id=o["source"], source_type=o["kind"], observed_at=now,
                confidence=conf, trust=trust, independence_group=o["source"],
                claim_key=key, claim_value=o["claim"])],
        ))
    query = models.QueryConstraintGraph(
        as_of=now,
        variables=[models.Variable(name="req", node_type="Requirement"),
                   models.Variable(name="obs", node_type="Observation")],
        relations=[models.RelationConstraint(id="r:verified", subject_var="req", relation="VERIFIED_BY",
                                             object_var="obs")],
        attributes=[models.AttributeConstraint(id="a:rid", variable="req", attribute="rid", op="eq", value=rid),
                    # Bind only a supporting observation; conflicting siblings are still compared by CTD's
                    # claim check, so a failing observation next to a passing one yields CONTRADICTED.
                    models.AttributeConstraint(id="a:supports", variable="obs", attribute="supports", op="eq",
                                               value=True)],
    )
    policy = controller.RuntimePolicy(min_confidence=0.9, min_trust=0.7, min_evidence_strength=0.7)
    result = resolver.Resolver(graph).resolve(query, policy)
    return {
        "state": result.state.value,
        "evidence_ids": list(result.evidence_ids),
        "contradictions": list(result.contradictions),
        "failure_reasons": [getattr(f, "code", str(f)) for f in result.failure_reasons],
    }


def justify(rid: str, requirement: str, obs: list[dict], review: dict | None) -> dict:
    et = toolkit()
    hypothesis = et.Hypothesis(f"H_{rid}", f"The build satisfies requirement {rid}: {requirement[:160]}", prior=0.5)
    for o in obs:
        subject = o["id"].split(":", 1)[1]
        if o["kind"] == "acceptance_test":
            if not o["claim"]:
                item = ("experimental", "undermines", 3.0, f"Acceptance test {subject} failed against the running service.")
            elif o["discriminating"]:
                item = ("experimental", "supports", 2.0,
                        f"Acceptance test {subject} passed and fails against every null service.")
            else:
                item = ("observational", "neutral", 0.0,
                        f"Acceptance test {subject} passed but also passes against a null service.")
        else:
            item = ("testimony", "undermines", 1.0, f"A model reviewer ({o['source']}) asserts that {rid} is not met.")
        kind, direction, weight, text = item
        hypothesis.add(et.Evidence(o["id"], text, kind, direction, weight, source=o["source"]))
    if review and review.get("is_valid") and not any(o["kind"] != "acceptance_test" for o in obs):
        hypothesis.add(et.Evidence(f"review:{rid}", f"The independent semantic reviewer judged {rid} met.",
                                   "testimony", "supports", 0.5, source="llm:reviewer"))
    hypothesis.what_would_settle_it = f"An additional independent discriminating acceptance test for {rid}."
    ledger = et.Ledger()
    ledger.add(hypothesis)
    if not hypothesis.evidence:
        return {"verdict": "NO_EVIDENCE", "justified": False, "band": None, "failures": ["no evidence"]}
    gate = et.gate(ledger, hypothesis.id, et.GateConfig(runs=_GATE_RUNS))
    justified = gate.verdict == "ROBUST"
    return {
        "verdict": gate.verdict,
        "justified": justified,
        "band": [round(x, 4) for x in gate.band] if justified and gate.band else None,
        "failures": list(gate.failures),
        "what_would_settle_it": None if justified else hypothesis.what_would_settle_it,
    }


_CTD_PRECEDENCE = ["CONTRADICTED", "UNRESOLVABLE", "PARTIAL", "BUDGET_EXHAUSTED", "PROVIDER_ERROR"]


def decide(contract: dict, spec: dict, calibration: dict, result: VerificationResult, ledger: dict,
           blocking: set[str], review: dict | None, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    catalog = governance.requirement_catalog(contract, spec)
    per_requirement = {}
    for rid, text in sorted(catalog.items()):
        obs = observations(rid, text, spec, calibration, result, ledger, blocking, review)
        per_requirement[rid] = {
            "requirement": text,
            "observations": [{k: v for k, v in o.items() if k != "confidence_trust"} for o in obs],
            "resolution": resolve(rid, obs, now),
            "justification": justify(rid, text, obs, review),
        }
    states = {r["resolution"]["state"] for r in per_requirement.values()}
    ctd_outcome = "RESOLVED" if states == {"RESOLVED"} else next(
        (s for s in _CTD_PRECEDENCE if s in states), "UNRESOLVABLE")
    justified = all(r["justification"]["justified"] for r in per_requirement.values())
    return {
        "ctd_outcome": ctd_outcome if per_requirement else "UNRESOLVABLE",
        "epistemic_verdict": "JUSTIFIED" if justified and per_requirement else "UNJUSTIFIED",
        "release": bool(per_requirement) and ctd_outcome == "RESOLVED" and justified,
        "requirements": per_requirement,
        "decided_at": now.isoformat(),
    }
