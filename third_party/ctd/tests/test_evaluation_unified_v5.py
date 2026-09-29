from ctd.evaluation import evaluate_structural_cases
from ctd.structural import StructuralCase, StructuralRelation


def R(pred, *args):
    return StructuralRelation(pred=pred, args=args)


def test_evaluation_rejects_schema_invalid_cases_instead_of_scoring_them():
    bad = StructuralCase(
        id="bad",
        domain="x",
        relations=(R("SATURATES", "resource", "extra"),),
        types={"resource": "resource", "extra": "signal"},
    )
    report = evaluate_structural_cases([bad])
    assert report.accepted_cases == 0
    assert report.rejected_cases[0]["case_id"] == "bad"
