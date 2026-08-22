"""Step 5 — event clustering (batch job A).

HDBSCAN over a rolling 30-day window of complete events, using a precomputed
distance that adds a temporal penalty to cosine distance:

    d(i, j) = cosine_distance(i, j) + TEMPORAL_WEIGHT × |days_i − days_j|

Cosine distance on unit vectors is bounded by 2, so at the default weight of
0.15 any pair more than 13.3 days apart is already farther than the most
dissimilar possible text — which is exactly the spec's "events further apart
than 14 days should rarely co-cluster", without a second tunable.

A precomputed matrix costs n² memory, so the window is capped at
MAX_CLUSTER_EVENTS and truncation is logged rather than silently applied.

Cluster ids are stable across runs: each run's centroids are matched against the
previous run's by cosine ≥ CLUSTER_MATCH_T, so a cluster that keeps growing
keeps its id (and therefore its score history).
"""

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np
from sklearn.cluster import HDBSCAN
from sqlalchemy import exists, func, select, true, update

from ..config import get_settings
from ..db import session_scope
from ..models import Cluster, Event
from ..pipeline.vectors import VectorStore, get_vector_store, utcnow

log = logging.getLogger("horizon.batch.clustering")

NOISE = -1
LABEL_MAX_CHARS = 90


@dataclass
class ClusteringReport:
    events: int = 0
    clustered: int = 0
    noise: int = 0
    clusters: int = 0
    reused_ids: int = 0
    new_ids: int = 0
    dormant: int = 0
    truncated: int = 0

    def as_log(self) -> dict:
        return dict(self.__dict__)


@dataclass
class _EventRow:
    id: uuid.UUID
    summary: str
    created_at: datetime
    event_time: datetime | None
    categories: list[str]

    @property
    def occurred_at(self) -> datetime:
        """When the event happened, falling back to when we saw it.

        event_time is null for roughly one story in seven — an article that never
        states when something happened still belongs on the timeline somewhere,
        and ingestion time is the least-wrong stand-in.
        """
        return self.event_time or self.created_at


@dataclass
class _NewCluster:
    members: list[int]
    centroid: np.ndarray
    label: str
    categories: list[str] = field(default_factory=list)


# ── distance ─────────────────────────────────────────────────────────────────


def normalize_rows(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return matrix / np.maximum(norms, 1e-12)


def build_distance_matrix(
    embeddings: np.ndarray, days: np.ndarray, temporal_weight: float
) -> np.ndarray:
    """Cosine distance plus a linear temporal penalty, as a dense n×n matrix."""
    unit = normalize_rows(embeddings)
    # Clip guards against 1 + 1e-16 from floating point, which would go negative.
    cosine = 1.0 - np.clip(unit @ unit.T, -1.0, 1.0)
    temporal = np.abs(days[:, None] - days[None, :]) * temporal_weight
    distance = cosine + temporal
    np.fill_diagonal(distance, 0.0)
    return distance


def _label_for(rows: Sequence[_EventRow], members: Sequence[int], centroid_rank: int) -> str:
    """Use the medoid event's summary — a real sentence beats a bag of keywords.

    The reasoner replaces this with an LLM-written label in phase 3.
    """
    summary = rows[members[centroid_rank]].summary or ""
    summary = summary.strip()
    if len(summary) <= LABEL_MAX_CHARS:
        return summary
    return summary[:LABEL_MAX_CHARS].rsplit(" ", 1)[0] + "…"


def _top_categories(rows: Sequence[_EventRow], members: Sequence[int], top: int = 3) -> list[str]:
    counts: dict[str, int] = {}
    for i in members:
        for category in rows[i].categories:
            counts[category] = counts.get(category, 0) + 1
    return [c for c, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:top]]


def assemble_clusters(
    labels: np.ndarray, rows: Sequence[_EventRow], embeddings: np.ndarray
) -> list[_NewCluster]:
    unit = normalize_rows(embeddings)
    clusters: list[_NewCluster] = []
    for label in sorted(set(labels.tolist()) - {NOISE}):
        members = np.flatnonzero(labels == label).tolist()
        centroid = unit[members].mean(axis=0)
        centroid /= max(float(np.linalg.norm(centroid)), 1e-12)
        # Medoid = member closest to the centroid; its summary names the cluster.
        centroid_rank = int(np.argmax(unit[members] @ centroid))
        clusters.append(
            _NewCluster(
                members=members,
                centroid=centroid,
                label=_label_for(rows, members, centroid_rank),
                categories=_top_categories(rows, members),
            )
        )
    return clusters


# ── persistence ──────────────────────────────────────────────────────────────


async def _load_events(cutoff: datetime, limit: int) -> tuple[list[_EventRow], int]:
    """Complete events in the window, newest first.

    Returns the capped rows plus how many were available, so the caller can log
    what the cap dropped instead of silently clustering a subset.
    """
    async with session_scope() as session:
        available = (
            await session.scalar(
                select(func.count())
                .select_from(Event)
                .where(Event.incomplete.is_(False), Event.created_at >= cutoff)
            )
            or 0
        )
        result = await session.execute(
            select(Event.id, Event.summary, Event.created_at, Event.event_time, Event.categories)
            .where(Event.incomplete.is_(False), Event.created_at >= cutoff)
            .order_by(Event.created_at.desc())
            .limit(limit)
        )
        rows = [
            _EventRow(
                id=row.id,
                summary=row.summary or "",
                created_at=row.created_at,
                event_time=row.event_time,
                categories=list(row.categories or []),
            )
            for row in result
        ]
    return rows, available


async def _match_existing_ids(
    clusters: list[_NewCluster], vectors: VectorStore, threshold: float
) -> list[uuid.UUID | None]:
    """Greedy one-to-one match of this run's centroids to the previous run's.

    Best match wins; a previous cluster can only be claimed once, otherwise two
    split clusters would both inherit the same id and its score history.
    """
    scored: list[tuple[float, int, uuid.UUID]] = []
    for index, cluster in enumerate(clusters):
        for hit in await vectors.search_centroids(cluster.centroid.tolist(), limit=3):
            if hit.score >= threshold:
                scored.append((hit.score, index, hit.event_id))

    assigned: list[uuid.UUID | None] = [None] * len(clusters)
    claimed: set[uuid.UUID] = set()
    for _, index, cluster_id in sorted(scored, key=lambda s: -s[0]):
        if assigned[index] is None and cluster_id not in claimed:
            assigned[index] = cluster_id
            claimed.add(cluster_id)
    return assigned


async def _persist(
    clusters: list[_NewCluster],
    cluster_ids: list[uuid.UUID],
    rows: Sequence[_EventRow],
    noise_ids: list[uuid.UUID],
) -> int:
    """Write cluster rows and event assignments. Returns the dormant count."""
    settings = get_settings()
    now = utcnow()
    live_ids = set(cluster_ids)

    async with session_scope() as session:
        for cluster, cluster_id in zip(clusters, cluster_ids, strict=True):
            members = [rows[i] for i in cluster.members]
            # Observation time, not event time: `first_seen` drives the provisional
            # gate, which asks how long we have been watching this cluster. A
            # member that reports a 2023 event must not make a cluster born today
            # look like it has years of score history.
            seen = [m.created_at for m in members]
            latest = max(m.occurred_at for m in members)
            existing = await session.get(Cluster, cluster_id)
            if existing is None:
                session.add(
                    Cluster(
                        id=cluster_id,
                        label=cluster.label,
                        first_seen=min(seen),
                        last_seen=latest,
                        event_count=len(members),
                        status="active",
                        centroid_id=cluster_id,
                    )
                )
            else:
                # first_seen is the cluster's own history, not this window's.
                existing.first_seen = min(existing.first_seen or min(seen), min(seen))
                existing.last_seen = max(existing.last_seen or latest, latest)
                existing.event_count = len(members)
                existing.label = cluster.label
                existing.status = "active"
                existing.centroid_id = cluster_id

            await session.execute(
                update(Event)
                .where(Event.id.in_([m.id for m in members]))
                .values(cluster_id=cluster_id)
            )

        # Noise points are the input to weak signal detection — they must not
        # keep a cluster_id from a previous run.
        for chunk_start in range(0, len(noise_ids), 500):
            await session.execute(
                update(Event)
                .where(Event.id.in_(noise_ids[chunk_start : chunk_start + 500]))
                .values(cluster_id=None)
            )

        # Re-clustering can dissolve a cluster entirely: its events get reassigned
        # and it is simply absent from this run. Without this it would keep its
        # last event_count forever and go on looking active with zero members.
        emptied = await session.execute(
            update(Cluster)
            .where(
                Cluster.status == "active",
                Cluster.id.notin_(live_ids) if live_ids else true(),
                ~exists().where(Event.cluster_id == Cluster.id),
            )
            .values(status="dormant", event_count=0)
        )

        stale_before = now - timedelta(days=settings.cluster_dormant_days)
        conditions = [Cluster.status == "active", Cluster.last_seen < stale_before]
        if live_ids:
            conditions.append(Cluster.id.notin_(live_ids))
        stale = await session.execute(
            update(Cluster).where(*conditions).values(status="dormant")
        )
    return (emptied.rowcount or 0) + (stale.rowcount or 0)


# ── entry point ──────────────────────────────────────────────────────────────


async def run_clustering(vectors: VectorStore | None = None) -> ClusteringReport:
    settings = get_settings()
    store = vectors or get_vector_store()
    report = ClusteringReport()

    cutoff = utcnow() - timedelta(days=settings.cluster_window_days)
    rows, available = await _load_events(cutoff, settings.max_cluster_events)
    report.truncated = max(0, available - len(rows))
    if report.truncated:
        log.warning(
            "clustering window truncated — oldest events excluded",
            extra={"available": available, "used": len(rows), "cap": settings.max_cluster_events},
        )

    if len(rows) < settings.min_cluster_size:
        log.info("too few events to cluster", extra={"events": len(rows)})
        report.events = len(rows)
        return report

    stored = dict(await store.scroll_events(since=cutoff, limit=settings.max_cluster_events))
    usable = [row for row in rows if row.id in stored]
    missing = len(rows) - len(usable)
    if missing:
        log.warning("events without a stored vector, skipped", extra={"count": missing})
    if len(usable) < settings.min_cluster_size:
        report.events = len(usable)
        return report

    embeddings = np.asarray([stored[row.id] for row in usable], dtype=np.float64)
    epoch_days = np.asarray(
        [row.occurred_at.timestamp() / 86400.0 for row in usable], dtype=np.float64
    )
    # Relative days keep the numbers small; only differences affect the metric.
    days = epoch_days - epoch_days.min()

    distance = build_distance_matrix(embeddings, days, settings.temporal_weight)
    labels = HDBSCAN(
        min_cluster_size=settings.min_cluster_size,
        metric="precomputed",
        cluster_selection_method=settings.cluster_selection_method,
    ).fit_predict(distance)

    clusters = assemble_clusters(labels, usable, embeddings)
    matched = await _match_existing_ids(clusters, store, settings.cluster_match_t)
    cluster_ids = [existing or uuid.uuid4() for existing in matched]

    noise_ids = [usable[i].id for i in np.flatnonzero(labels == NOISE).tolist()]
    report.dormant = await _persist(clusters, cluster_ids, usable, noise_ids)

    # Rebuild the centroid collection so the next run matches against this one.
    await store.clear_centroids()
    for cluster, cluster_id in zip(clusters, cluster_ids, strict=True):
        await store.upsert_centroid(cluster_id, cluster.centroid.tolist(), cluster_id=cluster_id)

    report.events = len(usable)
    report.clusters = len(clusters)
    report.clustered = len(usable) - len(noise_ids)
    report.noise = len(noise_ids)
    report.reused_ids = sum(1 for m in matched if m is not None)
    report.new_ids = report.clusters - report.reused_ids

    log.info("clustering complete", extra=report.as_log())
    return report
