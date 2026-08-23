"""SQLAlchemy 2.0 models — the full Horizon schema.

Phase 1 only writes `sources`, `raw_articles` and `events`, but the whole schema
ships in the first migration so later phases add code, not columns.

Enum-like columns are TEXT + CHECK rather than native PG enums: adding a value
later is an ALTER on the constraint, not a type migration.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SOURCE_TYPES = ("rss", "searxng", "watchlist_hook")
ARTICLE_STATUSES = ("queued", "processed", "dropped_duplicate", "dropped_lowcred", "failed")
CLUSTER_STATUSES = ("active", "dormant")
WEAK_SIGNAL_STATUSES = (
    "candidate",
    "dispatched",
    "verified_true",
    "verified_false",
    "expired",
)
PESTEL_DIMENSIONS = ("P", "E", "S", "T", "E2", "L")
SIGNAL_TYPES = ("weak_signal", "trend_breakout")
DISPATCH_STATUSES = ("pending", "delivered", "failed", "disabled")
VERDICTS = ("true_signal", "false_signal", "inconclusive")


def _check(column: str, allowed: tuple[str, ...], name: str) -> CheckConstraint:
    values = ", ".join(f"'{v}'" for v in allowed)
    return CheckConstraint(f"{column} IN ({values})", name=name)


class Base(DeclarativeBase):
    pass


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class Source(Base):
    __tablename__ = "sources"
    __table_args__ = (
        _check("type", SOURCE_TYPES, "ck_sources_type"),
        CheckConstraint(
            "credibility_weight >= 0 AND credibility_weight <= 1",
            name="ck_sources_credibility_range",
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False, default="rss")
    credibility_weight: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RawArticle(Base):
    __tablename__ = "raw_articles"
    __table_args__ = (
        _check("status", ARTICLE_STATUSES, "ck_raw_articles_status"),
        Index("ix_raw_articles_source_id", "source_id"),
        Index("ix_raw_articles_fetched_at", "fetched_at"),
        Index("ix_raw_articles_status", "status"),
    )

    id: Mapped[uuid.UUID] = _pk()
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sources.id", ondelete="SET NULL"), nullable=True
    )
    url: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    title: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    lang: Mapped[str | None] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        Index("ix_events_raw_article_id", "raw_article_id"),
        Index("ix_events_cluster_id", "cluster_id"),
        Index("ix_events_created_at", "created_at"),
        Index("ix_events_event_time", "event_time"),
        Index("ix_events_incomplete", "incomplete"),
        # The DESK feed page filters by verdict and orders by score.
        Index("ix_events_triage_verdict", "triage_verdict"),
        Index("ix_events_triage_total", "triage_total"),
    )

    id: Mapped[uuid.UUID] = _pk()
    raw_article_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("raw_articles.id", ondelete="SET NULL")
    )
    actors: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    action: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    event_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    categories: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list
    )
    summary: Mapped[str | None] = mapped_column(Text)
    extraction_confidence: Mapped[float | None] = mapped_column(Float)
    # Editorial triage, moved here from OSINT//DESK when Horizon took over
    # everything on the way in. 0–10 each; see pipeline/triage.py.
    score_relevance: Mapped[float | None] = mapped_column(Float)
    score_urgency: Mapped[float | None] = mapped_column(Float)
    score_impact: Mapped[float | None] = mapped_column(Float)
    score_novelty: Mapped[float | None] = mapped_column(Float)
    score_reliability: Mapped[float | None] = mapped_column(Float)
    score_sensitivity: Mapped[float | None] = mapped_column(Float)
    score_actionability: Mapped[float | None] = mapped_column(Float)
    triage_total: Mapped[float | None] = mapped_column(Float)
    triage_verdict: Mapped[str | None] = mapped_column(String(20))
    incomplete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    credibility_weight: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    embedding_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    cluster_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clusters.id", ondelete="SET NULL")
    )
    # Append-only audit trail of dedup "update" merges — see pipeline/dedup.py
    event_updates: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # 128-perm MinHash signature, kept so the L1 index can be rebuilt from Postgres
    minhash_sig: Mapped[bytes | None] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Cluster(Base):
    __tablename__ = "clusters"
    __table_args__ = (
        _check("status", CLUSTER_STATUSES, "ck_clusters_status"),
        Index("ix_clusters_last_seen", "last_seen"),
        Index("ix_clusters_status", "status"),
    )

    id: Mapped[uuid.UUID] = _pk()
    label: Mapped[str | None] = mapped_column(Text)
    first_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    event_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    centroid_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class Score(Base):
    __tablename__ = "scores"
    __table_args__ = (
        Index("ix_scores_cluster_id", "cluster_id"),
        Index("ix_scores_window_start", "window_start"),
        Index("ix_scores_cluster_window", "cluster_id", "window_start", unique=True),
    )

    id: Mapped[uuid.UUID] = _pk()
    cluster_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clusters.id", ondelete="CASCADE"), nullable=False
    )
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    frequency: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    velocity: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    acceleration: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    z_frequency: Mapped[float | None] = mapped_column(Float)
    z_velocity: Mapped[float | None] = mapped_column(Float)
    z_acceleration: Mapped[float | None] = mapped_column(Float)
    trend_score: Mapped[float | None] = mapped_column(Float)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class WeakSignal(Base):
    __tablename__ = "weak_signals"
    __table_args__ = (
        _check("status", WEAK_SIGNAL_STATUSES, "ck_weak_signals_status"),
        Index("ix_weak_signals_event_id", "event_id"),
        Index("ix_weak_signals_cluster_id", "cluster_id"),
        Index("ix_weak_signals_created_at", "created_at"),
        Index("ix_weak_signals_status", "status"),
    )

    id: Mapped[uuid.UUID] = _pk()
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.id", ondelete="CASCADE")
    )
    cluster_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clusters.id", ondelete="CASCADE")
    )
    novelty_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    isolation_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    burst_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    combined_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="candidate")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DrivingForce(Base):
    __tablename__ = "driving_forces"
    __table_args__ = (_check("dimension", PESTEL_DIMENSIONS, "ck_driving_forces_dimension"),)

    id: Mapped[uuid.UUID] = _pk()
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    dimension: Mapped[str] = mapped_column(String(4), nullable=False)
    definition: Mapped[str] = mapped_column(Text, nullable=False)
    weight: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class ForceAssessment(Base):
    __tablename__ = "force_assessments"
    __table_args__ = (
        Index("ix_force_assessments_cluster_id", "cluster_id"),
        Index("ix_force_assessments_force_id", "force_id"),
        Index("ix_force_assessments_assessed_at", "assessed_at"),
    )

    id: Mapped[uuid.UUID] = _pk()
    cluster_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clusters.id", ondelete="CASCADE"), nullable=False
    )
    force_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("driving_forces.id", ondelete="CASCADE"),
        nullable=False,
    )
    impact: Mapped[float] = mapped_column(Float, nullable=False)
    uncertainty: Mapped[float] = mapped_column(Float, nullable=False)
    ahp_rank: Mapped[int | None] = mapped_column(Integer)
    assessed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Scenario(Base):
    __tablename__ = "scenarios"
    __table_args__ = (
        Index("ix_scenarios_cluster_id", "cluster_id"),
        Index("ix_scenarios_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = _pk()
    cluster_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clusters.id", ondelete="CASCADE"), nullable=False
    )
    best_case: Mapped[str | None] = mapped_column(Text)
    worst_case: Mapped[str | None] = mapped_column(Text)
    likely_case: Mapped[str | None] = mapped_column(Text)
    indicators: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    source_event_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, default=list
    )
    model: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Dispatch(Base):
    __tablename__ = "dispatches"
    __table_args__ = (
        _check("signal_type", SIGNAL_TYPES, "ck_dispatches_signal_type"),
        _check("osint_desk_status", DISPATCH_STATUSES, "ck_dispatches_osint_status"),
        Index("ix_dispatches_ref_id", "ref_id"),
        Index("ix_dispatches_created_at", "created_at"),
        Index("ix_dispatches_osint_desk_status", "osint_desk_status"),
        Index("ix_dispatches_delivery_due", "osint_desk_status", "next_attempt_at"),
    )

    id: Mapped[uuid.UUID] = _pk()
    signal_type: Mapped[str] = mapped_column(String(32), nullable=False)
    ref_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    channels: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    osint_desk_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending"
    )
    osint_desk_signal_id: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # Retry state, persisted so the 30s→2m→10m→1h schedule survives a restart.
    delivery_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Verdict(Base):
    __tablename__ = "verdicts"
    __table_args__ = (
        _check("verdict", VERDICTS, "ck_verdicts_verdict"),
        Index("ix_verdicts_dispatch_id", "dispatch_id"),
        Index("ix_verdicts_received_at", "received_at"),
        # One verdict per dispatch: the corpus wants the analyst's current answer.
        Index("ix_verdicts_dispatch_unique", "dispatch_id", unique=True),
    )

    id: Mapped[uuid.UUID] = _pk()
    dispatch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dispatches.id", ondelete="CASCADE"), nullable=False
    )
    verdict: Mapped[str] = mapped_column(String(32), nullable=False)
    analyst_note: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
