"""a new beat has to look back, not only forward

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-21

The matcher looks back `beat_lookback_hours` (12) on every run, which is right
for keeping up and wrong for starting. An editor defines a beat because a
subject is *already* running: "อิสราเอลในประเทศไทย" was created on 19 Sep with
42 matching events sitting in the store, spanning 22 Aug to 20 Sep — and none
of them in the last 12 hours. The beat matched nothing, looked broken, and was
in fact working exactly as written.

`backfilled_at` marks a beat as having had its one wide pass over the archive.
Null means it has not, so the next run reaches back `beat_backfill_days`
instead. Stamped after the pass, so it happens once per beat rather than every
cycle — the wide window is expensive and only the first run needs it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "beats", sa.Column("backfilled_at", sa.DateTime(timezone=True), nullable=True)
    )
    # Beats that already exist have only ever seen a 12-hour window, so they are
    # exactly the ones that need the backfill. Left null deliberately.


def downgrade() -> None:
    op.drop_column("beats", "backfilled_at")
