from datetime import UTC, datetime

from ctd.graph import EvidenceGraph
from ctd.models import Node, QueryConstraintGraph, RelationConstraint, ResolutionState, Variable
from ctd.premortem import PreMortemEngine
from ctd.resolver import Resolver
from ctd.structural import PredicateVocabulary, StructuralCase, StructuralRelation, StructuralTarget
from ctd.transfer import HypothesisState, StructuralTransferEngine, TransferPolicy

NOW = datetime(2026, 9, 12, tzinfo=UTC)


def R(pred, *args):
    return StructuralRelation(pred=pred, args=args)


def source(case_id, domain, group, check=True):
    return StructuralCase(
        id=case_id,
        domain=domain,
        independence_group=group,
        relations=(R("CAUSES", R("RETRIES", "client", "service"), R("INCREASES", "load", "pressure")), R("SATURATES", "service", "pressure")),
        types={"client": "actor", "service": "resource", "load": "load", "pressure": "state"},
        metadata={"incident": True},
        severity=4,
        check_templates={"SATURATES": "Stress {0} and measure {1}."} if check else {},
    )


def target():
    return StructuralTarget(
        name="gateway",
        domain="software",
        relations=(R("CAUSES", R("REDUCES", "controller", "queue"), R("INCREASES", "demand", "target_pressure")),),
        types={"controller": "actor", "queue": "resource", "demand": "load", "target_pressure": "state"},
    )


def test_independent_domain_convergence_increases_hypothesis_priority():
    engine = StructuralTransferEngine(PredicateVocabulary.core())
    one = engine.transfer([source("a", "distributed", "g1")], target(), TransferPolicy(min_depth=2))
    two = engine.transfer([source("a", "distributed", "g1"), source("b", "ecology", "g2")], target(), TransferPolicy(min_depth=2))

    h1 = next(h for h in one.hypotheses if h.relation.pred == "SATURATES")
    h2 = next(h for h in two.hypotheses if h.relation.pred == "SATURATES")
    assert h2.convergence == 2
    assert h2.priority_score > h1.priority_score


def test_premortem_deliverables_are_checkable():
    result = PreMortemEngine().analyze([source("a", "distributed", "g1")], target())
    assert result.findings
    assert all(finding.check for finding in result.findings)


def test_structural_hypothesis_cannot_promote_resolution_without_evidence():
    transfer = StructuralTransferEngine().transfer([source("a", "distributed", "g1")], target(), TransferPolicy(min_depth=2))
    assert transfer.hypotheses and all(h.state == HypothesisState.HYPOTHESIS for h in transfer.hypotheses)

    graph = EvidenceGraph()
    graph.add_node(Node(id="queue", type="Resource"))
    graph.add_node(Node(id="pressure", type="State"))
    query = QueryConstraintGraph(
        variables=[Variable(name="q", node_type="Resource"), Variable(name="p", node_type="State")],
        relations=[RelationConstraint(id="saturates", subject_var="q", relation="SATURATES", object_var="p")],
        as_of=NOW,
    )
    result = Resolver(graph).resolve(query)
    assert result.state != ResolutionState.RESOLVED
