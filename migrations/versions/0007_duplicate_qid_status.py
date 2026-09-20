"""a qid_status for "this Q-number already belongs to another entity"

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-20

`ix_entities_qid` is unique on purpose: one Wikidata item is one entity, and
that is precisely what makes a Q-number able to merge "จ.กาฬสินธุ์" and
"จังหวัดกาฬสินธุ์" into one thing. So two entities resolving to the same
Q-number is not a failure of the lookup — it is the lookup discovering that two
rows are the same subject.

The backfill had no way to say that. It wrote the qid, hit the unique index,
and the UniqueViolationError took down the whole run: 14,113 entities sat at
`pending` because the job died partway through every time, and the crash named
the constraint rather than the duplicate.

`duplicate` records the finding and lets the run continue. The pair still needs
a human — merging two entities is not reversible — so the entity also goes to
the review queue with the name of the one already holding the Q-number.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "qid_status IN ('pending', 'linked', 'no_match', 'unavailable')"
_NEW = "qid_status IN ('pending', 'linked', 'no_match', 'unavailable', 'duplicate')"


def upgrade() -> None:
    op.drop_constraint("ck_entities_qid_status", "entities", type_="check")
    op.create_check_constraint("ck_entities_qid_status", "entities", _NEW)


def downgrade() -> None:
    # Back to pending rather than no_match: a duplicate is undecided, not
    # decided against, and the next backfill will find the collision again.
    op.execute("UPDATE entities SET qid_status = 'pending' WHERE qid_status = 'duplicate'")
    op.drop_constraint("ck_entities_qid_status", "entities", type_="check")
    op.create_check_constraint("ck_entities_qid_status", "entities", _OLD)
