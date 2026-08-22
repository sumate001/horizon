"""initial horizon schema

Revision ID: 0001
Revises:
Create Date: 2026-08-22

Creates the full schema up front. Phase 1 only writes sources / raw_articles /
events; the analytics and integration tables exist so later phases ship code,
not column migrations.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "sources",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("credibility_weight", sa.Float(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "type IN ('rss', 'searxng', 'watchlist_hook')", name="ck_sources_type"
        ),
        sa.CheckConstraint(
            "credibility_weight >= 0 AND credibility_weight <= 1",
            name="ck_sources_credibility_range",
        ),
    )

    op.create_table(
        "raw_articles",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "source_id", UUID, sa.ForeignKey("sources.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("url", sa.Text(), nullable=False, unique=True),
        sa.Column("title", sa.Text()),
        sa.Column("body", sa.Text()),
        sa.Column("fetched_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("lang", sa.String(16)),
        sa.Column("status", sa.String(32), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued', 'processed', 'dropped_duplicate', "
            "'dropped_lowcred', 'failed')",
            name="ck_raw_articles_status",
        ),
    )
    op.create_index("ix_raw_articles_source_id", "raw_articles", ["source_id"])
    op.create_index("ix_raw_articles_fetched_at", "raw_articles", ["fetched_at"])
    op.create_index("ix_raw_articles_status", "raw_articles", ["status"])

    op.create_table(
        "clusters",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("label", sa.Text()),
        sa.Column("first_seen", TS),
        sa.Column("last_seen", TS),
        sa.Column("event_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("centroid_id", UUID),
        sa.CheckConstraint("status IN ('active', 'dormant')", name="ck_clusters_status"),
    )
    op.create_index("ix_clusters_last_seen", "clusters", ["last_seen"])
    op.create_index("ix_clusters_status", "clusters", ["status"])

    op.create_table(
        "events",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "raw_article_id",
            UUID,
            sa.ForeignKey("raw_articles.id", ondelete="SET NULL"),
        ),
        sa.Column("actors", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("action", sa.Text()),
        sa.Column("location", sa.Text()),
        sa.Column("event_time", TS),
        sa.Column(
            "categories",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("summary", sa.Text()),
        sa.Column("extraction_confidence", sa.Float()),
        sa.Column("incomplete", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("source_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("credibility_weight", sa.Float(), nullable=False),
        sa.Column("embedding_id", UUID),
        sa.Column("cluster_id", UUID, sa.ForeignKey("clusters.id", ondelete="SET NULL")),
        sa.Column("event_updates", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("minhash_sig", sa.LargeBinary()),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_events_raw_article_id", "events", ["raw_article_id"])
    op.create_index("ix_events_cluster_id", "events", ["cluster_id"])
    op.create_index("ix_events_created_at", "events", ["created_at"])
    op.create_index("ix_events_event_time", "events", ["event_time"])
    op.create_index("ix_events_incomplete", "events", ["incomplete"])

    op.create_table(
        "scores",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "cluster_id",
            UUID,
            sa.ForeignKey("clusters.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("window_start", TS, nullable=False),
        sa.Column("window_end", TS, nullable=False),
        sa.Column("frequency", sa.Float(), nullable=False, server_default="0"),
        sa.Column("velocity", sa.Float(), nullable=False, server_default="0"),
        sa.Column("acceleration", sa.Float(), nullable=False, server_default="0"),
        sa.Column("z_frequency", sa.Float()),
        sa.Column("z_velocity", sa.Float()),
        sa.Column("z_acceleration", sa.Float()),
        sa.Column("trend_score", sa.Float()),
        sa.Column("computed_at", TS, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_scores_cluster_id", "scores", ["cluster_id"])
    op.create_index("ix_scores_window_start", "scores", ["window_start"])
    op.create_index(
        "ix_scores_cluster_window", "scores", ["cluster_id", "window_start"], unique=True
    )

    op.create_table(
        "weak_signals",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("event_id", UUID, sa.ForeignKey("events.id", ondelete="CASCADE")),
        sa.Column("cluster_id", UUID, sa.ForeignKey("clusters.id", ondelete="CASCADE")),
        sa.Column("novelty_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("isolation_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("burst_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("combined_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(32), nullable=False, server_default="candidate"),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('candidate', 'dispatched', 'verified_true', "
            "'verified_false', 'expired')",
            name="ck_weak_signals_status",
        ),
    )
    op.create_index("ix_weak_signals_event_id", "weak_signals", ["event_id"])
    op.create_index("ix_weak_signals_cluster_id", "weak_signals", ["cluster_id"])
    op.create_index("ix_weak_signals_created_at", "weak_signals", ["created_at"])
    op.create_index("ix_weak_signals_status", "weak_signals", ["status"])

    op.create_table(
        "driving_forces",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("name", sa.Text(), nullable=False, unique=True),
        sa.Column("dimension", sa.String(4), nullable=False),
        sa.Column("definition", sa.Text(), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.CheckConstraint(
            "dimension IN ('P', 'E', 'S', 'T', 'E2', 'L')",
            name="ck_driving_forces_dimension",
        ),
    )

    op.create_table(
        "force_assessments",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "cluster_id",
            UUID,
            sa.ForeignKey("clusters.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "force_id",
            UUID,
            sa.ForeignKey("driving_forces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("impact", sa.Float(), nullable=False),
        sa.Column("uncertainty", sa.Float(), nullable=False),
        sa.Column("ahp_rank", sa.Integer()),
        sa.Column("assessed_at", TS, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_force_assessments_cluster_id", "force_assessments", ["cluster_id"])
    op.create_index("ix_force_assessments_force_id", "force_assessments", ["force_id"])
    op.create_index("ix_force_assessments_assessed_at", "force_assessments", ["assessed_at"])

    op.create_table(
        "scenarios",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "cluster_id",
            UUID,
            sa.ForeignKey("clusters.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("best_case", sa.Text()),
        sa.Column("worst_case", sa.Text()),
        sa.Column("likely_case", sa.Text()),
        sa.Column("indicators", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column(
            "source_event_ids",
            postgresql.ARRAY(UUID),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("model", sa.Text()),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_scenarios_cluster_id", "scenarios", ["cluster_id"])
    op.create_index("ix_scenarios_created_at", "scenarios", ["created_at"])

    op.create_table(
        "dispatches",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("signal_type", sa.String(32), nullable=False),
        sa.Column("ref_id", UUID, nullable=False),
        sa.Column("channels", postgresql.ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("osint_desk_status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("osint_desk_signal_id", sa.Text()),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "signal_type IN ('weak_signal', 'trend_breakout')",
            name="ck_dispatches_signal_type",
        ),
        sa.CheckConstraint(
            "osint_desk_status IN ('pending', 'delivered', 'failed', 'disabled')",
            name="ck_dispatches_osint_status",
        ),
    )
    op.create_index("ix_dispatches_ref_id", "dispatches", ["ref_id"])
    op.create_index("ix_dispatches_created_at", "dispatches", ["created_at"])
    op.create_index("ix_dispatches_osint_desk_status", "dispatches", ["osint_desk_status"])

    op.create_table(
        "verdicts",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "dispatch_id",
            UUID,
            sa.ForeignKey("dispatches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("verdict", sa.String(32), nullable=False),
        sa.Column("analyst_note", sa.Text()),
        sa.Column("received_at", TS, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "verdict IN ('true_signal', 'false_signal', 'inconclusive')",
            name="ck_verdicts_verdict",
        ),
    )
    op.create_index("ix_verdicts_dispatch_id", "verdicts", ["dispatch_id"])
    op.create_index("ix_verdicts_received_at", "verdicts", ["received_at"])


def downgrade() -> None:
    for table in (
        "verdicts",
        "dispatches",
        "scenarios",
        "force_assessments",
        "driving_forces",
        "weak_signals",
        "scores",
        "events",
        "clusters",
        "raw_articles",
        "sources",
    ):
        op.drop_table(table)
