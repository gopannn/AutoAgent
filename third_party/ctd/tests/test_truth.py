from datetime import UTC, datetime, timedelta

from ctd.models import Claim, ClaimStatus, Evidence
from ctd.truth import EvidenceFusion, TruthMaintainer

AS_OF = datetime(2026, 9, 12, tzinfo=UTC)


def ev(evidence_id: str, *, group: str, source_type: str = "registry", confidence: float = 0.9) -> Evidence:
    return Evidence(
        id=evidence_id,
        source_id=f"src:{evidence_id}",
        source_type=source_type,
        observed_at=AS_OF - timedelta(days=1),
        confidence=confidence,
        trust=0.9,
        independence_group=group,
    )


def test_fusion_discounts_duplicate_independence_groups():
    fused = EvidenceFusion().fuse([ev("a", group="feed"), ev("b", group="feed")], as_of=AS_OF)
    independent = EvidenceFusion().fuse(
        [ev("a", group="feed1"), ev("b", group="feed2")], as_of=AS_OF
    )

    assert independent.strength > fused.strength
    assert fused.source_diversity == 1
    assert independent.source_diversity == 2


def test_fusion_rejects_temporally_invalid_and_disallowed_source_evidence():
    expired = ev("expired", group="old")
    expired.valid_until = AS_OF - timedelta(days=10)
    wrong_source = ev("wrong", group="wrong", source_type="social")

    result = EvidenceFusion().fuse(
        [expired, wrong_source],
        as_of=AS_OF,
        required_source_classes={"registry"},
    )

    assert result.strength == 0.0
    assert set(result.rejected_evidence_ids) == {"expired", "wrong"}


def test_newer_temporal_claim_supersedes_old_claim_without_contradiction():
    active_old = Claim(
        key="cert:Y:status",
        value="active",
        status=ClaimStatus.SUPPORTED,
        valid_from=AS_OF - timedelta(days=100),
        valid_until=AS_OF - timedelta(days=5),
    )
    revoked_new = Claim(
        key="cert:Y:status",
        value="revoked",
        status=ClaimStatus.SUPPORTED,
        valid_from=AS_OF - timedelta(days=5),
    )

    assessment = TruthMaintainer().evaluate_claims([active_old, revoked_new], as_of=AS_OF)

    assert assessment.status == ClaimStatus.SUPERSEDED
    assert assessment.value == "revoked"
    assert assessment.contradiction_risk < 1.0


def test_simultaneous_incompatible_claims_are_disputed():
    claims = [
        Claim(key="x", value="a", status=ClaimStatus.SUPPORTED, valid_from=AS_OF - timedelta(days=1)),
        Claim(key="x", value="b", status=ClaimStatus.SUPPORTED, valid_from=AS_OF - timedelta(days=1)),
    ]

    assessment = TruthMaintainer().evaluate_claims(claims, as_of=AS_OF)

    assert assessment.status == ClaimStatus.DISPUTED
    assert assessment.contradiction_risk == 1.0


def test_fusion_exposes_rejection_reasons_per_evidence_record():
    expired = ev("expired-reason", group="old")
    expired.valid_until = AS_OF - timedelta(days=1)
    wrong_source = ev("wrong-reason", group="wrong", source_type="social")

    result = EvidenceFusion().fuse(
        [expired, wrong_source],
        as_of=AS_OF,
        required_source_classes={"registry"},
    )

    assert result.rejection_reasons["expired-reason"] == "temporally_invalid"
    assert result.rejection_reasons["wrong-reason"] == "source_class_not_allowed"
