"""wikidata link state on entities

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-24

`qid` alone cannot say whether an entity has no Wikidata item or has simply not
been looked at yet. Both read as NULL, so every ingest would re-ask about the
same village headman forever — slow for us and rude to a free API that asks
clients to keep their rate modest.

`qid_status` separates the two, which turns the lookup into a once-per-entity
cost. `unavailable` is kept distinct from `no_match` for the same reason: a
network failure is worth retrying and an honest absence is not.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

QID_STATUSES = ("pending", "linked", "no_match", "unavailable")


def upgrade() -> None:
    op.add_column(
        "entities",
        sa.Column("qid_status", sa.String(16), nullable=False, server_default="pending"),
    )
    op.add_column("entities", sa.Column("qid_confidence", sa.Float(), nullable=True))
    op.add_column("entities", sa.Column("qid_reason", sa.Text(), nullable=True))
    op.add_column(
        "entities", sa.Column("qid_checked_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_check_constraint(
        "ck_entities_qid_status",
        "entities",
        f"qid_status IN ({', '.join(repr(v) for v in QID_STATUSES)})",
    )
    op.create_index("ix_entities_qid_status", "entities", ["qid_status"])
    # Rows written before this migration already carry a Q-number if the linker
    # ran; mark them linked so they are not queued for a lookup they passed.
    op.execute("UPDATE entities SET qid_status = 'linked' WHERE qid IS NOT NULL")


def downgrade() -> None:
    op.drop_index("ix_entities_qid_status", table_name="entities")
    op.drop_constraint("ck_entities_qid_status", "entities", type_="check")
    op.drop_column("entities", "qid_checked_at")
    op.drop_column("entities", "qid_reason")
    op.drop_column("entities", "qid_confidence")
    op.drop_column("entities", "qid_status")
