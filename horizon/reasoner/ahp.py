"""Analytic Hierarchy Process — the maths behind driving force impact scores.

The LLM only ever answers one question: "which of these two clusters impacts
this driving force more, A or B?". That gives a matrix of pairwise preferences;
AHP turns it into a ranking with a built-in measure of how self-contradictory
the answers were.

Kept free of I/O so the arithmetic can be pinned in tests without a model.
"""

import numpy as np

#: Saaty intensity recorded for every win. The LLM answers A or B with no
#: strength attached, so all wins have to share one intensity — and that choice
#: decides whether CR can still tell a ranking apart from a contradiction.
#:
#: A fixed intensity cannot compound: a clean chain A>B>C implies A>C at p², but
#: it gets recorded at p, which registers as inconsistency even though nothing
#: contradicts. Measured CR for a contradiction-free chain against a cyclic one:
#:
#:     p     transitive n=3 / n=6     cyclic n=3   n=6 with two reversals
#:     1.5      0.016 / 0.015            0.144            0.052
#:     2.0      0.046 / 0.044            0.431            0.159
#:     2.5      0.081 / 0.077            0.776            0.292
#:     3.0      0.117 / 0.112            1.149            0.439
#:
#: At 3.0 a perfectly ordered field already breaches Saaty's 0.10 cutoff; at 1.5
#: two genuine reversals slip under it. 2.0 separates the two cleanly, and
#: "weak preference" is the honest reading of an answer that carries no strength.
PREFERENCE = 2.0

#: Saaty's random consistency index by matrix order (RI[n] for n alternatives).
RANDOM_INDEX = {1: 0.0, 2: 0.0, 3: 0.58, 4: 0.90, 5: 1.12, 6: 1.24, 7: 1.32, 8: 1.41}

#: Above this, the comparisons contradict each other enough that the ranking is
#: not trustworthy — Saaty's conventional cutoff.
MAX_CONSISTENCY_RATIO = 0.10


def matrix_from_wins(wins: dict[tuple[int, int], bool], size: int) -> np.ndarray:
    """Build a reciprocal comparison matrix from pairwise A/B outcomes.

    `wins[(i, j)]` is True when alternative i was judged to impact the force more
    than j. The reciprocal entry is filled automatically; unjudged pairs stay at
    1.0, meaning "no preference expressed".
    """
    matrix = np.ones((size, size), dtype=np.float64)
    for (i, j), i_wins in wins.items():
        matrix[i, j] = PREFERENCE if i_wins else 1.0 / PREFERENCE
        matrix[j, i] = 1.0 / matrix[i, j]
    np.fill_diagonal(matrix, 1.0)
    return matrix


def priority_vector(matrix: np.ndarray, iterations: int = 100, tol: float = 1e-9) -> np.ndarray:
    """Principal eigenvector by power iteration, normalised to sum to 1."""
    size = matrix.shape[0]
    vector = np.full(size, 1.0 / size, dtype=np.float64)
    for _ in range(iterations):
        nxt = matrix @ vector
        total = nxt.sum()
        if total <= 0:
            return vector
        nxt /= total
        if np.abs(nxt - vector).max() < tol:
            return nxt
        vector = nxt
    return vector


def consistency_ratio(matrix: np.ndarray, weights: np.ndarray) -> float:
    """Saaty's CR. 0 means perfectly consistent; > 0.1 means treat with suspicion.

    Orders above the published RI table return 0.0 rather than a fabricated
    index — no number is better than an invented one.
    """
    size = matrix.shape[0]
    if size < 3:
        return 0.0
    random_index = RANDOM_INDEX.get(size)
    if not random_index:
        return 0.0

    weighted_sum = matrix @ weights
    with np.errstate(divide="ignore", invalid="ignore"):
        ratios = np.divide(weighted_sum, weights, out=np.zeros_like(weights), where=weights > 0)
    lambda_max = float(ratios[weights > 0].mean()) if (weights > 0).any() else float(size)
    consistency_index = (lambda_max - size) / (size - 1)
    return max(0.0, consistency_index / random_index)


def impact_scores(wins: dict[tuple[int, int], bool], size: int) -> tuple[np.ndarray, float]:
    """Pairwise outcomes → impact per alternative in [0, 1], plus the CR.

    Priorities are rescaled against the strongest alternative rather than left as
    shares that sum to 1: with six clusters every share sits near 0.17, which
    reads as "no impact" when the field is really saying "this one leads".
    """
    matrix = matrix_from_wins(wins, size)
    weights = priority_vector(matrix)
    ratio = consistency_ratio(matrix, weights)

    strongest = float(weights.max())
    scaled = weights / strongest if strongest > 0 else weights
    return np.clip(scaled, 0.0, 1.0), ratio


def ranks_from(scores: np.ndarray) -> list[int]:
    """1-based ranks, highest score first. Ties take the same rank as the first."""
    order = np.argsort(-scores, kind="stable")
    ranks = [0] * len(scores)
    for position, index in enumerate(order, start=1):
        ranks[int(index)] = position
    return ranks
