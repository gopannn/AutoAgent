"""Example project adapter used by the generic negative-test harness."""


def validate(artifact):
    source = artifact.get("evidence", {}).get("source", {})
    return {
        "eligibility_is_derived": {
            "passes": "eligible" not in artifact.get("session", {}),
            "detail": "session must not cache the derived eligibility predicate",
        },
        "risk_band_is_not_stored": {
            "passes": "risk_band" not in artifact.get("account", {}),
            "detail": "risk_band is a view over risk_score and must not be stored",
        },
        "evidence_is_traceable": {
            "passes": bool(source.get("id") and source.get("locator")),
            "detail": "every evidence item requires source.id and source.locator",
        },
    }
