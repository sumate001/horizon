"""Trend maths pinned to exact values on fixed fixtures, as the spec requires."""

from datetime import UTC, datetime, timedelta
from itertools import pairwise

import numpy as np
import pytest

from horizon.batch.trends import (
    EWMA_SPAN,
    PROVISIONAL_DAYS,
    compute_series,
    ewma,
    floor_window,
    is_provisional,
    score_windows,
    trend_score,
    window_grid,
    zscore,
)

# ── windowing ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "moment,expected_hour",
    [
        (datetime(2026, 8, 22, 0, 0, tzinfo=UTC), 0),
        (datetime(2026, 8, 22, 5, 59, tzinfo=UTC), 0),
        (datetime(2026, 8, 22, 6, 0, tzinfo=UTC), 6),
        (datetime(2026, 8, 22, 13, 30, tzinfo=UTC), 12),
        (datetime(2026, 8, 22, 23, 59, tzinfo=UTC), 18),
    ],
)
def test_floor_window_snaps_to_six_hour_boundaries(moment, expected_hour):
    floored = floor_window(moment)
    assert floored.hour == expected_hour
    assert (floored.minute, floored.second, floored.microsecond) == (0, 0, 0)


def test_window_grid_is_contiguous_and_inclusive():
    start = datetime(2026, 8, 20, 1, 0, tzinfo=UTC)
    end = datetime(2026, 8, 21, 13, 0, tzinfo=UTC)
    grid = window_grid(start, end)
    assert grid[0] == datetime(2026, 8, 20, 0, 0, tzinfo=UTC)
    assert grid[-1] == datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
    assert all((b - a) == timedelta(hours=6) for a, b in pairwise(grid))


# ── EWMA ─────────────────────────────────────────────────────────────────────


def test_ewma_of_a_constant_series_is_that_constant():
    assert ewma([5.0] * 10) == pytest.approx([5.0] * 10)


def test_ewma_first_point_equals_the_input():
    assert ewma([3.0, 9.0, 1.0])[0] == pytest.approx(3.0)


def test_ewma_matches_the_adjusted_formula_by_hand():
    """alpha = 2/(span+1) = 2/7; adjusted EWMA of [1, 2] is
    (2 + 1·(1−alpha)) / (1 + (1−alpha))."""
    alpha = 2.0 / (EWMA_SPAN + 1.0)
    decay = 1.0 - alpha
    expected = (2.0 + 1.0 * decay) / (1.0 + decay)
    assert ewma([1.0, 2.0])[1] == pytest.approx(expected)


def test_ewma_lags_a_step_change():
    values = ewma([0.0, 0.0, 0.0, 10.0])
    assert 0 < values[3] < 10.0


# ── z-scores ─────────────────────────────────────────────────────────────────


def test_zscore_needs_two_prior_points():
    series = np.array([1.0, 2.0, 3.0])
    assert zscore(series, 0) is None
    assert zscore(series, 1) is None
    assert zscore(series, 2) is not None


def test_zscore_of_a_flat_history_is_zero_not_infinite():
    assert zscore(np.array([4.0, 4.0, 4.0, 4.0]), 3) == 0.0


def test_zscore_uses_the_sample_standard_deviation():
    """History [0, 2, 4]: mean 2, sample sd 2 → (8 − 2) / 2 = 3."""
    assert zscore(np.array([0.0, 2.0, 4.0, 8.0]), 3) == pytest.approx(3.0)


# ── series composition ───────────────────────────────────────────────────────


def test_all_three_series_share_the_window_alignment():
    freq, velocity, acceleration = compute_series([1.0, 2.0, 3.0, 4.0, 5.0])
    assert len(freq) == len(velocity) == len(acceleration) == 5
    # Nothing precedes the first window, so it has no change to report.
    assert velocity[0] == 0.0
    assert acceleration[0] == 0.0


def test_a_quiet_cluster_produces_no_velocity():
    _, velocity, acceleration = compute_series([0.0] * 8)
    assert velocity == pytest.approx([0.0] * 8)
    assert acceleration == pytest.approx([0.0] * 8)


def test_a_rising_cluster_has_positive_velocity():
    _, velocity, _ = compute_series([0.0, 1.0, 3.0, 7.0, 15.0])
    assert all(v > 0 for v in velocity[1:])


# ── trend score ──────────────────────────────────────────────────────────────


def test_trend_score_applies_the_configured_weights():
    """0.3·1 + 0.4·2 + 0.3·3 = 2.0"""
    assert trend_score(1.0, 2.0, 3.0) == pytest.approx(2.0)


@pytest.mark.parametrize(
    "z", [(None, 1.0, 1.0), (1.0, None, 1.0), (1.0, 1.0, None)]
)
def test_trend_score_is_none_until_every_component_exists(z):
    assert trend_score(*z) is None


def test_score_windows_returns_one_row_per_window_with_aligned_bounds():
    grid = window_grid(
        datetime(2026, 8, 20, 0, 0, tzinfo=UTC), datetime(2026, 8, 21, 18, 0, tzinfo=UTC)
    )
    scores = score_windows(grid, [1.0, 0.0, 2.0, 5.0, 9.0, 14.0, 20.0, 27.0])
    assert len(scores) == len(grid)
    assert all(s.window_end - s.window_start == timedelta(hours=6) for s in scores)
    # The first two windows have too little history behind them to z-score.
    assert scores[0].trend_score is None
    assert scores[-1].trend_score is not None


def test_a_sustained_surge_scores_above_the_breakout_threshold():
    """A cluster that is flat and then climbs hard should clear 2.5."""
    grid = window_grid(
        datetime(2026, 8, 1, 0, 0, tzinfo=UTC), datetime(2026, 8, 4, 18, 0, tzinfo=UTC)
    )
    flat = [1.0] * 10
    surge = [4.0, 9.0, 16.0, 25.0, 36.0, 49.0]
    scores = score_windows(grid, flat + surge)
    assert scores[-1].trend_score is not None
    assert scores[-1].trend_score > 2.5


def test_steady_reporting_never_looks_like_a_breakout():
    grid = window_grid(
        datetime(2026, 8, 1, 0, 0, tzinfo=UTC), datetime(2026, 8, 4, 18, 0, tzinfo=UTC)
    )
    scores = score_windows(grid, [3.0] * 16)
    assert scores[-1].trend_score == pytest.approx(0.0)


# ── provisional gate ─────────────────────────────────────────────────────────


def test_a_cluster_younger_than_the_history_requirement_is_provisional():
    now = datetime(2026, 8, 22, tzinfo=UTC)
    assert is_provisional(now - timedelta(days=PROVISIONAL_DAYS - 1), now) is True
    assert is_provisional(now - timedelta(days=PROVISIONAL_DAYS + 1), now) is False


def test_a_cluster_with_no_first_seen_is_provisional():
    assert is_provisional(None, datetime(2026, 8, 22, tzinfo=UTC)) is True
