"""dispatch delivery retry state

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-22

The outbound backoff runs 30s → 2m → 10m → 1h, so a delivery can stay in flight
for over an hour. Holding that in process memory would lose every pending retry
on restart, which is exactly when deliveries are most likely to be pending.
Three operational columns make the retry state survive a restart and stay
queryable — "which signals are stuck, and why" is an operator question.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "dispatches",
        sa.Column("delivery_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "dispatches", sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("dispatches", sa.Column("last_error", sa.Text(), nullable=True))

    # The delivery sweeper's only query: pending work that is due.
    op.create_index(
        "ix_dispatches_delivery_due",
        "dispatches",
        ["osint_desk_status", "next_attempt_at"],
    )

    # One verdict per dispatch — the feedback corpus wants the analyst's current
    # answer, not every revision of it.
    op.create_index(
        "ix_verdicts_dispatch_unique", "verdicts", ["dispatch_id"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_verdicts_dispatch_unique", table_name="verdicts")
    op.drop_index("ix_dispatches_delivery_due", table_name="dispatches")
    op.drop_column("dispatches", "last_error")
    op.drop_column("dispatches", "next_attempt_at")
    op.drop_column("dispatches", "delivery_attempts")
