"""Editorial triage — pinned against the OSINT//DESK implementation it replaces.

A score has to mean the same thing before and after the move, so the formula and
the thresholds are checked by hand rather than by re-deriving them from the code.
"""

import pytest

from horizon.pipeline.triage import (
    SCORED_DIMENSIONS,
    VERDICTS,
    calculate_total,
    clamp,
    determine_verdict,
    score,
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
    assert calculate_total(scores()) == pytest.approx(7.5)


def test_sensitivity_is_a_multiplier_not_a_seventh_dimension():
    """It scales the mean; it is not averaged into it."""
    flat = scores(sensitivity=0.0)
    assert calculate_total(flat) == pytest.approx(5.0)
    assert calculate_total(scores(sensitivity=10.0)) == pytest.approx(10.0)


def test_total_is_capped_at_ten():
    maxed = scores(**dict.fromkeys(SCORED_DIMENSIONS, 10.0), sensitivity=10.0)
    assert calculate_total(maxed) == 10.0


def test_an_all_zero_article_totals_zero():
    zeroed = scores(**dict.fromkeys((*SCORED_DIMENSIONS, "sensitivity"), 0.0))
    assert calculate_total(zeroed) == 0.0


def test_reliability_counts_toward_the_total():
    """It is one of the six — a distrusted source drags the whole score down."""
    trusted = calculate_total(scores(reliability=10.0, sensitivity=0.0))
    distrusted = calculate_total(scores(reliability=0.0, sensitivity=0.0))
    assert trusted > distrusted


# ── verdicts ─────────────────────────────────────────────────────────────────


def test_a_high_total_is_priority():
    assert determine_verdict(7.5, scores()) == "PRIORITY"


def test_extreme_urgency_is_priority_regardless_of_total():
    """Something that must be acted on now outranks its own average."""
    assert determine_verdict(2.0, scores(urgency=9.0)) == "PRIORITY"


def test_priority_is_checked_before_fast_track():
    """A story qualifying for both is reported as the more urgent one."""
    both = scores(urgency=9.0, impact=9.0, reliability=9.0)
    assert determine_verdict(3.0, both) == "PRIORITY"


def test_high_impact_from_a_trusted_source_is_fast_track():
    assert determine_verdict(3.0, scores(impact=8.0, reliability=7.0)) == "FAST_TRACK"


def test_high_impact_from_a_distrusted_source_is_not_fast_tracked():
    """Impact alone does not earn a fast track — the source has to be good for it."""
    assert determine_verdict(6.0, scores(impact=9.0, reliability=3.0)) == "INVESTIGATE"


def test_a_middling_total_is_investigate():
    assert determine_verdict(5.5, scores()) == "INVESTIGATE"


def test_everything_else_passes():
    assert determine_verdict(5.49, scores(urgency=0.0, impact=0.0)) == "PASS"


def test_every_verdict_is_reachable():
    produced = {
        determine_verdict(8.0, scores()),
        determine_verdict(3.0, scores(impact=8.0, reliability=7.0)),
        determine_verdict(6.0, scores(impact=0.0)),
        determine_verdict(1.0, scores(urgency=0.0, impact=0.0)),
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
    )
    assert result.verdict == "PASS"


def test_the_result_maps_onto_the_event_columns():
    columns = score({"relevance": 6}, credibility_weight=0.8).as_columns()
    assert columns["score_relevance"] == 6.0
    assert columns["score_reliability"] == pytest.approx(8.0)
    assert "triage_total" in columns
    assert columns["triage_verdict"] in VERDICTS
