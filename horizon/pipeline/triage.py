"""Editorial triage scoring.

Ported from OSINT//DESK's triage module when Horizon took over everything on the
way in. The dimensions, the formula and the verdict thresholds are unchanged, so
scores from before and after the move remain comparable.

One thing did change, deliberately. OSINT//DESK asked the model to score
`reliability` by reading the article; Horizon already knows how much the
newsroom trusts each outlet — `sources.credibility_weight`, set and tuned by an
analyst — so that number is used instead of a guess. A model reading one article
cannot know an outlet's track record, and pretending otherwise put a fabricated
figure into a formula that drives what humans look at.

`novelty` is still the model's read of "is this new information", which is weak:
it judges from the article alone. Weak signal detection later measures novelty
properly, against every event in the corpus. Treat this one as a rough prior.
"""

from dataclasses import dataclass

#: Weighted into `total`. sensitivity is excluded — it is a multiplier below.
SCORED_DIMENSIONS = (
    "relevance",
    "urgency",
    "impact",
    "novelty",
    "reliability",
    "actionability",
)

VERDICTS = ("PRIORITY", "FAST_TRACK", "INVESTIGATE", "PASS")


@dataclass(frozen=True)
class TriageResult:
    relevance: float
    urgency: float
    impact: float
    novelty: float
    reliability: float
    sensitivity: float
    actionability: float
    total: float
    verdict: str

    def as_columns(self) -> dict:
        return {
            "score_relevance": self.relevance,
            "score_urgency": self.urgency,
            "score_impact": self.impact,
            "score_novelty": self.novelty,
            "score_reliability": self.reliability,
            "score_sensitivity": self.sensitivity,
            "score_actionability": self.actionability,
            "triage_total": self.total,
            "triage_verdict": self.verdict,
        }


def clamp(value: object, low: float = 0.0, high: float = 10.0) -> float:
    """Coerce a model-supplied score into range. Junk becomes 0."""
    try:
        return max(low, min(high, float(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return low


def calculate_total(scores: dict[str, float]) -> float:
    """Mean of the six weighted dimensions, lifted by sensitivity.

    Kept identical to OSINT//DESK's `_calculate_total` so a score means the same
    thing on both sides of the move.
    """
    base = sum(scores.get(dimension, 0.0) for dimension in SCORED_DIMENSIONS) / len(
        SCORED_DIMENSIONS
    )
    return round(min(10.0, base * (1 + scores.get("sensitivity", 0.0) * 0.1)), 2)


def determine_verdict(total: float, scores: dict[str, float]) -> str:
    """Thresholds carried over verbatim from OSINT//DESK.

    Order matters: PRIORITY is checked before FAST_TRACK, so a story that
    qualifies for both is reported as the more urgent of the two.
    """
    if total >= 7.5 or scores.get("urgency", 0.0) >= 9:
        return "PRIORITY"
    if scores.get("impact", 0.0) >= 8 and scores.get("reliability", 0.0) >= 7:
        return "FAST_TRACK"
    if total >= 5.5:
        return "INVESTIGATE"
    return "PASS"


def score(payload: dict, *, credibility_weight: float) -> TriageResult:
    """Turn a model response plus the source's credibility into a triage result.

    `credibility_weight` is the 0–1 figure from the source registry, rescaled to
    the 0–10 the rest of the dimensions use.
    """
    scores = {
        "relevance": clamp(payload.get("relevance")),
        "urgency": clamp(payload.get("urgency")),
        "impact": clamp(payload.get("impact")),
        "novelty": clamp(payload.get("novelty")),
        "sensitivity": clamp(payload.get("sensitivity")),
        "actionability": clamp(payload.get("actionability")),
        "reliability": clamp(credibility_weight * 10.0),
    }
    total = calculate_total(scores)
    return TriageResult(**scores, total=total, verdict=determine_verdict(total, scores))
