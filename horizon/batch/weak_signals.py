"""Step 6 — weak signal detection (batch job C, runs after clustering).

Candidates are the things clustering could not place: events that came out as
noise, and clusters small enough to still be forming (event_count ≤ 5). For each
one:

    novelty     1 − max cosine similarity to any cluster centroid
    isolation   Isolation Forest over
                [reports_per_day, distinct_sources, mean_credibility, category_rarity]
    burst       Kleinberg two-state automaton over daily counts

    combined = (0.4·novelty + 0.3·isolation + 0.3·burst) × mean_credibility

Multiplying by credibility rather than adding it is deliberate: an anomaly seen
only in low-trust outlets should be damped, not merely scored slightly lower.

Candidates at or above WEAK_SIGNAL_T become `weak_signals` rows and are
published to `horizon:signals`.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
from sklearn.ensemble import IsolationForest
from sqlalchemy import select

from ..config import get_settings
from ..db import session_scope
from ..models import Cluster, Event, WeakSignal
from ..pipeline.vectors import VectorStore, get_vector_store, utcnow
from .burst import burst_score
from .signals import publish

log = logging.getLogger("horizon.batch.weak_signals")

SMALL_CLUSTER_MAX = 5
CANDIDATE_WINDOW_DAYS = 14
#: Isolation Forest needs a population to call anything an outlier.
MIN_ISOLATION_SAMPLES = 8


@dataclass
class Candidate:
    event_id: uuid.UUID | None
    cluster_id: uuid.UUID | None
    title: str
    vector: list[float]
    categories: list[str]
    daily_counts: list[int]
    reports_per_day: float
    distinct_sources: int
    mean_credibility: float

    novelty_score: float = 0.0
    isolation_score: float = 0.0
    burst_score: float = 0.0
    category_rarity: float = 0.0

    @property
    def combined_score(self) -> float:
        settings = get_settings()
        weighted = (
            settings.weak_novelty_w * self.novelty_score
            + settings.weak_isolation_w * self.isolation_score
            + settings.weak_burst_w * self.burst_score
        )
        return weighted * self.mean_credibility


@dataclass
class WeakSignalReport:
    candidates: int = 0
    detected: int = 0
    published: int = 0
    isolation_skipped: bool = False

    def as_log(self) -> dict:
        return dict(self.__dict__)


# ── scoring helpers ──────────────────────────────────────────────────────────


def category_rarity(categories: list[str], corpus_counts: dict[str, int], total: int) -> float:
    """Rarity of the candidate's least common category, in [0, 1].

    The rarest label carries the signal: a story tagged both "การเมือง" and
    "พลังงาน" is interesting because of the energy angle, not the politics.
    """
    if not categories or total <= 0:
        return 1.0
    shares = [corpus_counts.get(c, 0) / total for c in categories]
    return float(1.0 - min(shares))


def isolation_scores(features: np.ndarray, *, random_state: int = 0) -> np.ndarray:
    """Isolation Forest anomaly scores mapped to [0, 1], higher = more isolated.

    sklearn's `score_samples` returns the negated anomaly score of the original
    paper, so negating it back lands directly in the paper's [0, 1] range where
    values above 0.5 are considered anomalous.
    """
    forest = IsolationForest(
        n_estimators=100, contamination="auto", random_state=random_state
    ).fit(features)
    return np.clip(-forest.score_samples(features), 0.0, 1.0)


def daily_counts_from(timestamps: list[datetime], since: datetime, until: datetime) -> list[int]:
    """One bucket per day across the window, so bursts have a baseline to beat."""
    days = max(1, (until.date() - since.date()).days + 1)
    counts = [0] * days
    for moment in timestamps:
        index = (moment.date() - since.date()).days
        if 0 <= index < days:
            counts[index] += 1
    return counts


# ── candidate collection ─────────────────────────────────────────────────────


async def _corpus_category_counts(since: datetime) -> tuple[dict[str, int], int]:
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Event.categories).where(
                    Event.incomplete.is_(False), Event.created_at >= since
                )
            )
        ).scalars()
        counts: dict[str, int] = {}
        total = 0
        for categories in rows:
            total += 1
            for category in categories or []:
                counts[category] = counts.get(category, 0) + 1
    return counts, total


async def _noise_candidates(
    since: datetime, until: datetime, vectors: dict[uuid.UUID, list[float]]
) -> list[Candidate]:
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(
                    Event.id,
                    Event.summary,
                    Event.categories,
                    Event.created_at,
                    Event.source_count,
                    Event.credibility_weight,
                ).where(
                    Event.cluster_id.is_(None),
                    Event.incomplete.is_(False),
                    Event.created_at >= since,
                )
            )
        ).all()

    candidates = []
    for row in rows:
        vector = vectors.get(row.id)
        if vector is None:
            continue
        candidates.append(
            Candidate(
                event_id=row.id,
                cluster_id=None,
                title=row.summary or "",
                vector=vector,
                categories=list(row.categories or []),
                daily_counts=daily_counts_from([row.created_at], since, until),
                # A lone event is one report on one day, by definition.
                reports_per_day=float(row.source_count),
                distinct_sources=int(row.source_count),
                mean_credibility=float(row.credibility_weight),
            )
        )
    return candidates


async def _small_cluster_candidates(
    since: datetime, until: datetime, vectors: dict[uuid.UUID, list[float]]
) -> list[Candidate]:
    """Clusters small enough to still be forming, scored as a whole.

    `vectors` is the already-fetched event→embedding map; the centroid is
    recomputed from members rather than read back, so a cluster whose membership
    changed since the last clustering run is still scored against what it is now.
    """
    async with session_scope() as session:
        clusters = (
            await session.execute(
                select(Cluster.id, Cluster.label).where(
                    Cluster.status == "active", Cluster.event_count <= SMALL_CLUSTER_MAX
                )
            )
        ).all()
        if not clusters:
            return []

        members_by_cluster: dict[uuid.UUID, list] = {}
        rows = (
            await session.execute(
                select(
                    Event.id,
                    Event.cluster_id,
                    Event.created_at,
                    Event.source_count,
                    Event.credibility_weight,
                    Event.categories,
                ).where(Event.cluster_id.in_([c.id for c in clusters]))
            )
        ).all()
        for row in rows:
            members_by_cluster.setdefault(row.cluster_id, []).append(row)

    out: list[Candidate] = []
    for cluster_id, label in clusters:
        members = members_by_cluster.get(cluster_id, [])
        if not members:
            continue

        span = max(m.created_at for m in members) - min(m.created_at for m in members)
        span_days = max(1.0, span.days + 1)
        member_vectors = [vectors[m.id] for m in members if m.id in vectors]

        centroid: list[float] = []
        if member_vectors:
            mean = np.mean(np.asarray(member_vectors, dtype=np.float64), axis=0)
            centroid = (mean / max(float(np.linalg.norm(mean)), 1e-12)).tolist()

        out.append(
            Candidate(
                event_id=None,
                cluster_id=cluster_id,
                title=label or "",
                vector=centroid,
                categories=sorted({c for m in members for c in (m.categories or [])}),
                daily_counts=daily_counts_from([m.created_at for m in members], since, until),
                reports_per_day=sum(m.source_count for m in members) / span_days,
                distinct_sources=sum(m.source_count for m in members),
                mean_credibility=float(np.mean([m.credibility_weight for m in members])),
            )
        )
    return out


async def _novelty(candidate: Candidate, store: VectorStore) -> float:
    """1 − similarity to the nearest cluster centroid.

    A cluster candidate always matches its own centroid at 1.0, so its novelty
    comes from the *second* nearest — how far it sits from the rest of the map.
    """
    if not candidate.vector:
        return 0.0
    hits = await store.search_centroids(candidate.vector, limit=3)
    if candidate.cluster_id is not None:
        hits = [h for h in hits if h.event_id != candidate.cluster_id]
    if not hits:
        return 1.0
    return float(1.0 - max(0.0, min(1.0, hits[0].score)))


# ── entry point ──────────────────────────────────────────────────────────────


async def run_weak_signal_detection(vectors: VectorStore | None = None) -> WeakSignalReport:
    settings = get_settings()
    store = vectors or get_vector_store()
    report = WeakSignalReport()

    until = utcnow()
    since = until - timedelta(days=CANDIDATE_WINDOW_DAYS)

    # One scroll serves both candidate kinds — noise events use their own vector,
    # small clusters average their members' into a centroid.
    vectors = dict(await store.scroll_events(since=since))

    candidates = await _noise_candidates(since, until, vectors)
    candidates.extend(await _small_cluster_candidates(since, until, vectors))

    report.candidates = len(candidates)
    if not candidates:
        log.info("no weak signal candidates", extra=report.as_log())
        return report

    corpus_counts, corpus_total = await _corpus_category_counts(since)
    for candidate in candidates:
        candidate.novelty_score = await _novelty(candidate, store)
        candidate.burst_score = burst_score(candidate.daily_counts)
        candidate.category_rarity = category_rarity(
            candidate.categories, corpus_counts, corpus_total
        )

    if len(candidates) >= MIN_ISOLATION_SAMPLES:
        features = np.asarray(
            [
                [c.reports_per_day, c.distinct_sources, c.mean_credibility, c.category_rarity]
                for c in candidates
            ],
            dtype=np.float64,
        )
        for candidate, score in zip(candidates, isolation_scores(features), strict=True):
            candidate.isolation_score = float(score)
    else:
        # Below this the forest would just be describing noise; say so rather
        # than emitting a confident-looking zero.
        report.isolation_skipped = True
        log.info(
            "too few candidates for Isolation Forest, isolation_score left at 0",
            extra={"candidates": len(candidates), "minimum": MIN_ISOLATION_SAMPLES},
        )

    detected = [c for c in candidates if c.combined_score >= settings.weak_signal_t]
    report.detected = len(detected)

    if not detected:
        # Silence here is ambiguous — "nothing anomalous" and "the threshold is
        # unreachable with this much history" look identical from outside. Report
        # how close the field got, and which component held it back.
        best = max(candidates, key=lambda c: c.combined_score)
        log.info(
            "no candidate reached the threshold",
            extra={
                "threshold": settings.weak_signal_t,
                "best_combined": round(best.combined_score, 4),
                "best_novelty": round(best.novelty_score, 4),
                "best_isolation": round(best.isolation_score, 4),
                "max_burst": round(max(c.burst_score for c in candidates), 4),
                # Kleinberg needs several days of counts; before that burst is
                # structurally 0 and a third of the score is simply unavailable.
                "burst_has_history": max(len(c.daily_counts) for c in candidates) >= 3,
            },
        )

    for candidate in detected:
        signal_id = uuid.uuid4()
        async with session_scope() as session:
            session.add(
                WeakSignal(
                    id=signal_id,
                    event_id=candidate.event_id,
                    cluster_id=candidate.cluster_id,
                    novelty_score=candidate.novelty_score,
                    isolation_score=candidate.isolation_score,
                    burst_score=candidate.burst_score,
                    combined_score=candidate.combined_score,
                    status="candidate",
                )
            )
        if await publish(
            "weak_signal",
            signal_id,
            title=candidate.title,
            combined_score=round(candidate.combined_score, 4),
            categories=candidate.categories,
            cluster_id=str(candidate.cluster_id) if candidate.cluster_id else None,
            event_id=str(candidate.event_id) if candidate.event_id else None,
        ):
            report.published += 1

    log.info("weak signal detection complete", extra=report.as_log())
    return report
