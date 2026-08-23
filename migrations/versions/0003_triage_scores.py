"""editorial triage scores on events

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-23

Horizon took over all inbound handling, so OSINT//DESK's per-item editorial
scoring moves here. The dimensions and thresholds are unchanged; `reliability`
is now taken from the source registry rather than guessed by the model, since
that number already exists and is analyst-maintained.

Existing rows keep NULL scores. They were extracted before triage moved, and
back-filling would mean inventing editorial judgement after the fact.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCORES = (
    "score_relevance",
    "score_urgency",
    "score_impact",
    "score_novelty",
    "score_reliability",
    "score_sensitivity",
    "score_actionability",
    "triage_total",
)


def upgrade() -> None:
    for column in SCORES:
        op.add_column("events", sa.Column(column, sa.Float(), nullable=True))
    op.add_column("events", sa.Column("triage_verdict", sa.String(20), nullable=True))

    # The DESK feed page filters by verdict and sorts by score.
    op.create_index("ix_events_triage_verdict", "events", ["triage_verdict"])
    op.create_index("ix_events_triage_total", "events", ["triage_total"])


def downgrade() -> None:
    op.drop_index("ix_events_triage_total", table_name="events")
    op.drop_index("ix_events_triage_verdict", table_name="events")
    op.drop_column("events", "triage_verdict")
    for column in SCORES:
        op.drop_column("events", column)
