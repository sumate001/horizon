"""beats: what the newsroom asked to follow, and what matched

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-20

A beat defined in OSINT//DESK could only ever filter what the detectors happened
to send. Horizon dispatches what it finds anomalous — 246 of 9,939 events, 2.5%
— so an editor could define "อิสราเอลในประเทศไทย", have 90 matching events sit
in this database, 21 of them from the last two days, and receive none of them.
The beat was not wrong and the detection was not wrong; nothing connected them.

`beats` mirrors the editorial list so matching can run here. It is a cache and
not a source of truth: OSINT//DESK owns the list and Horizon pulls it, which is
what lets matching continue while the other side is down.

`beat_matches` records each event judged to be on a beat, and is also what stops
the batch re-sending the same event every run.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "signal_type IN ('weak_signal', 'trend_breakout')"
_NEW = "signal_type IN ('weak_signal', 'trend_breakout', 'beat_match')"


def upgrade() -> None:
    op.create_table(
        "beats",
        # OSINT//DESK's signal_profiles.id, reused so a match can name it back.
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "categories", sa.ARRAY(sa.Text()), nullable=False, server_default="{}"
        ),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "synced_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_beats_active", "beats", ["active"])

    op.create_table(
        "beat_matches",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "beat_id",
            UUID(as_uuid=True),
            sa.ForeignKey("beats.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "event_id",
            UUID(as_uuid=True),
            sa.ForeignKey("events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # One event is on a beat once. Without this the batch would re-send the
        # same story on every run, which is the bug weak signals already had.
        sa.UniqueConstraint("beat_id", "event_id", name="uq_beat_matches_beat_event"),
    )
    op.create_index("ix_beat_matches_created_at", "beat_matches", ["created_at"])

    op.drop_constraint("ck_dispatches_signal_type", "dispatches", type_="check")
    op.create_check_constraint("ck_dispatches_signal_type", "dispatches", _NEW)


def downgrade() -> None:
    # Dispatches of a type the old vocabulary cannot express are removed rather
    # than relabelled as detections — a beat match is not a detector firing.
    op.execute("DELETE FROM dispatches WHERE signal_type = 'beat_match'")
    op.drop_constraint("ck_dispatches_signal_type", "dispatches", type_="check")
    op.create_check_constraint("ck_dispatches_signal_type", "dispatches", _OLD)
    op.drop_index("ix_beat_matches_created_at", table_name="beat_matches")
    op.drop_table("beat_matches")
    op.drop_index("ix_beats_active", table_name="beats")
    op.drop_table("beats")
