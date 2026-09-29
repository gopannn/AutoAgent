"""v5.1 unification regression tests.

Three defects, each of which was invisible to the 210 tests that already
passed, and each of which could only be seen by comparing two parts of the
system against each other rather than testing either alone.
"""

from __future__ import annotations

import pytest

from ctd.authz import AuthorizationFilter
from ctd.controller import RuntimePolicy
from ctd.graph import EvidenceGraph
from ctd.kernel import (
    CORE_SCHEMA, Knobs, R, Record, Rel, TargetPattern, align, walk,
)
from ctd.kernel.schema import ENT, Schema, Signature
from ctd.kernel.transfer import project
from ctd.models import Node
from ctd.providers import GraphProvider
from ctd.structural import StructuralCase, StructuralRelation
from ctd.structural_repository import InMemoryStructuralCaseRepository
from ctd.unified_close import (
    CloseConstraintSpec, CloseRecordModel, UnifiedCloseOperator,
    UnifiedCloseRequest, _authorized,
)


# --------------------------------------------------------------------------
# 1. One authorization policy, not three
# --------------------------------------------------------------------------

def _case(cid: str, tenant: str | None, label: str | None = None) -> StructuralCase:
    return StructuralCase(
        id=cid, domain="d", tenant_id=tenant, security_label=label,
        relations=[StructuralRelation(
            pred="CAUSES",
            args=[StructuralRelation(pred="EXCESS", args=["l"]),
                  StructuralRelation(pred="QUEUEING", args=["r"])])],
        types={"l": "LOAD", "r": "RESOURCE"},
    )


def test_untagged_records_are_hidden_from_every_tenant():
    """REGRESSION: an untagged record was visible to every tenant through the
    provider and the repository, and hidden by the close operator. A
    cross-tenant disclosure whose occurrence depended on which code path asked.
    """
    authz = AuthorizationFilter(tenant_id="t1")
    assert not authz.tenant_allowed(None)
    assert not authz.tenant_allowed("t2")
    assert authz.tenant_allowed("t1")


def test_all_three_call_sites_now_agree():
    """The actual defect was the divergence, so the test compares them rather
    than asserting any one of them in isolation."""
    graph = EvidenceGraph()
    graph.add_node(Node(id="untagged", type="X", attributes={}))
    graph.add_node(Node(id="foreign", type="X",
                        attributes={"tenant_id": "t2"}))
    graph.add_node(Node(id="mine", type="X", attributes={"tenant_id": "t1"}))
    provider = GraphProvider(graph, tenant_id="t1")

    repo = InMemoryStructuralCaseRepository()
    for cid, tenant in (("untagged", None), ("foreign", "t2"), ("mine", "t1")):
        repo.save(_case(cid, tenant))
    visible = {c.id for c in repo.list(tenant_id="t1")}

    policy = RuntimePolicy(tenant_id="t1")

    for node_id, tenant, expected in (("untagged", None, False),
                                      ("foreign", "t2", False),
                                      ("mine", "t1", True)):
        attrs = {} if tenant is None else {"tenant_id": tenant}
        by_provider = provider._tenant_allowed(attrs)
        by_repo = node_id in visible
        by_close = _authorized(
            CloseRecordModel(id=node_id, domain="d", attrs=attrs), policy)
        assert by_provider == by_repo == by_close == expected, (
            f"{node_id}: provider={by_provider} repo={by_repo} "
            f"close={by_close}, expected {expected}")


def test_unlabelled_stays_visible_and_the_asymmetry_is_deliberate():
    """Label fails OPEN while tenant fails CLOSED. That is a decision, so it
    gets a test; an absent label means unclassified, an absent tenant means
    unattributed, and only one of those is safe to share."""
    authz = AuthorizationFilter(
        tenant_id="t1", allowed_security_labels=frozenset({"PUBLIC"}))
    assert authz.label_allowed(None)
    assert authz.label_allowed("PUBLIC")
    assert not authz.label_allowed("SECRET")
    assert not authz.tenant_allowed(None)


def test_wildcards_and_scopes():
    star = AuthorizationFilter(allowed_security_labels=frozenset({"*"}),
                               authorization_scope=frozenset({"*"}))
    assert star.label_allowed("ANYTHING")
    assert star.scope_allowed(["nothing-in-common"])

    scoped = AuthorizationFilter(authorization_scope=frozenset({"design"}))
    assert scoped.scope_allowed(None)              # undeclared: open
    assert scoped.scope_allowed("design")
    assert scoped.scope_allowed(["ops", "design"])
    assert not scoped.scope_allowed(["ops"])


def test_refusals_are_explainable():
    """A filter that silently drops rows produces empty result sets nobody can
    explain, and the first question is always 'filtered, or absent?'."""
    authz = AuthorizationFilter(
        tenant_id="t1", allowed_security_labels=frozenset({"PUBLIC"}))
    assert "no tenant tag" in authz.refusal_reason({})
    assert "tenant 't2'" in authz.refusal_reason({"tenant_id": "t2"})
    assert "SECRET" in authz.refusal_reason(
        {"tenant_id": "t1", "security_label": "SECRET"})
    assert authz.refusal_reason(
        {"tenant_id": "t1", "security_label": "PUBLIC"}) is None


# --------------------------------------------------------------------------
# 2. The projection guard inspects nested relations
# --------------------------------------------------------------------------

def _pair():
    src = Record(
        id="src", domain="a", attrs={"incident": True, "severity": 4},
        types={"w": "AGENT", "p": "RESOURCE", "c": "LOAD", "t": "SIGNAL"},
        rels=(R("DEPENDS", "w", "p"),
              R("CAUSES", R("EXCESS", "c"), R("QUEUEING", "p")),
              R("INCREASES", R("QUEUEING", "p"), "t"),
              R("SENSES", "w", "t"),
              R("CAUSES", R("SENSES", "w", "t"), R("RETRIES", "w", "p"))))
    tgt = TargetPattern(
        name="t", domain="b",
        types={"a": "AGENT", "r": "RESOURCE", "l": "LOAD", "s": "SIGNAL"},
        rels=(R("DEPENDS", "a", "r"),
              R("CAUSES", R("EXCESS", "l"), R("QUEUEING", "r")),
              R("INCREASES", R("QUEUEING", "r"), "s")))
    return src, tgt


def _tight_schema() -> Schema:
    """EXCESS restricted to LOAD, so EXCESS(cash) is inadmissible while the
    CAUSES that contains it is perfectly well formed."""
    return Schema(
        [CORE_SCHEMA.signature(p) for p in sorted(CORE_SCHEMA.vocabulary)
         if p != "EXCESS"]
        + [Signature("EXCESS", (ENT("LOAD"),), "too much {0}")],
        role_parents={"SERVICE": "AGENT", "CASH": "RESOURCE"},
    )


def _nested_fixture():
    """A source relation with no target counterpart, so it must be PROJECTED,
    whose inner argument becomes inadmissible once substituted.

    Getting this fixture right is most of the test: an earlier version used a
    source whose only nested relations already had target counterparts, so
    they were mapped rather than projected, the guard had nothing to refuse,
    and guarded and unguarded output were identical. The test passed while
    proving nothing.
    """
    src = Record(
        id="src", domain="a", attrs={"incident": True, "severity": 4},
        types={"w": "AGENT", "p": "RESOURCE", "c": "LOAD"},
        rels=(R("DEPENDS", "w", "p"),
              R("QUEUEING", "p"),
              # no counterpart in the target -> projected, not mapped
              R("CAUSES", R("EXCESS", "c"), R("SATURATES", "p"))))
    # `l` is typed CASH, so the substituted EXCESS(l) violates its signature
    # while the CAUSES wrapping it does not.
    tgt = TargetPattern(
        name="bad", domain="b",
        types={"a": "AGENT", "r": "RESOURCE", "l": "CASH"},
        rels=(R("DEPENDS", "a", "r"), R("QUEUEING", "r")))
    return src, tgt


def test_guard_refuses_an_inadmissible_subrelation():
    """REGRESSION: the guard validated only the outermost predicate, so
    CAUSES(INCOMPLETE(cash), QUEUEING(stage)) passed — CAUSES takes two
    relations and nobody looked inside. Found by a deployment, not by the
    benchmark, because the benchmark corpus never produced a container whose
    argument was inadmissible."""
    tight = _tight_schema()
    src, tgt = _nested_fixture()
    mapping = align(src, tgt, Knobs(use_types=False), tight)
    mapping.ent_map.setdefault("c", "l")          # force the bad binding
    admitted, blocked = project(mapping, tgt,
                                Knobs(guard_projection=True), tight)
    for rel in admitted:
        for part in walk([rel]):
            assert not tight.check_args(part.pred, part.args, tgt.types), (
                f"guard admitted {rel} containing an inadmissible {part}")


def test_the_guard_actually_changes_the_output():
    """A guard that refuses nothing is a guard nobody should trust, so the
    unguarded run must emit something the guarded run does not."""
    tight = _tight_schema()
    src, tgt = _nested_fixture()

    def run(guarded: bool):
        mapping = align(src, tgt, Knobs(use_types=False), tight)
        mapping.ent_map.setdefault("c", "l")
        return project(mapping, tgt, Knobs(guard_projection=guarded), tight)

    unguarded, _ = run(False)
    guarded, blocked = run(True)

    bad = [r for r in unguarded
           for part in walk([r])
           if tight.check_args(part.pred, part.args, tgt.types)]
    assert bad, "fixture failed to produce an inadmissible projection"
    assert blocked, "the guard refused nothing"
    assert len(guarded) < len(unguarded)


# --------------------------------------------------------------------------
# 3. Every non-answer carries a structured gap
# --------------------------------------------------------------------------

def _records(n: int, **attrs) -> list[CloseRecordModel]:
    return [CloseRecordModel(id=f"r{i}", domain="d",
                             attrs={"kind": "x", **attrs}, text="body")
            for i in range(n)]


def test_request_names_the_binding_constraint():
    result = UnifiedCloseOperator().run(UnifiedCloseRequest(
        query="q",
        records=_records(3),
        constraints=[CloseConstraintSpec(name="kind", kind="has",
                                         attr="kind", values=["x"]),
                     CloseConstraintSpec(name="impossible", kind="has",
                                         attr="kind", values=["nope"])],
        expect_unique=False,
    ))
    assert result.outcome == "REQUEST"
    assert result.gaps and result.gap is result.gaps[0]
    assert result.gaps[0].constraint_id == "impossible"


def test_ambiguity_yields_a_specification_gap_not_a_data_request():
    """REGRESSION: ABSTAIN returned prose only. Ambiguity and missing evidence
    need opposite actions — more evidence cannot resolve an underspecified
    query — so collapsing them into one untyped string forced the caller to
    parse English to decide what to do."""
    result = UnifiedCloseOperator().run(UnifiedCloseRequest(
        query="q",
        records=_records(3),
        constraints=[CloseConstraintSpec(name="kind", kind="has",
                                         attr="kind", values=["x"])],
        expect_unique=True,
    ))
    assert result.outcome == "ABSTAIN"
    assert result.gaps, "ABSTAIN must still say what would settle it"
    gap = result.gaps[0]
    assert gap.constraint_kind == "domain"
    assert gap.constraint_id == "__specification__"
    assert "discriminating constraint" in gap.suggested_action
    assert gap.candidate_count_before_failure >= 2


def test_budget_exhaustion_is_typed_as_budget_not_as_missing_data():
    result = UnifiedCloseOperator().run(UnifiedCloseRequest(
        query="q",
        records=_records(4),
        constraints=[CloseConstraintSpec(name="kind", kind="has",
                                         attr="kind", values=["x"]),
                     CloseConstraintSpec(name="lex", kind="lexical",
                                         terms=["body"])],
        expect_unique=False,
        policy=RuntimePolicy(max_expansions=1),
    ))
    assert result.outcome == "ABSTAIN"
    assert result.gaps
    gap = result.gaps[0]
    assert gap.constraint_kind == "budget"
    assert "allowance" in gap.required_evidence.lower()


def test_closed_carries_no_gap():
    result = UnifiedCloseOperator().run(UnifiedCloseRequest(
        query="q",
        records=_records(1),
        constraints=[CloseConstraintSpec(name="kind", kind="has",
                                         attr="kind", values=["x"])],
        expect_unique=True,
    ))
    assert result.outcome == "CLOSED"
    assert result.gaps == [] and result.gap is None


def test_unauthorized_records_never_reach_the_operator():
    result = UnifiedCloseOperator().run(UnifiedCloseRequest(
        query="q",
        records=_records(2, tenant_id="t2"),
        constraints=[CloseConstraintSpec(name="kind", kind="has",
                                         attr="kind", values=["x"])],
        expect_unique=False,
        policy=RuntimePolicy(tenant_id="t1"),
    ))
    assert result.telemetry["authorized_records"] == 0
    assert result.telemetry["input_records"] == 2
    assert result.outcome in ("REQUEST", "ABSTAIN")


# --------------------------------------------------------------------------
# 4. Migration path for the tenant behaviour change
# --------------------------------------------------------------------------

def test_legacy_mode_restores_pre_51_behaviour_with_a_warning():
    """A breaking change with no migration path is just a breakage.

    Legacy mode exists so an upgrade does not silently empty a deployment's
    result sets on the day it lands. The warning exists because a silent legacy
    mode never gets switched off -- nobody is ever reminded it is on.
    """
    import warnings
    from ctd.authz import TenantMode, UntaggedRecordWarning

    legacy = AuthorizationFilter(tenant_id="t1", tenant_mode=TenantMode.LEGACY)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert legacy.tenant_allowed(None) is True
    assert any(issubclass(w.category, UntaggedRecordWarning) for w in caught)

    # legacy relaxes ONLY the untagged case; a foreign tenant is still refused
    assert legacy.tenant_allowed("t2") is False
    assert legacy.tenant_allowed("t1") is True


def test_strict_is_the_default_everywhere():
    from ctd.authz import TenantMode
    assert AuthorizationFilter(tenant_id="t1").tenant_mode == TenantMode.STRICT
    assert AuthorizationFilter.from_policy(
        RuntimePolicy(tenant_id="t1")).tenant_mode == TenantMode.STRICT


def test_the_audit_sizes_the_exposure_before_the_switch():
    """Run this BEFORE switching to strict. The number that matters is how
    many records are visible to everyone today and will become visible to
    nobody -- finding that out from a user reporting an empty screen is the
    expensive order to do it in."""
    from ctd.authz import audit_tenant_tags

    records = ([CloseRecordModel(id=f"ok{i}", domain="d",
                                 attrs={"tenant_id": "t1"}) for i in range(3)]
               + [CloseRecordModel(id=f"bare{i}", domain="d", attrs={})
                  for i in range(2)])
    audit = audit_tenant_tags(records)
    assert audit.total == 5
    assert audit.tagged == 3
    assert audit.untagged == 2
    assert audit.untagged_ids == ("bare0", "bare1")
    assert audit.tenants == ("t1",)
    assert not audit.safe_to_switch
    assert "NOT safe to switch" in str(audit)

    clean = audit_tenant_tags(records[:3])
    assert clean.safe_to_switch and "Safe to switch" in str(clean)


def test_legacy_mode_flows_from_the_runtime_policy():
    result = UnifiedCloseOperator().run(UnifiedCloseRequest(
        query="q",
        records=[CloseRecordModel(id="untagged", domain="d",
                                  attrs={"kind": "x"})],
        constraints=[CloseConstraintSpec(name="kind", kind="has",
                                         attr="kind", values=["x"])],
        expect_unique=True,
        policy=RuntimePolicy(tenant_id="t1", tenant_mode="legacy"),
    ))
    assert result.telemetry["authorized_records"] == 1
    assert result.outcome == "CLOSED"

    strict = UnifiedCloseOperator().run(UnifiedCloseRequest(
        query="q",
        records=[CloseRecordModel(id="untagged", domain="d",
                                  attrs={"kind": "x"})],
        constraints=[CloseConstraintSpec(name="kind", kind="has",
                                         attr="kind", values=["x"])],
        expect_unique=True,
        policy=RuntimePolicy(tenant_id="t1"),
    ))
    assert strict.telemetry["authorized_records"] == 0
