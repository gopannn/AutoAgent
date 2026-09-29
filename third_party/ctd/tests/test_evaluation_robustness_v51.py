"""Perturbation-study tests.

The standing criticism of this benchmark is that the corpus shares a
vocabulary by construction. That is usually left as an unfalsifiable caveat in
a release note. It does not have to be: perturb the encoding in the ways a
second author would differ, and measure.

Two of these tests exist purely to stop an arm from passing vacuously. Both
perturbation arms failed that way on their first run — one changed nothing and
reported a perfect score, the other changed the names the answer was written in
and reported a total collapse. An arm that does not perturb, or that is scored
against the wrong thing, is worse than no arm: it produces a number people
quote.
"""

from __future__ import annotations

from ctd.adapters import ALL_INCIDENTS, INTERIORS_SCHEMA
from ctd.adapters.cross_domain import CROSS_DOMAIN_INCIDENTS
from ctd.kernel import R
from ctd.kernel.evaluate import (
    _kin_library, format_perturbations, kin_equal, kin_swap_count,
    run_ablations, run_perturbations,
)
from ctd.kernel.schema import CORE_SCHEMA


def test_the_kin_perturbation_actually_perturbs():
    """ANTI-VACUITY. The first version tried only the alphabetically first
    family sibling and reverted on any signature failure, so it changed 0 of
    127 relations and the arm scored a flawless +0%."""
    changed, total = kin_swap_count(CROSS_DOMAIN_INCIDENTS, CORE_SCHEMA)
    assert total > 100
    assert changed >= 10, (
        f"only {changed}/{total} relations re-worded; the arm is vacuous")


def test_rewritten_library_is_still_schema_valid():
    """A perturbation that produces invalid encodings measures the validator,
    not the engine."""
    from ctd.kernel import validate
    for rec in _kin_library(CROSS_DOMAIN_INCIDENTS, CORE_SCHEMA):
        v = validate(rec, CORE_SCHEMA)
        assert v.ok, f"{rec.id}: {[f.code for f in v.errors]}"


def test_kin_equality_credits_family_substitution_only():
    assert kin_equal(R("SATURATES", "x"), R("FAILS", "x"), CORE_SCHEMA)
    assert kin_equal(R("CAUSES", R("EXCESS", "l"), R("SATURATES", "r")),
                     R("CAUSES", R("EXCESS", "l"), R("FAILS", "r")),
                     CORE_SCHEMA)
    # different entity -> not equal
    assert not kin_equal(R("SATURATES", "x"), R("FAILS", "y"), CORE_SCHEMA)
    # different family -> not equal
    assert not kin_equal(R("SATURATES", "x"), R("QUEUEING", "x"), CORE_SCHEMA)
    # CAUSES is family-less by design and must never be credited
    assert not kin_equal(R("CAUSES", R("EXCESS", "l"), R("EXCESS", "m")),
                         R("PRECEDES", R("EXCESS", "l"), R("EXCESS", "m")),
                         CORE_SCHEMA)


def test_alignment_is_invariant_under_entity_renaming():
    """The load-bearing structural claim: alignment matches relations, not
    names. If this moves, the cross-domain story is unsound."""
    arms = run_perturbations(CROSS_DOMAIN_INCIDENTS, CORE_SCHEMA)
    base = next(a for a in arms if a.name == "unperturbed").result
    renamed = next(a for a in arms if a.name == "entities renamed").result
    assert renamed.mrr == base.mrr
    assert renamed.hit_rate_at(10) == base.hit_rate_at(10)
    assert renamed.deep_hit_rate_at(10) == base.deep_hit_rate_at(10)


def test_rewording_the_library_costs_nothing_under_kin_credited_scoring():
    """The answer to the vocabulary criticism, measured.

    Under strict equality, re-wording the library costs 75% of MRR — but that
    is the metric refusing to credit the substitution kinship exists to make.
    Under kin-credited scoring the engine recovers the same findings, which is
    what 'does not depend on shared authorship' means operationally.
    """
    arms = run_perturbations(CROSS_DOMAIN_INCIDENTS, CORE_SCHEMA)
    base = next(a for a in arms if a.name == "unperturbed").result
    reworded = next(a for a in arms if a.name == "library re-worded").result
    assert reworded.kin_hit_rate_at(10) == base.kin_hit_rate_at(10)
    assert reworded.kin_hit_rate_at(10) > 0


def test_degradation_is_graceful_not_catastrophic():
    arms = run_perturbations(CROSS_DOMAIN_INCIDENTS, CORE_SCHEMA)
    base = next(a for a in arms if a.name == "unperturbed").result
    dropped = next(a for a in arms if "dropped" in a.name).result
    distracted = next(a for a in arms if "distractor" in a.name).result
    assert dropped.mrr >= base.mrr * 0.7, "losing one relation should not collapse it"
    assert distracted.mrr >= base.mrr, "an unrelated relation must not help or hurt"


def test_the_report_renders():
    assert "kin@" in format_perturbations(
        run_perturbations(CROSS_DOMAIN_INCIDENTS, CORE_SCHEMA))


# --------------------------------------------------------------------------
# The combined corpus resolves what eleven incidents could not
# --------------------------------------------------------------------------

def test_combined_corpus_separates_the_guard_ablations():
    """At n=11 every guard arm scored identically and the release notes had to
    report that as a corpus limitation. At n=20 they separate, so components
    that could not be shown to earn their place now are."""
    arms = run_ablations(ALL_INCIDENTS, schema=INTERIORS_SCHEMA)
    by = {a.name: a for a in arms}
    full = by["full"]

    assert len(full.per_query) == 20

    # kinship and subsumption demotion both now demonstrably contribute
    assert by["no kinship"].mrr < full.mrr * 0.5
    assert by["flat ranking"].hit_rate_at(1) < full.hit_rate_at(1)

    # and the controls stay beaten on the metric that matters
    assert by["frequency control"].deep_hit_rate_at(10) == 0.0
    assert full.deep_hit_rate_at(10) > 0.3
    assert by["random control"].mrr < full.mrr * 0.2
