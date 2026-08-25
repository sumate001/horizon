"""a verdict for "right detection, wrong beat"

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-25

OSINT//DESK reported every dismissal as `false_signal`, including the common
case where the detection was correct and the story simply was not something the
newsroom follows. `verdicts` is the corpus for tuning detection thresholds, so
an editor clearing off-beat stories would have been teaching the radar to
suppress exactly the detections that were working.

`off_topic` separates relevance from accuracy. Anything reading this table for
detection quality must filter to ACCURACY_VERDICTS.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "verdict IN ('true_signal', 'false_signal', 'inconclusive')"
_NEW = "verdict IN ('true_signal', 'false_signal', 'inconclusive', 'off_topic')"


def upgrade() -> None:
    op.drop_constraint("ck_verdicts_verdict", "verdicts", type_="check")
    op.create_check_constraint("ck_verdicts_verdict", "verdicts", _NEW)


def downgrade() -> None:
    # Rows already recorded as off_topic have no honest equivalent in the old
    # vocabulary, so they are removed rather than relabelled as detection errors.
    op.execute("DELETE FROM verdicts WHERE verdict = 'off_topic'")
    op.drop_constraint("ck_verdicts_verdict", "verdicts", type_="check")
    op.create_check_constraint("ck_verdicts_verdict", "verdicts", _OLD)
