from verification_compiler.coverage import coverage_matrix, coverage_problems
from verification_compiler.policy import evaluate_spec

from . import fakes


def test_all_compiled_contract_items_need_test_links():
    spec = fakes.spec()
    assert coverage_problems(fakes.CONTRACT, spec) == []
    assert [row["id"] for row in coverage_matrix(fakes.CONTRACT, spec)] == ["FR_1", "EP_1"]
    spec["acceptance_tests"][0]["covers"] = ["FR_1"]
    assert any("EP_1" in issue for issue in evaluate_spec(spec, fakes.CONTRACT))


def test_unknown_or_invented_coverage_id_is_rejected():
    spec = fakes.spec()
    spec["acceptance_tests"][0]["covers"].append("FR_999")
    assert any("unknown requirement coverage ids" in issue for issue in coverage_problems(fakes.CONTRACT, spec))
