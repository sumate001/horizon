"""Kleinberg two-state burst detection.

A compact version of the automaton from Kleinberg (2002), "Bursty and
Hierarchical Structure in Streams", specialised to two states and Poisson
arrivals per day:

    state 0  baseline rate λ₀ = mean(counts)
    state 1  burst rate    λ₁ = s · λ₀

Being in a state costs the negative log-likelihood of that day's count under its
rate; entering the burst state additionally costs γ·log(n), which stops the
automaton from flipping to "burst" on every noisy day. Viterbi picks the cheapest
state sequence, and the score is the share of days it decided were bursting.

Implemented here rather than pulled in: the published `burst_detection` package
is a thin wrapper over the same recurrence and would be a dependency for ~40
lines.
"""

import math

#: How much hotter the burst state is than baseline.
BURST_SCALE = 2.0
#: Transition cost multiplier — higher means bursts must be more convincing.
GAMMA = 1.0


def _poisson_nll(count: int, rate: float) -> float:
    """−log P(count | Poisson(rate)), with the constant term kept for symmetry."""
    rate = max(rate, 1e-9)
    return rate - count * math.log(rate) + math.lgamma(count + 1)


def burst_score(
    counts: list[int], *, scale: float = BURST_SCALE, gamma: float = GAMMA
) -> float:
    """Fraction of days the automaton assigns to the burst state, in [0, 1].

    Returns 0.0 for series too short or too flat to say anything: a single day of
    activity is not a burst, it is a data point.
    """
    if len(counts) < 3:
        return 0.0

    total = sum(counts)
    if total == 0:
        return 0.0

    base_rate = total / len(counts)
    if base_rate <= 0:
        return 0.0
    rates = (base_rate, base_rate * scale)
    transition_cost = gamma * math.log(len(counts))

    # Viterbi over two states.
    costs = [_poisson_nll(counts[0], rates[0]), _poisson_nll(counts[0], rates[1]) + transition_cost]
    backpointers: list[tuple[int, int]] = []

    for count in counts[1:]:
        next_costs = [0.0, 0.0]
        step: list[int] = []
        for state in (0, 1):
            emission = _poisson_nll(count, rates[state])
            # Only 0 → 1 is penalised; relaxing back to baseline is free.
            options = [
                costs[0] + emission + (transition_cost if state == 1 else 0.0),
                costs[1] + emission,
            ]
            best = min(range(2), key=lambda prev: options[prev])
            next_costs[state] = options[best]
            step.append(best)
        backpointers.append((step[0], step[1]))
        costs = next_costs

    state = min(range(2), key=lambda s: costs[s])
    path = [state]
    for previous in reversed(backpointers):
        state = previous[state]
        path.append(state)
    path.reverse()

    return sum(path) / len(path)
