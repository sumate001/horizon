import numpy as np
import pytest

from horizon.reasoner.ahp import (
    MAX_CONSISTENCY_RATIO,
    PREFERENCE,
    consistency_ratio,
    impact_scores,
    matrix_from_wins,
    priority_vector,
    ranks_from,
)

# ── matrix construction ──────────────────────────────────────────────────────


def test_the_matrix_is_reciprocal_with_a_unit_diagonal():
    matrix = matrix_from_wins({(0, 1): True, (0, 2): False}, 3)
    assert np.allclose(np.diag(matrix), 1.0)
    assert matrix[0, 1] == pytest.approx(PREFERENCE)
    assert matrix[1, 0] == pytest.approx(1.0 / PREFERENCE)
    assert matrix[0, 2] == pytest.approx(1.0 / PREFERENCE)
    assert matrix[2, 0] == pytest.approx(PREFERENCE)


def test_pairs_nobody_judged_stay_neutral():
    """An unasked comparison must read as "no preference", not as a loss."""
    matrix = matrix_from_wins({(0, 1): True}, 3)
    assert matrix[0, 2] == pytest.approx(1.0)
    assert matrix[1, 2] == pytest.approx(1.0)


# ── priority vector ──────────────────────────────────────────────────────────


def test_an_all_neutral_matrix_gives_every_alternative_the_same_weight():
    weights = priority_vector(np.ones((4, 4)))
    assert weights == pytest.approx([0.25] * 4)


def test_the_priority_vector_sums_to_one():
    matrix = matrix_from_wins({(0, 1): True, (0, 2): True, (1, 2): True}, 3)
    assert priority_vector(matrix).sum() == pytest.approx(1.0)


def test_a_total_winner_outweighs_a_total_loser():
    #  0 beats 1 and 2; 1 beats 2.
    matrix = matrix_from_wins({(0, 1): True, (0, 2): True, (1, 2): True}, 3)
    weights = priority_vector(matrix)
    assert weights[0] > weights[1] > weights[2]


def test_the_priority_vector_recovers_a_known_saaty_matrix():
    """A perfectly consistent matrix built from weights [0.6, 0.3, 0.1]
    must return those weights."""
    truth = np.array([0.6, 0.3, 0.1])
    matrix = truth[:, None] / truth[None, :]
    assert priority_vector(matrix) == pytest.approx(truth, abs=1e-6)


# ── consistency ──────────────────────────────────────────────────────────────


def test_a_perfectly_consistent_matrix_has_zero_inconsistency():
    truth = np.array([0.5, 0.3, 0.2])
    matrix = truth[:, None] / truth[None, :]
    assert consistency_ratio(matrix, priority_vector(matrix)) == pytest.approx(0.0, abs=1e-6)


def test_consistency_is_undefined_below_three_alternatives():
    """Two alternatives cannot contradict themselves."""
    matrix = matrix_from_wins({(0, 1): True}, 2)
    assert consistency_ratio(matrix, priority_vector(matrix)) == 0.0


def test_a_rock_paper_scissors_cycle_is_flagged_as_inconsistent():
    """0 beats 1, 1 beats 2, and 2 beats 0 — no ranking can satisfy all three."""
    cycle = matrix_from_wins({(0, 1): True, (1, 2): True, (2, 0): True}, 3)
    ratio = consistency_ratio(cycle, priority_vector(cycle))
    assert ratio > MAX_CONSISTENCY_RATIO


def test_a_transitive_ranking_stays_within_the_acceptable_ratio():
    transitive = matrix_from_wins({(0, 1): True, (0, 2): True, (1, 2): True}, 3)
    ratio = consistency_ratio(transitive, priority_vector(transitive))
    assert ratio <= MAX_CONSISTENCY_RATIO


def test_the_preference_intensity_keeps_order_and_contradiction_separable():
    """The whole point of PREFERENCE: a contradiction-free field must pass and a
    self-contradictory one must fail, at the same conventional 0.10 cutoff.

    A fixed intensity cannot express compounding, so this only holds for a
    narrow band of values — see the measurements in ahp.py.
    """
    clean = {(i, j): True for i in range(6) for j in range(i + 1, 6)}
    contradictory = {**clean, (0, 5): False, (1, 4): False}

    def ratio(wins):
        matrix = matrix_from_wins(wins, 6)
        return consistency_ratio(matrix, priority_vector(matrix))

    assert ratio(clean) <= MAX_CONSISTENCY_RATIO
    assert ratio(contradictory) > MAX_CONSISTENCY_RATIO


# ── impact scores ────────────────────────────────────────────────────────────


def test_impact_scores_land_in_the_unit_interval_with_a_leader_at_one():
    scores, _ = impact_scores({(0, 1): True, (0, 2): True, (1, 2): True}, 3)
    assert ((scores >= 0.0) & (scores <= 1.0)).all()
    assert scores.max() == pytest.approx(1.0)


def test_impact_scores_preserve_the_pairwise_ordering():
    scores, _ = impact_scores({(0, 1): True, (0, 2): True, (1, 2): True}, 3)
    assert scores[0] > scores[1] > scores[2]


def test_an_undecided_field_scores_everyone_equally():
    scores, ratio = impact_scores({}, 4)
    assert scores == pytest.approx([1.0] * 4)
    assert ratio == pytest.approx(0.0)


def test_the_triggered_cluster_losing_every_comparison_scores_lowest():
    """Index 0 is the triggered cluster; it loses to all five others."""
    wins = {(0, j): False for j in range(1, 6)}
    scores, _ = impact_scores(wins, 6)
    assert scores[0] == scores.min()
    assert scores[0] < 1.0


def test_the_triggered_cluster_winning_every_comparison_scores_highest():
    wins = {(0, j): True for j in range(1, 6)}
    scores, _ = impact_scores(wins, 6)
    assert scores[0] == pytest.approx(1.0)


# ── ranks ────────────────────────────────────────────────────────────────────


def test_ranks_run_from_one_for_the_strongest():
    assert ranks_from(np.array([0.2, 1.0, 0.5])) == [3, 1, 2]


def test_tied_scores_keep_a_stable_order():
    assert ranks_from(np.array([0.5, 0.5, 0.9])) == [2, 3, 1]
