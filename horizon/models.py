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
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY
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
#: `beat_match` is not a third detector. weak_signal and trend_breakout mean
#: "the engine noticed something"; beat_match means "the newsroom asked for this
#: subject and an event matched it". Without it, a beat defined in OSINT//DESK
#: could only sort what the detectors happened to send — 2.5% of what Horizon
#: knows — so an editor could follow "อิสราเอลในประเทศไทย" while 90 matching
#: events sat here and none travelled. They are graded differently at verdict
#: time too: a detector misfiring and a beat the newsroom wrote itself are not
#: the same kind of wrong.
SIGNAL_TYPES = ("weak_signal", "trend_breakout", "beat_match")
DISPATCH_STATUSES = ("pending", "delivered", "failed", "disabled")
#: `off_topic` is not a fourth grade of wrongness. It says the detection was
#: right and the story simply is not on this newsroom's beat, which is feedback
#: about relevance rather than accuracy. Anything tuning detection thresholds
#: must exclude it: OSINT//DESK used to report every dismissal as false_signal,
#: so an editor clearing off-beat stories would have trained the radar to
#: suppress the detections that were working.
VERDICTS = ("true_signal", "false_signal", "inconclusive", "off_topic")
#: The subset that says anything about whether detection was correct.
ACCURACY_VERDICTS = ("true_signal", "false_signal", "inconclusive")
ENTITY_TYPES = ("person", "org", "place", "team", "generic", "unknown")
#: auto — the pipeline decided and was confident. needs_review — it was not, and
#: nobody has looked yet. confirmed/rejected — a human has.
ENTITY_REVIEW_STATUSES = ("auto", "needs_review", "confirmed", "rejected")
#: pending — not looked up yet. linked — has a Q-number. no_match — Wikidata was
#: asked and had nothing, which is the normal answer for two thirds of the long
#: tail. unavailable — the API could not be reached, so it is worth retrying.
#: `duplicate` means the Q-number the lookup found is already held by another
#: entity. `ix_entities_qid` is unique on purpose — one Wikidata item is one
#: entity, which is what lets a Q-number merge spelling variants — so a
#: collision is the lookup discovering two rows are the same subject, not a
#: failure. It needs a human because merging entities is not reversible.
QID_STATUSES = ("pending", "linked", "no_match", "unavailable", "duplicate")


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


class Beat(Base):
    """What OSINT//DESK is watching for, mirrored here so matching can run.

    A cache, not a source of truth: the editorial list lives in OSINT//DESK and
    is pulled from it. Keeping a copy is what lets Horizon keep matching while
    the other side is down, which is the property the loose coupling is for.
    """

    __tablename__ = "beats"
    __table_args__ = (Index("ix_beats_active", "active"),)

    #: OSINT//DESK's signal_profiles.id, reused so a match can name it back.
    id: Mapped[uuid.UUID] = _pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    #: The free text an editor wrote. This is what the model actually reads —
    #: a subject is always narrower than a category.
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    categories: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class BeatMatch(Base):
    """One event judged to be on one beat. The row a beat_match dispatch points at.

    Also the record of what has already been sent: without it the batch would
    re-match and re-send the same event on every run.
    """

    __tablename__ = "beat_matches"
    __table_args__ = (
        UniqueConstraint("beat_id", "event_id", name="uq_beat_matches_beat_event"),
        Index("ix_beat_matches_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = _pk()
    beat_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("beats.id", ondelete="CASCADE"), nullable=False
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    #: Why the model said yes, in Thai. Travels to OSINT//DESK and is shown to
    #: the editor, so a beat that is matching the wrong thing is visible.
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
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


class Entity(Base):
    """One thing in the world, however many ways the news spells it.

    `qid` is reserved for a Wikidata identifier and is deliberately nullable:
    measured against this corpus, Wikidata covers 75% of the names that repeat
    but only 33% of the long tail, and local figures — a village headman, a
    patrol officer — will never be in it. An entity is real whether or not an
    encyclopaedia agrees.
    """

    __tablename__ = "entities"
    __table_args__ = (
        _check("entity_type", ENTITY_TYPES, "ck_entities_type"),
        _check("review_status", ENTITY_REVIEW_STATUSES, "ck_entities_review_status"),
        Index("ix_entities_review_status", "review_status"),
        Index("ix_entities_type", "entity_type"),
        _check("qid_status", QID_STATUSES, "ck_entities_qid_status"),
        Index("ix_entities_qid", "qid", unique=True, postgresql_where=text("qid IS NOT NULL")),
        Index("ix_entities_qid_status", "qid_status"),
        Index("ix_entities_aliases", "aliases", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = _pk()
    canonical_name: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    #: Every normalised surface form seen for this entity. The lookup key on
    #: ingest, which is why it carries a GIN index. Typed with the PostgreSQL
    #: ARRAY rather than the generic one so `.overlap()` (the `&&` operator) is
    #: available — the generic type has no such comparator and the lookup would
    #: fail at runtime, quietly creating a new entity for every mention.
    aliases: Mapped[list[str]] = mapped_column(PG_ARRAY(Text), nullable=False, default=list)
    qid: Mapped[str | None] = mapped_column(String(32))
    #: Distinguishes "no Wikidata item exists" from "we have not asked yet".
    #: Without it every ingest would re-query the same unknown village headman
    #: forever, which is both slow and rude to a free API.
    qid_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    qid_confidence: Mapped[float | None] = mapped_column(Float)
    #: Why the linker chose this item, or why it refused to. Shown to reviewers.
    qid_reason: Mapped[str | None] = mapped_column(Text)
    qid_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    review_status: Mapped[str] = mapped_column(String(16), nullable=False, default="auto")
    decided_by: Mapped[str] = mapped_column(String(16), nullable=False, default="rules")
    #: Why this was queued, in Thai, shown to the reviewer. NULL means it was
    #: not queued — the queue exists to be acted on, not to be a list of scores.
    risk: Mapped[str | None] = mapped_column(Text)
    #: Kept for the review queue: what the analyst is being asked to judge.
    mention_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reviewed_by: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EventEntity(Base):
    """Which entities an event is about, and how each was written there.

    `surface_form` is kept rather than normalised away: when a merge turns out to
    be wrong, this is the only record of what the article actually said.
    """

    __tablename__ = "event_entities"
    __table_args__ = (
        Index("ix_event_entities_event", "event_id"),
        Index("ix_event_entities_entity", "entity_id"),
        Index("ix_event_entities_unique", "event_id", "entity_id", "surface_form", unique=True),
    )

    id: Mapped[uuid.UUID] = _pk()
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entities.id", ondelete="CASCADE"), nullable=False
    )
    surface_form: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
