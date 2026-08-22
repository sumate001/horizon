"""Step 7 — driving force scoring, gated behind a signal.

For every active PESTEL force, the triggered cluster is compared head-to-head
against the current top clusters. Each comparison is one binary LLM question;
AHP turns the resulting matrix into an impact score plus a consistency ratio
that says how much the answers contradicted each other.

Cost is the reason this is gated: with 6 forces and 6 alternatives it is
6 × 15 = 90 comparisons plus 6 uncertainty questions per signal. Comparisons
between two *rival* clusters do not involve the triggered one and stay valid for
every signal in the same run, so they are cached across signals.
"""

import logging
import uuid
from dataclasses import dataclass, field
from itertools import combinations

from sqlalchemy import select

from ..config import get_settings
from ..db import session_scope
from ..llm.ollama import OllamaClient, OllamaError, get_ollama
from ..llm.prompts import pairwise_messages, uncertainty_messages
from ..models import Cluster, DrivingForce, Event, ForceAssessment
from .ahp import MAX_CONSISTENCY_RATIO, impact_scores, ranks_from

log = logging.getLogger("horizon.reasoner.forces")

#: low / medium / high → the numeric uncertainty stored on the assessment.
UNCERTAINTY_LEVELS = {"low": 0.2, "medium": 0.5, "high": 0.8}
DEFAULT_UNCERTAINTY = 0.5


@dataclass
class ForceResult:
    force_id: uuid.UUID
    force_name: str
    impact: float
    uncertainty: float
    ahp_rank: int
    consistency_ratio: float
    comparisons: int

    @property
    def trustworthy(self) -> bool:
        return self.consistency_ratio <= MAX_CONSISTENCY_RATIO


@dataclass
class ForceScoringReport:
    cluster_id: uuid.UUID
    alternatives: int = 0
    results: list[ForceResult] = field(default_factory=list)
    llm_failures: int = 0

    def as_log(self) -> dict:
        return {
            "cluster_id": str(self.cluster_id),
            "alternatives": self.alternatives,
            "forces_scored": len(self.results),
            "llm_failures": self.llm_failures,
            "inconsistent": [r.force_name for r in self.results if not r.trustworthy],
        }


class PairwiseJudge:
    """Asks the model which of two clusters moves a force more, and remembers.

    A failed or unparseable answer is a *skip*, not a guess: the pair is left
    unjudged, which AHP reads as "no preference expressed" rather than inventing
    a winner.
    """

    def __init__(self, client: OllamaClient | None = None) -> None:
        self.client = client or get_ollama()
        self.cache: dict[tuple[uuid.UUID, str, str], bool] = {}
        self.failures = 0

    async def judge(
        self,
        *,
        force_id: uuid.UUID,
        force_name: str,
        force_definition: str,
        left: tuple[str, str],
        right: tuple[str, str],
    ) -> bool | None:
        key = (force_id, left[0], right[0])
        if key in self.cache:
            return self.cache[key]
        flipped = (force_id, right[0], left[0])
        if flipped in self.cache:
            return not self.cache[flipped]

        try:
            answer = await self.client.chat_json(
                pairwise_messages(force_name, force_definition, left[1], right[1]),
                purpose="ahp_pairwise",
            )
        except OllamaError as exc:
            self.failures += 1
            log.warning(
                "pairwise comparison failed", extra={"force": force_name, "error": str(exc)}
            )
            return None

        choice = str(answer.get("choice", "")).strip().upper()
        if choice not in {"A", "B"}:
            self.failures += 1
            log.warning(
                "pairwise answer unusable", extra={"force": force_name, "answer": choice[:40]}
            )
            return None

        self.cache[key] = choice == "A"
        return self.cache[key]


async def _load_alternatives(
    cluster_id: uuid.UUID, top_n: int
) -> list[tuple[str, str]]:
    """The triggered cluster first, then the busiest rivals. (id, label) pairs."""
    async with session_scope() as session:
        triggered = await session.get(Cluster, cluster_id)
        if triggered is None:
            return []

        rivals = (
            await session.execute(
                select(Cluster.id, Cluster.label)
                .where(Cluster.status == "active", Cluster.id != cluster_id)
                .order_by(Cluster.event_count.desc(), Cluster.last_seen.desc())
                .limit(top_n)
            )
        ).all()

    label = triggered.label or "(ไม่มีชื่อกลุ่ม)"
    return [(str(cluster_id), label)] + [
        (str(rid), rlabel or "(ไม่มีชื่อกลุ่ม)") for rid, rlabel in rivals
    ]


async def _cluster_summaries(cluster_id: uuid.UUID, limit: int = 10) -> list[str]:
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Event.summary)
                .where(Event.cluster_id == cluster_id, Event.summary.isnot(None))
                .order_by(Event.created_at.desc())
                .limit(limit)
            )
        ).scalars()
        return [s for s in rows if s]


async def _uncertainty(
    judge: PairwiseJudge, force: DrivingForce, label: str, summaries: list[str]
) -> float:
    try:
        answer = await judge.client.chat_json(
            uncertainty_messages(force.name, force.definition, label, summaries),
            purpose="ahp_uncertainty",
        )
    except OllamaError as exc:
        judge.failures += 1
        log.warning("uncertainty query failed", extra={"force": force.name, "error": str(exc)})
        return DEFAULT_UNCERTAINTY

    level = str(answer.get("uncertainty", "")).strip().lower()
    if level not in UNCERTAINTY_LEVELS:
        judge.failures += 1
        log.warning(
            "uncertainty answer unusable",
            extra={"force": force.name, "answer": level[:20]},
        )
        return DEFAULT_UNCERTAINTY
    return UNCERTAINTY_LEVELS[level]


async def score_driving_forces(
    cluster_id: uuid.UUID, *, client: OllamaClient | None = None
) -> ForceScoringReport:
    """Score every active force for one cluster and persist the assessments."""
    settings = get_settings()
    report = ForceScoringReport(cluster_id=cluster_id)

    alternatives = await _load_alternatives(cluster_id, settings.ahp_top_clusters)
    if len(alternatives) < 2:
        log.info(
            "not enough clusters to compare, skipping force scoring",
            extra={"cluster_id": str(cluster_id), "alternatives": len(alternatives)},
        )
        return report
    report.alternatives = len(alternatives)

    async with session_scope() as session:
        forces = list(
            (
                await session.execute(
                    select(DrivingForce).where(DrivingForce.active.is_(True)).order_by(DrivingForce.name)
                )
            ).scalars()
        )

    judge = PairwiseJudge(client)
    summaries = await _cluster_summaries(cluster_id)

    for force in forces:
        wins: dict[tuple[int, int], bool] = {}
        for i, j in combinations(range(len(alternatives)), 2):
            verdict = await judge.judge(
                force_id=force.id,
                force_name=force.name,
                force_definition=force.definition,
                left=alternatives[i],
                right=alternatives[j],
            )
            if verdict is not None:
                wins[(i, j)] = verdict

        scores, ratio = impact_scores(wins, len(alternatives))
        ranks = ranks_from(scores)
        uncertainty = await _uncertainty(judge, force, alternatives[0][1], summaries)

        result = ForceResult(
            force_id=force.id,
            force_name=force.name,
            impact=float(scores[0]),
            uncertainty=uncertainty,
            ahp_rank=ranks[0],
            consistency_ratio=ratio,
            comparisons=len(wins),
        )
        report.results.append(result)

        if not result.trustworthy:
            # Persisted anyway — an inconsistent judgement is still evidence, and
            # hiding it would leave the analyst wondering why a force is missing.
            log.warning(
                "pairwise answers contradict each other",
                extra={
                    "force": force.name,
                    "consistency_ratio": round(ratio, 4),
                    "limit": MAX_CONSISTENCY_RATIO,
                },
            )

        async with session_scope() as session:
            session.add(
                ForceAssessment(
                    cluster_id=cluster_id,
                    force_id=force.id,
                    impact=result.impact,
                    uncertainty=result.uncertainty,
                    ahp_rank=result.ahp_rank,
                )
            )

    report.llm_failures = judge.failures
    log.info("driving force scoring complete", extra=report.as_log())
    return report
