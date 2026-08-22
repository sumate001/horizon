import uuid
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from sklearn.cluster import HDBSCAN

from horizon.batch.clustering import (
    NOISE,
    _EventRow,
    assemble_clusters,
    build_distance_matrix,
    normalize_rows,
)
from horizon.config import get_settings

TEMPORAL_WEIGHT = get_settings().temporal_weight
SELECTION = get_settings().cluster_selection_method


def _rows(n: int, base: datetime | None = None) -> list[_EventRow]:
    base = base or datetime(2026, 8, 1, tzinfo=UTC)
    return [
        _EventRow(
            id=uuid.uuid4(),
            summary=f"สรุปเหตุการณ์ที่ {i}",
            created_at=base + timedelta(hours=i),
            event_time=None,
            categories=["เศรษฐกิจ"],
        )
        for i in range(n)
    ]


def _cluster_of(dim: int, count: int, seed: int, spread: float = 0.05) -> np.ndarray:
    """`count` vectors tightly grouped around one random direction."""
    rng = np.random.default_rng(seed)
    centre = rng.normal(size=dim)
    centre /= np.linalg.norm(centre)
    return centre + rng.normal(scale=spread, size=(count, dim))


# ── normalisation ────────────────────────────────────────────────────────────


def test_normalize_rows_produces_unit_vectors():
    matrix = np.array([[3.0, 4.0], [1.0, 0.0], [0.0, 2.0]])
    assert np.allclose(np.linalg.norm(normalize_rows(matrix), axis=1), 1.0)


def test_normalize_rows_survives_a_zero_vector():
    """An all-zero embedding would divide by zero; it must not produce NaN."""
    assert np.isfinite(normalize_rows(np.zeros((1, 4)))).all()


# ── the metric ───────────────────────────────────────────────────────────────


def test_identical_text_at_the_same_moment_is_distance_zero():
    embeddings = np.array([[1.0, 0.0], [1.0, 0.0]])
    days = np.array([0.0, 0.0])
    assert build_distance_matrix(embeddings, days, TEMPORAL_WEIGHT)[0, 1] == pytest.approx(0.0)


def test_opposite_text_reaches_the_cosine_maximum_of_two():
    embeddings = np.array([[1.0, 0.0], [-1.0, 0.0]])
    days = np.array([0.0, 0.0])
    assert build_distance_matrix(embeddings, days, TEMPORAL_WEIGHT)[0, 1] == pytest.approx(2.0)


def test_the_diagonal_is_always_zero():
    embeddings = np.random.default_rng(0).normal(size=(6, 8))
    days = np.arange(6, dtype=np.float64)
    assert np.allclose(np.diag(build_distance_matrix(embeddings, days, TEMPORAL_WEIGHT)), 0.0)


def test_the_matrix_is_symmetric():
    embeddings = np.random.default_rng(1).normal(size=(7, 8))
    days = np.array([0.0, 1.0, 3.0, 3.5, 9.0, 12.0, 20.0])
    matrix = build_distance_matrix(embeddings, days, TEMPORAL_WEIGHT)
    assert np.allclose(matrix, matrix.T)


def test_time_apart_costs_the_weight_per_day():
    embeddings = np.array([[1.0, 0.0], [1.0, 0.0]])  # identical text
    days = np.array([0.0, 4.0])
    matrix = build_distance_matrix(embeddings, days, TEMPORAL_WEIGHT)
    assert matrix[0, 1] == pytest.approx(4.0 * TEMPORAL_WEIGHT)


# Measured over every pair of real events in the production Qdrant collection:
# bge-m3 cosine distance on Thai news spans [0.13, 0.87], mean 0.66, sd 0.067.
# It never approaches the theoretical maximum of 2, which is why the temporal
# weight has to be calibrated against these numbers and not against the bound.
OBSERVED_MAX_SEMANTIC_DISTANCE = 0.87
OBSERVED_SEMANTIC_SD = 0.067


def test_two_weeks_apart_outweighs_any_semantic_distance_the_data_produces():
    assert 14 * TEMPORAL_WEIGHT > OBSERVED_MAX_SEMANTIC_DISTANCE

    identical = np.array([[1.0, 0.0], [1.0, 0.0]])
    far_apart = build_distance_matrix(identical, np.array([0.0, 14.0]), TEMPORAL_WEIGHT)[0, 1]
    assert far_apart > OBSERVED_MAX_SEMANTIC_DISTANCE


def test_a_single_day_stays_inside_the_semantic_noise_floor():
    """One day may nudge the ranking; it must not decide it.

    The spec's 0.15 costs 2.2 sd per day, which is what collapsed clustering into
    time buckets. Anything under ~1.5 sd keeps topic in charge at short ranges.
    """
    assert TEMPORAL_WEIGHT / OBSERVED_SEMANTIC_SD < 1.5


# ── end-to-end clustering behaviour ──────────────────────────────────────────


def _labels(embeddings: np.ndarray, days: np.ndarray, min_cluster_size: int = 4) -> np.ndarray:
    distance = build_distance_matrix(embeddings, days, TEMPORAL_WEIGHT)
    return HDBSCAN(
        min_cluster_size=min_cluster_size, metric="precomputed", cluster_selection_method=SELECTION
    ).fit_predict(distance)


def test_two_distinct_topics_on_the_same_day_form_two_clusters():
    embeddings = np.vstack([_cluster_of(32, 8, seed=1), _cluster_of(32, 8, seed=2)])
    labels = _labels(embeddings, np.zeros(16))
    assert len(set(labels.tolist()) - {NOISE}) == 2
    assert len(set(labels[:8].tolist())) == 1
    assert set(labels[:8].tolist()) != set(labels[8:].tolist())


def test_the_same_topic_three_weeks_apart_does_not_co_cluster():
    """Semantically identical, but far enough apart in time to be separate stories."""
    topic = _cluster_of(32, 16, seed=3)
    days = np.concatenate([np.zeros(8), np.full(8, 21.0)])
    labels = _labels(topic, days)
    early, late = set(labels[:8].tolist()), set(labels[8:].tolist())
    assert not (early & late - {NOISE})


def test_an_unrelated_one_off_event_is_left_as_noise():
    rng = np.random.default_rng(7)
    outlier = rng.normal(size=(1, 32))
    outlier /= np.linalg.norm(outlier)
    embeddings = np.vstack([_cluster_of(32, 8, seed=4), outlier])
    labels = _labels(embeddings, np.zeros(9))
    assert labels[-1] == NOISE


# ── cluster assembly ─────────────────────────────────────────────────────────


def test_assemble_clusters_skips_noise_and_normalises_centroids():
    embeddings = np.vstack([_cluster_of(16, 5, seed=5), _cluster_of(16, 5, seed=6)])
    labels = np.array([0] * 5 + [1] * 4 + [NOISE])
    clusters = assemble_clusters(labels, _rows(10), embeddings)

    assert len(clusters) == 2
    assert [len(c.members) for c in clusters] == [5, 4]
    assert all(np.linalg.norm(c.centroid) == pytest.approx(1.0) for c in clusters)


def test_the_cluster_label_comes_from_a_member_summary():
    embeddings = _cluster_of(16, 5, seed=8)
    rows = _rows(5)
    clusters = assemble_clusters(np.zeros(5, dtype=int), rows, embeddings)
    assert clusters[0].label in {r.summary for r in rows}


def test_cluster_categories_are_ranked_by_how_often_members_carry_them():
    rows = _rows(4)
    rows[0].categories = ["เศรษฐกิจ", "การเมือง"]
    rows[1].categories = ["เศรษฐกิจ"]
    rows[2].categories = ["เศรษฐกิจ", "พลังงาน"]
    rows[3].categories = ["การเมือง"]
    clusters = assemble_clusters(np.zeros(4, dtype=int), rows, _cluster_of(16, 4, seed=9))
    assert clusters[0].categories[0] == "เศรษฐกิจ"
    assert set(clusters[0].categories) == {"เศรษฐกิจ", "การเมือง", "พลังงาน"}
