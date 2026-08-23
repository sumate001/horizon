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

from ..config import get_settings

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


@dataclass(frozen=True)
class TriageSettings:
    """Everything tunable about the formula, in one place.

    Kept as a value object rather than read from config inside the maths so the
    same functions can score a live article and answer "what if we used 0.05
    instead" against stored data.
    """

    sensitivity_coefficient: float
    priority_total: float
    priority_urgency: float
    fasttrack_impact: float
    fasttrack_reliability: float
    investigate_total: float

    @classmethod
    def from_env(cls) -> "TriageSettings":
        s = get_settings()
        return cls(
            sensitivity_coefficient=s.triage_sensitivity_coefficient,
            priority_total=s.triage_priority_total,
            priority_urgency=s.triage_priority_urgency,
            fasttrack_impact=s.triage_fasttrack_impact,
            fasttrack_reliability=s.triage_fasttrack_reliability,
            investigate_total=s.triage_investigate_total,
        )


def calculate_total(scores: dict[str, float], settings: TriageSettings | None = None) -> float:
    """Mean of the six weighted dimensions, lifted by sensitivity.

    The lift is `1 + sensitivity × coefficient`. OSINT//DESK hardcoded the
    coefficient at 0.1, which lets a mid-range story reach the cap on the
    strength of sensitivity alone; it is configurable here for that reason.
    """
    settings = settings or TriageSettings.from_env()
    base = sum(scores.get(dimension, 0.0) for dimension in SCORED_DIMENSIONS) / len(
        SCORED_DIMENSIONS
    )
    lift = 1 + scores.get("sensitivity", 0.0) * settings.sensitivity_coefficient
    return round(min(10.0, base * lift), 2)


def determine_verdict(
    total: float, scores: dict[str, float], settings: TriageSettings | None = None
) -> str:
    """Order matters: PRIORITY is checked before FAST_TRACK, so a story that
    qualifies for both is reported as the more urgent of the two."""
    settings = settings or TriageSettings.from_env()
    if total >= settings.priority_total or scores.get("urgency", 0.0) >= settings.priority_urgency:
        return "PRIORITY"
    if (
        scores.get("impact", 0.0) >= settings.fasttrack_impact
        and scores.get("reliability", 0.0) >= settings.fasttrack_reliability
    ):
        return "FAST_TRACK"
    if total >= settings.investigate_total:
        return "INVESTIGATE"
    return "PASS"


def dimensions(payload: dict, *, credibility_weight: float) -> dict[str, float]:
    """The six scored dimensions plus sensitivity, cleaned and in range."""
    return {
        "relevance": clamp(payload.get("relevance")),
        "urgency": clamp(payload.get("urgency")),
        "impact": clamp(payload.get("impact")),
        "novelty": clamp(payload.get("novelty")),
        "sensitivity": clamp(payload.get("sensitivity")),
        "actionability": clamp(payload.get("actionability")),
        "reliability": clamp(credibility_weight * 10.0),
    }


def score(
    payload: dict, *, credibility_weight: float, settings: TriageSettings | None = None
) -> TriageResult:
    """Turn a model response plus the source's credibility into a triage result.

    `credibility_weight` is the 0–1 figure from the source registry, rescaled to
    the 0–10 the rest of the dimensions use.
    """
    settings = settings or TriageSettings.from_env()
    scores = dimensions(payload, credibility_weight=credibility_weight)
    total = calculate_total(scores, settings)
    return TriageResult(**scores, total=total, verdict=determine_verdict(total, scores, settings))


def rescore(stored: dict[str, float], settings: TriageSettings) -> tuple[float, str]:
    """Recompute from dimensions already on an event.

    Tuning runs through here: the six dimensions are stored, so trying a
    different coefficient costs a division, not an LLM call.
    """
    total = calculate_total(stored, settings)
    return total, determine_verdict(total, stored, settings)
