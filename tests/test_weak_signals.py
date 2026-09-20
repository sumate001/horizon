import uuid
from datetime import UTC, datetime

import numpy as np
import pytest

from horizon.batch.burst import burst_score
from horizon.batch.weak_signals import (
    _OPEN_STATUSES,
    Candidate,
    category_rarity,
    daily_counts_from,
    isolation_scores,
)
from horizon.models import WEAK_SIGNAL_STATUSES

# ── Kleinberg burst ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("counts", [[], [5], [1, 2]])
def test_a_series_too_short_to_judge_scores_zero(counts):
    assert burst_score(counts) == 0.0


def test_silence_is_not_a_burst():
    assert burst_score([0] * 10) == 0.0


def test_a_flat_series_is_not_a_burst():
    assert burst_score([3] * 12) == pytest.approx(0.0)


def test_a_spike_after_quiet_days_is_a_burst():
    quiet_then_spike = [0, 0, 1, 0, 1, 0, 0, 12, 15, 11]
    assert burst_score(quiet_then_spike) > 0.0


def test_the_burst_score_is_a_fraction_of_days():
    score = burst_score([0, 0, 0, 0, 0, 0, 20, 25, 22, 18])
    assert 0.0 <= score <= 1.0


def test_a_bigger_spike_bursts_at_least_as_much_as_a_smaller_one():
    baseline = [1, 1, 1, 1, 1, 1, 1, 1]
    assert burst_score([*baseline, 30]) >= burst_score([*baseline, 3])


def test_a_higher_gamma_makes_bursts_harder_to_declare():
    counts = [0, 1, 0, 1, 8, 9, 7, 1]
    assert burst_score(counts, gamma=8.0) <= burst_score(counts, gamma=0.1)


# ── category rarity ──────────────────────────────────────────────────────────


def test_an_unseen_category_is_maximally_rare():
    assert category_rarity(["พลังงาน"], {"การเมือง": 50}, 50) == pytest.approx(1.0)


def test_a_ubiquitous_category_is_not_rare():
    assert category_rarity(["การเมือง"], {"การเมือง": 100}, 100) == pytest.approx(0.0)


def test_rarity_follows_the_least_common_category():
    """A story tagged both common and rare is interesting for the rare angle.

    พลังงาน holds 5% of the corpus, so it drives the score (1 − 0.05), not the
    90% that การเมือง holds.
    """
    counts = {"การเมือง": 90, "พลังงาน": 5}
    assert category_rarity(["การเมือง", "พลังงาน"], counts, 100) == pytest.approx(0.95)


def test_a_story_in_only_common_categories_is_not_rare():
    counts = {"การเมือง": 90, "เศรษฐกิจ": 80}
    assert category_rarity(["การเมือง", "เศรษฐกิจ"], counts, 100) == pytest.approx(0.20)


def test_an_untagged_candidate_is_treated_as_rare():
    assert category_rarity([], {"การเมือง": 10}, 10) == pytest.approx(1.0)


def test_rarity_is_defined_on_an_empty_corpus():
    assert category_rarity(["การเมือง"], {}, 0) == pytest.approx(1.0)


# ── isolation ────────────────────────────────────────────────────────────────


def test_isolation_scores_stay_within_the_unit_interval():
    rng = np.random.default_rng(0)
    features = np.vstack([rng.normal(size=(30, 4)), np.array([[50.0, 50.0, 50.0, 50.0]])])
    scores = isolation_scores(features)
    assert scores.shape == (31,)
    assert ((scores >= 0.0) & (scores <= 1.0)).all()


def test_an_obvious_outlier_scores_above_the_crowd():
    rng = np.random.default_rng(1)
    crowd = rng.normal(loc=1.0, scale=0.1, size=(40, 4))
    features = np.vstack([crowd, np.array([[99.0, 99.0, 99.0, 99.0]])])
    scores = isolation_scores(features)
    assert scores[-1] > scores[:-1].max()


# ── daily bucketing ──────────────────────────────────────────────────────────


def test_daily_counts_cover_every_day_in_the_window():
    since = datetime(2026, 8, 1, 6, 0, tzinfo=UTC)
    until = datetime(2026, 8, 5, 6, 0, tzinfo=UTC)
    counts = daily_counts_from([datetime(2026, 8, 3, 12, 0, tzinfo=UTC)], since, until)
    assert len(counts) == 5
    assert counts == [0, 0, 1, 0, 0]


def test_timestamps_outside_the_window_are_ignored():
    since = datetime(2026, 8, 1, tzinfo=UTC)
    until = datetime(2026, 8, 3, tzinfo=UTC)
    counts = daily_counts_from(
        [datetime(2026, 7, 20, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC)], since, until
    )
    assert sum(counts) == 0


# ── combined score ───────────────────────────────────────────────────────────


def _candidate(**kwargs) -> Candidate:
    base = {
        "event_id": None,
        "cluster_id": None,
        "title": "ทดสอบ",
        "vector": [],
        "categories": [],
        "daily_counts": [],
        "reports_per_day": 1.0,
        "distinct_sources": 1,
        "mean_credibility": 1.0,
    }
    base.update(kwargs)
    return Candidate(**base)


def test_combined_score_applies_the_configured_weights():
    """(0.4·1 + 0.3·1 + 0.3·1) × 1.0 = 1.0"""
    candidate = _candidate(novelty_score=1.0, isolation_score=1.0, burst_score=1.0)
    assert candidate.combined_score == pytest.approx(1.0)


def test_combined_score_mixes_the_three_components():
    """0.4·0.9 + 0.3·0.5 + 0.3·0.2 = 0.57"""
    candidate = _candidate(novelty_score=0.9, isolation_score=0.5, burst_score=0.2)
    assert candidate.combined_score == pytest.approx(0.57)


def test_low_credibility_damps_the_whole_score():
    """Credibility multiplies rather than adds: an anomaly seen only in
    low-trust outlets is suppressed, not merely nudged down."""
    strong = _candidate(novelty_score=1.0, isolation_score=1.0, burst_score=1.0)
    weak = _candidate(
        novelty_score=1.0, isolation_score=1.0, burst_score=1.0, mean_credibility=0.3
    )
    assert weak.combined_score == pytest.approx(strong.combined_score * 0.3)


def test_a_perfectly_ordinary_candidate_scores_zero():
    assert _candidate().combined_score == pytest.approx(0.0)


# ── the gate that decides what is even eligible ──────────────────────────────


def test_a_candidate_that_is_not_growing_is_not_a_weak_signal_however_odd():
    """The failure this gate exists to stop, stated as a test.

    A one-off story is maximally unlike a corpus of Thai political news simply
    by being irrelevant to it, so novelty and isolation both reward it — and
    together they are 70% of the score. Measured over 994 real candidates, 987
    were single events and the ranking they produced was led by a woman with a
    birthmark condition and a blood-noodle shop. "Small but growing" is the
    definition and the growing half is not optional.
    """
    odd_but_static = _candidate(novelty_score=1.0, isolation_score=1.0, burst_score=0.0)

    assert odd_but_static.combined_score > 0.6  # would clear any usable threshold
    assert not odd_but_static.is_emerging


def test_any_evidence_of_growth_is_enough_to_be_eligible():
    """The gate asks a yes/no question; how strongly it is growing is the score's
    job, not the gate's."""
    assert _candidate(burst_score=0.13).is_emerging


def test_the_threshold_is_reachable_by_a_real_candidate():
    """Regression on the miscalibration itself, not on a number.

    0.65 was unreachable: novelty tops out near 0.55 on this corpus and burst
    near 0.13, so the best candidate in three days scored 0.364 and the detector
    reported "nothing anomalous" every run. A threshold no real candidate can
    reach is indistinguishable from a broken detector, so this pins the ceiling
    the data actually has against the setting.
    """
    from horizon.config import get_settings

    best_observed = _candidate(
        novelty_score=0.55, isolation_score=0.79, burst_score=0.13, mean_credibility=0.9
    )
    assert best_observed.combined_score >= get_settings().weak_signal_t


# ── not filing the same signal twice ─────────────────────────────────────────


def test_a_clustered_candidate_is_identified_by_its_cluster():
    """The batch runs on a schedule and a story keeps qualifying until it stops
    growing, so the same signal is filed every cycle unless something says two
    runs are talking about one story. A cluster is that identity — the story is
    the same however many articles join it."""
    cluster, event = uuid.uuid4(), uuid.uuid4()

    assert _candidate(cluster_id=cluster, event_id=event).ref == cluster


def test_a_noise_candidate_has_only_itself_to_be_identified_by():
    event = uuid.uuid4()

    assert _candidate(event_id=event).ref == event


def test_a_candidate_referring_to_nothing_is_not_matched_against_anything():
    """`None in already_open` must never be true, or one unclustered signal
    would suppress every other."""
    assert _candidate().ref is None


# ── what counts as "already in someone's inbox" ──────────────────────────────


def test_a_signal_already_sent_still_suppresses_the_same_story():
    """The reasoner flips candidate → dispatched the moment a signal leaves for
    OSINT//DESK. If that took it out of the open set, the batch three hours
    later would file the identical story again — which is what happened: 254
    signals for 188 stories, one of them seven times."""
    assert "dispatched" in _OPEN_STATUSES


def test_a_story_reopens_only_once_somebody_has_ruled_on_it():
    """A verdict is the one thing that makes a fresh burst news again. Anything
    else still awaits an answer and must not be re-sent."""
    closed = set(WEAK_SIGNAL_STATUSES) - set(_OPEN_STATUSES)

    assert closed == {"verified_true", "verified_false", "expired"}


def test_every_open_status_is_a_real_status():
    """A typo here would silently stop suppressing anything."""
    assert set(_OPEN_STATUSES) <= set(WEAK_SIGNAL_STATUSES)
