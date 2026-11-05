"""work mode (WFO / WFH / WFCO) on attendance events

Revision ID: 0002
Revises: 0001
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing events were punched on the office network, so they default to WFO.
    # (No table rebuild on SQLite: that would drop the append-only triggers.)
    if op.get_bind().dialect.name == "sqlite":
        op.execute("ALTER TABLE attendance_events ADD COLUMN work_mode VARCHAR(4) NOT NULL DEFAULT 'WFO' "
                   "CHECK (work_mode IN ('WFO','WFH','WFCO'))")
    else:
        op.add_column("attendance_events", sa.Column("work_mode", sa.String(4), nullable=False, server_default="WFO"))
        op.create_check_constraint("ck_evt_mode", "attendance_events", "work_mode IN ('WFO','WFH','WFCO')")


def downgrade() -> None:
    raise NotImplementedError("attendance_events is append-only; downgrade is not supported")
