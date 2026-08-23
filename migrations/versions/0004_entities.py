"""entity resolution store

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-23

`events.actors` holds free text, so one person arrives under several spellings
and counts as several actors. Measured on 2,660 real mentions: 2,204 distinct
strings, of which string rules alone collapse 16% with 96% precision, and the
remaining errors are all cases where characters cannot decide — a country versus
its national team, an organisation versus its director, two ministries with the
same Thai name in different countries.

`events.actors` is left in place. It is what the extractor said, and when a merge
turns out to be wrong that record is the only way to see what happened.

`qid` is nullable on purpose: Wikidata covers 75% of the names that repeat here
and 33% of the long tail, so most local figures will never have one.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ENTITY_TYPES = ("person", "org", "place", "team", "generic", "unknown")
REVIEW_STATUSES = ("auto", "needs_review", "confirmed", "rejected")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    op.create_table(
        "entities",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("canonical_name", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.String(16), nullable=False, server_default="unknown"),
        sa.Column(
            "aliases",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column("qid", sa.String(32), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("review_status", sa.String(16), nullable=False, server_default="auto"),
        sa.Column("decided_by", sa.String(16), nullable=False, server_default="rules"),
        # Why a row is in the queue, in Thai, shown to whoever reviews it.
        sa.Column("risk", sa.Text(), nullable=True),
        sa.Column("mention_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reviewed_by", sa.Text(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "first_seen", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "last_seen", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(_in("entity_type", ENTITY_TYPES), name="ck_entities_type"),
        sa.CheckConstraint(_in("review_status", REVIEW_STATUSES), name="ck_entities_review_status"),
    )
    op.create_index("ix_entities_review_status", "entities", ["review_status"])
    op.create_index("ix_entities_type", "entities", ["entity_type"])
    # Partial, so the many entities with no Wikidata match do not collide on NULL.
    op.create_index(
        "ix_entities_qid", "entities", ["qid"], unique=True, postgresql_where=sa.text("qid IS NOT NULL")
    )
    # The lookup on every ingest is "does any known entity already answer to this
    # surface form", which is an array-containment query.
    op.create_index("ix_entities_aliases", "entities", ["aliases"], postgresql_using="gin")

    op.create_table(
        "event_entities",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "entity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("entities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("surface_form", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_event_entities_event", "event_entities", ["event_id"])
    op.create_index("ix_event_entities_entity", "event_entities", ["entity_id"])
    op.create_index(
        "ix_event_entities_unique",
        "event_entities",
        ["event_id", "entity_id", "surface_form"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_table("event_entities")
    op.drop_table("entities")
