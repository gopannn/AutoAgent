from datetime import UTC, datetime

from ctd.graph import EvidenceGraph
from ctd.models import AttributeConstraint, Node, QueryConstraintGraph, RelationConstraint, Variable
from ctd.planner import QueryPlanner
from ctd.providers import GraphProvider

AS_OF = datetime(2026, 9, 12, tzinfo=UTC)


def fixture():
    graph = EvidenceGraph()
    graph.add_node(Node(id="s:1", type="Supplier", attributes={"risk": "low"}))
    graph.add_node(Node(id="s:2", type="Supplier", attributes={"risk": "high"}))
    graph.add_node(Node(id="c:x", type="Component", attributes={"code": "X"}))
    graph.add_node(Node(id="c:y", type="Component", attributes={"code": "Y"}))
    query = QueryConstraintGraph(
        as_of=AS_OF,
        variables=[Variable(name="supplier", node_type="Supplier"), Variable(name="component", node_type="Component")],
        relations=[RelationConstraint(id="r:produces", subject_var="supplier", relation="PRODUCES", object_var="component")],
        attributes=[
            AttributeConstraint(id="a:component", variable="component", attribute="code", op="eq", value="X"),
            AttributeConstraint(id="a:risk", variable="supplier", attribute="risk", op="eq", value="low", hard=False),
        ],
    )
    return query, GraphProvider(graph)


def test_planner_prioritizes_selective_hard_attribute_before_broad_relation():
    query, provider = fixture()

    plan = QueryPlanner().plan(query, provider)

    assert plan.operations[0].constraint_id == "a:component"
    assert plan.operations[0].hard is True
    assert plan.operations[-1].constraint_id == "a:risk"


def test_plan_is_deterministic_for_same_snapshot_and_query():
    query, provider = fixture()

    first = QueryPlanner().plan(query, provider).model_dump()
    second = QueryPlanner().plan(query, provider).model_dump()

    assert first == second


def test_profile_prior_is_advisory_and_does_not_promote_soft_over_hard():
    query, provider = fixture()

    plan = QueryPlanner().plan(query, provider, profile={"preferred_order": ["a:risk", "r:produces"]})

    hard_ids = [operation.constraint_id for operation in plan.operations if operation.hard]
    assert plan.operations[0].constraint_id in hard_ids
    assert plan.operations[-1].constraint_id == "a:risk"
