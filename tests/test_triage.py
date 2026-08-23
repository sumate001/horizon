"""Editorial triage — pinned against the OSINT//DESK implementation it replaces.

A score has to mean the same thing before and after the move, so the formula and
the thresholds are checked by hand rather than by re-deriving them from the code.
"""

import pytest

from horizon.pipeline.triage import (
    SCORED_DIMENSIONS,
    VERDICTS,
    TriageSettings,
    calculate_total,
    clamp,
    determine_verdict,
    rescore,
    score,
)

#: Pin the formula, not whatever the deployment happens to be tuned to. The
#: coefficient is the number most likely to be changed, so a test that reads it
#: from the environment would stop testing anything.
FIXED = TriageSettings(
    sensitivity_coefficient=0.1,
    priority_total=7.5,
    priority_urgency=9.0,
    fasttrack_impact=8.0,
    fasttrack_reliability=7.0,
    investigate_total=5.5,
)


def scores(**overrides) -> dict:
    base = dict.fromkeys((*SCORED_DIMENSIONS, "sensitivity"), 5.0)
    base.update(overrides)
    return base


# ── clamping ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value,expected", [(-3, 0.0), (0, 0.0), (7.5, 7.5), (10, 10.0), (99, 10.0)]
)
def test_scores_are_held_inside_the_scale(value, expected):
    assert clamp(value) == expected


@pytest.mark.parametrize("value", [None, "high", "", [], {}])
def test_a_junk_score_costs_that_dimension_not_the_article(value):
    """A model that returns nonsense for one dimension must not fail the item."""
    assert clamp(value) == 0.0


# ── the formula ──────────────────────────────────────────────────────────────


def test_total_is_the_mean_of_six_dimensions_lifted_by_sensitivity():
    """All six at 5 with sensitivity 5: 5 × (1 + 0.5) = 7.5"""
    assert calculate_total(scores(), FIXED) == pytest.approx(7.5)


def test_sensitivity_is_a_multiplier_not_a_seventh_dimension():
    """It scales the mean; it is not averaged into it."""
    flat = scores(sensitivity=0.0)
    assert calculate_total(flat, FIXED) == pytest.approx(5.0)
    assert calculate_total(scores(sensitivity=10.0), FIXED) == pytest.approx(10.0)


def test_total_is_capped_at_ten():
    maxed = scores(**dict.fromkeys(SCORED_DIMENSIONS, 10.0), sensitivity=10.0)
    assert calculate_total(maxed, FIXED) == 10.0


def test_an_all_zero_article_totals_zero():
    zeroed = scores(**dict.fromkeys((*SCORED_DIMENSIONS, "sensitivity"), 0.0))
    assert calculate_total(zeroed, FIXED) == 0.0


def test_reliability_counts_toward_the_total():
    """It is one of the six — a distrusted source drags the whole score down."""
    trusted = calculate_total(scores(reliability=10.0, sensitivity=0.0), FIXED)
    distrusted = calculate_total(scores(reliability=0.0, sensitivity=0.0), FIXED)
    assert trusted > distrusted


# ── verdicts ─────────────────────────────────────────────────────────────────


def test_a_high_total_is_priority():
    assert determine_verdict(7.5, scores(), FIXED) == "PRIORITY"


def test_extreme_urgency_is_priority_regardless_of_total():
    """Something that must be acted on now outranks its own average."""
    assert determine_verdict(2.0, scores(urgency=9.0), FIXED) == "PRIORITY"


def test_priority_is_checked_before_fast_track():
    """A story qualifying for both is reported as the more urgent one."""
    both = scores(urgency=9.0, impact=9.0, reliability=9.0)
    assert determine_verdict(3.0, both, FIXED) == "PRIORITY"


def test_high_impact_from_a_trusted_source_is_fast_track():
    assert determine_verdict(3.0, scores(impact=8.0, reliability=7.0), FIXED) == "FAST_TRACK"


def test_high_impact_from_a_distrusted_source_is_not_fast_tracked():
    """Impact alone does not earn a fast track — the source has to be good for it."""
    assert determine_verdict(6.0, scores(impact=9.0, reliability=3.0), FIXED) == "INVESTIGATE"


def test_a_middling_total_is_investigate():
    assert determine_verdict(5.5, scores(), FIXED) == "INVESTIGATE"


def test_everything_else_passes():
    assert determine_verdict(5.49, scores(urgency=0.0, impact=0.0), FIXED) == "PASS"


def test_every_verdict_is_reachable():
    produced = {
        determine_verdict(8.0, scores(), FIXED),
        determine_verdict(3.0, scores(impact=8.0, reliability=7.0), FIXED),
        determine_verdict(6.0, scores(impact=0.0), FIXED),
        determine_verdict(1.0, scores(urgency=0.0, impact=0.0), FIXED),
    }
    assert produced == set(VERDICTS)


# ── reliability comes from the source registry ───────────────────────────────


def test_reliability_is_taken_from_the_source_not_the_article():
    """The whole point of the change: a model reading one article cannot know an
    outlet's track record, but the source registry does."""
    result = score({"relevance": 5}, credibility_weight=0.9)
    assert result.reliability == pytest.approx(9.0)


def test_a_model_supplied_reliability_is_ignored():
    result = score({"reliability": 10}, credibility_weight=0.2)
    assert result.reliability == pytest.approx(2.0)


def test_credibility_is_rescaled_from_zero_to_one_onto_zero_to_ten():
    assert score({}, credibility_weight=0.0).reliability == 0.0
    assert score({}, credibility_weight=1.0).reliability == 10.0


def test_a_missing_dimension_scores_zero_rather_than_failing():
    result = score({}, credibility_weight=0.5)
    assert result.relevance == 0.0
    assert result.verdict == "PASS"


# ── the whole thing ──────────────────────────────────────────────────────────


def test_a_strong_story_from_a_trusted_outlet_reaches_priority():
    result = score(
        {
            "relevance": 9,
            "urgency": 8,
            "impact": 9,
            "novelty": 8,
            "sensitivity": 3,
            "actionability": 8,
        },
        credibility_weight=0.9,
        settings=FIXED,
    )
    assert result.verdict == "PRIORITY"
    assert result.total > 7.5


def test_a_thin_story_from_a_weak_outlet_passes():
    result = score(
        {
            "relevance": 2,
            "urgency": 1,
            "impact": 2,
            "novelty": 1,
            "sensitivity": 0,
            "actionability": 1,
        },
        credibility_weight=0.3,
        settings=FIXED,
    )
    assert result.verdict == "PASS"


def test_the_result_maps_onto_the_event_columns():
    columns = score({"relevance": 6}, credibility_weight=0.8).as_columns()
    assert columns["score_relevance"] == 6.0
    assert columns["score_reliability"] == pytest.approx(8.0)
    assert "triage_total" in columns
    assert columns["triage_verdict"] in VERDICTS


# ── tunability ───────────────────────────────────────────────────────────────


def test_the_sensitivity_coefficient_changes_the_total():
    """The reason it is configurable: at 0.1 a mid story reaches the ceiling on
    sensitivity alone, which is what saturated the live scale."""
    strong = scores(sensitivity=10.0)
    assert calculate_total(strong, FIXED) == pytest.approx(10.0)

    gentle = TriageSettings(**{**FIXED.__dict__, "sensitivity_coefficient": 0.03})
    assert calculate_total(strong, gentle) == pytest.approx(6.5)


def test_raising_the_priority_threshold_demotes_borderline_stories():
    strict = TriageSettings(**{**FIXED.__dict__, "priority_total": 9.0})
    assert determine_verdict(8.0, scores(urgency=0.0), FIXED) == "PRIORITY"
    assert determine_verdict(8.0, scores(urgency=0.0), strict) == "INVESTIGATE"


def test_rescore_recomputes_from_stored_dimensions_without_a_model():
    """Tuning runs through this: the dimensions are on the row already."""
    stored = scores(sensitivity=10.0)
    hot, _ = rescore(stored, FIXED)
    cool, _ = rescore(stored, TriageSettings(**{**FIXED.__dict__, "sensitivity_coefficient": 0.0}))
    assert hot > cool == pytest.approx(5.0)
