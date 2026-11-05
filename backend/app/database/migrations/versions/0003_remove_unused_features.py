"""remove features that no longer have a screen: day status, admin-assisted corrections, HR review cases

Revision ID: 0003
Revises: 0002
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # (attendance_events is untouched: it stays append-only.)
    for table in ("hr_review_cases", "correction_requests", "attendance_status"):
        op.drop_table(table)


def downgrade() -> None:
    raise NotImplementedError("these features were removed; restore from a backup if needed")
